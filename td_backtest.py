"""Touchdown backtest: our anytime-TD model vs Kalshi's pre-kickoff prices for one finished week.

Our side is rebuilt with only data from before that week (nflverse stats, play-by-play and snaps through the prior
week, last season, and the closing spread/total from the schedule), following the same recipe as projections.py:
scoring rate (this season + last, role-scaled), red-zone / end-zone / goal-line looks, share of the offense times
the implied team total, the defense's red-zone rate and how it allows TDs, snap ceiling, backup-RB discount, floors.
Kalshi's side is the last hourly candle before kickoff on each player's "1+ touchdowns" market.
Usage:  python td_backtest.py [week]     (default: last completed week)
"""
import json, math, sys, time, os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from urllib.request import urlopen, Request
from urllib.error import HTTPError
import analytics, situational
from analytics import rows, fix, f, i, mean, norm

SEASON = int(os.environ.get("EDGE_BT_SEASON") or 2026)   # set EDGE_BT_SEASON=2025 to replay last season
TD_SERIES = "KXNFLTD" if SEASON >= 2026 else "KXNFLANYTD"   # Kalshi renamed the anytime-TD market for 2026
K_API = "https://api.elections.kalshi.com/trade-api/v2"
K_CODE = {"JAX": "JAC", "LAR": "LA", "WSH": "WAS"}          # ESPN-style -> Kalshi team codes
MON = "JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split()
TD_PER_PT, TD_PASS, PRIOR = 0.105, 0.6, 6
RZ_T, EZ_T, RZ_C, GL_C = 0.22, 0.20, 0.10, 0.20
FLOOR = {"RB": 0.07, "WR": 0.05, "TE": 0.05, "QB": 0.06}


def kget(path, tries=6):
    for n in range(tries):
        try:
            with urlopen(Request(K_API + path, headers={"User-Agent": "nfl-edge-board", "Accept": "application/json"}), timeout=30) as r:
                return json.loads(r.read().decode())
        except HTTPError as e:
            if e.code == 429: time.sleep(1.5 * (n + 1)); continue
            raise
    raise RuntimeError("kalshi rate limit")


def k_markets(event):
    """A game's markets: live/recent ones, or Kalshi's archive for older seasons."""
    ms = kget(f"/markets?event_ticker={event}&limit=300").get("markets", [])
    return ms or kget(f"/historical/markets?event_ticker={event}&limit=300").get("markets", [])


def k_candles(series, ticker, start, end):
    """Hourly candles, from the series endpoint or (older seasons) the archive; each as (bid, ask) closes in dollars."""
    q = f"start_ts={start}&end_ts={end}&period_interval=60"
    try: cs = kget(f"/series/{series}/markets/{ticker}/candlesticks?{q}").get("candlesticks", [])
    except HTTPError: cs = []
    if not cs:
        try: cs = kget(f"/historical/markets/{ticker}/candlesticks?{q}").get("candlesticks", [])
        except HTTPError: cs = []
    out = []
    for c in cs:
        b, a = c.get("yes_bid") or {}, c.get("yes_ask") or {}
        bv, av = b.get("close_dollars") or b.get("close"), a.get("close_dollars") or a.get("close")
        if bv and av: out.append((float(bv), float(av)))
    return out


def k_name(title):
    """Player name from a market title: "Zay Flowers: 1+ touchdowns", "Baltimore at Buffalo: Anytime Touchdown Scorer:
    Zay Flowers", or "Zach Ertz records 60+ receiving yards"."""
    if " records " in title: return title.split(" records ")[0].strip()
    parts = [x.strip() for x in title.split(":")]
    return parts[-1] if len(parts) >= 3 else parts[0]


def kickoff_utc(g):
    et = datetime.strptime(g["gameday"] + " " + (g["gametime"] or "13:00"), "%Y-%m-%d %H:%M")
    return (et + timedelta(hours=4 if 3 <= et.month <= 10 else 5)).replace(tzinfo=timezone.utc)   # EDT until early Nov


