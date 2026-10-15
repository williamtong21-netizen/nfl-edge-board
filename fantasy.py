"""Rest-of-season fantasy values for the app's league connect + trade finder (Sleeper player ids).

  season rate   each player's points per game this season (nflverse weekly stats), recent games weighted (DECAY per game
                back), shrunk toward a bench-level player at his position (K_SHRINK games)
  this week     points from his prop lines when he has them (line 60% / our projection 40%, TDs from the anytime price)
  rest of year  50/50 of the two when both exist; Out / IR players scaled down (Sleeper injury status)
  schedule      every remaining game's opponent, rated by the half-PPR points that defense allows to the position per game this
                season vs the league (shrunk toward average early on): "sf" = average factor over his remaining games, "gl" = games
                left (byes don't count), "nx" = next four opponents [week, team, factor]
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
    p = os.path.join(cache_dir, "sleeper_players_nfl3.json")
    if os.path.exists(p) and time.time() - os.path.getmtime(p) < 20 * 3600:
        return json.load(open(p, encoding="utf-8"))
    with urlopen(Request("https://api.sleeper.app/v1/players/nfl", headers={"User-Agent": "edge-board"}), timeout=120) as r: d = json.loads(r.read())
    slim = {k: {"n": v.get("full_name") or f'{v.get("first_name", "")} {v.get("last_name", "")}'.strip(), "pos": v.get("position"), "tm": v.get("team"),
                "gsis": (v.get("gsis_id") or "").strip() or None, "inj": v.get("injury_status"), "yid": str(v["yahoo_id"]) if v.get("yahoo_id") else None}
            for k, v in d.items() if v.get("position") in ("QB", "RB", "WR", "TE", "K") and v.get("active")}
    json.dump(slim, open(p, "w", encoding="utf-8"))
    return slim


K_DEF = {"QB": 4.0, "RB": 4.0, "WR": 4.0, "TE": 8.0}   # games of "league average" mixed into each defense's points allowed (TE is noisiest)


def schedule(season, week, st):
    """{team: [(week, opponent)]} for games not yet played, and {(defense, pos): factor} for points allowed vs average."""
    from analytics import rows, f, i, fix
    sched = [g for g in rows("schedules/games.csv.gz", 6 * 3600) if g["season"] == str(season) and g["game_type"] == "REG"]
    left = defaultdict(list)
    for g in sched:
        if i(g["week"]) < week or g.get("result") not in ("", "NA", None): continue
        h, a = fix(g["home_team"]), fix(g["away_team"])
        left[h].append((i(g["week"]), a)); left[a].append((i(g["week"]), h))
    allowed = defaultdict(lambda: defaultdict(float)); games = defaultdict(set)
    for w in st:
        if w.get("position") not in BENCH or not w.get("opponent_team"): continue
        d = fix(w["opponent_team"]); allowed[(d, w["position"])][i(w["week"])] += std_points(w, f) + 0.5 * (f(w.get("receptions")) or 0)
        games[d].add(i(w["week"]))
    lg = {}
    for pos in BENCH:
        xs = [v for (d, p), wk in allowed.items() if p == pos for v in wk.values()]
        lg[pos] = sum(xs) / len(xs) if xs else 1
    fac = {}
    for (d, pos), wk in allowed.items():
        n = len(games[d]); m = sum(wk.values()) / max(1, n)
        fac[(d, pos)] = round(max(0.8, min(1.2, 1 + (m / lg[pos] - 1) * n / (n + K_DEF[pos]))), 3)
    return left, fac


def build(props, season, week, out_dir, cache_dir):
    from analytics import rows, f, i, norm, fix
    st = [w for w in rows(f"stats_player/stats_player_week_{season}.csv", 6 * 3600) if w.get("season_type") == "REG"]
    games = defaultdict(list)
    for w in st:
        if w.get("position") in BENCH: games[w["player_id"]].append((i(w["week"]), std_points(w, f), f(w.get("receptions")) or 0, w))
    this_week = {}
    for pls in props.values():
        for pl in pls:
            pp = prop_points(pl)
            if pp: this_week[(norm(pl["n"]), pl.get("tm"))] = pp
    left, fac = schedule(season, week, st)
    def sched_of(team, pos):
        g = sorted(left.get(fix(team or ""), []))
        fs = [fac.get((o, pos), 1.0) for _, o in g]
        return {"sf": round(sum(fs) / len(fs), 3) if fs else 1.0, "gl": len(g), "nx": [[w, o, fac.get((o, pos), 1.0)] for w, o in g[:4]]}
    # how often each player reaches the yardage bonus marks (shrunk toward his position's rate), plus passing TDs / INTs per game
    # so the page can score any league's bonuses and TD / INT values
    BZ = {"pass300": ("passing_yards", 300), "pass400": ("passing_yards", 400), "rush100": ("rushing_yards", 100), "rush200": ("rushing_yards", 200),
          "rec100": ("receiving_yards", 100), "rec200": ("receiving_yards", 200), "rr100": ("rush_rec", 100), "rr200": ("rush_rec", 200)}
    yd = lambda w, c: (f(w.get("rushing_yards")) or 0) + (f(w.get("receiving_yards")) or 0) if c == "rush_rec" else f(w.get(c)) or 0
    pos_rate = defaultdict(lambda: [0, 0])
    for gs in games.values():
        for x in gs:
            for k, (c, t) in BZ.items(): pr = pos_rate[(x[3]["position"], k)]; pr[0] += yd(x[3], c) >= t; pr[1] += 1
    def bonus_rates(gs, pos):
        out = {}
        for k, (c, t) in BZ.items():
            n, base = len(gs), pos_rate[(pos, k)]
            prior = base[0] / base[1] if base[1] else 0
            r = (sum(yd(x[3], c) >= t for x in gs) + 4 * prior) / (n + 4)
            if r >= 0.005: out[k] = round(r, 3)
        return out
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
        out[sid] = {"n": sp[sid]["n"], "pos": pos, "tm": sp[sid]["tm"], "s": round(s * k, 2), "r": round(r * k, 2), "g": len(gs), "inj": sp[sid]["inj"],
                    "lg": [[x[0], round(x[1], 1), x[2]] for x in gs[:6][::-1]],   # last games: [week, non-reception points, receptions]
                    "bz": bonus_rates(gs, pos),
                    **({"pt": round(sum((f(x[3].get("passing_tds")) or 0) for x in gs) / len(gs), 2),
                        "it": round(sum((f(x[3].get("passing_interceptions")) or 0) for x in gs) / len(gs), 2)} if pos == "QB" else {}),
                    **sched_of(sp[sid]["tm"] or w0.get("team"), pos)}
    # players with props this week but no games yet (rookies, returning from injury)
    for sid, v in sp.items():
        if sid in out or not v["tm"]: continue
        tw = this_week.get((norm(v["n"]), v["tm"]))
        if tw: out[sid] = {"n": v["n"], "pos": v["pos"], "tm": v["tm"], "s": round(tw[0], 2), "r": round(tw[1], 2), "g": 0, "inj": v["inj"], **sched_of(v["tm"], v["pos"])}
    # kickers: names and teams only (the page projects them from team totals)
    for sid, v in sp.items():
        if v["pos"] == "K" and v["tm"]: out[sid] = {"n": v["n"], "pos": "K", "tm": v["tm"], "s": 0, "r": 0, "g": 0, "inj": v["inj"]}
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
