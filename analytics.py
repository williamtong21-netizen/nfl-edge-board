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
        tg = (team, p["game_id"])   # team totals per game, so shares only count games the player actually played
        if p.get("pass") == "1" and p.get("receiver_player_id"):
            team_tgt[tg] += 1; team_air[tg] += air or 0
        if p.get("rush") == "1" and p.get("rusher_player_id"): team_car[tg] += 1
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
        tt = sum(team_tgt[(t, gid)] for gid in games[e]); ta = sum(team_air[(t, gid)] for gid in games[e]); tc = sum(team_car[(t, gid)] for gid in games[e])
        u = {"g": g}
        if a["tgt"]:
            ts, ash = a["tgt"] / tt if tt else None, a["air"] / ta if ta else None
            u.update({"tpg": r2(a["tgt"] / g, 1), "ts": r2(ts), "ash": r2(ash), "wopr": r2(1.5 * ts + 0.7 * ash) if ts is not None and ash is not None else None,
                      "adot": r2(a["air"] / a["tgt"], 1), "cr": r2(a["rec"] / a["tgt"]), "yac": r2(a["yac"] / a["rec"], 1) if a["rec"] else None,
                      "ept": r2(a["repa"] / a["tgt"]), "rz_t": int(a["rz_t"]), "ez_t": int(a["ez_t"])})
        if a["car"]:
            u.update({"cpg": r2(a["car"] / g, 1), "rsh": r2(a["car"] / tc) if tc else None, "ypc": r2(a["ryd"] / a["car"], 1),
                      "epr": r2(a["uepa"] / a["car"]), "rsr": r2(a["usr"] / a["car"]), "rz_c": int(a["rz_c"]), "gl_c": int(a["gl_c"])})
        if a["db"]:
            u.update({"dbpg": r2(a["db"] / g, 1), "epd": r2(a["qepa"] / a["db"]), "cpoe": r2(a["cpoe"] / a["cpoe_n"], 1) if a["cpoe_n"] else None,
                      "qadot": r2(a["qair"] / a["att"], 1) if a["att"] else None, "skr": r2(a["sk"] / a["db"])})
        season_out[e] = u
    for e, wk in weekly.items():
        snaps = [v["snap"] for (y, _), v in wk.items() if y == season and v.get("snap") is not None]
        if snaps: season_out.setdefault(e, {})["snap"] = r2(mean(snaps), 2)
    return {"season": season_out, "weekly": {e: {f"{y}-{w}": v for (y, w), v in wk.items()} for e, wk in weekly.items()}}


# ---------------------------------------------------------------- lineup changes: key players back from injury, or out
KEY_TS, KEY_RS, KEY_DB = 0.18, 0.45, 20   # a key pass catcher (target share), lead back (carry share), starting QB (dropbacks/game)
LINEUP_SHRINK = 0.5                       # how much of a player's measured edge over his replacements we trust
QB_SHRINK, QB_PRIOR, QB_K = 0.7, -0.10, 150   # QB change: trust in the gap; EPA/dropback we assume for an unproven QB; dropbacks of prior
RUSHER, COVER = 0.55, 0.9                 # key pass rusher: sacks + half QB hits per game; key cover man: passes defended + 2x INTs per game
DEF_PTS = {"rush": 0.7, "cover": 0.6}     # points the opposing offense gains when one is out
OL_PTS, OL_CAP = 0.5, 1.5                 # points an offense loses per starting lineman out, and the cap
OL_POS = ("T", "G", "C", "OT", "OG", "OL", "LT", "RT", "LG", "RG")
OUT_WORDS = ("out", "reserve", "doubtful", "suspend", "pup")
norm = lambda n: " ".join(w for w in "".join(c for c in (n or "").lower() if c.isalpha() or c == " ").split() if w not in ("jr", "sr", "ii", "iii", "iv"))