def build(week):
    st = [w for w in rows(f"stats_player/stats_player_week_{SEASON}.csv", 6 * 3600) if w.get("season_type") == "REG"]
    prev = [w for w in rows(f"stats_player/stats_player_week_{SEASON - 1}.csv", 30 * 86400) if w.get("season_type") == "REG"]
    pbp = [p for p in rows(f"pbp/play_by_play_{SEASON}.csv.gz", 6 * 3600) if p.get("season_type") == "REG" and i(p.get("week")) < week]
    snaps = [s for s in rows(f"snap_counts/snap_counts_{SEASON}.csv", 6 * 3600) if i(s.get("week")) < week]
    try: snaps_prev = rows(f"snap_counts/snap_counts_{SEASON - 1}.csv", 30 * 86400)
    except Exception: snaps_prev = []
    sched = [g for g in rows("schedules/games.csv.gz", 6 * 3600) if g["season"] == str(SEASON) and g["game_type"] == "REG" and i(g["week"]) == week]
    before = [w for w in st if i(w["week"]) < week]
    this = {norm(w["player_display_name"]): w for w in st if i(w["week"]) == week}
    co = situational.coaches(SEASON)

    # team context from earlier weeks: red-zone TD rate allowed, pass/rush TD splits, TDs allowed by position
    drives = defaultdict(lambda: {"rz": False, "td": False})
    tds = {"off": defaultdict(lambda: [0, 0]), "def": defaultdict(lambda: [0, 0])}
    look = defaultdict(lambda: defaultdict(int)); team_gl = defaultdict(int); pgames = defaultdict(set)
    for p in pbp:
        pos, dfn = fix(p.get("posteam") or ""), fix(p.get("defteam") or "")
        if not pos or not dfn: continue
        key, yl, air = (p["game_id"], pos, p.get("drive")), f(p.get("yardline_100")), f(p.get("air_yards"))
        if yl is not None and yl <= 20: drives[key]["rz"] = True; drives[key]["def"] = dfn
        if p.get("touchdown") == "1" and fix(p.get("td_team") or "") == pos:
            drives[key]["td"] = True
            k = 0 if p.get("pass") == "1" else 1 if p.get("rush") == "1" else None
            if k is not None: tds["off"][pos][k] += 1; tds["def"][dfn][k] += 1
        rid, uid = p.get("receiver_player_name"), p.get("rusher_player_name")
        if p.get("pass") == "1" and p.get("receiver_player_id"):
            a = look[p["receiver_player_id"]]; pgames[p["receiver_player_id"]].add(p["game_id"])
            if yl is not None and yl <= 20: a["rz_t"] += 1
            if yl is not None and air is not None and air >= yl: a["ez_t"] += 1
        if p.get("rush") == "1" and p.get("rusher_player_id"):
            a = look[p["rusher_player_id"]]; pgames[p["rusher_player_id"]].add(p["game_id"])
            if yl is not None and yl <= 20: a["rz_c"] += 1
            if yl is not None and yl <= 5: a["gl_c"] += 1; team_gl[(pos, p["game_id"])] += 1
    rz = defaultdict(lambda: [0, 0])
    for v in drives.values():
        if v["rz"] and v.get("def"): rz[v["def"]][0] += v["td"]; rz[v["def"]][1] += 1
    rz_rate = {t: a / b for t, (a, b) in rz.items() if b}
    lg_rz = mean(list(rz_rate.values()))
    split = lambda t, s: (tds[s][t][0] + PRIOR * TD_PASS) / (sum(tds[s][t]) + PRIOR)
    dvp, dvp_g = defaultdict(float), defaultdict(set)
    for w in before:
        d, pg = fix(w.get("opponent_team") or ""), w.get("position_group")
        if pg in ("RB", "WR", "TE", "QB"):
            dvp[(d, pg)] += (f(w.get("rushing_tds")) or 0) + (f(w.get("receiving_tds")) or 0); dvp_g[d].add(w["game_id"])
    dvp_pg = {k: v / max(1, len(dvp_g[k[0]])) for k, v in dvp.items()}
    lg_dvp = {pg: mean([v for (d, p2), v in dvp_pg.items() if p2 == pg]) for pg in ("RB", "WR", "TE", "QB")}
    team_tot = defaultdict(lambda: defaultdict(float))
    for w in before:
        k = (fix(w["team"]), i(w["week"]))
        team_tot[k]["tgt"] += f(w.get("targets")) or 0; team_tot[k]["car"] += f(w.get("carries")) or 0
    snap = defaultdict(list); snap_prev = defaultdict(list)
    for s_ in snaps: snap[norm(s_["player"])].append((i(s_["week"]), f(s_.get("offense_pct")) or 0))
    for s_ in snaps_prev: snap_prev[norm(s_["player"])].append(f(s_.get("offense_pct")) or 0)

    games = {}
    for g in sched:
        H, A = fix(g["home_team"]), fix(g["away_team"])
        sl, tl = f(g.get("spread_line")), f(g.get("total_line"))   # spread_line: home expected margin
        if sl is None or tl is None: continue
        games[(H, A)] = {"g": g, "kick": kickoff_utc(g), "imp": {H: tl / 2 + sl / 2, A: tl / 2 - sl / 2}, "opp": {H: A, A: H}}

    def model(nm, w4):
        team, pg = fix(w4["team"]), w4.get("position_group")
        gm = next((v for k, v in games.items() if team in k), None)
        if not gm or pg not in FLOOR: return None
        opp, pts = gm["opp"][team], gm["imp"][team]
        mine = [w for w in before if norm(w["player_display_name"]) == nm]
        mine_prev = [w for w in prev if norm(w["player_display_name"]) == nm]
        pid = w4["player_id"]
        sn = [v for _, v in sorted(snap.get(nm, []))]
        gact = max(len(mine), sum(1 for v in sn if v > 0))
        c_td = sum((f(w.get("rushing_tds")) or 0) + (f(w.get("receiving_tds")) or 0) for w in mine)
        pv_g, pv_td = len(mine_prev), sum((f(w.get("rushing_tds")) or 0) + (f(w.get("receiving_tds")) or 0) for w in mine_prev)
        moved = bool(mine_prev and fix(mine_prev[-1]["team"]) != team)
        prev_w = 0.3 if (co.get(team) or {}).get("yrs") == 1 or moved else 0.6
        sc, sp = mean([v for v in sn if v > 0]), mean([v for v in snap_prev.get(nm, []) if v > 0])
        role = max(0.25, min(1.25, sc / sp)) if sc and sp else 1.0
        dk = (opp, pg); mult = max(0.85, min(1.15, 1 + 0.5 * (dvp_pg.get(dk, lg_dvp[pg] or 0) / lg_dvp[pg] - 1))) if lg_dvp.get(pg) else 1.0
        g_all = gact + prev_w * pv_g
        hist = ((c_td + prev_w * pv_td * role) / g_all) * mult if g_all else None
        f_rz = max(0.85, min(1.15, 1 + 0.6 * (rz_rate[opp] / lg_rz - 1))) if opp in rz_rate and lg_rz else 1.0
        pshare = max(0.35, min(0.8, 0.5 * split(team, "off") + 0.5 * split(opp, "def")))
        lk, gpl = look.get(pid, {}), max(len(pgames.get(pid, ())), gact)
        looks = ((RZ_T * lk.get("rz_t", 0) + EZ_T * lk.get("ez_t", 0) + RZ_C * lk.get("rz_c", 0) + GL_C * lk.get("gl_c", 0)) / gpl * f_rz) if gpl else None
        tts = [(f(w.get("targets")) or 0) / team_tot[(fix(w["team"]), i(w["week"]))]["tgt"] for w in mine if team_tot[(fix(w["team"]), i(w["week"]))]["tgt"]]
        rss = [(f(w.get("carries")) or 0) / team_tot[(fix(w["team"]), i(w["week"]))]["car"] for w in mine if team_tot[(fix(w["team"]), i(w["week"]))]["car"]]
        ts, rs = mean(tts) or 0, mean(rss) or 0
        tgl = sum(team_gl[(team, gid)] for gid in pgames.get(pid, ()))
        gls = lk.get("gl_c", 0) / tgl if tgl >= 2 else None
        usage = None
        if pg == "QB":
            car = mean([f(w.get("carries")) or 0 for w in mine])
            if car: usage = 0.035 * car * mult
        elif ts or rs:
            ts_e = 0.65 * ts + 0.35 * min(0.6, lk.get("ez_t", 0) / gpl / 1.6) if gpl else ts
            rs_e = 0.6 * rs + 0.4 * (gls if gls is not None else min(1, lk.get("gl_c", 0) / gpl / 1.3) if gpl else 0) if pg == "RB" else rs
            usage = TD_PER_PT * pts * f_rz * (pshare * ts_e + (1 - pshare) * rs_e) * (1 + (mult - 1) / 2)
        thin = g_all < 3 and usage is not None
        parts = ([] if thin or hist is None else [(0.35, hist)]) + ([(0.25, looks)] if looks is not None else []) + ([(0.4, usage)] if usage is not None else [])
        if not parts: return None
        lam = min(sum(w * v for w, v in parts) / sum(w for w, _ in parts), 1.2)
        recent = sn[-3:]
        glb = pg == "RB" and (gls or 0) >= 0.35 and lk.get("gl_c", 0) >= 2
        if recent and pg != "QB":
            lam = min(lam, TD_PER_PT * pts * max(mean(recent), gls if glb and gls else 0, 0.04) * 0.8)
            if pg == "RB" and not glb and mean(recent) < 0.45: lam *= 0.85
        lam = max(lam, FLOOR[pg] * (0.5 if recent and max(recent) < 0.15 else 1))
        return {"team": team, "opp": opp, "pos": pg, "p": min(0.75, 1 - math.exp(-lam)), "kick": gm["kick"], "game": gm["g"]}

    # Kalshi: each game's "1+ touchdowns" markets and their last price before kickoff
    out = []
    for (H, A), gm in games.items():
        d = gm["g"]["gameday"]; y, mth, dd = d[2:4], MON[int(d[5:7]) - 1], d[8:10]
        ev = f"{TD_SERIES}-{y}{mth}{dd}{K_CODE.get(A, A)}{K_CODE.get(H, H)}"
        try: ms = k_markets(ev)
        except Exception as e: print("kalshi", ev, e, file=sys.stderr); continue
        end = int(gm["kick"].timestamp())
        for m in ms:
            if (TD_SERIES == "KXNFLTD" and not m["ticker"].endswith("-1")) or ":" not in m["title"] or "D/ST" in m["title"]: continue
            nm = norm(k_name(m["title"])); w4 = this.get(nm)
            if not w4: continue                                   # didn't play: the market would be void
            ours = model(nm, w4)
            if not ours: continue
            try: cs = k_candles(TD_SERIES, m["ticker"], end - 36 * 3600, end)
            except Exception: cs = []
            if not cs: continue
            bid, ask = cs[-1]
            if ask >= 0.99 or bid <= 0: continue
            scored = ((f(w4.get("rushing_tds")) or 0) + (f(w4.get("receiving_tds")) or 0)) > 0
            out.append({"n": w4["player_display_name"], **{k: ours[k] for k in ("team", "opp", "pos", "p")}, "game": f"{A} @ {H}",
                        "bid": bid, "ask": ask, "mid": (bid + ask) / 2, "scored": scored})
            time.sleep(0.12)
    return out


