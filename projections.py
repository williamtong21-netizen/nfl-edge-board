"""Edge Board projections: our own numbers to compare against the books.

Team model: each offense's EPA/play meets the opposing defense's EPA/play allowed (both shrunk toward league average
early in the season), times projected plays, converted with the league's points per play, plus home field.
Prop model: recency-weighted recent production x matchup (defense vs position) x scoring environment, with a
distribution per stat type to turn the projection into a hit chance against the line.

These are simple, transparent models, not a market-beating oracle; they're a second opinion next to the line.
"""
import math

HFA = 1.7            # points of home-field advantage (split across both scores)
MARGIN_SD = 13.5     # NFL scoring margin standard deviation vs a projection
SHRINK = 8           # games of league-average "prior" blended into each team's EPA (early-season EPA is noisy)
QB_OUT = 4.0         # points off a team whose starting QB is out or doubtful
SHRINK_P = 0.85      # props: hit chances are pulled 15% of the way back toward 50/50 (the page uses the same rule for alt lines)
ANCHOR = 0.6         # props: how far we move from the book's line toward our raw number (books know injuries, game plans)

PROP_STATS = {
    "Pass yds": ["passingYards"], "Pass TD": ["passingTouchdowns"], "Completions": ["completions"], "Pass att": ["passingAttempts"],
    "INT": ["interceptions"], "Rush yds": ["rushingYards"], "Carries": ["rushingAttempts"], "Rec yds": ["receivingYards"],
    "Receptions": ["receptions"], "Rush+rec yds": ["rushingYards", "receivingYards"], "Pass+rush yds": ["passingYards", "rushingYards"],
    "Long rec": ["longReception"], "Anytime TD": ["rushingTouchdowns", "receivingTouchdowns"],
}
# how each market is distributed around its projection: ("lognormal", cv) | ("poisson",) | ("normal", cv)
DIST = {"Pass yds": ("lognormal", 0.28), "Pass+rush yds": ("lognormal", 0.27), "Rush yds": ("lognormal", 0.55),
        "Rec yds": ("lognormal", 0.65), "Rush+rec yds": ("lognormal", 0.5), "Long rec": ("lognormal", 0.55),
        "Pass att": ("normal", 0.16), "Completions": ("normal", 0.2), "Receptions": ("poisson",), "Carries": ("poisson",),
        "Pass TD": ("poisson",), "INT": ("poisson",)}


def phi(z): return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def p_over(dist, proj, line):
    """Chance the stat finishes above the line."""
    if proj is None or proj <= 0: return 0.0
    kind = dist[0]
    if kind == "poisson":
        k = math.floor(line)  # P(X > line) = 1 - P(X <= floor(line))
        return 1 - sum(math.exp(-proj) * proj ** i / math.factorial(i) for i in range(k + 1))
    if kind == "normal":
        return 1 - phi((line - proj) / max(proj * dist[1], 0.5))
    s2 = math.log(1 + dist[1] ** 2)
    return 1 - phi((math.log(max(line, 0.01)) - (math.log(proj) - s2 / 2)) / math.sqrt(s2))


# ---------------------------------------------------------------- teams
def team_projections(games, an):
    """Adds g['proj'] = {h, a, margin (home), total, pHome} to every game with analytics for both teams."""
    teams = an.get("teams", {}) if an else {}
    off = {t: v["off"] for t, v in teams.items() if v.get("off")}
    dfn = {t: v["def"] for t, v in teams.items() if v.get("def")}
    if len(off) < 20: return
    lg_epa = sum(o["epa"] for o in off.values()) / len(off)
    lg_plays = sum(o["ppg"] for o in off.values()) / len(off)
    ppg = {}
    for g in games:
        for side in ("home", "away"):
            st = g[side].get("stats") or {}
            if st.get("totalPointsPerGame") is not None: ppg[g[side]["abbr"]] = st["totalPointsPerGame"]
    ppp = [ppg[t] / off[t]["ppg"] for t in ppg if t in off and off[t]["ppg"]]
    if not ppp: return
    lg_ppp = sum(ppp) / len(ppp)
    an["league"] = {"epa": round(lg_epa, 3), "plays": round(lg_plays, 1), "ppp": round(lg_ppp, 3)}

    def pts(o, d):
        wo, wd = o["g"] / (o["g"] + SHRINK), d["g"] / (d["g"] + SHRINK)
        epa = lg_epa + wo * (o["epa"] - lg_epa) + wd * (d["epa"] - lg_epa)
        plays = lg_plays + 0.5 * wo * (o["ppg"] - lg_plays) + 0.5 * wd * (d["ppg"] - lg_plays)
        return plays * (lg_ppp + (epa - lg_epa))
    for g in games:
        H, A = g["home"]["abbr"], g["away"]["abbr"]
        if not all(x in off and x in dfn for x in (H, A)): continue
        h, a = pts(off[H], dfn[A]) + HFA / 2, pts(off[A], dfn[H]) - HFA / 2
        qb_out = {s: any(x.get("r") == "starting QB" and any(k in (x.get("s") or "").lower() for k in ("out", "reserve", "doubtful", "suspend"))
                         for x in g[s].get("inj", [])) for s in ("home", "away")}
        if qb_out["home"]: h -= QB_OUT
        if qb_out["away"]: a -= QB_OUT
        g["proj"] = {"h": h, "a": a, "qbOut": [s for s, v in qb_out.items() if v]}
    # recenter so the average projected game matches this season's actual scoring (shrinkage pulls totals low otherwise)
    projected = [g for g in games if g.get("proj")]
    shift = (2 * sum(ppg.values()) / len(ppg) - sum(g["proj"]["h"] + g["proj"]["a"] for g in projected) / len(projected)) / 2 if projected else 0
    an["league"]["shift"] = round(shift, 2)
    for g in projected:
        h, a = g["proj"]["h"] + shift, g["proj"]["a"] + shift
        g["proj"].update({"h": round(h, 1), "a": round(a, 1), "margin": round(h - a, 1), "total": round(h + a, 1), "pHome": round(phi((h - a) / MARGIN_SD), 3)})


