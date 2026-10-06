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
LC_BACK_W = 0.4      # props: weight on a teammate's games played while a now-returning key player was out (his volume was inflated)
LC_OUT_W = 0.6       # props: weight on a teammate's games played alongside a key player who is out this week
TD_PER_PT = 0.105    # offensive touchdowns per projected point (about 2.4 TDs in a 23-point game)
TD_FLOOR = {"RB": 0.07, "WR": 0.05, "TE": 0.05, "QB": 0.03}   # lowest TD rate (per game) we'll give a player with a prop line
TD_PASS = 0.6        # share of offensive touchdowns that come through the air
LC_SHARE = 0.5       # props: share of an out player's targets/carries we hand to a teammate in proportion to his own share

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
        sit = g.get("sit") or {}
        h += sit.get("home", 0); a += sit.get("away", 0)     # rest, travel, neutral site, division, weather
        # key players back from injury or newly out: the season numbers were built with a different lineup
        lc = an.get("lineup") or {}
        h += (lc.get(H) or {}).get("pts", 0) + (lc.get(A) or {}).get("dpts", 0)   # our injuries, plus the other defense's
        a += (lc.get(A) or {}).get("pts", 0) + (lc.get(H) or {}).get("dpts", 0)
        qb_out = {s: any(x.get("r") == "starting QB" and any(k in (x.get("s") or "").lower() for k in ("out", "reserve", "doubtful", "suspend"))
                         for x in g[s].get("inj", [])) for s in ("home", "away")}
        # a missing starter is priced by the lineup model (who actually starts, and his record); the flat cut is the fallback
        has_qb = lambda t: any(k["role"] == "QB" for k in (lc.get(t) or {}).get("keys", []))
        if qb_out["home"] and not has_qb(H): h -= QB_OUT
        if qb_out["away"] and not has_qb(A): a -= QB_OUT
        g["proj"] = {"h": h, "a": a, "qbOut": [s for s, v in qb_out.items() if v],
                     "lc": {s: round((lc.get(g[s]["abbr"]) or {}).get("pts", 0) + (lc.get(g[o]["abbr"]) or {}).get("dpts", 0), 2)
                            for s, o in (("home", "away"), ("away", "home")) if (lc.get(g[s]["abbr"]) or {}).get("pts") or (lc.get(g[o]["abbr"]) or {}).get("dpts")}}
    # recenter so the average projected game matches this season's actual scoring (shrinkage pulls totals low otherwise)
    projected = [g for g in games if g.get("proj")]
    shift = (2 * sum(ppg.values()) / len(ppg) - sum(g["proj"]["h"] + g["proj"]["a"] for g in projected) / len(projected)) / 2 if projected else 0
    an["league"]["shift"] = round(shift, 2)
    for g in projected:
        h, a = g["proj"]["h"] + shift, g["proj"]["a"] + shift
        if (g.get("sit") or {}).get("div"):   # rivals keep it closer
            mid, half = (h + a) / 2, (h - a) / 2 * 0.9
            h, a = mid + half, mid - half
        g["proj"].update({"h": round(h, 1), "a": round(a, 1), "margin": round(h - a, 1), "total": round(h + a, 1), "pHome": round(phi((h - a) / MARGIN_SD), 3)})


# ---------------------------------------------------------------- props
def _dvp_key(m, pos):
    if pos == "QB" and any(x in m for x in ("Pass", "Completions", "INT")): return ("QB", "ptd" if m == "Pass TD" else "pyd")
    if "Rush+rec" in m: return (pos, "tot")
    if m in ("Rush yds", "Carries"): return (pos, "ryd")
    if m == "Receptions": return (pos, "rec")
    if m == "Anytime TD": return (pos, "td")
    return (pos, "yds")


# ---------------------------------------------------------------- team identity and game script
PASS_MARKETS = {"Pass yds", "Completions", "Pass att", "Pass TD", "INT", "Rec yds", "Receptions", "Long rec", "Pass+rush yds"}
RUSH_MARKETS = {"Rush yds", "Carries"}
STYLE_CUT = 0.04       # pass rate over expected beyond +/-4% marks a pass-heavy / run-heavy offense (or a pass / run funnel defense)
SCRIPT_PER_PT = 0.004  # each point a team is expected to trail by adds ~0.4% to its pass rate (trailing teams throw)
TILT_TO_VOLUME = 0.6   # how much of that pass-rate tilt shows up in a player's pass- or run-based volume


def team_styles(an):
    """Offense: pass-heavy / run-heavy / balanced. Defense: pass funnel (teams throw on it) / run funnel / neutral.
    Early-season samples are shrunk toward average before labeling."""
    for t, v in (an or {}).get("teams", {}).items():
        o, d = v.get("off"), v.get("def")
        if not o or not d: continue
        w = o["g"] / (o["g"] + 3)
        op, dp = w * (o.get("proe") or 0), w * (d.get("proe") or 0)
        v["style"] = {"off": "pass-heavy" if op >= STYLE_CUT else "run-heavy" if op <= -STYLE_CUT else "balanced",
                      "def": "pass funnel" if dp >= STYLE_CUT else "run funnel" if dp <= -STYLE_CUT else "neutral",
                      "op": round(op, 3), "dp": round(dp, 3)}


