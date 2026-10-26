"""Alternate lines with real prices from Kalshi's public market data (no account or key needed).

Kalshi lists ladders like "Bijan Robinson: 90+ rushing yards" or "BAL Ravens wins by over 6.5 points" as Yes/No
contracts priced in dollars, so a 0.49 Yes ask is roughly a 49% chance. We attach every rung to our props and games:

  props:  pr["alt"] = [{"l": 89.5, "ya": 0.49, "na": 0.52, "v": volume}, ...]   (Yes = over the line, No = under)
  games:  g["kalshi"] = {"ml": {abbr: {...}}, "spread": {abbr: [rungs: abbr wins by over l]}, "total": [...], "tt": {abbr: [...]}}

Player markets Kalshi lists that ESPN has no line for are added as new props (line = the middle rung), so they get
projections and ladders too.
"""
import json, os, re, sys, time
from urllib.error import HTTPError
from urllib.request import urlopen, Request
from urllib.parse import urlencode

API = "https://api.elections.kalshi.com/trade-api/v2/markets"
PLAYER_SERIES = {"KXNFLPASSYDS": "Pass yds", "KXNFLRSHYDS": "Rush yds", "KXNFLRECYDS": "Rec yds", "KXNFLREC": "Receptions",
                 "KXNFLRRYDS": "Rush+rec yds", "KXNFLPASSCOMP": "Completions", "KXNFLPASSATT": "Pass att",
                 "KXNFLRSHATT": "Carries", "KXNFLPASSTDS": "Pass TD", "KXNFLTD": "Anytime TD"}
GAME_SERIES = ("KXNFLGAME", "KXNFLSPREAD", "KXNFLTOTAL", "KXNFLTEAMTOTAL")
TO_ESPN = {"WAS": "WSH", "JAC": "JAX", "LA": "LAR"}
FIRST_TD = "KXNFLFIRSTTD"   # NBA mode (nba_sync) swaps these series names and sets this to None
norm = lambda n: " ".join(w for w in "".join(c for c in (n or "").lower() if c.isalnum() or c == " ").split() if w not in ("jr", "sr", "ii", "iii", "iv", "v"))


def _get(params, tries=4):
    req = Request(API + "?" + urlencode(params), headers={"User-Agent": "nfl-edge-board", "Accept": "application/json"})
    for n in range(tries):
        try:
            with urlopen(req, timeout=30) as r: return json.loads(r.read().decode("utf-8"))
        except HTTPError as e:   # rate limited: back off and try again
            if e.code != 429 or n == tries - 1: raise
            time.sleep(2 * (n + 1))


def markets(series):
    out, cursor = [], None
    while True:
        q = {"series_ticker": series, "status": "open", "limit": 1000}
        if cursor: q["cursor"] = cursor
        d = _get(q)
        out += d.get("markets", [])
        cursor = d.get("cursor")
        if not cursor or not d.get("markets"): return out


def _price(m):
    f = lambda k: float(m[k]) if m.get(k) not in (None, "") else None
    ya, na = f("yes_ask_dollars"), f("no_ask_dollars")
    if ya is None and na is None: return None
    if (ya or 1) >= 0.99 and (na or 1) >= 0.99: return None          # no real market on either side
    return {"ya": ya, "na": na, "yb": f("yes_bid_dollars"), "v": round(float(m.get("volume_fp") or 0)), "tk": m.get("ticker")}


SERIES_API = "https://api.elections.kalshi.com/trade-api/v2/series/"
_TITLES = {}


