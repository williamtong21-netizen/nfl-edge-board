"""College early-week flags: games where our number is 3+ points off the OPENING line, Power 4 games only.

Why only this: cfb_edge_backtest (2,600 FBS games 2023-26) found our number predicts which way college lines move (3+ off the
opener: the line moved our way 62% of the time, ~0.75 pts on average) but bets at the CLOSING line only break even. So the
edge, if any, is betting early, near the opener. Power 4 games (at least one P4 team) only: Group of 5 matchups lost.
Sides use our spread; totals use the mixed total (half points ratings, half run / pass unit matchups).
A flag needs us 3+ off the opener AND 3+ off the current line (the same way), so a lean the market already moved to is not
flagged late. Each flag is frozen the first time a sync sees it (opener, our number, line at the time) and graded after the
game: did the line move our way from the flagged line by kickoff, and the record at the flagged line / opener / close.
"""
import json, os, sys
from datetime import datetime, timezone

EDGE = 3.0
WIND_SKIP = 12   # mph forecast: no total flags (our totals ignore weather; NFL 2015-25 unders hit 55-59% in 10+ mph wind)
BACKTEST = {   # cfb_edge_backtest, 3+ off the opener, games with a Power 4 team
    "side": {"n": 636, "moved": 0.652, "open": 0.562, "close": 0.528, "games": "Power 4 games 2023-26",
             "test": "2025-26 alone (out of sample): moved our way 69%, 54.7% at the opener, 49.8% at the close"},
    "total": {"n": 479, "moved": 0.667, "open": 0.543, "close": 0.514, "games": "all FBS games 2025-26 (out of sample)",
              "test": "mixed total fit on 2023-24, tested on 2025-26"},
}


def find(games):
    """Current flags for upcoming games: [{gid, kind, pick, text, open, ours, edge, line}]."""
    out = []
    for g in games:
        if g.get("state") != "pre" or not g.get("ours"): continue
        H, A, o = g["home"], g["away"], g.get("odds") or {}
        if not (H.get("conf") or A.get("conf")) or not g["ours"].get("fbs"): continue   # a Power 4 team, FBS vs FBS (as backtested)
        hso, to = o.get("hso"), o.get("to")
        if hso is not None and o.get("hs") is not None:
            e = g["ours"]["m"] + hso                                  # our home margin minus the opener's home margin
            ec = g["ours"]["m"] + o["hs"]                             # ... and minus today's line: still worth betting now
            if abs(e) >= EDGE and abs(ec) >= EDGE and (e > 0) == (ec > 0):
                home = e > 0; t = H if home else A; cur = o["hs"] if home else -o["hs"]
                out.append({"gid": g["id"], "kind": "side", "pick": "home" if home else "away", "team": t["abbr"],
                            "text": f"{t['abbr']} {'+' if cur > 0 else ''}{cur:g}" if cur else f"{t['abbr']} PK",
                            "open": hso, "line": o["hs"], "ours": g["ours"]["m"], "edge": round(abs(ec), 1)})
        windy = ((g.get("wx") or {}).get("wind") or 0) >= WIND_SKIP and not g.get("indoor")
        if to is not None and o.get("t") is not None and not windy:
            e, ec = g["ours"]["t"] - to, g["ours"]["t"] - o["t"]
            if abs(e) >= EDGE and abs(ec) >= EDGE and (e > 0) == (ec > 0):
                out.append({"gid": g["id"], "kind": "total", "pick": "over" if e > 0 else "under",
                            "text": f"{'Over' if e > 0 else 'Under'} {o['t']:g}", "open": to, "line": o["t"], "ours": g["ours"]["t"], "edge": round(abs(ec), 1)})
    return out


def track(games, flags, res, path):
    """Freeze new flags, keep each one's closing line current until kickoff, grade the finished ones. Returns the record."""
    try: st = json.load(open(path, encoding="utf-8"))
    except (FileNotFoundError, ValueError): st = {"flags": {}}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    byid = {g["id"]: g for g in games}
    for fl in flags:
        k = f"{fl['gid']}:{fl['kind']}"
        if k not in st["flags"]:
            g = byid[fl["gid"]]
            st["flags"][k] = {**fl, "at": now, "w": g["week"], "d": g["date"], "h": g["home"]["abbr"], "a": g["away"]["abbr"], "new": True}
    for k, f_ in st["flags"].items():
        r = res["games"].get(f_["gid"]) or {}
        if r.get("cs") is not None: f_["close"] = r["cs"] if f_["kind"] == "side" else r.get("ct")
        if r.get("st") == "post" and r.get("hs") is not None: f_["hs"], f_["as"] = r["hs"], r["as"]
    json.dump(st, open(path, "w", encoding="utf-8"))
    return st


def grade(st):
    """moved our way / record at the opener, at the flagged line and at the close, per kind."""
    out = {}
    for kind in ("side", "total"):
        d = {"n": 0, "moved": [0, 0], "open": [0, 0, 0], "flag": [0, 0, 0], "close": [0, 0, 0], "pending": 0}
        for f_ in st["flags"].values():
            if f_["kind"] != kind: continue
            d["n"] += 1
            sg = 1 if f_["pick"] == "over" else -1
            # side: spreads are home lines (negative = home favored); a home pick wants the line to go more negative
            if f_.get("close") is not None and f_.get("line") is not None:
                mv = (f_["line"] - f_["close"]) if kind == "side" else (f_["close"] - f_["line"])
                mv *= (1 if f_["pick"] == "home" else -1) if kind == "side" else sg
                if mv > 0: d["moved"][0] += 1
                elif mv < 0: d["moved"][1] += 1
            if f_.get("hs") is None: d["pending"] += 1; continue
            for key, ln in (("open", f_.get("open")), ("flag", f_.get("line")), ("close", f_.get("close"))):
                if ln is None: continue
                if kind == "side":
                    a = (f_["hs"] - f_["as"] + ln) * (1 if f_["pick"] == "home" else -1)
                else:
                    a = (f_["hs"] + f_["as"] - ln) * sg
                d[key][0 if a > 0 else 1 if a < 0 else 2] += 1
        out[kind] = d
    return out


def alert(st, games, cfg, path):
    """One digest per pass when new flags appear (group channel, quiet midnight-8 AM ET)."""
    import alerts
    new = [f_ for f_ in st["flags"].values() if f_.get("new")]
    if not new: return 0
    top = alerts.topic(cfg)
    try:
        from zoneinfo import ZoneInfo
        if datetime.now(ZoneInfo("America/New_York")).hour < 8: return 0   # held until the morning
    except Exception: pass
    if top:
        def num(f_):
            m = f_["ours"]
            return f"{f_['h']} by {m:.1f}" if m > 0 else f"{f_['a']} by {-m:.1f}" if m < 0 else "even"
        lines = [f"{f_['a']} @ {f_['h']}: {f_['text']} (opened {f_['h']} {f_['open']:+g}, ours {num(f_)})" if f_["kind"] == "side"
                 else f"{f_['a']} @ {f_['h']}: {f_['text']} (opened {f_['open']:g}, ours {f_['ours']:g})" for f_ in new[:8]]
        body = "\n".join(lines) + ("\n..." if len(new) > 8 else "") + "\nEarly-line edge only: bet near the opener. Past record in the app."
        try: alerts.send(top, f"College early leans: {len(new)} new", body, tags="football,mortar_board")
        except Exception as e: print("cfb flag alert failed", e, file=sys.stderr); return 0
    for f_ in new: f_.pop("new", None)
    json.dump(st, open(path, "w", encoding="utf-8"))
    return len(new)
