"""Alternate lines with real prices from Kalshi's public market data (no account or key needed).

Kalshi lists ladders like "Bijan Robinson: 90+ rushing yards" or "BAL Ravens wins by over 6.5 points" as Yes/No
contracts priced in dollars, so a 0.49 Yes ask is roughly a 49% chance. We attach every rung to our props and games:

  props:  pr["alt"] = [{"l": 89.5, "ya": 0.49, "na": 0.52, "v": volume}, ...]   (Yes = over the line, No = under)
  games:  g["kalshi"] = {"ml": {abbr: {...}}, "spread": {abbr: [rungs: abbr wins by over l]}, "total": [...], "tt": {abbr: [...]}}

Player markets Kalshi lists that ESPN has no line for are added as new props (line = the middle rung), so they get
projections and ladders too.
"""
import json, os, re, sys, time
from urllib.request import urlopen, Request
from urllib.parse import urlencode

API = "https://api.elections.kalshi.com/trade-api/v2/markets"
PLAYER_SERIES = {"KXNFLPASSYDS": "Pass yds", "KXNFLRSHYDS": "Rush yds", "KXNFLRECYDS": "Rec yds", "KXNFLREC": "Receptions",
                 "KXNFLRRYDS": "Rush+rec yds", "KXNFLPASSCOMP": "Completions", "KXNFLPASSATT": "Pass att",
                 "KXNFLRSHATT": "Carries", "KXNFLPASSTDS": "Pass TD", "KXNFLTD": "Anytime TD"}
GAME_SERIES = ("KXNFLGAME", "KXNFLSPREAD", "KXNFLTOTAL", "KXNFLTEAMTOTAL")
TO_ESPN = {"WAS": "WSH", "JAC": "JAX", "LA": "LAR"}
norm = lambda n: " ".join(w for w in "".join(c for c in (n or "").lower() if c.isalnum() or c == " ").split() if w not in ("jr", "sr", "ii", "iii", "iv", "v"))


def _get(params):
    req = Request(API + "?" + urlencode(params), headers={"User-Agent": "nfl-edge-board", "Accept": "application/json"})
    with urlopen(req, timeout=30) as r: return json.loads(r.read().decode("utf-8"))


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
    return {"ya": ya, "na": na, "yb": f("yes_bid_dollars"), "v": round(float(m.get("volume_fp") or 0))}


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
            k.setdefault("url", {}).setdefault({"KXNFLGAME": "ml", "KXNFLTOTAL": "total", "KXNFLSPREAD": "spread"}.get(series, "tt"), page_url(series, m["event_ticker"]))
            suffix = m["ticker"].split("-")[-1]
            if series == "KXNFLGAME":
                t = _team(suffix, g)
                if t: k.setdefault("ml", {})[t] = p; n += 1
            elif series == "KXNFLTOTAL":
                k.setdefault("total", []).append({"l": m["floor_strike"], **p}); n += 1
            else:
                t = _team("".join(c for c in suffix if c.isalpha()), g)
                if t: k.setdefault("spread" if series == "KXNFLSPREAD" else "tt", {}).setdefault(t, []).append({"l": m["floor_strike"], **p}); n += 1
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
    return n
