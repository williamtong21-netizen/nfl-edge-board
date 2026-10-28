"""Polymarket: a second real-money exchange next to Kalshi (public read-only API, no key).

For each game on the slate: the moneyline, the main spread and the main total (the most-traded line), with
  price      Polymarket's best ask / last trade on each side (cents = chance)
  vol        dollars traded on that market so far
  6h money   taker dollars over the last ~6 hours by side: buying a side counts for it, selling a side counts for the
             other (selling Heat at 44 cents = buying Hawks at 56)
Games match by team nickname ('Heat') or code ('mia') and start time within 3 hours.
  apply(games, sport) -> {game id: {"url", "ml" | "spread" | "total": {...}}}
"""
import json, sys, time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from urllib.request import urlopen, Request

GAMMA = "https://gamma-api.polymarket.com"
TRADES = "https://data-api.polymarket.com/trades"
UA = {"User-Agent": "nfl-edge-board", "Accept": "application/json"}
LEAGUE = {"nfl": "nfl", "nba": "nba", "cfb": "cfb", "cbb": "ncaab"}
CODE = {"nyk": "NY", "sas": "SA", "gsw": "GS", "nop": "NO", "uta": "UTAH", "was": "WSH", "jac": "JAX", "la": "LAR"}
FLOW_H = 6
MAX_PAGES = 4          # 500 trades a page; enough for the busiest NBA moneylines over 6 hours


def get(url, params=None, tries=3):
    q = url + ("?" + urlencode(params) if params else "")
    for n in range(tries):
        try:
            with urlopen(Request(q, headers=UA), timeout=25) as r: return json.loads(r.read().decode("utf-8"))
        except Exception:
            if n == tries - 1: raise
            time.sleep(1.5 * (n + 1))


def series_id(sport):
    for s in get(f"{GAMMA}/sports"):
        if s.get("sport") == LEAGUE.get(sport): return s.get("series")
    return None


def events(sport):
    sid = series_id(sport)
    if not sid: return []
    out, off = [], 0
    while True:
        page = get(f"{GAMMA}/events", {"series_id": sid, "active": "true", "closed": "false", "limit": 100, "offset": off})
        out += page; off += 100
        if len(page) < 100 or off >= 500: return out


def _game_for(e, games):
    teams = e.get("teams") or []
    try: t0 = datetime.fromisoformat((e.get("startTime") or "").replace("Z", "+00:00"))
    except ValueError: return None, None
    def side(t, g):
        for s in ("home", "away"):
            code = (t.get("abbreviation") or "").lower()
            if (t.get("name") or "").lower() == (g[s].get("name") or "").lower() or CODE.get(code, code.upper()) == g[s]["abbr"]: return s
        return None
    for g in games:
        if abs(datetime.fromisoformat(g["date"].replace("Z", "+00:00")) - t0) > timedelta(hours=3): continue
        sides = {t.get("name"): side(t, g) for t in teams}
        if len(teams) == 2 and set(sides.values()) == {"home", "away"}: return g, sides
    return None, None


def _prices(m):
    try: px = [float(x) for x in json.loads(m.get("outcomePrices") or "[]")]
    except ValueError: px = []
    return json.loads(m.get("outcomes") or "[]"), px


def flow(cid, outcomes, since):
    """Taker dollars by outcome name since a unix time."""
    usd = {o: 0.0 for o in outcomes}
    for page in range(MAX_PAGES):
        try: rows = get(TRADES, {"market": cid, "limit": 500, "offset": page * 500, "takerOnly": "true"})
        except Exception as e: print("polymarket trades failed", cid[:10], e, file=sys.stderr); break
        for t in rows:
            if t.get("timestamp", 0) < since: return usd
            o, p, sz = t.get("outcome"), float(t.get("price") or 0), float(t.get("size") or 0)
            if o not in usd: continue
            if t.get("side") == "BUY": usd[o] += sz * p
            else:
                other = next((x for x in outcomes if x != o), None)
                if other: usd[other] += sz * (1 - p)
        if len(rows) < 500: break
    return usd


def market_row(m, keymap, since):
    """keymap: outcome name -> our key ('h' / 'a' / 'o' / 'u')."""
    outs, px = _prices(m)
    if len(outs) != 2 or len(px) != 2: return None
    row = {"vol": round(float(m.get("volume") or 0))}
    for o, p in zip(outs, px):
        if o in keymap: row[keymap[o]] = round(p, 3)
    usd = flow(m["conditionId"], outs, since) if row["vol"] >= 500 else {}
    d = {keymap[o]: round(v) for o, v in usd.items() if o in keymap}
    if sum(d.values()) >= 100: row["d"] = d; row["dusd"] = sum(d.values()); row["h6"] = FLOW_H
    return row


def apply(games, sport):
    pre = [g for g in games if g.get("state") == "pre"]
    if not pre: return {}
    try: evs = events(sport)
    except Exception as e: print("polymarket events failed", e, file=sys.stderr); return {}
    since = int((datetime.now(timezone.utc) - timedelta(hours=FLOW_H)).timestamp())
    out = {}
    for e in evs:
        g, sides = _game_for(e, pre)
        if not g: continue
        names = {s: n for n, s in sides.items()}      # 'home' -> 'Hawks' (NBA) or 'Green Bay Packers' (NFL)
        ms = e.get("markets") or []
        def keys(m):   # outcome name -> 'h' / 'a'; outcomes use nicknames ('Packers') even when teams are full names
            outs, _ = _prices(m)
            return {o: k for o in outs for s, k in (("home", "h"), ("away", "a"))
                    if o.lower() in (names[s].lower(), (g[s].get("name") or "").lower()) or names[s].lower().endswith(" " + o.lower())}
        res = {"url": f"https://polymarket.com/event/{e.get('slug')}"}
        ml = next((m for m in ms if m.get("sportsMarketType") == "moneyline"), None)
        if ml:
            r = market_row(ml, keys(ml), since)
            if r: res["ml"] = r
        sp = max((m for m in ms if m.get("sportsMarketType") == "spreads"), key=lambda m: float(m.get("volume") or 0), default=None)
        if sp and float(sp.get("volume") or 0) >= 500:
            outs, _ = _prices(sp)
            km = keys(sp)
            r = market_row(sp, km, since)
            if r and outs and km.get(outs[0]):   # the line belongs to the first outcome ('Spread: Hawks (-3.5)')
                ln = float(sp.get("line") or 0)
                r["l"] = ln if km[outs[0]] == "h" else -ln     # home team's line
                res["spread"] = r
        tt = max((m for m in ms if m.get("sportsMarketType") == "totals"), key=lambda m: float(m.get("volume") or 0), default=None)
        if tt and float(tt.get("volume") or 0) >= 500:
            r = market_row(tt, {"Over": "o", "Under": "u"}, since)
            if r: r["l"] = float(tt.get("line") or 0); res["total"] = r
        if len(res) > 1: out[g["id"]] = res
    return out


if __name__ == "__main__":
    import os
    sport = sys.argv[1] if len(sys.argv) > 1 else "nba"
    path = {"nfl": "out/slate.json", "nba": "out/nba/slate.json", "cbb": "out/cbb/slate.json"}[sport]
    root = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(root, path), encoding="utf-8") as f: games = json.load(f)["games"]
    r = apply(games, sport)
    print(len(r), "games")
    for gid, v in list(r.items())[:6]: print(gid, json.dumps(v)[:400])
