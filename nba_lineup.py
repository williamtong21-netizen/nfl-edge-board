"""NBA "teammate out": when a regular sits, how much do the teammates who play gain?

  regulars       players averaging 20+ minutes who played 60%+ of the team's last 10 games
  missing share  for each stat: the absent regulars' recent per-game average / the team's per-game total (all of it, and the
                 part from players at the same position: G / F / C)
  boost          a teammate's projection x (1 + A x missing share + B x same-position missing share), fit by stat and role
                 (starter / bench) on 2025-26: actual / our usual projection in games with regulars out
fit():   learns A / B from last season.   check(): re-scores the saved Kalshi backtest lines with the boost (Brier).
Live: boost(book, pid, team, stat, before, out_ids) for the sync (out_ids from the injury report and the news check).
"""
import json, os, sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
import nba_model as M

COEF_PATH = os.path.join(ROOT, "nba_lineup.json")
POS = lambda p: "C" if (p or "").startswith("C") else "G" if "G" in (p or "") else "F"


def team_index(rows):
    """{(team, date): [rows]} and {team: sorted dates}."""
    by = defaultdict(list)
    for r in rows: by[(r["team_abbreviation"], r["game_date"])].append(r)
    dates = defaultdict(list)
    for (t, d) in by: dates[t].append(d)
    for t in dates: dates[t].sort()
    return by, dates


def regulars(by, dates, team, before, n=10):
    """{athlete id: {min, pos, stats..., share}} for a team's last n games before a date."""
    ds = [d for d in dates[team] if d < before][-n:]
    if len(ds) < 4: return {}, {}
    agg = defaultdict(lambda: defaultdict(float)); cnt = defaultdict(int); pos = {}
    tot = defaultdict(float)
    for d in ds:
        for r in by[(team, d)]:
            pid = r["athlete_id"]; cnt[pid] += 1; pos[pid] = POS(r.get("athlete_position_abbreviation"))
            agg[pid]["min"] += M.f(r["minutes"]) or 0
            for st in M.STAT.values():
                v = M.f(r.get(st)) or 0; agg[pid][st] += v; tot[st] += v
    out = {}
    for pid, a in agg.items():
        g = cnt[pid]
        if g / len(ds) >= 0.6 and a["min"] / g >= 20:
            out[pid] = {"pos": pos[pid], "min": a["min"] / g, **{st: a[st] / g for st in M.STAT.values()}}
    team_avg = {st: tot[st] / len(ds) for st in M.STAT.values()}
    return out, team_avg


def missing(by, dates, team, date, played_ids, later_ids):
    """Absent regulars (not in this game's box, but back with the team later = not traded / waived) and their shares."""
    regs, tavg = regulars(by, dates, team, date)
    gone = [pid for pid in regs if pid not in played_ids and pid in later_ids]
    return {pid: regs[pid] for pid in gone}, tavg


def shares(absent, tavg, pos):
    out = {}
    for st in M.STAT.values():
        t = tavg.get(st) or 1
        out[st] = (sum(a[st] for a in absent.values()) / t, sum(a[st] for a in absent.values() if a["pos"] == pos) / t)
    return out


def samples(season=2026):
    rows = M.box(season); book = M.Book(rows); by, dates = team_index(rows)
    later = defaultdict(set)   # players seen with each team, for the "came back" test
    for r in rows: later[r["team_abbreviation"]].add(r["athlete_id"])
    league = {}
    for st in M.STAT.values():
        tot = defaultdict(float)
        for r in rows: tot[r["game_id"]] += M.f(r.get(st)) or 0
        league[st] = sum(tot.values()) / max(1, len(tot)) / 2
    out = []
    for (team, date), grp in by.items():
        played = {r["athlete_id"] for r in grp}
        absent, tavg = missing(by, dates, team, date, played, later[team])
        for r in grp:
            pid = r["athlete_id"]; hist = book.player(pid, date)
            if len(hist) < 5: continue
            starter = sum(1 for h in hist[-10:] if h.get("starter") == "true") >= 5
            sh = shares(absent, tavg, POS(r.get("athlete_position_abbreviation")))
            for st in M.STAT.values():
                pj = M.project(book, pid, st, date, None, r["opponent_team_abbreviation"], league[st])
                if not pj or pj[0] < 0.5: continue
                out.append((st, starter, sh[st][0], sh[st][1], (M.f(r.get(st)) or 0) / pj[0], pj[0]))
    return out


