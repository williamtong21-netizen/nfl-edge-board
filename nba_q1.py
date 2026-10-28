"""NBA first-quarter moneyline: fair odds from the full-game spread, Kalshi's 1Q prices, and a watch-only lean.

Model (fit Oct 2026 on 979 games of 2025-26 with ESPN closing spreads): home 1Q margin ~ Normal(C + K x home edge, SD),
home edge = minus the home spread. About 1 in 20 first quarters end tied (P(|margin| < 0.5) ~ 4.8%).
  3-way (Kalshi KXNBA1Q: home / away / TIE)   p = model chances, ties included
  2-way (most books; a tie refunds)            fair = p / (1 - tie)
Kalshi's pre-tip prices matched the model (Brier 0.1669 vs ours 0.1646 on 56 games), so the price is fair on average.
The one pattern worth watching, by favorite size (2025-26 halves and 2026-27 so far, all three agreeing in direction):
  10+ point favorites   won the 1Q 72% / 74% / 78% vs 68-69% expected  -> lean the favorite
  3.5-9.5 favorites     won 48% / 54% / 48% vs 57-58% expected         -> lean the underdog (weaker: nearly gone late 2025-26)
Watch-only until it grades out live: every pre-tip read is saved (state q1_record_<season>.json) and graded after the game.
"""
import json, math, os, sys
from datetime import datetime, timedelta, timezone

C, K, SD = -0.0053, 0.3348, 8.276
BIG, MID = 10, (3.5, 9.5)
NUDGE = 0.04          # how far the pattern moves the favorite's chance in the lean (about the gap we saw); never shown as our odds
SERIES = "KXNBA1Q"
Phi = lambda x: 0.5 * (1 + math.erf(x / math.sqrt(2)))


def chances(hs):
    """{h, a, t}: 3-way chances for the home team / away team / a tie after one quarter, from the home spread."""
    mu = C + K * (-hs)
    h, a = 1 - Phi((0.5 - mu) / SD), Phi((-0.5 - mu) / SD)
    return {"h": round(h, 3), "a": round(a, 3), "t": round(1 - h - a, 3)}


def american(p):
    if not p or p <= 0 or p >= 1: return None
    return round(-100 * p / (1 - p)) if p >= 0.5 else round(100 * (1 - p) / p)


def lean(hs):
    """The favorite-size pattern: (side 'h'/'a', strength 1-2, why) or None."""
    if hs is None: return None
    fav, size = ("h" if hs < 0 else "a"), abs(hs)
    if size >= BIG: return {"side": fav, "who": "favorite", "st": 2, "why": f"{size:g}-point favorites have won the 1st quarter more often than the spread says in every sample (about 74% vs 69%)"}
    if MID[0] <= size <= MID[1]: return {"side": "a" if fav == "h" else "h", "who": "underdog", "st": 1, "why": f"{size:g}-point favorites have won the 1st quarter less often than the spread says (about 52% vs 57%); weaker and less steady"}
    return None


def apply(games, kalshi):
    """Adds g["q1"] to every pre-game NBA game with a spread; kalshi = the kalshi module (TO_ESPN, markets, _price, page_url)."""
    pre = [g for g in games if g["state"] == "pre" and (g.get("odds") or {}).get("hs") is not None and not g.get("pre_season")]
    try: ms = kalshi.markets(SERIES)
    except Exception as e: print("kalshi", SERIES, "failed", e, file=sys.stderr); ms = []
    kx = {}
    for m in ms:
        g = kalshi._game_for(m["event_ticker"], pre); p = kalshi._price(m)
        if not g or not p: continue
        code = m["ticker"].split("-")[-1]
        side = "t" if code == "TIE" else {"home": "h", "away": "a"}.get(next((s for s in ("home", "away") if g[s]["abbr"] == kalshi.TO_ESPN.get(code, code)), None))
        if side: kx.setdefault(g["id"], {"url": kalshi.page_url(SERIES, m["event_ticker"])})[side] = {"ya": p["ya"], "v": p["v"]}
    n = 0
    for g in pre:
        hs = g["odds"]["hs"]
        p = chances(hs); no_tie = 1 - p["t"]
        q = {"hs": hs, "p": p, "two": {s: round(p[s] / no_tie, 3) for s in ("h", "a")}, "kx": kx.get(g["id"]), "lean": lean(hs)}
        if q["lean"]:
            s = q["lean"]["side"]
            q["lean"]["p"] = round(min(0.95, p[s] + NUDGE), 3)   # pattern-adjusted 3-way chance for the lean side
            ya = ((q["kx"] or {}).get(s) or {}).get("ya")
            if ya: q["lean"]["edge"] = round(q["lean"]["p"] - ya, 3)
        g["q1"] = q; n += 1
    return n


