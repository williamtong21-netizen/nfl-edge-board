"""Rest-of-season fantasy values for the app's league connect + trade finder (Sleeper player ids).

  season rate   each player's points per game this season (nflverse weekly stats), recent games weighted (DECAY per game
                back), shrunk toward a bench-level player at his position (K_SHRINK games)
  this week     points from his prop lines when he has them (line 60% / our projection 40%, TDs from the anytime price)
  rest of year  50/50 of the two when both exist; Out / IR players scaled down (Sleeper injury status)
Points are split into "s" (everything except receptions) and "r" (receptions per game) so the page can score any
league's PPR setting. Output: out/ff_values.json  {season, week, players: {sleeper id: {n, pos, tm, s, r, g, inj}}}
"""
import json, math, os, sys, time
from collections import defaultdict
from urllib.request import urlopen, Request

DECAY = 0.85
K_SHRINK = 3.0
BENCH = {"QB": (13.0, 0.0), "RB": (5.0, 1.5), "WR": (4.5, 2.0), "TE": (3.5, 1.5)}   # (s, r) of a fringe player
INJ_SCALE = {"Out": 0.85, "Doubtful": 0.9, "IR": 0.35, "PUP": 0.35, "Sus": 0.5}      # rest-of-season, not this week


def std_points(w, f):
    g = lambda k: f(w.get(k)) or 0
    return (g("passing_yards") * 0.04 + g("passing_tds") * 4 - g("passing_interceptions") * 2 + (g("rushing_yards") + g("receiving_yards")) * 0.1
            + (g("rushing_tds") + g("receiving_tds")) * 6 - 2 * (g("rushing_fumbles_lost") + g("receiving_fumbles_lost") + g("sack_fumbles_lost"))
            + 2 * (g("passing_2pt_conversions") + g("rushing_2pt_conversions") + g("receiving_2pt_conversions")))


def prop_points(pl):
    """(s, r) for this week from the player's prop lines, or None."""
    e = {}
    for pr in pl.get("props", []):
        if pr["m"] == "Anytime TD":
            if pr.get("pOver") is not None: e[pr["m"]] = -math.log(1 - min(0.95, pr["pOver"]))
        elif pr.get("l") is not None:
            e[pr["m"]] = 0.6 * pr["l"] + 0.4 * pr["proj"] if (pr.get("proj") or 0) > 0 else pr["l"]
    qb = pl.get("p") == "QB"
    ryd = e.get("Rush yds", (e["Rush+rec yds"] - e["Rec yds"]) if "Rush+rec yds" in e and "Rec yds" in e else 0)
    recy = e.get("Rec yds", 0); rec = e.get("Receptions", recy / 11 if recy else 0)
    pyd = e.get("Pass yds", 0) if qb else 0; ptd = e.get("Pass TD", 0) if qb else 0; it = e.get("INT", 0.7 if pyd else 0) if qb else 0
    td = e.get("Anytime TD", 0)
    if not (pyd or ryd or recy or td): return None
    return pyd * 0.04 + ptd * 4 - it * 2 + (ryd + recy) * 0.1 + td * 6, rec


def sleeper_players(cache_dir):
    p = os.path.join(cache_dir, "sleeper_players_nfl.json")
    if os.path.exists(p) and time.time() - os.path.getmtime(p) < 20 * 3600:
        return json.load(open(p, encoding="utf-8"))
    with urlopen(Request("https://api.sleeper.app/v1/players/nfl", headers={"User-Agent": "edge-board"}), timeout=120) as r: d = json.loads(r.read())
    slim = {k: {"n": v.get("full_name") or f'{v.get("first_name", "")} {v.get("last_name", "")}'.strip(), "pos": v.get("position"), "tm": v.get("team"),
                "gsis": (v.get("gsis_id") or "").strip() or None, "inj": v.get("injury_status")}
            for k, v in d.items() if v.get("position") in ("QB", "RB", "WR", "TE") and v.get("active")}
    json.dump(slim, open(p, "w", encoding="utf-8"))
    return slim


def build(props, season, week, out_dir, cache_dir):
    from analytics import rows, f, i, norm
    st = [w for w in rows(f"stats_player/stats_player_week_{season}.csv", 6 * 3600) if w.get("season_type") == "REG"]
    games = defaultdict(list)
    for w in st:
        if w.get("position") in BENCH: games[w["player_id"]].append((i(w["week"]), std_points(w, f), f(w.get("receptions")) or 0, w))
    this_week = {}
    for pls in props.values():
        for pl in pls:
            pp = prop_points(pl)
            if pp: this_week[(norm(pl["n"]), pl.get("tm"))] = pp
    sp = sleeper_players(cache_dir)
    by_gsis = {v["gsis"]: k for k, v in sp.items() if v["gsis"]}
    by_name = {(norm(v["n"]), v["pos"]): k for k, v in sp.items()}
    out = {}
    for pid, gs in games.items():
        gs.sort(key=lambda x: -x[0]); w0 = gs[0][3]; pos = w0["position"]
        sid = by_gsis.get(pid) or by_name.get((norm(w0["player_display_name"]), pos))
        if not sid: continue
        wt = [DECAY ** k for k in range(len(gs))]; bs, br = BENCH[pos]
        s = (sum(x[1] * a for x, a in zip(gs, wt)) + bs * K_SHRINK) / (sum(wt) + K_SHRINK)
        r = (sum(x[2] * a for x, a in zip(gs, wt)) + br * K_SHRINK) / (sum(wt) + K_SHRINK)
        tw = this_week.get((norm(w0["player_display_name"]), sp[sid]["tm"]))
        if tw: s, r = 0.5 * s + 0.5 * tw[0], 0.5 * r + 0.5 * tw[1]
        k = INJ_SCALE.get(sp[sid]["inj"] or "", 1.0)
        out[sid] = {"n": sp[sid]["n"], "pos": pos, "tm": sp[sid]["tm"], "s": round(s * k, 2), "r": round(r * k, 2), "g": len(gs), "inj": sp[sid]["inj"]}
    # players with props this week but no games yet (rookies, returning from injury)
    for sid, v in sp.items():
        if sid in out or not v["tm"]: continue
        tw = this_week.get((norm(v["n"]), v["tm"]))
        if tw: out[sid] = {"n": v["n"], "pos": v["pos"], "tm": v["tm"], "s": round(tw[0], 2), "r": round(tw[1], 2), "g": 0, "inj": v["inj"]}
    doc = {"season": season, "week": week, "players": out}
    with open(os.path.join(out_dir, "ff_values.json"), "w", encoding="utf-8") as fh: json.dump(doc, fh, separators=(",", ":"))
    return len(out)


if __name__ == "__main__":
    ROOT = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, ROOT)
    import glob
    props = {}
    for fn in glob.glob(os.path.join(ROOT, "out", "props-*.json")): props.update(json.load(open(fn, encoding="utf-8"))["games"])
    sl = json.load(open(os.path.join(ROOT, "out", "slate.json"), encoding="utf-8"))
    print(build(props, sl["season"], sl["week"], os.path.join(ROOT, "out"), os.path.join(ROOT, "data")))
