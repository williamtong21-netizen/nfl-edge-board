"""Unit matchups for the college model: opponent-adjusted run and pass efficiency on both sides of the ball, plus pace.

Each game gives four observations per team (CFBD advanced box: PPA = expected points added per play):
  rush PPA = league rush avg + team rush offense + opponent rush defense      (pass the same way)
  plays    = league avg + team pace + opponent pace allowed
solved by the same shrink-toward-prior iteration as cfb_model.solve, so a big day against a bad run defense counts for less.
Matchup for a game: each offense's expected PPA = its pass rate x (pass O + opp pass D) + run rate x (run O + opp run D),
times expected plays -> expected "points above average" for each side. cfb_edge_backtest tests whether this adds to
the base model (margins) and replaces the weak points-based total.
"""
import json, os
from collections import defaultdict
import cfb_model as cm

TOT_C = (49.57, 0.425, 0.0305)   # total = a + b x (both offenses' expected points above average) + c x expected plays (fit 2023-24)
W_UNIT_TOT = 0.5   # our total = half the points-rating total, half this one (2025-26 test: 54.3% at the opener on 3+ edges)
LAM_U = 4.0        # games of prior in each unit rating
CARRY_U = 0.5      # share of last season's unit ratings carried over


def load_adv(year):
    """game id -> team -> {rp, rn, pp, pn, plays}: rush / pass PPA and play counts."""
    A = json.load(open(os.path.join(cm.CACHE, f"adv_{year}.json"), encoding="utf-8"))
    out = defaultdict(dict)
    for a in A:
        o = a.get("offense") or {}
        r, p = o.get("rushingPlays") or {}, o.get("passingPlays") or {}
        if not o.get("plays") or r.get("ppa") is None or p.get("ppa") is None: continue
        rn = round(r["totalPPA"] / r["ppa"]) if r.get("ppa") and r.get("totalPPA") is not None else None
        pn = round(p["totalPPA"] / p["ppa"]) if p.get("ppa") and p.get("totalPPA") is not None else None
        if not rn or not pn or rn < 0 or pn < 0: continue
        out[a["gameId"]][a["team"]] = {"rp": r["ppa"], "rn": rn, "pp": p["ppa"], "pn": pn, "plays": o["plays"]}
    return out


def _solve(obs, prior, lam=LAM_U, iters=40):
    """obs: [(team, opp, value)]; value = mu + O[team] + D[opp]. Returns mu, O, D."""
    mu = sum(v for *_, v in obs) / max(1, len(obs))
    O, D = dict(prior.get("O", {})), dict(prior.get("D", {}))
    by_o, by_d = defaultdict(list), defaultdict(list)
    for t, x, v in obs: by_o[t].append((x, v)); by_d[x].append((t, v))
    for _ in range(iters):
        for t, gs in by_o.items(): O[t] = (lam * prior.get("O", {}).get(t, 0) + sum(v - mu - D.get(x, 0) for x, v in gs)) / (lam + len(gs))
        for t, gs in by_d.items(): D[t] = (lam * prior.get("D", {}).get(t, 0) + sum(v - mu - O.get(x, 0) for x, v in gs)) / (lam + len(gs))
    return {"mu": mu, "O": O, "D": D}


def unit_ratings(games, adv, prior=None):
    """games: cfb_model game dicts (finished); adv: load_adv() for the season. prior: last season's carried units."""
    prior = prior or {}
    obs = {"run": [], "pass": [], "pace": []}; pr = defaultdict(lambda: [0, 0])
    for g in games:
        if not g["done"]: continue
        a = adv.get(g["id"], {})
        for t, x in ((g["home"], g["away"]), (g["away"], g["home"])):
            s = a.get(t)
            if not s: continue
            obs["run"].append((t, x, s["rp"])); obs["pass"].append((t, x, s["pp"])); obs["pace"].append((t, x, s["plays"]))
            pr[t][0] += s["pn"]; pr[t][1] += s["pn"] + s["rn"]
    U = {k: _solve(v, prior.get(k, {})) for k, v in obs.items()}
    # pass rate: shrink toward ~48% with 150 plays of prior (and last season's rate when known)
    lastp = prior.get("prate", {})
    U["prate"] = {t: (p + 150 * lastp.get(t, 0.48)) / (n + 150) for t, (p, n) in pr.items()}
    return U


def carry(final):
    return {k: ({"O": {t: v * CARRY_U for t, v in final[k]["O"].items()}, "D": {t: v * CARRY_U for t, v in final[k]["D"].items()}} if k != "prate" else final[k])
            for k in ("run", "pass", "pace", "prate")}


def matchup(U, home, away, neutral=False):
    """expected PPA per play and plays for each offense -> (home pts-above-avg, away pts-above-avg, plays)."""
    def side(o, d):
        pr = U["prate"].get(o, 0.48)
        run = U["run"]["mu"] + U["run"]["O"].get(o, 0) + U["run"]["D"].get(d, 0)
        pas = U["pass"]["mu"] + U["pass"]["O"].get(o, 0) + U["pass"]["D"].get(d, 0)
        plays = U["pace"]["mu"] + U["pace"]["O"].get(o, 0) + U["pace"]["D"].get(d, 0)
        lg = pr * U["pass"]["mu"] + (1 - pr) * U["run"]["mu"]
        return (pr * pas + (1 - pr) * run), lg, plays
    ph, lh, nh = side(home, away); pa, la, na = side(away, home)
    return {"ppa_h": ph, "ppa_a": pa, "lg_h": lh, "lg_a": la, "plays_h": nh, "plays_a": na}


def unit_total(U, home, away, neutral=False):
    x = matchup(U, home, away, neutral)
    ut = (x["ppa_h"] - x["lg_h"]) * x["plays_h"] + (x["ppa_a"] - x["lg_a"]) * x["plays_a"]
    return TOT_C[0] + TOT_C[1] * ut + TOT_C[2] * (x["plays_h"] + x["plays_a"])


def compact(U):
    r = lambda d: {t: round(v, 4) for t, v in d.items()}
    return {**{k: {"mu": round(U[k]["mu"], 4), "O": r(U[k]["O"]), "D": r(U[k]["D"])} for k in ("run", "pass", "pace")}, "prate": r(U["prate"])}


def season_finals(allg):
    """final unit ratings per season, each season starting from the last one's (carried)."""
    finals, prior = {}, {}
    for s in sorted({g["season"] for g in allg}):
        try: adv = load_adv(s)
        except FileNotFoundError: continue
        finals[s] = unit_ratings([g for g in allg if g["season"] == s], adv, prior)
        prior = carry(finals[s])
    return finals
