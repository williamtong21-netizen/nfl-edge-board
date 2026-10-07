"""Yardage / receptions backtest: our prop reads vs Kalshi's pre-kickoff prices, for finished weeks.

Markets: passing yards, rushing yards, receiving yards, receptions. Our side is rebuilt from data before the week,
following projections.prop_projections: a recency-weighted average of the player's games (this season, and last
season at reduced weight; cameo games skipped), times the defense-vs-position matchup and the scoring environment
(implied team total vs the team's usual scoring), anchored toward the line, then turned into an over chance.
"The line" is the Kalshi rung priced closest to 50/50 before kickoff (sportsbook lines aren't archived for free).
Usage:  python prop_backtest.py 1 2 3 4
"""
import json, math, os, sys, time
from collections import defaultdict
import analytics, situational
from analytics import rows, fix, f, i, mean, norm
from td_backtest import kget, kickoff_utc, MON, K_CODE, SEASON, fee

SERIES = {"KXNFLPASSYDS": ("Pass yds", "passing_yards", "QB", "pyd"), "KXNFLRSHYDS": ("Rush yds", "rushing_yards", None, "ryd"),
          "KXNFLRECYDS": ("Rec yds", "receiving_yards", None, "yds"), "KXNFLREC": ("Receptions", "receptions", None, "rec")}
# the rest of the board (added with the usage model's second pass)
SERIES2 = {"KXNFLPASSATT": ("Pass att", "attempts", "QB", None), "KXNFLPASSCOMP": ("Completions", "completions", "QB", None),
           "KXNFLPASSTDS": ("Pass TD", "passing_tds", "QB", None), "KXNFLPASSINT": ("INT", "passing_interceptions", "QB", None),
           "KXNFLRSHATT": ("Carries", "carries", None, None), "KXNFLRRYDS": ("Rush+rec yds", "rushing_yards+receiving_yards", None, None)}
DIST = {"Pass yds": ("lognormal", 0.28), "Rush yds": ("lognormal", 0.55), "Rec yds": ("lognormal", 0.65), "Receptions": ("poisson",),
        "Pass att": ("normal", 0.16), "Completions": ("normal", 0.2), "Pass TD": ("poisson",), "INT": ("poisson",), "Carries": ("poisson",),
        "Rush+rec yds": ("lognormal", 0.5)}
ANCHOR, SHRINK_P = 0.6, 0.85


def phi(z): return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def p_over(dist, proj, line):
    if proj <= 0: return 0.0
    if dist[0] == "normal": return 1 - phi((line - proj) / max(0.5, dist[1] * proj))
    if dist[0] == "poisson":
        k = math.floor(line); return 1 - sum(math.exp(-proj) * proj ** j / math.factorial(j) for j in range(k + 1))
    s2 = math.log(1 + dist[1] ** 2)
    return 1 - phi((math.log(max(line, 0.01)) - (math.log(proj) - s2 / 2)) / math.sqrt(s2))


_CTX = {}


def stat(r, col):
    return sum(f(r.get(c)) or 0 for c in col.split("+"))