def _quarter1(day):
    """{espn game id: (home 1Q pts, away 1Q pts)} for finals on a US date ('20261028')."""
    import sync
    out = {}
    try: sb = sync.get(f"{sync.ESPN}/scoreboard?dates={day}&limit=100")
    except Exception as e: print("1Q scores failed", day, e, file=sys.stderr); return out
    for e in sb.get("events", []):
        c = e["competitions"][0]
        if not c.get("status", {}).get("type", {}).get("completed"): continue
        q = {}
        for x in c["competitors"]:
            ls = x.get("linescores") or []
            if ls: q[x["homeAway"]] = float(ls[0].get("value", ls[0].get("displayValue", 0)))
        if len(q) == 2: out[e["id"]] = (q["home"], q["away"])
    return out


def record(games, season, data_dir, now=None):
    """Save each game's last pre-tip read; grade yesterday's and today's finals. Returns the season report."""
    now = now or datetime.now(timezone.utc)
    path = os.path.join(data_dir, f"q1_record_{season}.json")
    try:
        with open(path, encoding="utf-8") as f: rec = json.load(f)
    except (FileNotFoundError, ValueError): rec = {}
    for g in games:
        q = g.get("q1")
        if g["state"] == "pre" and q and "res" not in rec.get(g["id"], {}):
            rec[g["id"]] = {"d": g["date"], "m": f'{g["away"]["abbr"]} @ {g["home"]["abbr"]}', "hs": q["hs"], "p": q["p"],
                            "kx": {s: (q["kx"] or {}).get(s, {}).get("ya") for s in ("h", "a", "t")} if q.get("kx") else None,
                            "lean": {k: q["lean"][k] for k in ("side", "who", "st")} if q.get("lean") else None}
    open_ids = {gid for gid, r in rec.items() if "res" not in r}
    if open_ids:
        for d in (1, 0):
            day = (now - timedelta(hours=5) - timedelta(days=d)).strftime("%Y%m%d")
            for gid, (h, a) in _quarter1(day).items():
                if gid in open_ids: rec[gid]["res"] = "h" if h > a else "a" if a > h else "t"; rec[gid]["q"] = [h, a]
    os.makedirs(data_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f: json.dump(rec, f)
    return report(rec)


def report(rec):
    done = [r for r in rec.values() if "res" in r]
    leans = [r for r in done if r.get("lean")]
    out = {"games": len(done), "ties": sum(r["res"] == "t" for r in done)}
    if done:   # Brier on the 3-way outcome: our model vs Kalshi's last pre-tip price
        bs = lambda p, r: sum((p.get(s, 0) - (r["res"] == s)) ** 2 for s in ("h", "a", "t"))
        out["brier"] = round(sum(bs(r["p"], r) for r in done) / len(done), 4)
        kx = [r for r in done if r.get("kx") and all(r["kx"].get(s) is not None for s in ("h", "a"))]
        if kx:
            out["kxGames"] = len(kx)
            out["brierKx"] = round(sum(bs({s: r["kx"][s] or 0 for s in ("h", "a", "t")}, r) for r in kx) / len(kx), 4)
            out["brierOursKx"] = round(sum(bs(r["p"], r) for r in kx) / len(kx), 4)
    for who in ("favorite", "underdog"):
        rs = [r for r in leans if r["lean"]["who"] == who]
        if rs:
            w = sum(r["res"] == r["lean"]["side"] for r in rs)
            exp = sum(r["p"][r["lean"]["side"]] for r in rs)
            out[who] = {"n": len(rs), "w": w, "exp": round(exp, 1)}
    return out


if __name__ == "__main__":
    for hs in (-12.5, -10.5, -7.5, -3.5, -1.5, 2.5, 8.5):
        p = chances(hs); print(hs, p, "2-way home", american(p["h"] / (1 - p["t"])), lean(hs) and lean(hs)["who"])
