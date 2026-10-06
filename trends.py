"""Betting trends (ATS / over-under from nflverse closing lines) and player pages (full logs, splits, history vs opponent)."""
from collections import defaultdict

from analytics import rows, fix, f, i

SPLITS = ("all", "fav", "dog", "home", "road", "after_loss", "after_win", "prime", "div", "short", "bye")


def team_trends(season, slate_games, years=3, coaches=None):
    """ATS and over/under records over the last `years` seasons plus this one, with situational splits,
    the splits that apply to each team's next game, and recent head-to-head meetings (added to slate games as g['trend'])."""
    sched = rows("schedules/games.csv.gz", 6 * 3600)
    done = [g for g in sched if g["game_type"] == "REG" and g["result"] not in ("", "NA") and g["spread_line"] not in ("", "NA")
            and season - years <= int(g["season"]) <= season]
    done.sort(key=lambda g: (g["gameday"], g["gametime"]))
    rec = defaultdict(lambda: {"w": defaultdict(lambda: [0] * 6), "s": defaultdict(lambda: [0] * 6)})  # [atsW, atsL, atsP, over, under, ouPush]
    last_margin, last_season = {}, {}

    def tags(t, home, spread_team, g, rest):
        out = ["all", "home" if home else "road"]
        if spread_team > 0: out.append("fav")
        elif spread_team < 0: out.append("dog")
        if last_season.get(t) == g["season"] and last_margin.get(t):
            out.append("after_loss" if last_margin[t] < 0 else "after_win")
        if (g.get("gametime") or "00:00") >= "19:00": out.append("prime")
        if g.get("div_game") == "1": out.append("div")
        if rest is not None and rest <= 6: out.append("short")
        if rest is not None and rest >= 13: out.append("bye")
        return out

    for g in done:
        res, line, tot, tl = f(g["result"]), f(g["spread_line"]), f(g["total"]), f(g["total_line"])
        for home in (True, False):
            t = fix(g["home_team"] if home else g["away_team"])
            spread_team, margin = (line, res) if home else (-line, -res)
            ats = margin - spread_team
            ou = (tot - tl) if tot is not None and tl is not None else None
            rest = f(g["home_rest"] if home else g["away_rest"])
            cur = (coaches or {}).get(t, {}).get("n")
            if cur and g.get("home_coach" if home else "away_coach") not in (cur, "", None):   # only the current coach's games count
                last_margin[t], last_season[t] = margin, g["season"]; continue
            for tag in tags(t, home, spread_team, g, rest):
                for scope in (("w", "s") if int(g["season"]) == season else ("w",)):
                    a = rec[t][scope][tag]
                    a[0 if ats > 0 else 1 if ats < 0 else 2] += 1
                    if ou is not None: a[3 if ou > 0 else 4 if ou < 0 else 5] += 1
            last_margin[t], last_season[t] = margin, g["season"]

    by_espn = {g.get("espn"): g for g in sched}
    for sg in slate_games:
        sg.pop("trend", None)
        row, o = by_espn.get(sg["id"]), sg.get("odds") or {}
        if not row or sg["state"] == "post": continue
        hs, ctx = o.get("hs"), {}
        for home in (True, False):
            t = fix(row["home_team"] if home else row["away_team"])
            spread_team = (-hs if home else hs) if hs is not None else 0
            rest = f(row["home_rest"] if home else row["away_rest"])
            ctx["home" if home else "away"] = [x for x in tags(t, home, spread_team, row, rest) if x != "all"]
        pair = {sg["home"]["abbr"], sg["away"]["abbr"]}
        meet = [g for g in done if {fix(g["home_team"]), fix(g["away_team"])} == pair][-6:]
        ctx["h2h"] = [{"y": int(g["season"]), "wk": int(g["week"]), "h": fix(g["home_team"]), "a": fix(g["away_team"]),
                       "hs": i(g["home_score"]), "as": i(g["away_score"]), "line": f(g["spread_line"]), "tl": f(g["total_line"])} for g in reversed(meet)]
        ctx["coach"] = {s: (coaches or {}).get(sg[s]["abbr"]) for s in ("home", "away")}
        sg["trend"] = ctx
    return {"window": f"{season - years}-{season}", "teams": {t: {s: dict(v[s]) for s in ("w", "s")} for t, v in rec.items()}}