def build(week, markets=None):
    st = [w for w in rows(f"stats_player/stats_player_week_{SEASON}.csv", 6 * 3600) if w.get("season_type") == "REG"]
    prev = [w for w in rows(f"stats_player/stats_player_week_{SEASON - 1}.csv", 30 * 86400) if w.get("season_type") == "REG"]
    snaps = rows(f"snap_counts/snap_counts_{SEASON}.csv", 6 * 3600)
    sched = [g for g in rows("schedules/games.csv.gz", 6 * 3600) if g["season"] == str(SEASON) and g["game_type"] == "REG"]
    co = situational.coaches(SEASON)
    before = [w for w in st if i(w["week"]) < week]
    this = {norm(w["player_display_name"]): w for w in st if i(w["week"]) == week}
    by_player = defaultdict(list)
    for w in prev: by_player[norm(w["player_display_name"])].append((SEASON - 1, i(w["week"]), w))
    for w in before: by_player[norm(w["player_display_name"])].append((SEASON, i(w["week"]), w))
    snap = {(norm(s_["player"]), i(s_["week"])): f(s_.get("offense_pct")) or 0 for s_ in snaps}
    # defense vs position (per game) before the week, and each team's points per game
    dv, dg = defaultdict(float), defaultdict(set)
    for w in before:
        d, pg = fix(w.get("opponent_team") or ""), w.get("position_group")
        for col in ("passing_yards", "rushing_yards", "receiving_yards", "receptions"): dv[(d, pg, col)] += f(w.get(col)) or 0
        dg[d].add(w["game_id"])
    dvp = {k: v / max(1, len(dg[k[0]])) for k, v in dv.items()}
    lg = defaultdict(list)
    for (d, pg, col), v in dvp.items(): lg[(pg, col)].append(v)
    lg = {k: mean(v) for k, v in lg.items()}
    ppg = defaultdict(list)
    for g in sched:
        if i(g["week"]) >= week or g.get("result") in ("", "NA", None): continue
        ppg[fix(g["home_team"])].append(f(g["home_score"])); ppg[fix(g["away_team"])].append(f(g["away_score"]))
    out = []
    for g in [g for g in sched if i(g["week"]) == week]:
        H, A = fix(g["home_team"]), fix(g["away_team"])
        sl, tl = f(g.get("spread_line")), f(g.get("total_line"))
        if sl is None or tl is None: continue
        imp = {H: tl / 2 + sl / 2, A: tl / 2 - sl / 2}; opp = {H: A, A: H}
        end = int(kickoff_utc(g).timestamp())
        d = g["gameday"]; tag = f"{d[2:4]}{MON[int(d[5:7]) - 1]}{d[8:10]}{K_CODE.get(A, A)}{K_CODE.get(H, H)}"
        for series_t, (mkt, col, only_pos, _) in (markets or SERIES).items():
            series = series_t
            try: ms = kget(f"/markets?event_ticker={series}-{tag}&limit=400").get("markets", [])
            except Exception as e: print("kalshi", series, tag, e, file=sys.stderr); continue
            ladders = defaultdict(list)
            for m in ms:
                if ":" in m["title"] and m.get("floor_strike") is not None: ladders[norm(m["title"].split(":")[0])].append(m)
            for nm, rungs in ladders.items():
                w4 = this.get(nm)
                if not w4: continue
                team, pg = fix(w4["team"]), w4.get("position_group")
                if team not in imp or (only_pos and pg != only_pos): continue
                hist = sorted(by_player.get(nm, []), key=lambda x: (x[0], x[1]), reverse=True)[:6]
                sn = [snap.get((nm, wk), None) for y, wk, _ in hist if y == SEASON]
                usual = sorted(x for x in sn if x is not None)
                usual = usual[len(usual) // 2] if usual else None
                moved = any(y == SEASON - 1 for y, _, _ in hist) and any(fix(r["team"]) != team for y, _, r in hist if y == SEASON - 1)
                prev_w = 0.3 if (co.get(team) or {}).get("yrs") == 1 or moved else 0.6
                vals, wts = [], []
                for age, (y, wk, r) in enumerate(hist):
                    s_ = snap.get((nm, wk)) if y == SEASON else None
                    if usual and s_ is not None and s_ < min(0.3, usual / 2): continue   # cameo / early exit
                    vals.append(stat(r, col)); wts.append(0.85 ** age * (1.0 if y == SEASON else prev_w))
                if len(vals) < 2: continue
                base = sum(v * w for v, w in zip(vals, wts)) / sum(wts)
                o = sum(dvp.get((opp[team], pg, c_), 0) for c_ in col.split("+")) or None
                lgv = sum(lg.get((pg, c_), 0) for c_ in col.split("+"))
                mult = max(0.85, min(1.15, 1 + 0.5 * (o / lgv - 1))) if o is not None and lgv else 1.0
                tp = mean(ppg.get(team, [])); env = max(0.85, min(1.15, 1 + 0.3 * (imp[team] / tp - 1))) if tp else 1.0
                raw = base * mult * env
                raw_old = raw
                try:   # the usage model (what the app now uses); the recency average is the fallback
                    import prop_rescore
                    if week not in _CTX: _CTX[week] = prop_rescore.context(week)
                    ru = prop_rescore.usage_raw(_CTX[week], nm, mkt)
                    if ru and ru > 0: raw = ru
                except Exception as e:
                    print("usage model failed", nm, mkt, e, file=sys.stderr)
                # price the 3 rungs nearest our number; the one closest to 50/50 plays the role of the book line
                near = sorted(rungs, key=lambda m: abs(m["floor_strike"] - raw))[:3]
                priced = []
                for m in near:
                    try: cs = kget(f"/series/{series}/markets/{m['ticker']}/candlesticks?start_ts={end - 36 * 3600}&end_ts={end}&period_interval=60").get("candlesticks", [])
                    except Exception: cs = []
                    cs = [c for c in cs if (c.get("yes_bid") or {}).get("close_dollars") and (c.get("yes_ask") or {}).get("close_dollars")]
                    if cs:
                        b_, a_ = float(cs[-1]["yes_bid"]["close_dollars"]), float(cs[-1]["yes_ask"]["close_dollars"])
                        if 0 < b_ and a_ < 0.99: priced.append((m, b_, a_))
                    time.sleep(0.1)
                if not priced: continue
                m, bid, ask = min(priced, key=lambda x: abs((x[1] + x[2]) / 2 - 0.5))
                L = m["floor_strike"]
                proj = L + ANCHOR * (raw - L)
                p = max(0.03, min(0.97, 0.5 + SHRINK_P * (p_over(DIST[mkt], proj, L) - 0.5)))
                actual = stat(w4, col)
                p_old = max(0.03, min(0.97, 0.5 + SHRINK_P * (p_over(DIST[mkt], L + ANCHOR * (raw_old - L), L) - 0.5)))
                out.append({"n": w4["player_display_name"], "m": mkt, "pos": pg, "game": f"{A} @ {H}", "wk": week, "line": L, "raw": round(raw, 1),
                            "p": round(p, 3), "p_old": round(p_old, 3), "raw_old": round(raw_old, 1), "bid": bid, "ask": ask, "mid": (bid + ask) / 2, "actual": actual, "scored": actual > L})
    return out


if __name__ == "__main__":
    two = "--rest" in sys.argv
    weeks = [int(x) for x in sys.argv[1:] if x.isdigit()] or [4]
    allr = []
    for w in weeks:
        r = build(w, SERIES2 if two else None); allr += r
        json.dump(r, open(os.path.join(analytics.CACHE, f"prop_backtest{'2' if two else ''}_w{w}.json"), "w"))
        print(f"week {w}: {len(r)} props", flush=True)
    print("done", len(allr))