# ---------------------------------------------------------------- props
def _dvp_key(m, pos):
    if pos == "QB" and any(x in m for x in ("Pass", "Completions", "INT")): return ("QB", "ptd" if m == "Pass TD" else "pyd")
    if "Rush+rec" in m: return (pos, "tot")
    if m in ("Rush yds", "Carries"): return (pos, "ryd")
    if m == "Receptions": return (pos, "rec")
    if m == "Anytime TD": return (pos, "td")
    return (pos, "yds")


def prop_projections(props, games, an, season):
    """Adds proj, pOver and lean to every prop line in `props` ({game_id: [player rows]})."""
    teams = (an or {}).get("teams", {})
    by_id = {g["id"]: g for g in games}
    lg = {}
    for d in teams.values():
        for pos, v in (d.get("dvp") or {}).items():
            for k, x in v.items():
                if k != "rk": lg.setdefault((pos, k), []).append(x)
    lg = {k: sum(v) / len(v) for k, v in lg.items() if v}
    for gid, rows in props.items():
        g = by_id.get(gid)
        if not g: continue
        for pl in rows:
            side = pl.get("side")
            team, opp = (g["home"], g["away"]) if side == "home" else (g["away"], g["home"])
            pos = {"FB": "RB", "HB": "RB"}.get(pl.get("p"), pl.get("p"))
            # scoring environment: our projected points vs what this offense usually scores
            env = 1.0
            if g.get("proj") and (team.get("stats") or {}).get("totalPointsPerGame"):
                mine = g["proj"]["h"] if side == "home" else g["proj"]["a"]
                env = max(0.85, min(1.15, 1 + 0.3 * (mine / team["stats"]["totalPointsPerGame"] - 1)))
            u = pl.get("u") or {}
            for pr in pl["props"]:
                keys = PROP_STATS.get(pr["m"])
                if not keys: continue
                vals, wts = [], []
                for age, r in enumerate(pl["log"]):            # log is newest first
                    v = [r["s"].get(k) for k in keys]
                    if all(x is None for x in v): continue
                    vals.append(sum(x or 0 for x in v)); wts.append(0.85 ** age * (1.0 if r["y"] == season else 0.6))
                if len(vals) < 2: continue
                base = sum(v * w for v, w in zip(vals, wts)) / sum(wts)
                dk = _dvp_key(pr["m"], pos)
                opp_v = (teams.get(opp["abbr"], {}).get("dvp") or {}).get(dk[0], {}).get(dk[1])
                mult = 1.0
                if opp_v is not None and lg.get(dk):
                    mult = max(0.85, min(1.15, 1 + 0.5 * (opp_v / lg[dk] - 1)))
                if pr["m"] == "Anytime TD":
                    lam = base * mult * env
                    looks = ((u.get("rz_t") or 0) + (u.get("rz_c") or 0)) / u["g"] if u.get("g") else None
                    if looks is not None: lam = 0.5 * lam + 0.5 * 0.17 * looks * env
                    lam = min(lam, 1.2)
                    pr["proj"] = round(lam, 2)
                    pr["pOver"] = round(min(0.75, 1 - math.exp(-lam)), 3)
                    for b in pr.get("books", []): b["p"] = pr["pOver"]
                else:
                    raw = base * mult * (env if pr["m"] not in ("INT",) else 1.0)
                    proj = pr["l"] + ANCHOR * (raw - pr["l"])
                    pr["proj"], pr["raw"] = round(proj, 1), round(raw, 1)
                    def chance(line):
                        p = p_over(DIST.get(pr["m"], ("lognormal", 0.5)), proj, line)
                        p = 0.5 + SHRINK_P * (p - 0.5)      # model uncertainty: pull every read part-way back to a coin flip
                        return round(max(0.03, min(0.97, p)), 3)  # same rule at every line, so alternate lines line up
                    pr["pOver"] = chance(pr["l"])
                    for b in pr.get("books", []): b["p"] = chance(b["l"])   # each book's own line gets its own hit chance
                pr["mx"] = round(mult, 2)
