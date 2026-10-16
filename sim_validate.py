"""Does the game simulator behave like real NFL games? (run after gamesim changes)

1. calibration   sim vs market: each team's points, win / cover / over, every player's main line vs our chance
2. realism       final margins (key numbers 3, 7, 10, 14), ties, offensive snaps per team vs real 2023-25 games
3. correlations  over/under agreement (phi) between role pairs (QB1 pass yds vs WR1 yds, RB1 rush yds vs win, ...)
                 in the sim vs the same measure on real 2023-25 games (situational.correlations)
Run: SIM_SRC=../nfl-edge-app/data python sim_validate.py  -> prints a report, saves data/nflverse/sim_validate.json
"""
import json, os, random, sys
from collections import defaultdict

import analytics, gamesim, situational

N = int(os.environ.get("SIM_N", 1000))


def phi(a, b):
    n11 = sum(1 for x, y in zip(a, b) if x and y); n10 = sum(1 for x, y in zip(a, b) if x and not y)
    n01 = sum(1 for x, y in zip(a, b) if y and not x); n00 = len(a) - n11 - n10 - n01
    den = ((n11 + n10) * (n01 + n00) * (n11 + n01) * (n10 + n00)) ** 0.5
    return (n11 * n00 - n10 * n01) / den if den else None


def roles(tm):
    r = {"QB1": tm.qb}
    rbs = sorted([p for p in tm.all if p.pos == "RB" and not p.id.endswith(("-o", "-r"))], key=lambda p: -(p.lines.get("Rush yds", (0,))[0] or p.cw * 100))
    wrs = sorted([p for p in tm.all if p.pos == "WR" and not p.id.endswith(("-o", "-r"))], key=lambda p: -(p.lines.get("Rec yds", (0,))[0] or p.tw * 100))
    tes = sorted([p for p in tm.all if p.pos == "TE"], key=lambda p: -(p.lines.get("Rec yds", (0,))[0] or p.tw * 100))
    for i, p in enumerate(rbs[:2]): r[f"RB{i + 1}"] = p
    for i, p in enumerate(wrs[:3]): r[f"WR{i + 1}"] = p
    if tes: r["TE1"] = tes[0]
    return r


FEAT = {"P": "Pass yds", "PT": "Pass TD", "R": "Rush yds", "Y": "Rec yds", "C": "Receptions"}