def lineup_changes(season, games, depth):
    """For each upcoming game, find key players whose availability differs from the games behind the team's numbers.
    back: active now but missed some of this season's games (the team's stats undersell him, teammates' oversell them).
    out:  ruled out now but played most of this season's games.
    depth = {team abbr: {"qb": starting QB name, "names": set of normalized offensive depth-chart names}}.
    Returns {team: {"weeks": [weeks played], "keys": [...], "pts": net points per game adjustment}} for teams on the slate."""
    cur = [w for w in rows(f"stats_player/stats_player_week_{season}.csv", 6 * 3600) if w.get("season_type") == "REG"]
    try: prev = [w for w in rows(f"stats_player/stats_player_week_{season - 1}.csv", 30 * 86400) if w.get("season_type") == "REG"]
    except Exception: prev = []
    try: snaps = rows(f"snap_counts/snap_counts_{season}.csv", 6 * 3600)
    except Exception: snaps = []
    team_weeks, played = defaultdict(set), defaultdict(set)
    for w in cur: team_weeks[fix(w["team"])].add(i(w["week"])); played[(fix(w["team"]), norm(w["player_display_name"]))].add(i(w["week"]))
    for s in snaps:
        if ((f(s.get("offense_pct")) or 0) > 0 or (f(s.get("defense_pct")) or 0) > 0) and s.get("game_type", "REG") == "REG":
            played[(fix(s["team"]), norm(s["player"]))].add(i(s["week"]))
    ol_snap = defaultdict(list)     # (team, name) -> offensive snap shares, for spotting starting linemen
    for s in snaps:
        if (s.get("position") or "") in OL_POS and (f(s.get("offense_pct")) or 0) > 0: ol_snap[(fix(s["team"]), norm(s["player"]))].append(f(s["offense_pct"]))
    # per team-week totals and per player-game lines, both seasons
    tot = defaultdict(lambda: defaultdict(float))
    games_of = defaultdict(list)    # normalized name -> [(season, week, team, row)]
    for yr, data in ((season, cur), (season - 1, prev)):
        for w in data:
            t, k = fix(w["team"]), (yr, i(w["week"]), fix(w["team"]))
            for c in ("targets", "receiving_epa", "carries", "rushing_epa", "attempts", "sacks_suffered", "passing_epa", "def_sacks", "def_qb_hits", "def_pass_defended", "def_interceptions"):
                tot[k][c] += f(w.get(c)) or 0
            games_of[norm(w["player_display_name"])].append((yr, i(w["week"]), t, w))
    team_qb = {}
    for w in cur:
        db_ = (f(w.get("attempts")) or 0) + (f(w.get("sacks_suffered")) or 0)
        if db_ <= 0 or w.get("position") != "QB": continue
        t, nm = fix(w["team"]), norm(w["player_display_name"])
        q = team_qb.setdefault(t, {"db": 0.0, "epa": 0.0, "by": defaultdict(float), "wk": defaultdict(list), "name": {}})
        q["db"] += db_; q["epa"] += f(w.get("passing_epa")) or 0; q["by"][nm] += db_; q["name"][nm] = w["player_display_name"]
        if db_ >= 10: q["wk"][nm].append(i(w["week"]))
    inj_by_team = {}
    for g in games:
        if g.get("state") != "pre": continue
        for s in ("home", "away"): inj_by_team[g[s]["abbr"]] = {norm(x["n"]): x for x in g[s].get("inj", [])}
    out = {}
    for team, inj in inj_by_team.items():
        dc = depth.get(team) or {}
        weeks = sorted(team_weeks.get(team, ()))
        if not weeks or not dc.get("names"): continue
        keys = []
        for nm in dc["names"]:
            gl = games_of.get(nm, [])
            if len(gl) < 3: continue
            pos = gl[-1][3].get("position_group")
            def per(c): return [f(r.get(c)) or 0 for *_, r in gl]
            def share(c): return mean([(f(r.get(c)) or 0) / tot[(y, w, t)][c] for y, w, t, r in gl if tot[(y, w, t)][c]]) or 0
            db = mean([a + s for a, s in zip(per("attempts"), per("sacks_suffered"))])
            kind_pos = ("RB" if pos == "RB" and share("carries") >= KEY_RS else
                        "REC" if pos in ("WR", "TE", "RB") and share("targets") >= KEY_TS else None)
            if not kind_pos: continue
            st = inj.get(nm)
            is_out = bool(st and any(x in (st.get("s") or "").lower() for x in OUT_WORDS))
            have = played.get((team, nm), set()) & set(weeks)
            missed = [w for w in weeks if w not in have]
            if is_out and kind_pos != "QB" and len(have) >= max(1, len(weeks) / 2): kind = "out"
            elif not is_out and missed: kind = "back"
            else: continue
            # his edge over the players who replace him, in expected points per game
            ref_weeks = missed if kind == "back" else sorted(have)
            def rate(num, den, mine):
                if mine:  # his own rate across both seasons
                    n, d = sum(per(num)), sum(per(den)) if den != "db" else sum(a + s for a, s in zip(per("attempts"), per("sacks_suffered")))
                else:     # everyone else on this team in the reference games
                    me = {(y, w): r for y, w, t, r in gl if t == team}
                    n = d = 0
                    for w in ref_weeks:
                        T, r = tot[(season, w, team)], me.get((season, w), {})
                        g_ = lambda c: f(r.get(c)) or 0 if r else 0
                        n += T[num] - (g_(num) if kind == "out" else 0)
                        d += (T["attempts"] + T["sacks_suffered"] - (g_("attempts") + g_("sacks_suffered") if kind == "out" else 0)) if den == "db" else T[den] - (g_(den) if kind == "out" else 0)
                return n / d if d else None
            if kind_pos == "QB":
                a, b = rate("passing_epa", "db", True), rate("passing_epa", "db", False)
                edge = (a - b) * db if a is not None and b is not None else 0
            else:
                edge = 0
                for num, den in (("receiving_epa", "targets"), ("rushing_epa", "carries")):
                    a, b, vol = rate(num, den, True), rate(num, den, False), mean(per(den))
                    if a is not None and b is not None and vol: edge += (a - b) * vol
            cap = 5.0 if kind_pos == "QB" else 2.5
            edge = max(-cap, min(cap, LINEUP_SHRINK * edge))
            # this season's team numbers already blend games with and without him; correct only the share that differs
            frac = len(missed) / len(weeks) if kind == "back" else len(have) / len(weeks)
            pts = round(edge * frac * (1 if kind == "back" else -1), 2)
            keys.append({"n": gl[-1][3]["player_display_name"], "nm": nm, "pos": pos, "role": kind_pos, "k": kind,
                         "miss": missed if kind == "back" else [], "have": sorted(have), "ts": round(share("targets"), 3), "rs": round(share("carries"), 3), "pts": pts})
        # ---- quarterback: who actually starts (first healthy QB on the depth chart) vs the QBs behind the season numbers
        isout = lambda nm: bool(inj.get(nm) and any(x in (inj[nm].get("s") or "").lower() for x in OUT_WORDS))
        qbs = [q for q in dc.get("qbs", []) if q]
        new = next((q for q in qbs if not isout(q)), None)
        tq = team_qb.get(team) or {}
        if new and tq.get("db"):
            main = max(tq["by"], key=tq["by"].get)
            share_new = tq["by"].get(new, 0) / tq["db"]
            if share_new < 0.8:
                gl = games_of.get(new, [])
                ndb = sum((f(r.get("attempts")) or 0) + (f(r.get("sacks_suffered")) or 0) for *_, r in gl)
                nepa = sum(f(r.get("passing_epa")) or 0 for *_, r in gl)
                epd_new = (nepa + QB_PRIOR * QB_K) / (ndb + QB_K)
                epd_team = tq["epa"] / tq["db"]
                gap = (epd_new - epd_team) * (1 - share_new)
                pts = round(max(-7.0, min(4.0, QB_SHRINK * gap * tq["db"] / len(weeks))), 2)
                outq = [dc["qbname"].get(q, q) for q in qbs[:qbs.index(new)] if isout(q)]
                keys.append({"n": dc["qbname"].get(new, new), "nm": new, "pos": "QB", "role": "QB", "k": "qb",
                             "main": tq["name"].get(main, main), "outq": outq, "with": sorted(tq["wk"].get(new, [])), "db": int(ndb),
                             "share": round(share_new, 2), "epd": round(epd_new, 3), "epdT": round(epd_team, 3),
                             "eff": round(max(0.8, min(1.1, 1 + 1.2 * gap)), 3), "miss": [], "have": [], "pts": pts})
        # ---- defense: top pass rushers and cover men who are out help the other team's offense
        dkeys = []
        for nm, x in inj.items():
            if not isout(nm) or (x.get("p") or "") in OL_POS: continue
            gl = [r for r in games_of.get(nm, []) if r[3].get("position_group") in ("DL", "LB", "DB")]
            if len(gl) < 4: continue
            have = played.get((team, nm), set()) & set(weeks)
            if len(have) < max(1, len(weeks) / 2): continue   # long-term absence: the season numbers already reflect it
            avg = lambda c: sum(f(r.get(c)) or 0 for *_, r in gl) / len(gl)
            rush, cov = avg("def_sacks") + 0.5 * avg("def_qb_hits"), avg("def_pass_defended") + 2 * avg("def_interceptions")
            kind = "rush" if rush >= RUSHER else "cover" if cov >= COVER else None
            if kind: dkeys.append({"n": x["n"], "p": x.get("p"), "k": kind, "rate": round(rush if kind == "rush" else cov, 2), "pts": DEF_PTS[kind]})
        # ---- offensive line: starting linemen (85%+ of snaps) who are out
        ol = []
        for nm, x in inj.items():
            sh = ol_snap.get((team, nm), [])
            if isout(nm) and len(sh) >= max(1, len(weeks) / 2) and mean(sh) >= 0.85: ol.append(x["n"])
        if ol:
            keys.append({"n": ", ".join(ol), "role": "OL", "k": "ol", "cnt": len(ol), "miss": [], "have": [],
                         "pts": round(-min(OL_CAP, OL_PTS * len(ol)), 2)})
        if keys or dkeys:
            out[team] = {"weeks": weeks, "keys": keys, "pts": round(sum(k["pts"] for k in keys), 2),
                         "def": dkeys, "dpts": round(min(2.0, sum(k["pts"] for k in dkeys)), 2)}
    return out
