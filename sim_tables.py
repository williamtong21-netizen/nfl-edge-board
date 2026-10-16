"""Play-level tables for the game simulator (gamesim.py), fitted from nflverse play-by-play (free, public).

league   pass rate by down / distance / score / clock, 4th-down go-punt-kick choices, field-goal make curve, punt and
         kickoff field position, yards samples for runs / catches / scrambles / sacks by position and field zone,
         seconds per play by outcome and game mode (normal, hurry-up, milking the clock)
coverage 2025 participation data (the newest season nflverse has charted): each defense's coverage mix and man rate,
         how passing goes vs each coverage and clean vs pressured; 2026 coverage charting isn't public
team     2026 pace, pass rate over expected, sack / QB-hit rates on both sides (pressure proxy), run inside vs outside
player   receivers' targets and yards per snap vs man and vs zone, QBs clean vs pressured, rushers inside vs outside
         (all shrunk toward 1.0 so small samples barely move)
Run: python sim_tables.py   -> data/sim_tables.json (state/ in the app repo; sync rebuilds it weekly)
"""
import csv, gzip, json, math, os, random, sys, time
from collections import defaultdict

import analytics

OUT = os.path.join(os.environ.get("EDGE_DATA") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"), "sim_tables.json")
YEARS = (2023, 2024, 2025, 2026)
fx = lambda v: float(v) if v not in ("", "NA", None) else None
ix = lambda v: int(float(v)) if v not in ("", "NA", None) else 0
fix = analytics.fix


def zone(ydl):
    """distance buckets for yardage samples; open-field samples only from deep in a team's own end (rarely cut off by
    the goal line), red-zone samples by distance with touchdowns tagged 999."""
    return "g2" if ydl <= 2 else "g5" if ydl <= 5 else "r10" if ydl <= 10 else "r15" if ydl <= 15 else "r20" if ydl <= 20 else "op" if ydl >= 60 else None


def distb(d):
    return "s" if d <= 2 else "m" if d <= 6 else "l" if d <= 10 else "x"


def scoreb(s):
    for i, hi in enumerate((-17, -9, -4, -1, 0, 3, 8, 16)):
        if s <= hi: return i
    return 8


def timeb(qtr, qsec, hsec):
    if qtr == 4 and qsec <= 300: return 2
    if qtr == 2 and hsec <= 120: return 1
    return 0


def mode(qtr, qsec, hsec, sd):
    """hurry: end of a half or trailing late; milk: leading late."""
    if qtr == 4 and qsec <= 300: return "hurry" if sd < 0 else "milk" if sd > 0 else "normal"
    if qtr == 2 and hsec <= 120: return "hurry"
    return "normal"


def cap(xs, n=400, seed=7):
    """keep at most n samples (random subset, fixed seed) so the table stays small."""
    if len(xs) <= n: return xs
    return random.Random(seed).sample(xs, n)


def shrink(obs, n, k, prior=1.0):
    return (obs * n + prior * k) / (n + k) if n + k else prior


def plays(year):
    """scrimmage + kick plays of the regular season as compact dicts, in file order (game, play order)."""
    p = analytics.fetch(f"pbp/play_by_play_{year}.csv.gz", 7 * 86400 if year >= 2026 else 365 * 86400)
    with gzip.open(p, "rt", encoding="utf-8") as fh:
        r = csv.reader(fh); h = next(r); c = {k: i for i, k in enumerate(h)}
        keep = ["game_id", "play_id", "season_type", "week", "posteam", "defteam", "qtr", "down", "ydstogo", "yardline_100",
                "quarter_seconds_remaining", "half_seconds_remaining", "game_seconds_remaining", "score_differential", "play_type",
                "yards_gained", "qb_dropback", "qb_scramble", "qb_kneel", "qb_spike", "sack", "qb_hit", "interception",
                "complete_pass", "incomplete_pass", "out_of_bounds", "penalty", "fumble_lost", "receiver_player_id",
                "rusher_player_id", "passer_player_id", "run_location", "run_gap", "field_goal_result", "kick_distance",
                "return_yards", "punt_blocked", "touchback", "return_touchdown", "kickoff_attempt", "punt_attempt",
                "field_goal_attempt", "two_point_attempt", "extra_point_result", "touchdown", "td_team", "home_team", "away_team",
                "penalty_team", "penalty_yards", "first_down_penalty"]
        idx = [(k, c[k]) for k in keep if k in c]
        for row in r:
            if row[c["season_type"]] != "REG": continue
            yield {k: row[i] for k, i in idx}


def build():
    t0 = time.time()
    pos = {}
    for p in analytics.rows("players/players.csv", 30 * 86400):
        if p.get("gsis_id"): pos[p["gsis_id"]] = p.get("position") or ""
    grp = lambda gid: {"QB": "QB", "RB": "RB", "FB": "RB", "WR": "WR", "TE": "TE"}.get(pos.get(gid, ""), "WR")

    pr = defaultdict(lambda: [0, 0])          # (down, distb, scoreb, timeb) -> [passes, plays]
    pr0 = defaultdict(lambda: [0, 0])         # (down, distb, timeb)
    fourth = defaultdict(lambda: [0, 0, 0])   # (distb4, ydl // 10) -> go, punt, fg
    fg = []                                   # (distance, made)
    punt = defaultdict(list)                  # kicking ydl // 5 -> receiving team's ydl
    kick = []                                 # receiving team's first snap ydl after a kickoff (2025-26 rules)
    runs = defaultdict(list); catch = defaultdict(list); scr = []; sackyd = []
    comp = defaultdict(lambda: [0, 0])        # (pos, zone) -> completions, targets
    clock = defaultdict(list)                 # (outcome, mode) -> seconds to next snap
    rate = defaultdict(float)
    team = defaultdict(lambda: defaultdict(float))
    rush_io = defaultdict(lambda: [0, 0, 0.0, 0.0])   # rusher -> n inside, n outside, yds inside, yds outside
    pr_exp = {}
    fouls = defaultdict(list)                 # off|def -> [(yards, auto first down)]; snaps counted in pen_n
    pen_n = defaultdict(int)

    def dist4(d): return "1" if d <= 1 else "2" if d <= 3 else "4" if d <= 6 else "7"

    for y in YEARS:
        prev = None; pending_kick = None
        for p in plays(y):
            pt = p["play_type"]; gid = p["game_id"]
            ydl, qtr = ix(p["yardline_100"]), ix(p["qtr"])
            qsec, hsec, sd = ix(p["quarter_seconds_remaining"]), ix(p["half_seconds_remaining"]), ix(p["score_differential"])
            # seconds between snaps (same game, same quarter)
            if prev and prev["gid"] == gid and prev["qtr"] == qtr and pt in ("pass", "run", "punt", "field_goal"):
                e = prev["gsec"] - ix(p["game_seconds_remaining"])
                if 0 < e <= 60: clock[(prev["out"], prev["mode"])].append(e)
                if prev["mode"] == "normal" and prev["neutral"] and y == 2026: team[prev["team"]]["pace_s"] += e; team[prev["team"]]["pace_n"] += 1
            prev = None
            if pending_kick and pending_kick[0] == gid and pt in ("pass", "run") and p["posteam"] == pending_kick[1]:
                kick.append(ydl); pending_kick = None
            if p["kickoff_attempt"] == "1" and y >= 2025 and p["return_touchdown"] != "1" and p["penalty"] != "1":
                rec = p["defteam"] if p["defteam"] else None
                pending_kick = (gid, rec) if rec else None
            if pt == "field_goal" and p["kick_distance"]:
                fg.append((ix(p["kick_distance"]), 1 if p["field_goal_result"] == "made" else 0))
            if pt == "punt" and p["penalty"] != "1" and p["punt_blocked"] != "1" and p["return_touchdown"] != "1" and p["kick_distance"]:
                opp = 80 if p["touchback"] == "1" else 100 - (ydl - ix(p["kick_distance"])) - ix(p["return_yards"])
                punt[min(ydl // 5, 19)].append(max(1, min(99, opp)))
            down = ix(p["down"])
            if down == 4 and pt in ("pass", "run", "punt", "field_goal") and timeb(qtr, qsec, hsec) == 0:
                fourth[(dist4(ix(p["ydstogo"])), min(ydl // 10, 9))][0 if pt in ("pass", "run") else 1 if pt == "punt" else 2] += 1
            if pt in ("pass", "run", "no_play") and p["qb_kneel"] != "1" and p["qb_spike"] != "1" and p["posteam"]:
                # plays wiped out by an accepted penalty (false starts, holding, pass interference ...): drawn per snap
                pen_n["snaps"] += 1
                py = ix(p["penalty_yards"])
                if pt == "no_play" and p["penalty"] == "1" and py > 0 and p["penalty_team"]:
                    fouls["off" if p["penalty_team"] == p["posteam"] else "def"].append((py, p["first_down_penalty"] == "1"))
            if pt not in ("pass", "run") or p["qb_kneel"] == "1" or p["qb_spike"] == "1": continue
            drop = p["qb_dropback"] == "1"
            if 1 <= down <= 3:
                k = (down, distb(ix(p["ydstogo"])), scoreb(sd), timeb(qtr, qsec, hsec))
                pr[k][0] += drop; pr[k][1] += 1
                pr0[k[:2] + k[3:]][0] += drop; pr0[k[:2] + k[3:]][1] += 1
            yg, z, pen = ix(p["yards_gained"]), zone(ydl), p["penalty"] == "1"
            ys = 999 if p["touchdown"] == "1" and p["td_team"] == p["posteam"] else yg
            off, de = fix(p["posteam"]), fix(p["defteam"])
            neutral = qtr <= 3 and abs(sd) <= 7
            if drop:
                rate["drop"] += 1
                if p["sack"] == "1":
                    out = "sack"; rate["sack"] += 1
                    if not pen: sackyd.append(yg)
                elif p["qb_scramble"] == "1":
                    out = "run"; rate["scr"] += 1
                    if not pen and ydl > 20: scr.append(yg)
                else:
                    rate["att"] += 1; rate["int"] += p["interception"] == "1"
                    rid = p["receiver_player_id"]
                    if not rid: rate["away"] += 1   # throwaways / no intended receiver
                    if p["complete_pass"] == "1":
                        out = "comp_oob" if p["out_of_bounds"] == "1" else "comp_in"
                        if not pen and rid and z: catch[(grp(rid), z)].append(ys)
                        if not pen and rid: rate["fum_c"] += p["fumble_lost"] == "1"; rate["catches"] += 1
                    else: out = "inc"
                    zc = "gl" if ydl <= 5 else "rz" if ydl <= 20 else "op"
                    if rid and not pen: comp[(grp(rid), zc)][1] += 1; comp[(grp(rid), zc)][0] += p["complete_pass"] == "1"
                if y == 2026:
                    team[off]["o_drop"] += 1; team[de]["d_drop"] += 1
                    team[off]["o_hit"] += p["sack"] == "1" or p["qb_hit"] == "1"; team[de]["d_hit"] += p["sack"] == "1" or p["qb_hit"] == "1"
                    team[off]["o_sack"] += p["sack"] == "1"; team[de]["d_sack"] += p["sack"] == "1"
                    if p["interception"] == "1": team[de]["d_int"] += 1
                    if p["sack"] != "1" and p["qb_scramble"] != "1": team[de]["d_att"] += 1
            else:
                out = "run"; rid = p["rusher_player_id"]; g = grp(rid)
                if not pen:
                    if z: runs[(g if g in ("RB", "QB") else "WR", z)].append(ys)
                    rate["runs"] += 1; rate["fum_r"] += p["fumble_lost"] == "1"
                    io = 0 if (p["run_location"] == "middle" or p["run_gap"] == "guard") else 1
                    if y >= 2025 and rid:
                        rush_io[rid][io] += 1; rush_io[rid][2 + io] += yg
                    if y == 2026:
                        team[de][f"d_run{io}_n"] += 1; team[de][f"d_run{io}_y"] += yg
            if y == 2026 and neutral and 1 <= down <= 3:
                team[off]["pr_n"] += 1; team[off]["pr_p"] += drop
                pr_exp.setdefault(off, []).append((down, distb(ix(p["ydstogo"])), scoreb(sd), timeb(qtr, qsec, hsec), drop))
            prev = {"gid": gid, "qtr": qtr, "gsec": ix(p["game_seconds_remaining"]), "out": out, "mode": mode(qtr, qsec, hsec, sd),
                    "neutral": neutral, "team": off}
        print("pbp", y, round(time.time() - t0), "s", file=sys.stderr)

    # pass rate: each cell shrunk toward its (down, distance, clock) parent
    def p_of(k):
        a, n = pr[k]; pa, pn = pr0[k[:2] + k[3:]]
        par = (pa + 1) / (pn + 2)
        return (a + 40 * par) / (n + 40)
    pass_rate = {f"{k[0]}{k[1]}{k[2]}{k[3]}": round(p_of(k), 3) for k in pr}
    # field goals: logistic make curve by distance
    a, b = 6.0, -0.1
    for _ in range(30):
        ga = gb = haa = hab = hbb = 0.0
        for d, m in fg:
            pm = 1 / (1 + math.exp(-(a + b * d))); w = pm * (1 - pm)
            ga += m - pm; gb += (m - pm) * d; haa += w; hab += w * d; hbb += w * d * d
        det = haa * hbb - hab * hab
        a += (hbb * ga - hab * gb) / det; b += (haa * gb - hab * ga) / det
    lg = {"sack": rate["sack"] / rate["drop"], "scr": rate["scr"] / rate["drop"], "int": rate["int"] / rate["att"],
          "away": rate["away"] / rate["att"], "fum_r": rate["fum_r"] / rate["runs"], "fum_c": rate["fum_c"] / rate["catches"]}
    # teams (2026): pace, pass rate over expected (log-odds), QB hits+sacks per dropback both ways, run inside/outside allowed
    teams = {}
    lg_hit_o = sum(t["o_hit"] for t in team.values()) / max(1, sum(t["o_drop"] for t in team.values()))
    lg_run = [sum(t[f"d_run{i}_y"] for t in team.values()) / max(1, sum(t[f"d_run{i}_n"] for t in team.values())) for i in (0, 1)]
    lg_int = sum(t["d_int"] for t in team.values()) / max(1, sum(t["d_att"] for t in team.values()))
    lg_pace = sum(t["pace_s"] for t in team.values()) / max(1, sum(t["pace_n"] for t in team.values()))
    for tm, t in team.items():
        if not tm: continue
        exp = [p_of(k[:4]) if k[:4] in pr else 0.58 for k in pr_exp.get(tm, [])]
        act = [k[4] for k in pr_exp.get(tm, [])]
        lo = 0.0
        if exp:
            e_m, a_m = sum(exp) / len(exp), (sum(act) + 0.0) / len(act)
            lo = (math.log(a_m / (1 - a_m)) - math.log(e_m / (1 - e_m))) * len(exp) / (len(exp) + 150)
        teams[tm] = {
            "pace": round(shrink(t["pace_s"] / t["pace_n"], t["pace_n"], 150, lg_pace) / lg_pace, 3) if t["pace_n"] else 1.0,
            "proe": round(lo, 3),
            "hit_o": round(shrink((t["o_hit"] / t["o_drop"]) / lg_hit_o, t["o_drop"], 150), 3) if t["o_drop"] else 1.0,
            "hit_d": round(shrink((t["d_hit"] / t["d_drop"]) / lg_hit_o, t["d_drop"], 150), 3) if t["d_drop"] else 1.0,
            "int_d": round(shrink((t["d_int"] / t["d_att"]) / lg_int, t["d_att"], 250), 3) if t["d_att"] else 1.0,
            "run_d": [round(shrink((t[f"d_run{i}_y"] / t[f"d_run{i}_n"]) / lg_run[i], t[f"d_run{i}_n"], 80), 3) if t[f"d_run{i}_n"] else 1.0 for i in (0, 1)],
        }
    rushers = {}
    for rid, (ni, no, yi, yo) in rush_io.items():
        if ni + no < 25: continue
        rushers[rid] = {"out": round((no + 0.4 * 20) / (ni + no + 20), 3),
                        "io": [round(shrink((yi / ni) / lg_run[0], ni, 40), 3) if ni else 1.0, round(shrink((yo / no) / lg_run[1], no, 40), 3) if no else 1.0]}
    sn = max(1, pen_n["snaps"])
    pens = {k: {"p": round(len(v) / sn, 4), "y": cap([x[0] for x in v], 200), "fd": round(sum(x[1] for x in v) / len(v), 3)} for k, v in fouls.items() if len(v) >= 50}
    cov = coverage()
    tables = {
        "v": 1, "built": time.strftime("%Y-%m-%d"), "years": list(YEARS),
        "pass_rate": pass_rate, "fourth": {f"{k[0]}|{k[1]}": [round(x / max(1, sum(v)), 3) for x in v] for k, v in fourth.items() if sum(v) >= 5},
        "fg": [round(a, 4), round(b, 5)],
        "punt": {str(k): cap(v, 150) for k, v in punt.items()},
        "kick": cap(kick, 300),
        "run": {f"{k[0]}|{k[1]}": cap(v, 1500 if k[1] == "op" else 400) for k, v in runs.items() if len(v) >= 25},
        "catch": {f"{k[0]}|{k[1]}": cap(v, 1500 if k[1] == "op" else 400) for k, v in catch.items() if len(v) >= 25},
        "comp": {f"{k[0]}|{k[1]}": round(v[0] / v[1], 3) for k, v in comp.items() if v[1] >= 30},
        "scr": cap(scr), "sack_yds": cap(sackyd, 200),
        "clock": {f"{k[0]}|{k[1]}": round(sum(v) / len(v), 1) for k, v in clock.items() if len(v) >= 30},
        "rate": {k: round(v, 4) for k, v in lg.items()},
        "team": teams, "rusher": rushers, "pen": pens, **cov,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump(tables, open(OUT, "w", encoding="utf-8"), separators=(",", ":"))
    print("sim tables", round(time.time() - t0), "s,", os.path.getsize(OUT) // 1024, "KB", file=sys.stderr)
    return tables


def coverage(year=2025):
    """2025 participation joined to 2025 play-by-play: coverage mix per defense, pass results by coverage and pressure,
    receivers vs man / zone, QBs clean / pressured."""
    path = analytics.fetch(f"pbp_participation/pbp_participation_{year}.csv", 365 * 86400)
    part = {}
    with open(path, encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if not r.get("defense_coverage_type") and r.get("was_pressure") in ("", None): continue
            part[(r["nflverse_game_id"], r["play_id"])] = (r.get("defense_coverage_type") or "", r.get("defense_man_zone_type") or "",
                                                           r.get("was_pressure") == "TRUE", r.get("offense_players") or "")
    cov_n = defaultdict(lambda: defaultdict(int))
    res = defaultdict(lambda: [0, 0.0, 0])          # key -> dropbacks, yards, sacks
    qb = defaultdict(lambda: defaultdict(float)); wr = defaultdict(lambda: defaultdict(float))
    lg = defaultdict(float)
    for p in plays(year):
        if p["qb_dropback"] != "1" or p["penalty"] == "1": continue
        x = part.get((p["game_id"], p["play_id"]))
        if not x: continue
        c, mz, press, onf = x; de = fix(p["defteam"]); yg = ix(p["yards_gained"]); sk = p["sack"] == "1"
        man = "man" if mz.upper().startswith("MAN") else "zone" if mz else ""
        if c: cov_n[de][c] += 1; cov_n["NFL"][c] += 1
        if man: cov_n[de]["_" + man] += 1; cov_n["NFL"]["_" + man] += 1
        for k in (("cov", c), ("press", press), ("mz", man)):
            if k[1] == "": continue
            s = res[f"{k[0]}|{k[1]}"]; s[0] += 1; s[1] += yg; s[2] += sk
        s = res["all"]; s[0] += 1; s[1] += yg; s[2] += sk
        q = p["passer_player_id"]
        if q:
            qb[q]["n" + str(press)] += 1; qb[q]["y" + str(press)] += yg
        lg["n" + str(press)] += 1; lg["y" + str(press)] += yg
        if man:
            for pid in onf.split(";"):
                if not pid: continue
                wr[pid]["s_" + man] += 1; lg["s_" + man] += 1
            rid = p["receiver_player_id"]
            if rid:
                wr[rid]["t_" + man] += 1; wr[rid]["y_" + man] += yg if p["complete_pass"] == "1" else 0
                lg["t_" + man] += 1; lg["y_" + man] += yg if p["complete_pass"] == "1" else 0
    allr = res["all"]; ypd = allr[1] / allr[0]
    by = {k: {"n": v[0], "ypd": round((v[1] / v[0]) / ypd, 3), "sack": round(v[2] / v[0], 4)} for k, v in res.items() if v[0] >= 200}
    mix = {}
    for tm, d in cov_n.items():
        tot = sum(v for k, v in d.items() if not k.startswith("_")) or 1
        mz = d.get("_man", 0) + d.get("_zone", 0) or 1
        mix[tm] = {"cov": {k: round(v / tot, 3) for k, v in d.items() if not k.startswith("_") and v / tot >= 0.01}, "man": round(d.get("_man", 0) / mz, 3), "n": tot}
    lg_press = lg["nTrue"] / (lg["nTrue"] + lg["nFalse"])
    ycl, ypr = lg["yFalse"] / lg["nFalse"], lg["yTrue"] / lg["nTrue"]
    qbs = {q: {"clean": round(shrink((d["yFalse"] / d["nFalse"]) / ycl, d["nFalse"], 120), 3) if d["nFalse"] else 1.0,
               "press": round(shrink((d["yTrue"] / d["nTrue"]) / ypr, d["nTrue"], 60), 3) if d["nTrue"] else 1.0}
           for q, d in qb.items() if d["nFalse"] + d["nTrue"] >= 60}
    rcv = {}
    for pid, d in wr.items():
        if d["t_man"] + d["t_zone"] < 15: continue
        out = {}
        for m in ("man", "zone"):
            tps = (d["t_" + m] / d["s_" + m]) / (lg["t_" + m] / lg["s_" + m]) if d["s_" + m] else 1.0
            tps_all = ((d["t_man"] + d["t_zone"]) / max(1, d["s_man"] + d["s_zone"])) / ((lg["t_man"] + lg["t_zone"]) / (lg["s_man"] + lg["s_zone"]))
            ypt = (d["y_" + m] / d["t_" + m]) / (lg["y_" + m] / lg["t_" + m]) if d["t_" + m] else 1.0
            ypt_all = ((d["y_man"] + d["y_zone"]) / (d["t_man"] + d["t_zone"])) / ((lg["y_man"] + lg["y_zone"]) / (lg["t_man"] + lg["t_zone"]))
            # relative to his own overall level: >1 = he gets open / gains more against this look
            out[m] = [round(shrink(tps / tps_all, d["s_" + m], 120), 3), round(shrink(ypt / ypt_all if ypt_all else 1, d["t_" + m], 25), 3)]
        rcv[pid] = out
    return {"cov_year": year, "cov_res": by, "cov_mix": mix, "press": round(lg_press, 4), "qb_press": qbs, "rcv_mz": rcv}


def ensure(max_age=7 * 86400):
    """the tables, rebuilt when older than a week (downloads ~110 MB of play-by-play, about a minute)."""
    try:   # the build date inside the file (a fresh git checkout resets file times)
        t = json.load(open(OUT, encoding="utf-8"))
        if time.time() - time.mktime(time.strptime(t["built"], "%Y-%m-%d")) <= max_age: return t
    except (FileNotFoundError, json.JSONDecodeError, KeyError, ValueError): pass
    return build()


if __name__ == "__main__":
    t = build()
    print(json.dumps({k: (v if not isinstance(v, (dict, list)) or len(json.dumps(v)) < 300 else f"<{len(v)}>") for k, v in t.items()}, indent=0)[:3000])