def main():
    src = os.environ.get("SIM_SRC") or os.path.join(gamesim.ROOT, "out")
    slate = json.load(open(os.path.join(src, "slate.json"), encoding="utf-8"))
    props = {}
    for f in sorted(os.listdir(src)):
        if f.startswith("props-"): props.update(json.load(open(os.path.join(src, f), encoding="utf-8"))["games"])
    T = gamesim.load_tables(); gs = gamesim.gsis_map()
    feats = defaultdict(list)            # pair key -> list of (a, b) bools pooled over games
    margins, ties, snaps, cal, gaps = defaultdict(int), 0, [], [], []
    total = 0
    for g in slate["games"]:
        if g.get("state") != "pre" or not props.get(g["id"]): continue
        H, A, NP = gamesim.build_teams(g, props[g["id"]], T, gs)
        sim = gamesim.Sim(T, H, A, NP)
        gamesim.calibrate(sim, 12345)
        rng = random.Random(99)
        res = []
        for _ in range(N):
            sc, st, qp, _, _ = sim.game(rng); res.append((sc, st)); snaps.append(sim.last_snaps / 2)
        o = g.get("odds") or {}; hs, tl = o.get("hs"), o.get("t")
        m = [r[0][0] - r[0][1] for r in res]; t = [r[0][0] + r[0][1] for r in res]
        for x in m: margins[abs(x)] += 1
        ties += sum(1 for x in m if x == 0); total += len(m)
        row = {"g": f"{A.abbr}@{H.abbr}", "pts": [round(sum(r[0][0] for r in res) / N, 1), round(sum(r[0][1] for r in res) / N, 1)],
               "tgt": [H.pts_target, A.pts_target], "hw": round(sum(x > 0 for x in m) / N, 3), "mkt_hw": (g.get("proj") or {}).get("pHome")}
        if hs is not None: row["hc"] = round(sum(x + hs > 0 for x in m) / N, 3)
        if tl is not None: row["ov"] = round(sum(x > tl for x in t) / N, 3)
        cal.append(row)
        for tm in (H, A):
            for p in tm.all:
                for mk, (l, po) in p.lines.items():
                    if mk == "Long rec": continue
                    gaps.append(sum(gamesim.stat_of(r[1], p, mk) > l for r in res) / N - po)
        # features per team per sim (vs the main line; game features vs the market)
        for side, tm, opp in (("home", H, A), ("away", A, H)):
            ti = 0 if side == "home" else 1
            rl = roles(tm)
            cols = {}
            for rn, p in rl.items():
                pos = rn[:2]
                for fk in situational.FEAT[pos]:
                    mk = FEAT[fk]
                    if mk not in p.lines: continue
                    l = p.lines[mk][0]
                    cols[f"{rn}.{fk}"] = [gamesim.stat_of(r[1], p, mk) > l for r in res]
                if pos != "QB" and "Anytime TD" in p.lines:
                    cols[f"{rn}.T"] = [gamesim.stat_of(r[1], p, "Anytime TD") > 0 for r in res]
            mm = [(r[0][ti] - r[0][1 - ti]) for r in res]
            cols["G.W"] = [x > 0 for x in mm]
            if hs is not None:
                sp = hs if side == "home" else -hs
                cols["G.S"] = [x + sp > 0 for x in mm]
            if tl is not None: cols["G.O"] = [x > tl for x in t]
            cols["G.TT"] = [r[0][ti] > tm.pts_target for r in res]
            ks = sorted(cols)
            for i in range(len(ks)):
                for j in range(i + 1, len(ks)):
                    feats[f"{ks[i]}|{ks[j]}|same"].append((cols[ks[i]], cols[ks[j]]))
        print(row, file=sys.stderr)
    emp = situational.correlations(slate["season"])["pairs"]
    comp = []
    for k, lst in feats.items():
        if k not in emp: continue
        a = [x for pa, _ in lst for x in pa]; b = [y for _, pb in lst for y in pb]
        r = phi(a, b)
        if r is None: continue
        comp.append((k, emp[k]["r"], round(r, 3), emp[k]["n"]))
    comp.sort(key=lambda x: -abs(x[1]))
    me = sum(abs(e - s) for _, e, s, _ in comp) / len(comp)
    ex = [e for _, e, _, _ in comp]; sx = [s for _, _, s, _ in comp]
    mx, my = sum(ex) / len(ex), sum(sx) / len(sx)
    cov = sum((x - mx) * (y - my) for x, y in zip(ex, sx)); vx = sum((x - mx) ** 2 for x in ex); vy = sum((y - my) ** 2 for y in sx)
    # real margins and snaps
    real_m = defaultdict(int); rn = 0
    for g in situational._sched():
        if g.get("game_type") == "REG" and g.get("result") not in ("", "NA", None) and 2015 <= int(g["season"]) <= 2025:
            real_m[abs(int(float(g["result"])))] += 1; rn += 1
    key = {k: [round(margins[k] / total, 3), round(real_m[k] / rn, 3)] for k in (0, 1, 2, 3, 4, 6, 7, 8, 10, 14, 17)}
    rep = {"games": cal, "player_gap_mean_abs": round(sum(abs(x) for x in gaps) / len(gaps), 3), "player_gap_bias": round(sum(gaps) / len(gaps), 3),
           "key_numbers_sim_vs_real": key, "snaps_per_team": round(sum(snaps) / len(snaps), 1),
           "corr": {"pairs": len(comp), "mean_abs_diff": round(me, 3), "agreement": round(cov / (vx * vy) ** 0.5, 3),
                    "mean_real": round(mx, 3), "mean_sim": round(my, 3), "top": comp[:30]}}
    json.dump(rep, open(os.path.join(analytics.CACHE, "sim_validate.json"), "w"), indent=1)
    print(json.dumps({k: v for k, v in rep.items() if k != "corr"}, indent=1))
    print(json.dumps({k: v for k, v in rep["corr"].items() if k != "top"}))
    for k, e, s, n in comp[:30]: print(f"{k:28s} real {e:+.3f}  sim {s:+.3f}")


if __name__ == "__main__":
    main()