def fit(season=2026):
    S = samples(season)
    coef = {}
    for st in M.STAT.values():
        for role in (True, False):
            xs = [x for x in S if x[0] == st and x[1] == role]
            # least squares on r - 1 = A m + B m_same (weights = projection, so tiny projections don't dominate), no intercept
            sxx = sum(w * m * m for _, _, m, s, r, w in xs); syy = sum(w * s * s for _, _, m, s, r, w in xs); sxy = sum(w * m * s for _, _, m, s, r, w in xs)
            sxr = sum(w * m * (r - 1) for _, _, m, s, r, w in xs); syr = sum(w * s * (r - 1) for _, _, m, s, r, w in xs)
            det = sxx * syy - sxy * sxy
            A, B = ((sxr * syy - syr * sxy) / det, (syr * sxx - sxr * sxy) / det) if det > 1e-9 else (0, 0)
            # base rate with nobody out (our projection's own bias), for reference
            base = [r for _, _, m, s, r, w in xs if m == 0]
            hit = [x for x in xs if x[2] >= 0.1]
            coef[f"{st}|{'S' if role else 'B'}"] = {"A": round(A, 3), "B": round(B, 3), "n_out": len(hit),
                                                     "ratio_none": round(sum(base) / max(1, len(base)), 3),
                                                     "ratio_out": round(sum(x[4] for x in hit) / max(1, len(hit)), 3)}
    json.dump(coef, open(COEF_PATH, "w"), indent=1)
    return coef


def boost(book, by, dates, later, pid, team, stat, before, out_ids, coef, pos, starter):
    """Multiplier for a player's projection given who's out tonight (out_ids: athlete ids)."""
    regs, tavg = regulars(by, dates, team, before)
    absent = {k: v for k, v in regs.items() if k in out_ids}
    if not absent: return 1.0
    m, ms = shares(absent, tavg, pos)[stat]
    c = coef.get(f"{stat}|{'S' if starter else 'B'}", {})
    return max(0.85, min(1.45, 1 + c.get("A", 0) * m + c.get("B", 0) * ms))


def check(season=2026):
    """Re-score the saved Kalshi backtest (nba_model.backtest rows) with the boost: Brier before / after."""
    coef = json.load(open(COEF_PATH))
    rows = M.box(season); book = M.Book(rows); by, dates = team_index(rows)
    later = defaultdict(set)
    for r in rows: later[r["team_abbreviation"]].add(r["athlete_id"])
    bt = json.load(open(os.path.join(M.CACHE, "nba_backtest.json")))
    name_day = {(M.norm(r["athlete_display_name"]), r["game_date"]): r for r in rows}
    res = []
    for b in bt:
        r = name_day.get((M.norm(b["n"]), b["day"]))
        if not r: continue
        team = r["team_abbreviation"]; played = {x["athlete_id"] for x in by[(team, b["day"])]}
        absent, _ = missing(by, dates, team, b["day"], played, later[team])
        hist = book.player(r["athlete_id"], b["day"])
        starter = sum(1 for h in hist[-10:] if h.get("starter") == "true") >= 5
        k = boost(book, by, dates, later, r["athlete_id"], team, b["stat"], b["day"], set(absent), coef, POS(r.get("athlete_position_abbreviation")), starter)
        p2 = max(0.03, min(0.97, M.p_over(b["stat"], b["mean"] * k, b["line"])))
        res.append({**b, "k": k, "p2": p2})
    br = lambda xs, key: sum((x[key] - x["scored"]) ** 2 for x in xs) / max(1, len(xs))
    hit = [x for x in res if abs(x["k"] - 1) > 0.01]
    print(f"all lines n={len(res)}: Brier ours {br(res, 'p'):.4f} -> with boost {br(res, 'p2'):.4f}  (Kalshi {br(res, 'mid'):.4f})")
    print(f"lines with a regular out n={len(hit)}: ours {br(hit, 'p'):.4f} -> {br(hit, 'p2'):.4f}  (Kalshi {br(hit, 'mid'):.4f})")
    for wt in (0.8,):
        for x in res: x["b1"] = wt * x["mid"] + (1 - wt) * x["p"]; x["b2"] = wt * x["mid"] + (1 - wt) * x["p2"]
        print(f"blend {wt} market: all {br(res, 'b1'):.4f} -> {br(res, 'b2'):.4f};  out-games {br(hit, 'b1'):.4f} -> {br(hit, 'b2'):.4f}")
    return res


if __name__ == "__main__":
    if "check" not in sys.argv:
        c = fit()
        for k, v in c.items(): print(k, v)
    check()
