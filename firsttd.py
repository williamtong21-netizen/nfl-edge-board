"""First touchdown scorer: a fair chance for every player, built from the anytime-TD chances the app already has.

Each player's expected touchdowns  lam_i = -ln(1 - p_anytime)  (Poisson). The game's expected touchdowns come from the
implied team totals (TD_PER_PT per point, both teams, defense/special-teams scores included). Every touchdown is
equally likely to be the first one, so
    P(first TD) = lam_i / game_lam x (1 - exp(-game_lam))        (the last factor: the game has a touchdown at all)
Listed players can't add up to more than their team's share; the rest is left to unlisted players and defense/ST.
backtest(): weeks 1-4 vs Kalshi's first-TD prices before kickoff (KXNFLFIRSTTD), graded on Kalshi's own results.
Usage:  python firsttd.py 1 2 3 4
"""
import json, math, os, sys, time
from collections import defaultdict

TD_PER_PT = 0.105
LISTED_MAX = 0.92      # listed players' share of their team's expected TDs at most (the rest: unlisted, D/ST)


def fair(players, imp):
    """players: [(key, team, p_anytime)], imp: {team: implied points}. Returns {key: P(first TD of the game)}."""
    lam_t = {t: TD_PER_PT * max(3.0, v) for t, v in imp.items()}
    game = sum(lam_t.values())
    if game <= 0: return {}
    lam = {k: -math.log(max(1e-6, 1 - min(0.95, p))) for k, _, p in players}
    for t in lam_t:   # don't let a team's listed players claim more than its share of the game's touchdowns
        keys = [k for k, tm, _ in players if tm == t]
        s = sum(lam[k] for k in keys)
        cap = LISTED_MAX * lam_t[t]
        if s > cap:
            for k in keys: lam[k] *= cap / s
    any_td = 1 - math.exp(-game)
    return {k: lam[k] / game * any_td for k, _, _ in players}


def attach(games, props):
    """The sync's hook: pl["ft"] = each player's first-TD chance, from the blended anytime chances and the game's lines."""
    n = 0
    for g in games:
        o = g.get("odds") or {}
        if g.get("state") != "pre" or o.get("hs") is None or o.get("t") is None or g["id"] not in props: continue
        imp = {"home": o["t"] / 2 - o["hs"] / 2, "away": o["t"] / 2 + o["hs"] / 2}
        pls = [(pl["id"], pl["side"], pr["pOver"]) for pl in props[g["id"]] for pr in pl["props"] if pr["m"] == "Anytime TD" and pr.get("pOver")]
        for pid, p in fair(pls, imp).items():
            pl = next(x for x in props[g["id"]] if x["id"] == pid); pl["ft"] = round(p, 4); n += 1
    return n


def backtest(weeks):
    import analytics
    from analytics import rows, fix, f, i, norm
    from td_backtest import kget, kickoff_utc, MON, K_CODE, SEASON, fee, k_markets, k_candles, k_name
    sched = [g for g in rows("schedules/games.csv.gz", 6 * 3600) if g["season"] == str(SEASON) and g["game_type"] == "REG"]
    out = []
    for w in weeks:
        td = json.load(open(os.path.join(analytics.CACHE, f"td_backtest_w{w}.json" if SEASON >= 2026 else f"td_backtest_{SEASON}_w{w}.json")))
        by_game = defaultdict(list)
        for r in td: by_game[r["game"]].append(r)
        for g in [g for g in sched if i(g["week"]) == w]:
            H, A = fix(g["home_team"]), fix(g["away_team"]); key = f"{A} @ {H}"
            sl, tl = f(g.get("spread_line")), f(g.get("total_line"))
            if sl is None or tl is None or key not in by_game: continue
            imp = {H: tl / 2 + sl / 2, A: tl / 2 - sl / 2}
            rs = by_game[key]
            for r in rs:   # the app's anytime chance: recalibrated model blended 60% with the market
                if "blend" not in r:
                    import projections
                    r["blend"] = projections.MKT_TD_W * r["mid"] + (1 - projections.MKT_TD_W) * projections.td_recal(r.get("p_raw", r["p"]))
            ours = fair([(norm(r["n"]), r["team"], r["blend"]) for r in rs], imp)           # what the app would show
            mkt = fair([(norm(r["n"]), r["team"], r["mid"]) for r in rs], imp)              # same recipe from Kalshi's anytime prices
            d = g["gameday"]; ev = f"KXNFLFIRSTTD-{d[2:4]}{MON[int(d[5:7]) - 1]}{d[8:10]}{K_CODE.get(A, A)}{K_CODE.get(H, H)}"
            try: ms = k_markets(ev)
            except Exception as e: print("kalshi", ev, e, file=sys.stderr); continue
            end = int(kickoff_utc(g).timestamp())
            for m in ms:
                if ":" not in m["title"] or m.get("result") not in ("yes", "no"): continue
                nm = norm(k_name(m["title"]))
                if nm not in ours: continue
                try: cs = k_candles("KXNFLFIRSTTD", m["ticker"], end - 36 * 3600, end)
                except Exception: cs = []
                if not cs: continue
                bid, ask = cs[-1]
                if ask >= 0.99 or bid <= 0: continue
                out.append({"n": k_name(m["title"]), "game": key, "wk": w, "p": ours[nm], "pm": mkt[nm], "bid": bid, "ask": ask,
                            "mid": (bid + ask) / 2, "scored": m["result"] == "yes"})
                time.sleep(0.1)
        print(f"week {w}: {sum(1 for r in out if r['wk'] == w)} players", file=sys.stderr, flush=True)
    return out


if __name__ == "__main__":
    import analytics
    from analytics import mean
    from td_backtest import bets
    weeks = [int(x) for x in sys.argv[1:]] or [1, 2, 3, 4]
    rs = backtest(weeks)
    json.dump(rs, open(os.path.join(analytics.CACHE, "firsttd_backtest.json"), "w"))
    print(len(rs), "player-games;", sum(r["scored"] for r in rs), "first TDs")
    for k, lbl in (("p", "ours (app's anytime chances)"), ("pm", "same recipe, Kalshi anytime"), ("mid", "Kalshi first-TD price")):
        print(f"  {lbl:30} Brier {mean([(r[k] - r['scored']) ** 2 for r in rs]):.5f}  avg {mean([r[k] for r in rs]):.4f}  (actual rate {mean([r['scored'] for r in rs]):.4f})")
    for wt in (0.0, 0.5, 0.7):
        for r in rs: r["b"] = wt * r["mid"] + (1 - wt) * r["p"]
        n, won, pl = bets(rs, "b")
        print(f"  blend {wt:.1f} market: Brier {mean([(r['b'] - r['scored']) ** 2 for r in rs]):.5f}  value bets {n} won {won} P/L ${pl:+.0f}")
