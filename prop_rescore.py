"""Re-score the saved yardage-prop backtest (prop_backtest_w*.json: Kalshi line, prices and result per player) with the
usage-based model, and compare it with the old recency-average model on exactly the same props. No API calls.

Usage model, all from data before the week:
  volume     = his recent share of team targets / carries x the team's expected pass / run plays this game
               (team pace from earlier weeks, more plays in high totals, trailing teams throw, leading teams run)
  efficiency = his yards per target / carry / attempt and catch rate, shrunk toward the position average,
               times how the opponent's defense allows them
Usage:  python prop_rescore.py
"""
import json, math, os, sys
from collections import defaultdict
import analytics, situational
from analytics import rows, fix, f, i, mean, norm
from td_backtest import SEASON, bets
from prop_backtest import p_over, DIST, ANCHOR, SHRINK_P

POS_EFF = {"ypt": {"WR": 8.2, "TE": 7.2, "RB": 5.8}, "cr": {"WR": 0.63, "TE": 0.70, "RB": 0.78},
           "ypc": {"RB": 4.3, "QB": 5.5, "WR": 6.0}, "ypa": 6.6}
K = {"tgt": 50, "car": 80, "att": 200}        # sample size (targets / carries / attempts) at which his own rate gets half the weight
QB_YPC = 5.0                                  # prior yards per carry for a quarterback's runs
SCRIPT = 0.012                                # pass share change per point of expected margin (trailing teams throw)
DECAY = 0.85                                  # weight per game back in a player's history (shares and volumes)
RUN_SCRIPT = 1.0                              # how strongly game script moves the run game (1 = mirror of the pass side)


def context(week):
    st = [w for w in rows(f"stats_player/stats_player_week_{SEASON}.csv", 6 * 3600) if w.get("season_type") == "REG"]
    prev = [w for w in rows(f"stats_player/stats_player_week_{SEASON - 1}.csv", 30 * 86400) if w.get("season_type") == "REG"]
    snaps = rows(f"snap_counts/snap_counts_{SEASON}.csv", 6 * 3600)
    sched = [g for g in rows("schedules/games.csv.gz", 6 * 3600) if g["season"] == str(SEASON) and g["game_type"] == "REG"]
    before = [w for w in st if i(w["week"]) < week]
    team = defaultdict(lambda: defaultdict(float)); tw = defaultdict(set)
    for yr, data in ((SEASON, before), (SEASON - 1, prev)):
        for w in data:
            k = (yr, i(w["week"]), fix(w["team"]))
            for c in ("targets", "carries", "attempts", "sacks_suffered"): team[k][c] += f(w.get(c)) or 0
    for w in before: tw[fix(w["team"])].add(i(w["week"]))
    # team pace this season (pass attempts + sacks, carries per game), shrunk toward the league
    pace = {}
    lg_pass = mean([team[(SEASON, wk, t)]["attempts"] + team[(SEASON, wk, t)]["sacks_suffered"] for t in tw for wk in tw[t]]) or 35
    lg_run = mean([team[(SEASON, wk, t)]["carries"] for t in tw for wk in tw[t]]) or 26
    for t, wks in tw.items():
        n = len(wks); w_ = n / (n + 3)
        pace[t] = (lg_pass + w_ * (mean([team[(SEASON, wk, t)]["attempts"] + team[(SEASON, wk, t)]["sacks_suffered"] for wk in wks]) - lg_pass),
                   lg_run + w_ * (mean([team[(SEASON, wk, t)]["carries"] for wk in wks]) - lg_run))
    # defense efficiency allowed: yards per target / carry / attempt vs league, shrunk by sample
    d = defaultdict(lambda: defaultdict(float))
    for w in before:
        o, pg = fix(w.get("opponent_team") or ""), w.get("position_group")
        d[o]["ry"] += f(w.get("receiving_yards")) or 0; d[o]["tg"] += f(w.get("targets")) or 0; d[o]["rc"] += f(w.get("receptions")) or 0
        if pg == "RB": d[o]["uy"] += f(w.get("rushing_yards")) or 0; d[o]["uc"] += f(w.get("carries")) or 0
        if pg == "QB": d[o]["py"] += f(w.get("passing_yards")) or 0; d[o]["pa"] += f(w.get("attempts")) or 0
    tot = lambda k: sum(v[k] for v in d.values())
    lg = {"ypt": tot("ry") / max(1, tot("tg")), "cr": tot("rc") / max(1, tot("tg")), "ypc": tot("uy") / max(1, tot("uc")), "ypa": tot("py") / max(1, tot("pa"))}
    def dfac(o, num, den, key, k):
        x = d.get(o) or {}; n_ = x.get(den, 0)
        if not n_ or not lg[key]: return 1.0
        r = (x[num] / n_) / lg[key]; w_ = n_ / (n_ + k)
        return max(0.85, min(1.15, 1 + w_ * (r - 1)))
    games = {}
    for g in sched:
        if i(g["week"]) != week: continue
        H, A = fix(g["home_team"]), fix(g["away_team"]); sl, tl = f(g.get("spread_line")), f(g.get("total_line"))
        if sl is None or tl is None: continue
        for t, o, m in ((H, A, sl), (A, H, -sl)): games[t] = {"opp": o, "margin": m, "total": tl}
    hist = defaultdict(list)
    for w in prev: hist[norm(w["player_display_name"])].append((SEASON - 1, i(w["week"]), w))
    for w in before: hist[norm(w["player_display_name"])].append((SEASON, i(w["week"]), w))
    snap = {(norm(s_["player"]), i(s_["week"])): f(s_.get("offense_pct")) or 0 for s_ in snaps}
    # each team's passing vs rushing touchdowns so far (the passing-TD prop is the team's expected TDs through the air)
    team_td = defaultdict(lambda: [0.0, 0.0])
    for w in before:
        team_td[fix(w["team"])][0] += f(w.get("passing_tds")) or 0; team_td[fix(w["team"])][1] += f(w.get("rushing_tds")) or 0
    co = situational.coaches(SEASON)
    this = {norm(w["player_display_name"]): w for w in st if i(w["week"]) == week}
    return locals()