PAGE_COLS = {"completions": "cmp", "attempts": "att", "passing_yards": "pyd", "passing_tds": "ptd", "passing_interceptions": "int",
             "carries": "car", "rushing_yards": "ryd", "rushing_tds": "rtd", "targets": "tgt", "receptions": "rec",
             "receiving_yards": "yds", "receiving_tds": "td", "fantasy_points_ppr": "fp", "receiving_air_yards": "air"}


def player_pages(season, slate_games, years=3):
    """Season game logs, home/road splits (this season + last) and history against this week's opponent
    for every QB/RB/WR/TE on a slate team. Keyed by ESPN id when nflverse knows it."""
    xw = {p["gsis_id"]: p for p in rows("players/players.csv", 7 * 86400) if p.get("gsis_id")}
    sched = {g["game_id"]: g for g in rows("schedules/games.csv.gz", 6 * 3600)}
    teams = {g[s]["abbr"] for g in slate_games for s in ("home", "away")}
    nxt = {}
    for g in sorted(slate_games, key=lambda g: g["date"]):
        if g["state"] == "post": continue
        for s, o in (("home", "away"), ("away", "home")): nxt.setdefault(g[s]["abbr"], g[o]["abbr"])

    def row_of(w):
        gm = sched.get(w.get("game_id"), {})
        r = {"y": i(w["season"]), "w": i(w["week"]), "o": fix(w.get("opponent_team") or ""),
             "h": 1 if fix(gm.get("home_team", "")) == fix(w.get("team", "")) else 0}
        for k, short in PAGE_COLS.items():
            v = f(w.get(k))
            if v: r[short] = round(v, 1) if short == "fp" else int(v)
        if f(w.get("target_share")): r["ts"] = round(f(w["target_share"]), 2)
        return r

    snaps = {(s.get("pfr_player_id"), i(s["week"])): f(s.get("offense_pct")) for s in rows(f"snap_counts/snap_counts_{season}.csv", 6 * 3600)}
    out = {}
    for w in rows(f"stats_player/stats_player_week_{season}.csv", 6 * 3600):
        if w.get("season_type") != "REG" or w.get("position_group") not in ("QB", "RB", "WR", "TE") or fix(w.get("team", "")) not in teams: continue
        p = xw.get(w["player_id"], {})
        key = p.get("espn_id") or "g" + w["player_id"]
        pl = out.setdefault(key, {"n": w.get("player_display_name"), "p": w.get("position"), "gsis": w["player_id"], "log": [], "vs": []})
        pl["tm"] = fix(w["team"])  # most recent team (trades)
        r = row_of(w)
        sn = snaps.get((p.get("pfr_id"), r["w"]))
        if sn is not None: r["snap"] = round(sn, 2)
        pl["log"].append(r)
    by_gsis = {v["gsis"]: v for v in out.values()}
    for yr in range(season - 1, season - years - 1, -1):   # last season for splits; earlier ones only vs this week's opponent
        try: data = rows(f"stats_player/stats_player_week_{yr}.csv", 30 * 86400)
        except Exception: continue
        for w in data:
            pl = by_gsis.get(w.get("player_id"))
            if not pl or w.get("season_type") != "REG": continue
            r = row_of(w)
            if r["o"] == nxt.get(pl["tm"]): pl["vs"].append(r)
            if yr == season - 1: pl.setdefault("prev", []).append(r)
    for pl in out.values():
        pl["log"].sort(key=lambda r: r["w"])
        pl["opp"] = nxt.get(pl["tm"])
        pl["vs"] = sorted(pl["vs"] + [r for r in pl["log"] if r["o"] == pl["opp"]], key=lambda r: (r["y"], r["w"]), reverse=True)[:6]
        both = pl["log"] + pl.pop("prev", [])
        # last 10 games across this season and last, newest first: what a prop gets checked against
        pl["last"] = sorted(both, key=lambda r: (r["y"], r["w"]), reverse=True)[:10]
        sp = {}
        for name, flag in (("home", 1), ("road", 0)):
            g = [r for r in both if r["h"] == flag]
            if g: sp[name] = {"g": len(g), **{k: round(sum(r.get(k, 0) for r in g) / len(g), 1) for k in ("pyd", "ryd", "yds", "rec", "tgt", "car", "fp")}}
        pl["split"] = sp
    return out