def _series_title(series):
    """Kalshi's name for a series ('Pro Football Touchdowns'), cached on disk; it's part of their market page links."""
    path = os.path.join(os.environ.get("EDGE_DATA") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"), "kalshi_series.json")
    if not _TITLES:
        try:
            with open(path, encoding="utf-8") as f: _TITLES.update(json.load(f))
        except (FileNotFoundError, json.JSONDecodeError): pass
    if series not in _TITLES:
        try:
            req = Request(SERIES_API + series, headers={"User-Agent": "nfl-edge-board", "Accept": "application/json"})
            with urlopen(req, timeout=30) as r: _TITLES[series] = json.loads(r.read().decode("utf-8")).get("series", {}).get("title") or ""
            time.sleep(0.3)
            with open(path, "w", encoding="utf-8") as f: json.dump(_TITLES, f)
        except Exception as e:
            print("kalshi series title failed", series, e, file=sys.stderr); return ""
    return _TITLES.get(series, "")


def page_url(series, event_ticker):
    """Kalshi's web page for one game's market, e.g. kalshi.com/markets/kxnfltd/pro-football-touchdowns/kxnfltd-26oct11nygwas."""
    slug = re.sub(r"[^a-z0-9]+", "-", _series_title(series).lower()).strip("-")
    return f"https://kalshi.com/markets/{series.lower()}/{slug}/{event_ticker.lower()}" if slug else f"https://kalshi.com/markets/{series.lower()}"


def _game_for(event_ticker, games):
    """'KXNFLRSHYDS-26OCT05ATLNO' -> the slate game whose away+home codes spell 'ATLNO'."""
    code = event_ticker.split("-")[1][7:]
    for g in games:
        a, h = g["away"]["abbr"], g["home"]["abbr"]
        for ka in {a, {v: k for k, v in TO_ESPN.items()}.get(a, a)}:
            for kh in {h, {v: k for k, v in TO_ESPN.items()}.get(h, h)}:
                if ka + kh == code: return g
    return None


def _team(code, g):
    code = TO_ESPN.get(code, code)
    return code if code in (g["home"]["abbr"], g["away"]["abbr"]) else None


def apply(games, props):
    pre = [g for g in games if g["state"] == "pre"]
    for g in pre: g.pop("kalshi", None)
    n = 0
    for series in GAME_SERIES:
        try: ms = markets(series)
        except Exception as e:
            print("kalshi", series, "failed", e, file=sys.stderr); continue
        for m in ms:
            g = _game_for(m["event_ticker"], pre)
            p = _price(m)
            if not g or not p: continue
            k = g.setdefault("kalshi", {})
            k.setdefault("url", {}).setdefault({GAME_SERIES[0]: "ml", GAME_SERIES[2]: "total", GAME_SERIES[1]: "spread"}.get(series, "tt"), page_url(series, m["event_ticker"]))
            suffix = m["ticker"].split("-")[-1]
            if series == GAME_SERIES[0]:
                t = _team(suffix, g)
                if t: k.setdefault("ml", {})[t] = p; n += 1
            elif series == GAME_SERIES[2]:
                k.setdefault("total", []).append({"l": m["floor_strike"], **p}); n += 1
            else:
                t = _team("".join(c for c in suffix if c.isalpha()), g)
                if t: k.setdefault("spread" if series == GAME_SERIES[1] else "tt", {}).setdefault(t, []).append({"l": m["floor_strike"], **p}); n += 1
    for g in pre:
        for v in (g.get("kalshi") or {}).values():
            for rungs in (v.values() if isinstance(v, dict) else [v]):
                if isinstance(rungs, list): rungs.sort(key=lambda r: r["l"])
    for series, ours in PLAYER_SERIES.items():
        try: ms = markets(series)
        except Exception as e:
            print("kalshi", series, "failed", e, file=sys.stderr); continue
        by_game = {}
        for m in ms:
            g = _game_for(m["event_ticker"], pre)
            p = _price(m)
            if g and p and g["id"] in props: by_game.setdefault(g["id"], []).append((m, p))
        for gid, rows in by_game.items():
            players = {norm(pl["n"]): pl for pl in props[gid]}
            ladders, urls = {}, {}
            for m, p in rows:
                pl = players.get(norm(m["title"].split(":")[0]))
                if pl:
                    ladders.setdefault(pl["n"], []).append({"l": m["floor_strike"], **p})
                    urls[pl["n"]] = page_url(series, m["event_ticker"])
            for name, rungs in ladders.items():
                pl = players[norm(name)]
                rungs.sort(key=lambda r: r["l"])
                pr = next((x for x in pl["props"] if x["m"] == ours), None)
                if pr is None:   # a market ESPN has no line for: the rung closest to 50 cents becomes the line
                    mid = min(rungs, key=lambda r: abs((r["ya"] or 1) - 0.5))
                    pr = {"m": ours, "l": 0.5 if ours == "Anytime TD" else mid["l"], "o": None, "kalshiOnly": True}
                    pl["props"].append(pr)
                pr["alt"] = rungs; n += len(rungs)
                if urls.get(name): pr["kx"] = urls[name]
    # first touchdown scorer: one price per player, kept on the player (pl["ftk"])
    try: ms = markets(FIRST_TD) if FIRST_TD else []
    except Exception as e: print("kalshi KXNFLFIRSTTD failed", e, file=sys.stderr); ms = []
    for m in ms:
        g = _game_for(m["event_ticker"], pre); p = _price(m)
        if not g or not p or g["id"] not in props or ":" not in m["title"]: continue
        pl = next((x for x in props[g["id"]] if norm(x["n"]) == norm(m["title"].split(":")[0])), None)
        if pl: pl["ftk"] = {**p, "url": page_url(FIRST_TD, m["event_ticker"])}; n += 1
    try: flow(pre, props)
    except Exception as e: print("kalshi money flow failed", e, file=sys.stderr)
    return n


# ---------------------------------------------------------------- money on Kalshi and where it's moving
FLOW_FILE, FLOW_EVERY, FLOW_KEEP, FLOW_BACK = "kalshi_flow.json", 2 * 3600, 10 * 3600, 6 * 3600
TRADES_API = "https://api.elections.kalshi.com/trade-api/v2/markets/trades"
HOT_PROP, HOT_GAME, MAX_SPLITS = 1000, 25000, 40      # same "surge" rule as the app; at most 40 trade pulls per sync


def hot(x, min_usd):
    return bool(x) and (x.get("dusd") or 0) >= min_usd and x.get("d", 0) >= 0.25 * max(1, x["c"] - x["d"])


def taker_usd(ticker, since):
    """Dollars bought on each side by the trader who crossed the spread (Kalshi's taker_side), since a unix time:
    (yes dollars, no dollars). The taker is the one who chose to bet now, so this shows which side the new money took."""
    y = n = 0.0; cursor = None
    for _ in range(4):   # up to 4,000 trades
        q = {"ticker": ticker, "limit": 1000, "min_ts": int(since)}
        if cursor: q["cursor"] = cursor
        req = Request(TRADES_API + "?" + urlencode(q), headers={"User-Agent": "nfl-edge-board", "Accept": "application/json"})
        for k in range(4):   # rate limited: back off and try again
            try:
                with urlopen(req, timeout=30) as r: d = json.loads(r.read().decode("utf-8")); break
            except HTTPError as e:
                if e.code != 429 or k == 3: raise
                time.sleep(1.5 * (k + 1))
        for t in d.get("trades", []):
            c = float(t.get("count_fp") or t.get("count") or 0)
            if t.get("taker_side") == "yes": y += c * float(t.get("yes_price_dollars") or 0)
            elif t.get("taker_side") == "no": n += c * float(t.get("no_price_dollars") or 0)
        cursor = d.get("cursor")
        if not cursor or not d.get("trades"): break
        time.sleep(0.1)
    return y, n


def _mid(r):
    if not r: return None
    ya, yb = r.get("ya"), r.get("yb")
    return (ya + yb) / 2 if ya is not None and yb is not None else ya


def flow(pre, props, now=None):
    """Contracts traded and rough dollars (contracts x price) on every game market and prop ladder, and how much of it came in
    over about the last 6 hours, with the price move at the main line. A snapshot per market is kept at most every 2 hours
    for 10 hours (state kalshi_flow.json), so the file stays small and only changes every couple of hours.
      g["kalshi"]["money"] = {"ml" | "spread" | "total": {...}},  pr["kv"] = {...}
      {...} = {"c": contracts, "usd": dollars, "d": new contracts, "dusd": new dollars, "h": hours compared, "dp": cents}"""
    path = os.path.join(os.environ.get("EDGE_DATA") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"), FLOW_FILE)
    try:
        with open(path, encoding="utf-8") as f: st = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError): st = {}
    now = int(now or time.time()); keep = {}

    def track(key, rungs, main):
        c = sum(r.get("v") or 0 for r in rungs)
        if not c: return None
        usd = sum((r.get("v") or 0) * (_mid(r) or 0) for r in rungs)
        mid = round(_mid(main) * 100) if _mid(main) is not None else None
        h = [x for x in st.get(key, []) if now - x[0] <= FLOW_KEEP]
        if not h or now - h[-1][0] >= FLOW_EVERY: h.append([now, c, mid])
        keep[key] = h
        then = [x for x in h if now - x[0] >= FLOW_BACK - 1800] or h[:1]      # the snapshot from about 6 hours ago (or the oldest)
        t0, c0, m0 = then[-1]
        out = {"c": c, "usd": round(usd)}
        if now - t0 >= 1800:
            d = max(0, c - c0)
            out.update({"d": d, "dusd": round(d * usd / c), "h": round((now - t0) / 3600, 1)})
            if mid is not None and m0 is not None: out["dp"] = mid - m0
        return out

    near50 = lambda rs: min(rs, key=lambda r: abs((_mid(r) or 0) - 0.5)) if rs else None
    for g in pre:
        k = g.get("kalshi")
        if k:
            money = {}
            if k.get("ml"): money["ml"] = track(f"{g['id']}|ml", list(k["ml"].values()), k["ml"].get(g["home"]["abbr"]))
            if k.get("spread"): money["spread"] = track(f"{g['id']}|spread", [r for rs in k["spread"].values() for r in rs], None)
            if k.get("total"): money["total"] = track(f"{g['id']}|total", k["total"], near50(k["total"]))
            money = {a: b for a, b in money.items() if b}
            if money: k["money"] = money
        for pl in props.get(g["id"], []):
            for pr in pl["props"]:
                if not pr.get("alt"): continue
                main = next((r for r in pr["alt"] if r["l"] == pr.get("l")), None) or near50(pr["alt"])
                kv = track(f"{g['id']}|{pl['id']}|{pr['m']}", pr["alt"], main)
                if kv: pr["kv"] = kv
    # which side the new money took, for the markets that surged ("ys" = share on Yes: the over for props and totals,
    # the home team for moneylines and spreads). Only near a coin flip (25-75c): on long shots and heavy favorites the
    # trader crossing the spread is almost always buying Yes, whatever the news, so the split would mislead
    even = lambda r: r is not None and _mid(r) is not None and 0.25 <= _mid(r) <= 0.75
    jobs = []
    for g in pre:
        k = g.get("kalshi") or {}; M = k.get("money") or {}; H = g["home"]["abbr"]
        if hot(M.get("ml"), HOT_GAME) and k.get("ml") and even(k["ml"].get(H)):
            jobs.append((M["ml"], [(r.get("tk"), t == H) for t, r in k["ml"].items()]))
        if hot(M.get("total"), HOT_GAME) and k.get("total") and even(near50(k["total"])):
            jobs.append((M["total"], [(near50(k["total"]).get("tk"), True)]))
        if hot(M.get("spread"), HOT_GAME) and k.get("spread"):
            # a spread is a ladder on both teams ("PHI wins by 2.5+", "DAL wins by 4.5+"...): add up the busiest near-coin-flip
            # rungs on either side; Yes on a home rung or No on an away rung is money on the home team
            rungs = sorted(((r, t == H) for t, rs in k["spread"].items() for r in rs if even(r)), key=lambda x: -(x[0].get("v") or 0))[:6]
            if rungs: jobs.append((M["spread"], [(r.get("tk"), home) for r, home in rungs]))
        for pl in props.get(g["id"], []):
            for pr in pl["props"]:
                if hot(pr.get("kv"), HOT_PROP) and pr["m"] != "Anytime TD":   # TD markets are Yes-only betting: no split
                    main = next((r for r in pr["alt"] if r["l"] == pr.get("l")), None) or near50(pr["alt"])
                    if even(main): jobs.append((pr["kv"], [(main.get("tk"), True)]))
    jobs.sort(key=lambda j: -(j[0].get("dusd") or 0))
    for x, parts in jobs[:MAX_SPLITS]:
        since = now - (x.get("h") or 6) * 3600; yes = no = 0.0
        try:
            for tk, yes_is_ours in parts:
                if not tk: continue
                y, n = taker_usd(tk, since)
                yes, no = (yes + y, no + n) if yes_is_ours else (yes + n, no + y)   # away team's Yes = money on the away side
                time.sleep(0.25)
        except Exception as e: print("kalshi trades", e, file=sys.stderr); continue
        if yes + no >= 1000: x["ys"] = round(yes / (yes + no), 2); x["tusd"] = round(yes + no)
    for g in pre:   # the tickers were only needed for those pulls
        for v in (g.get("kalshi") or {}).values():
            for rungs in (v.values() if isinstance(v, dict) else [v]):
                for r in (rungs if isinstance(rungs, list) else [rungs] if isinstance(rungs, dict) else []): r.pop("tk", None)
        for pl in props.get(g["id"], []):
            (pl.get("ftk") or {}).pop("tk", None)
            for pr in pl["props"]:
                for r in pr.get("alt") or []: r.pop("tk", None)
    try:
        old = json.dumps(st, sort_keys=True); new = json.dumps(keep, sort_keys=True)
        if new != old:
            with open(path, "w", encoding="utf-8") as f: f.write(json.dumps(keep, separators=(",", ":"), sort_keys=True))
    except OSError as e: print("kalshi flow save failed", e, file=sys.stderr)