def ready(week):
    """A week can be backtested once every game is final and nflverse has posted that week's player stats."""
    sched = [g for g in rows("schedules/games.csv.gz", 6 * 3600) if g["season"] == str(SEASON) and g["game_type"] == "REG" and i(g["week"]) == week]
    if not sched or any(g.get("result") in ("", "NA", None) for g in sched): return False
    st = rows(f"stats_player/stats_player_week_{SEASON}.csv", 6 * 3600)
    return sum(1 for w in st if i(w.get("week")) == week) > 200


def summarize(rows_, wt):
    """One week's scorecard: accuracy for our model, Kalshi and the blend the app uses, and the $10 value-bet results."""
    for r in rows_: r["blend"] = wt * r["mid"] + (1 - wt) * r["p"]
    out = {"n": len(rows_), "scored": sum(r["scored"] for r in rows_), "wt": wt}
    for key in ("p", "mid", "blend"):
        out[key] = {"sse": round(sum((r[key] - r["scored"]) ** 2 for r in rows_), 4)}
    for key in ("p", "blend"):
        n, w, pl = bets(rows_, key); out[key].update({"bets": n, "won": w, "pl": round(pl, 2)})
    return out


def score(rows_, key):
    ps = [(r[key], r["scored"]) for r in rows_]
    brier = mean([(p - y) ** 2 for p, y in ps])
    ll = mean([-(math.log(max(1e-4, p)) if y else math.log(max(1e-4, 1 - p))) for p, y in ps])
    return brier, ll


