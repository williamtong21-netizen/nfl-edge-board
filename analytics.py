"""Advanced analytics from nflverse (free, public): play-by-play, weekly player stats, snap counts.

team_analytics(season)  -> per-team offense/defense efficiency with 1-32 ranks, plus defense-vs-position
player_usage(season)    -> per-player season usage/efficiency and per-week snap/target/carry lines, keyed by ESPN id
"""
import csv, gzip, io, os, time
from collections import defaultdict
from urllib.request import urlopen, Request

ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(os.environ.get("EDGE_DATA") or os.path.join(ROOT, "data"), "nflverse")
REL = "https://github.com/nflverse/nflverse-data/releases/download/"
TEAM = {"LA": "LAR", "WAS": "WSH"}  # nflverse -> ESPN abbreviations
fix = lambda t: TEAM.get(t, t)


def fetch(path, max_age):
    """Download an nflverse release file into the cache unless a fresh copy exists."""
    os.makedirs(CACHE, exist_ok=True)
    local = os.path.join(CACHE, path.replace("/", "__"))
    if not os.path.exists(local) or time.time() - os.path.getmtime(local) > max_age:
        req = Request(REL + path, headers={"User-Agent": "nfl-edge-board"})
        with urlopen(req, timeout=120) as r, open(local + ".part", "wb") as f: f.write(r.read())
        os.replace(local + ".part", local)
    return local


def rows(path, max_age):
    local = fetch(path, max_age)
    opener = (lambda p: gzip.open(p, "rt", encoding="utf-8")) if local.endswith(".gz") else (lambda p: open(p, encoding="utf-8"))
    with opener(local) as f: return list(csv.DictReader(f))


f = lambda v: float(v) if v not in ("", "NA", None) else None
i = lambda v: int(float(v)) if v not in ("", "NA", None) else 0
r2 = lambda v, n=3: round(v, n) if v is not None else None
mean = lambda xs: sum(xs) / len(xs) if xs else None


def ranks(table, key, higher_better):
    vals = [(t, v[key]) for t, v in table.items() if v.get(key) is not None]
    vals.sort(key=lambda x: x[1], reverse=higher_better)
    return {t: n + 1 for n, (t, _) in enumerate(vals)}


# ---------------------------------------------------------------- team efficiency
OFF_BETTER = {"epa": True, "pepa": True, "repa": True, "sr": True, "exp": True, "prate": None, "proe": None,
              "ppg": None, "rz": True, "third": True, "sackr": False, "ypp": True}