def game_tilt(g, an):
    """Each team's expected pass-rate tilt in this game: its own lean + how teams attack this defense + game script."""
    teams = (an or {}).get("teams", {})
    hs = (g.get("odds") or {}).get("hs")
    out = {}
    for side, other in (("home", "away"), ("away", "home")):
        st, os_ = teams.get(g[side]["abbr"], {}).get("style"), teams.get(g[other]["abbr"], {}).get("style")
        if not st or not os_: return None
        margin = (-hs if side == "home" else hs) if hs is not None else 0      # positive = expected to win by that much
        out[side] = round(max(-0.12, min(0.12, st["op"] + os_["dp"] - SCRIPT_PER_PT * margin)), 3)
    return out


def prop_projections(props, games, an, season):
    """Adds proj, pOver and lean to every prop line in `props` ({game_id: [player rows]})."""
    teams = (an or {}).get("teams", {})
    by_id = {g["id"]: g for g in games}
    team_styles(an)
    for g in games:
        t = game_tilt(g, an) if g.get("proj") else None
        if t: g["proj"]["tilt"] = t
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
            coach = ((an or {}).get("coaches") or {}).get(team["abbr"]) or {}
            prev_w = 0.3 if coach.get("yrs") == 1 else 0.6      # new head coach: last season's usage tells us less
            shrink_p = SHRINK_P - (0.05 if coach.get("yrs") == 1 else 0)
            wxm = (g.get("sit") or {}).get("wx") or {}
            # lineup changes around this player: reweight his games and shift volume toward or away from him
            lck = [k for k in ((an or {}).get("lineup", {}).get(team["abbr"]) or {}).get("keys", []) if k["n"] != pl["n"] and k["role"] != "OL"]
            me = next((k for k in ((an or {}).get("lineup", {}).get(team["abbr"]) or {}).get("keys", []) if k["n"] == pl["n"]), None)
            REC_M = ("Rec yds", "Receptions", "Long rec", "Rush+rec yds")
            def touches(k, m):   # does this lineup change move this market for this player?
                if k["role"] == "QB": return pos != "QB" and m not in RUSH_MARKETS
                if k["role"] == "RB": return pos == "RB" and m in ("Rush yds", "Carries", "Rush+rec yds")
                return pos in ("WR", "TE", "RB") and m in REC_M
            def hits(k, r):
                if k["k"] == "qb":   # games with other QBs count less, but only if the new starter has games of his own to lean on
                    return bool(k["with"]) and (r["y"] != season or r["w"] not in k["with"])
                return r["y"] == season and (r["w"] in k["miss"] if k["k"] == "back" else r["w"] in k["have"])
            def lc_w(r, m):
                w = 1.0
                for k in lck:
                    if touches(k, m) and hits(k, r): w *= LC_BACK_W if k["k"] in ("back", "qb") else LC_OUT_W
                return w
            notes = []
            if me and me["k"] == "qb": notes.append(f'Starting at QB{" for " + ", ".join(me["outq"]) if me["outq"] else ""}; {me["db"]} dropbacks in the last two seasons')
            if me and me["k"] == "back": notes.append(f'Back after missing {"weeks" if len(me["miss"]) > 1 else "week"} {", ".join(map(str, me["miss"]))}')
            for k in lck:
                if not any(touches(k, pr["m"]) for pr in pl["props"]): continue
                n = sum(1 for r in pl["log"] if hits(k, r))
                if k["k"] == "qb":
                    who = f'{k["n"]} starts at QB' + (f' for {", ".join(k["outq"])}' if k["outq"] else '')
                    eff = f', so we expect {"less" if k["eff"] < 1 else "more"} from the passing game' if abs(k["eff"] - 1) >= 0.03 else ''
                    notes.append(who + eff + (f'; {n} of his games came with other QBs, so they count less' if n else '')); continue
                if n: notes.append(f'{k["n"]} {"is back" if k["k"] == "back" else "is out"}; {n} of his games this year came {"without" if k["k"] == "back" else "with"} him, so they count less')
            if notes: pl["lc"] = notes
            def lc_vol(m):
                v = 1.0
                for k in lck:
                    if k["k"] == "qb" and len(k["with"]) < 2 and pos != "QB" and m in ("Rec yds", "Long rec", "Rush+rec yds", "Receptions"):
                        v *= k["eff"] if m != "Receptions" else 1 + (k["eff"] - 1) / 2   # a weaker QB: fewer yards per target
                    if k["k"] != "out": continue
                    without = sum(1 for r in pl["log"] if r["y"] == season and r["w"] not in k["have"])
                    if without >= 2: continue    # he already has games without the star; the reweighting covers it
                    if m in ("Rec yds", "Receptions", "Long rec") and u.get("ts"): v *= 1 + LC_SHARE * k["ts"] / max(0.5, 1 - k["ts"])   # his targets spread in proportion to everyone's share
                    if m in ("Rush yds", "Carries") and pos == "RB" and k["role"] == "RB": v *= 1 + LC_SHARE * k["rs"]
                return round(max(0.75, min(v, 1.25)), 3)
            for pr in pl["props"]:
                keys = PROP_STATS.get(pr["m"])
                if not keys: continue
                vals, wts = [], []
                for age, r in enumerate(pl["log"]):            # log is newest first
                    v = [r["s"].get(k) for k in keys]
                    if all(x is None for x in v): continue
                    vals.append(sum(x or 0 for x in v)); wts.append(0.85 ** age * (1.0 if r["y"] == season else prev_w) * lc_w(r, pr["m"]))
                if len(vals) < 2: continue
                base = sum(v * w for v, w in zip(vals, wts)) / sum(wts)
                dk = _dvp_key(pr["m"], pos)
                opp_v = (teams.get(opp["abbr"], {}).get("dvp") or {}).get(dk[0], {}).get(dk[1])
                mult = 1.0
                if opp_v is not None and lg.get(dk):
                    mult = max(0.85, min(1.15, 1 + 0.5 * (opp_v / lg[dk] - 1)))
                if pr["m"] == "Anytime TD":
                    # three reads blended: his recent TDs, his red-zone looks, and his share of the offense times the
                    # touchdowns his team should score tonight (so a regular with no TDs yet isn't rated near zero)
                    hist = base * mult * env
                    looks = ((u.get("rz_t") or 0) + (u.get("rz_c") or 0)) / u["g"] if u.get("g") else None
                    usage = None
                    if g.get("proj"):
                        mine = g["proj"]["h"] if side == "home" else g["proj"]["a"]
                        us = [r.get("us") or {} for r in pl["log"] if r["y"] == season]
                        ts = u.get("ts") if u.get("ts") is not None else (sum(x.get("ts") or 0 for x in us) / len(us) if us else 0)
                        rs = u.get("rsh") if u.get("rsh") is not None else (sum(x.get("rs") or 0 for x in us) / len(us) if us else 0)
                        if ts or rs: usage = TD_PER_PT * mine * (TD_PASS * (ts or 0) + (1 - TD_PASS) * (rs or 0)) * mult
                    parts = [(0.35, hist)] + ([(0.25, 0.17 * looks * env)] if looks is not None else []) + ([(0.4, usage)] if usage is not None else [])
                    lam = min(sum(w * v for w, v in parts) / sum(w for w, _ in parts), 1.2)
                    lam = max(lam, TD_FLOOR.get(pos, 0.04))   # anyone getting a prop line plays; nobody is a true zero
                    pr["proj"] = round(lam, 2)
                    pr["pOver"] = round(min(0.75, 1 - math.exp(-lam)), 3)
                    for b in pr.get("books", []): b["p"] = pr["pOver"]
                else:
                    # game script: a pass-leaning matchup feeds passing and receiving volume and starves the run game
                    tilt = ((g.get("proj") or {}).get("tilt") or {}).get(side, 0)
                    vol = 1 + TILT_TO_VOLUME * tilt if pr["m"] in PASS_MARKETS else 1 - TILT_TO_VOLUME * tilt if pr["m"] in RUSH_MARKETS else 1.0
                    pr["vol"] = round(vol, 3)
                    lv = lc_vol(pr["m"]) * (wxm.get("long", 1) if pr["m"] == "Long rec" else wxm.get("pass", 1) if pr["m"] in PASS_MARKETS else wxm.get("rush", 1) if pr["m"] in RUSH_MARKETS else 1)
                    lv = round(lv, 3)
                    if wxm and lv != 1 and abs(lv - lc_vol(pr["m"])) > 0.005: pr["wxv"] = round(lv / lc_vol(pr["m"]), 3)
                    if lv != 1.0: pr["lcv"] = lv
                    raw = base * mult * vol * lv * (env if pr["m"] not in ("INT",) else 1.0)
                    proj = pr["l"] + ANCHOR * (raw - pr["l"])
                    pr["proj"], pr["raw"] = round(proj, 1), round(raw, 1)
                    def chance(line):
                        p = p_over(DIST.get(pr["m"], ("lognormal", 0.5)), proj, line)
                        p = 0.5 + shrink_p * (p - 0.5)      # model uncertainty: pull every read part-way back to a coin flip
                        return round(max(0.03, min(0.97, p)), 3)  # same rule at every line, so alternate lines line up
                    pr["pOver"] = chance(pr["l"])
                    for b in pr.get("books", []): b["p"] = chance(b["l"])   # each book's own line gets its own hit chance
                pr["mx"] = round(mult, 2)