def fee(p): return 0.07 * p * (1 - p)


def bets(rows_, key, edge=0.03, stake=10):
    """Flat $10 on whichever side our number says Kalshi has mispriced by 3+ points after the fee."""
    pl, n, w = 0.0, 0, 0
    for r in rows_:
        p, yes_cost, no_cost = r[key], r["ask"] + fee(r["ask"]), (1 - r["bid"]) + fee(1 - r["bid"])
        if p - yes_cost >= edge: n += 1; win = r["scored"]; w += win; pl += stake * ((1 - yes_cost) / yes_cost if win else -1)
        elif (1 - p) - no_cost >= edge: n += 1; win = not r["scored"]; w += win; pl += stake * ((1 - no_cost) / no_cost if win else -1)
    return n, w, pl


if __name__ == "__main__":
    week = int(sys.argv[1]) if len(sys.argv) > 1 else 4
    res = build(week)
    for r in res: r["blend"] = 0.5 * r["p"] + 0.5 * r["mid"]
    json.dump(res, open(os.path.join(analytics.CACHE, f"td_backtest_w{week}.json"), "w"), default=str)
    print(f"Week {week}: {len(res)} players with a Kalshi price and our number, {sum(r['scored'] for r in res)} scored")
    for key, lbl in (("p", "Our model"), ("mid", "Kalshi"), ("blend", "50/50 blend")):
        b, ll = score(res, key); n, w, pl = bets(res, key) if key != "mid" else (0, 0, 0)
        print(f"  {lbl:12} Brier {b:.4f}  log loss {ll:.4f}" + (f"  | bets {n}, won {w}, P/L ${pl:+.2f} on $10 each" if key != "mid" else ""))
    print("Calibration (our model / Kalshi / blend):")
    for lo, hi in ((0, .15), (.15, .3), (.3, .45), (.45, .6), (.6, 1)):
        for key in ("p", "mid", "blend"):
            g = [r for r in res if lo <= r[key] < hi]
            if g: print(f"  {key:5} {int(lo*100):>2}-{int(hi*100):<3}% n={len(g):3}  avg said {mean([r[key] for r in g])*100:4.1f}%  scored {sum(r['scored'] for r in g)/len(g)*100:4.1f}%")
    big = sorted(res, key=lambda r: -abs(r["p"] - r["mid"]))[:12]
    print("Biggest disagreements:")
    for r in big: print(f"  {r['n']:24} {r['pos']} {r['game']:10} ours {r['p']*100:4.1f}%  Kalshi {r['mid']*100:4.1f}%  -> {'SCORED' if r['scored'] else 'no'}")