def team_analytics(season):
    pbp = rows(f"pbp/play_by_play_{season}.csv.gz", 6 * 3600)
    pbp = [p for p in pbp if p.get("season_type") == "REG"]
    side = {"off": defaultdict(lambda: defaultdict(list)), "def": defaultdict(lambda: defaultdict(list))}
    games = {"off": defaultdict(set), "def": defaultdict(set)}
    drives = defaultdict(lambda: {"rz": False, "td": False})
    for p in pbp:
        pos, dfn = p.get("posteam"), p.get("defteam")
        if not pos or not dfn: continue
        pos, dfn = fix(pos), fix(dfn)
        key = (p["game_id"], pos, p.get("drive"))
        yl = f(p.get("yardline_100"))
        if yl is not None and yl <= 20: drives[key]["rz"] = True
        if p.get("touchdown") == "1" and fix(p.get("td_team") or "") == pos: drives[key]["td"] = True
        if p.get("play_type") not in ("pass", "run") or f(p.get("epa")) is None: continue
        epa, is_pass, yds = f(p["epa"]), p.get("pass") == "1", f(p.get("yards_gained")) or 0
        neutral = (0.2 <= (f(p.get("wp")) or 0) <= 0.8 and p.get("down") in ("1", "2")
                   and (f(p.get("half_seconds_remaining")) or 0) > 120)
        for s, t in (("off", pos), ("def", dfn)):
            d = side[s][t]; games[s][t].add(p["game_id"])
            d["epa"].append(epa); d["sr"].append(1 if p.get("success") == "1" else 0); d["ypp"].append(yds)
            d["pepa" if is_pass else "repa"].append(epa)
            d["exp"].append(1 if (is_pass and yds >= 20) or (not is_pass and yds >= 10) else 0)
            if neutral:
                d["prate"].append(1 if is_pass else 0)
                if f(p.get("xpass")) is not None: d["xpass"].append(f(p["xpass"]))
            if p.get("down") == "3": d["third"].append(1 if p.get("third_down_converted") == "1" else 0)
            if p.get("qb_dropback") == "1": d["sackr"].append(1 if p.get("sack") == "1" else 0)
    rz = {"off": defaultdict(list), "def": defaultdict(list)}
    defteam = {(p["game_id"], fix(p["posteam"]), p.get("drive")): fix(p["defteam"]) for p in pbp if p.get("posteam") and p.get("defteam")}
    for k, v in drives.items():
        if v["rz"]:
            rz["off"][k[1]].append(1 if v["td"] else 0)
            if k in defteam: rz["def"][defteam[k]].append(1 if v["td"] else 0)

    out = {}
    for s in ("off", "def"):
        tbl = {}
        for t, d in side[s].items():
            pr, xp = mean(d["prate"]), mean(d["xpass"])
            tbl[t] = {"epa": r2(mean(d["epa"])), "pepa": r2(mean(d["pepa"])), "repa": r2(mean(d["repa"])),
                      "sr": r2(mean(d["sr"])), "exp": r2(mean(d["exp"])), "ypp": r2(mean(d["ypp"]), 2),
                      "prate": r2(pr), "proe": r2((pr - xp) if pr is not None and xp is not None else None),
                      "ppg": r2(len(d["epa"]) / max(1, len(games[s][t])), 1), "rz": r2(mean(rz[s][t])),
                      "third": r2(mean(d["third"])), "sackr": r2(mean(d["sackr"])), "g": len(games[s][t])}
        rk = {}
        for k, hb in OFF_BETTER.items():
            if hb is None: hb = True  # neutral-style stats: rank 1 = highest
            elif s == "def" and k != "sackr": hb = not hb  # defense: allowing less is better
            elif s == "def" and k == "sackr": hb = True    # defense: more sacks is better
            rk[k] = ranks(tbl, k, hb)
        for t in tbl: tbl[t]["rk"] = {k: rk[k].get(t) for k in OFF_BETTER}
        out[s] = tbl
    teams = sorted(set(out["off"]) | set(out["def"]))
    res = {t: {"off": out["off"].get(t), "def": out["def"].get(t)} for t in teams}

    # defense vs position, from weekly player stats (what each defense allows to each position)
    ws = [w for w in rows(f"stats_player/stats_player_week_{season}.csv", 6 * 3600) if w.get("season_type") == "REG"]
    agg = defaultdict(lambda: defaultdict(float)); dg = defaultdict(set)
    for w in ws:
        d, pg = fix(w.get("opponent_team") or ""), w.get("position_group")
        if not d or pg not in ("QB", "RB", "WR", "TE"): continue
        dg[d].add(w["game_id"]); a = agg[(d, pg)]
        a["fp"] += f(w.get("fantasy_points_ppr")) or 0
        a["yds"] += f(w.get("receiving_yards")) or 0; a["rec"] += f(w.get("receptions")) or 0; a["tgt"] += f(w.get("targets")) or 0
        a["ryd"] += f(w.get("rushing_yards")) or 0; a["car"] += f(w.get("carries")) or 0
        a["pyd"] += f(w.get("passing_yards")) or 0; a["ptd"] += f(w.get("passing_tds")) or 0
        a["td"] += (f(w.get("receiving_tds")) or 0) + (f(w.get("rushing_tds")) or 0)
    dvp = defaultdict(dict)
    for (d, pg), a in agg.items():
        g = max(1, len(dg[d]))
        dvp[d][pg] = {k: round(v / g, 1) for k, v in a.items()}
        dvp[d][pg]["tot"] = round((a["yds"] + a["ryd"]) / g, 1)
    for pg in ("QB", "RB", "WR", "TE"):
        for k in ("fp", "yds", "rec", "ryd", "pyd", "td", "tot", "tgt", "car", "ptd"):
            tbl = {d: v[pg] for d, v in dvp.items() if pg in v}
            rk = ranks(tbl, k, True)  # 1 = allows the most = softest matchup
            for d in tbl: tbl[d].setdefault("rk", {})[k] = rk.get(d)
    for d, v in dvp.items(): res.setdefault(d, {})["dvp"] = v
    return {"season": season, "plays": len(pbp), "teams": res}