def usage_raw(C, nm, mkt, team=None, pos=None, game=None):
    """Projected stat for one player and market. team / pos / game ({opp, margin, total}) default to what the backtest
    knows; the live app passes them from the slate (its own team, the opponent and the current lines)."""
    if team is None:
        w4 = C["this"].get(nm)
        if not w4: return None
        team, pos = fix(w4["team"]), w4.get("position_group")
    t, pg = team, {"FB": "RB", "HB": "RB"}.get(pos, pos)
    gm = game or C["games"].get(t)
    if not gm: return None
    hist = sorted(C["hist"].get(nm, []), key=lambda x: (x[0], x[1]), reverse=True)[:8]
    if len(hist) < 2: return None
    sn = sorted(v for v in (C["snap"].get((nm, wk)) for y, wk, _ in hist if y == SEASON) if v is not None)
    usual = sn[len(sn) // 2] if sn else None
    moved = any(y == SEASON - 1 and fix(r["team"]) != t for y, _, r in hist)
    prev_w = 0.3 if (C["co"].get(t) or {}).get("yrs") == 1 or moved else 0.6
    def share(col, tcol):
        num = den = 0.0
        for age, (y, wk, r) in enumerate(hist):
            s_ = C["snap"].get((nm, wk)) if y == SEASON else None
            if usual and s_ is not None and s_ < min(0.3, usual / 2): continue
            tt = C["team"][(y, wk, fix(r["team"]))][tcol] + (C["team"][(y, wk, fix(r["team"]))]["sacks_suffered"] if tcol == "attempts" else 0)
            if not tt: continue
            wt = DECAY ** age * (1.0 if y == SEASON else prev_w)
            num += wt * (f(r.get(col)) or 0) / tt; den += wt
        return num / den if den else None
    def rate(num_col, den_col, prior, k):
        n = sum(f(r.get(num_col)) or 0 for *_, r in hist); dd = sum(f(r.get(den_col)) or 0 for *_, r in hist)
        return (n + prior * k) / (dd + k)
    pass_pg, run_pg = C["pace"].get(t, (35, 26))
    plays = 1 + 0.25 * (gm["total"] / 44.5 - 1)
    tilt = max(-0.12, min(0.12, -SCRIPT * gm["margin"]))         # trailing -> more passing
    team_pass, team_run = pass_pg * plays * (1 + tilt), run_pg * plays * (1 - RUN_SCRIPT * tilt)
    o = gm["opp"]
    if mkt in ("Rec yds", "Receptions"):
        ts = share("targets", "targets")
        if ts is None or pg not in POS_EFF["ypt"]: return None
        tgts = ts * team_pass * 0.94                               # ~6% of dropbacks are sacks / throwaways without a target
        if mkt == "Rec yds": return tgts * rate("receiving_yards", "targets", POS_EFF["ypt"][pg], K["tgt"]) * C["dfac"](o, "ry", "tg", "ypt", 150)
        return tgts * rate("receptions", "targets", POS_EFF["cr"][pg], K["tgt"]) * C["dfac"](o, "rc", "tg", "cr", 150)
    if mkt == "Rush yds" and pg == "QB":
        # QB runs are scrambles and designed runs, not a share of team carries: his own carries per game x yards per
        # carry (backtest weeks 1-4: 55.7% leans this way vs 41.9% as a carry share)
        num = den = 0.0
        for age, (y, wk, r) in enumerate(hist):
            wt = 0.85 ** age * (1.0 if y == SEASON else prev_w); num += wt * (f(r.get("carries")) or 0); den += wt
        return (num / den if den else 0) * rate("rushing_yards", "carries", QB_YPC, 60)
    if mkt == "Rush yds":
        cs = share("carries", "carries")
        if cs is None: return None
        prior = POS_EFF["ypc"].get(pg, 4.3)
        return cs * team_run * rate("rushing_yards", "carries", prior, K["car"]) * (C["dfac"](o, "uy", "uc", "ypc", 80) if pg == "RB" else 1.0)
    if mkt == "Pass yds":
        if pg != "QB": return None
        att = team_pass * 0.93
        return att * rate("passing_yards", "attempts", POS_EFF["ypa"], K["att"]) * C["dfac"](o, "py", "pa", "ypa", 120)
    # the rest of the board, same opportunity x efficiency idea
    if mkt in ("Pass att", "Completions", "Pass TD", "INT"):
        if pg != "QB": return None
        att = team_pass * 0.93                                       # dropbacks minus sacks / scrambles
        if mkt == "Pass att": return att
        if mkt == "Completions": return att * rate("completions", "attempts", 0.645, K["att"]) * C["dfac"](o, "rc", "tg", "cr", 150)
        if mkt == "Pass TD":
            # the team's expected points x TDs per point x share of its TDs through the air (shrunk to 60%): on Kalshi weeks
            # 1-4 this matched Kalshi (Brier 0.245 vs 0.244) where the QB's own TD rate (0.250) and the old average (0.248) didn't
            ptd, rtd = C["team_td"][t]
            share = (ptd + 0.6 * 8) / (ptd + rtd + 8)
            return (gm["total"] / 2 + gm["margin"] / 2) * 0.105 * 0.94 * share
        return att * rate("passing_interceptions", "attempts", 0.024, 400) * (1 + max(-0.15, min(0.15, 0.015 * -gm["margin"])))   # trailing QBs force throws
    if mkt == "Carries":
        if pg == "QB":
            num = den = 0.0
            for age, (y, wk, r) in enumerate(hist):
                wt = 0.85 ** age * (1.0 if y == SEASON else prev_w); num += wt * (f(r.get("carries")) or 0); den += wt
            return num / den if den else None
        cs = share("carries", "carries")
        return cs * team_run if cs is not None else None
    if mkt == "Rush+rec yds":
        parts = [usage_raw(C, nm, "Rush yds", team, pos, game), usage_raw(C, nm, "Rec yds", team, pos, game)]
        return sum(x for x in parts if x) if any(parts) else None
    if mkt == "Pass+rush yds":
        if pg != "QB": return None
        py, ry = usage_raw(C, nm, "Pass yds", team, pos, game), usage_raw(C, nm, "Rush yds", team, pos, game)
        return py + (ry or 0) if py else None
    return None


def chance(mkt, raw, L):
    proj = L + ANCHOR * (raw - L)
    return max(0.03, min(0.97, 0.5 + SHRINK_P * (p_over(DIST[mkt], proj, L) - 0.5)))


if __name__ == "__main__":
    allr = []
    for w in (1, 2, 3, 4):
        path = os.path.join(analytics.CACHE, f"prop_backtest_w{w}.json")
        if not os.path.exists(path): continue
        C = context(w)
        for r in json.load(open(path)):
            raw = usage_raw(C, norm(r["n"]), r["m"])
            if raw is None or raw <= 0: continue
            r["p_new"] = chance(r["m"], raw, r["line"]); r["raw_new"] = raw
            allr.append(r)
    print(f"{len(allr)} props re-scored with both models")
    def brier(k): return mean([(r[k] - r["scored"]) ** 2 for r in allr])
    def lean(k, th=0.55, g=None):
        g = [r for r in (g or allr) if abs(r[k] - 0.5) >= th - 0.5 and r["actual"] != r["line"]]
        h = sum((r[k] > 0.5) == r["scored"] for r in g); return h, len(g) - h
    for k, lbl in (("p", "Old model (recency average)"), ("p_new", "New model (usage x efficiency)"), ("mid", "Kalshi")):
        line = f"  {lbl:32} Brier {brier(k):.4f}"
        if k != "mid":
            h, l = lean(k); line += f" | leans 55%+ {h}-{l} ({h / max(1, h + l) * 100:.1f}%)"
        print(line)
    for k in ("p", "p_new"):
        for wt in (0.0, 0.4, 0.6):
            for r in allr: r["b"] = wt * r["mid"] + (1 - wt) * r[k]
            n, w_, pl = bets(allr, "b"); h, l = lean("b")
            print(f"  {k:6} market {wt:.1f}: Brier {brier('b'):.4f}  leans 55%+ {h}-{l} ({h / max(1, h + l) * 100:.1f}%)  bets {n} P/L ${pl:+.0f} ({pl / max(1, n * 10) * 100:+.1f}%)")
    print("By market (new model, 55%+ leans):")
    for m in ("Pass yds", "Rush yds", "Rec yds", "Receptions"):
        g = [r for r in allr if r["m"] == m]
        if g:
            h, l = lean("p_new", g=g); h0, l0 = lean("p", g=g)
            print(f"  {m:11} n={len(g):4}  new {h}-{l} ({h / max(1, h + l) * 100:.1f}%)  old {h0}-{l0} ({h0 / max(1, h0 + l0) * 100:.1f}%)  "
                  f"Brier new {mean([(r['p_new'] - r['scored']) ** 2 for r in g]):.4f} old {mean([(r['p'] - r['scored']) ** 2 for r in g]):.4f} Kalshi {mean([(r['mid'] - r['scored']) ** 2 for r in g]):.4f}")