# ---------------------------------------------------------------- player usage
def player_usage(season, espn_ids):
    """Season usage/efficiency plus per-week snaps/targets/carries for the given ESPN ids (this season and last)."""
    xw = rows("players/players.csv", 7 * 86400)
    by_espn = {p["espn_id"]: p for p in xw if p.get("espn_id") in espn_ids}
    gsis = {p["gsis_id"]: e for e, p in by_espn.items() if p.get("gsis_id")}
    pfr = {p["pfr_id"]: e for e, p in by_espn.items() if p.get("pfr_id")}
    weekly = defaultdict(dict)  # espn id -> {(season, week): {...}}
    for yr, age in ((season, 6 * 3600), (season - 1, 30 * 86400)):
        try:
            snaps = rows(f"snap_counts/snap_counts_{yr}.csv", age)
            stats = rows(f"stats_player/stats_player_week_{yr}.csv", age)
        except Exception:
            continue
        for s in snaps:
            e = pfr.get(s.get("pfr_player_id"))
            if e: weekly[e].setdefault((yr, i(s["week"])), {})["snap"] = r2(f(s.get("offense_pct")), 2)
        team_car = defaultdict(float)
        for w in stats: team_car[(w.get("team"), w.get("week"))] += f(w.get("carries")) or 0
        for w in stats:
            e = gsis.get(w.get("player_id"))
            if not e: continue
            car = f(w.get("carries")) or 0
            weekly[e].setdefault((yr, i(w["week"])), {}).update({
                "tgt": i(w.get("targets")), "ts": r2(f(w.get("target_share")), 2), "car": int(car),
                "rs": r2(car / team_car[(w.get("team"), w.get("week"))], 2) if team_car[(w.get("team"), w.get("week"))] else None})

    # season-level efficiency from this season's play-by-play
    pbp = [p for p in rows(f"pbp/play_by_play_{season}.csv.gz", 6 * 3600) if p.get("season_type") == "REG"]
    team_tgt, team_air, team_car = defaultdict(float), defaultdict(float), defaultdict(float)
    acc = defaultdict(lambda: defaultdict(float))
    games = defaultdict(set)
    for p in pbp:
        if p.get("play_type") not in ("pass", "run"): continue
        team, yl, air = p.get("posteam"), f(p.get("yardline_100")), f(p.get("air_yards"))
        rid, uid, qid = gsis.get(p.get("receiver_player_id")), gsis.get(p.get("rusher_player_id")), gsis.get(p.get("passer_player_id"))
        if p.get("pass") == "1" and p.get("receiver_player_id"):
            team_tgt[team] += 1; team_air[team] += air or 0
        if p.get("rush") == "1" and p.get("rusher_player_id"): team_car[team] += 1
        if rid:
            a = acc[rid]; a["tgt"] += 1; a["air"] += air or 0; a["team"] = team; games[rid].add(p["game_id"])
            a["rec"] += 1 if p.get("complete_pass") == "1" else 0; a["yac"] += f(p.get("yards_after_catch")) or 0
            a["repa"] += f(p.get("epa")) or 0
            if yl is not None and yl <= 20: a["rz_t"] += 1
            if yl is not None and air is not None and air >= yl: a["ez_t"] += 1
        if uid and p.get("rush") == "1":
            a = acc[uid]; a["car"] += 1; a["ryd"] += f(p.get("yards_gained")) or 0; a["team"] = team; games[uid].add(p["game_id"])
            a["uepa"] += f(p.get("epa")) or 0; a["usr"] += 1 if p.get("success") == "1" else 0
            if yl is not None and yl <= 20: a["rz_c"] += 1
            if yl is not None and yl <= 5: a["gl_c"] += 1
        if qid and p.get("qb_dropback") == "1":
            a = acc[qid]; a["db"] += 1; a["qepa"] += f(p.get("epa")) or 0; a["team"] = team; games[qid].add(p["game_id"])
            a["sk"] += 1 if p.get("sack") == "1" else 0
            if f(p.get("cpoe")) is not None: a["cpoe"] += f(p["cpoe"]); a["cpoe_n"] += 1
            if p.get("pass_attempt") == "1" and p.get("sack") != "1" and air is not None: a["qair"] += air; a["att"] += 1
    season_out = {}
    for e, a in acc.items():
        t, g = a["team"], max(1, len(games[e]))
        u = {"g": g}
        if a["tgt"]:
            ts, ash = a["tgt"] / team_tgt[t] if team_tgt[t] else None, a["air"] / team_air[t] if team_air[t] else None
            u.update({"tpg": r2(a["tgt"] / g, 1), "ts": r2(ts), "ash": r2(ash), "wopr": r2(1.5 * ts + 0.7 * ash) if ts is not None and ash is not None else None,
                      "adot": r2(a["air"] / a["tgt"], 1), "cr": r2(a["rec"] / a["tgt"]), "yac": r2(a["yac"] / a["rec"], 1) if a["rec"] else None,
                      "ept": r2(a["repa"] / a["tgt"]), "rz_t": int(a["rz_t"]), "ez_t": int(a["ez_t"])})
        if a["car"]:
            u.update({"cpg": r2(a["car"] / g, 1), "rsh": r2(a["car"] / team_car[t]) if team_car[t] else None, "ypc": r2(a["ryd"] / a["car"], 1),
                      "epr": r2(a["uepa"] / a["car"]), "rsr": r2(a["usr"] / a["car"]), "rz_c": int(a["rz_c"]), "gl_c": int(a["gl_c"])})
        if a["db"]:
            u.update({"dbpg": r2(a["db"] / g, 1), "epd": r2(a["qepa"] / a["db"]), "cpoe": r2(a["cpoe"] / a["cpoe_n"], 1) if a["cpoe_n"] else None,
                      "qadot": r2(a["qair"] / a["att"], 1) if a["att"] else None, "skr": r2(a["sk"] / a["db"])})
        season_out[e] = u
    for e, wk in weekly.items():
        snaps = [v["snap"] for (y, _), v in wk.items() if y == season and v.get("snap") is not None]
        if snaps: season_out.setdefault(e, {})["snap"] = r2(mean(snaps), 2)
    return {"season": season_out, "weekly": {e: {f"{y}-{w}": v for (y, w), v in wk.items()} for e, wk in weekly.items()}}
