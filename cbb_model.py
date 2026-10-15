"""College basketball: tempo-free team ratings (KenPom-style) -> our spread and total for any game.

  possessions   FGA - OREB + TO + 0.475 x FTA (each team's count, averaged)
  ratings       offense / defense in points per 100 possessions and tempo, adjusted for opponents by iterating
                (a team's raw efficiency x league average / its opponents' rating), home court worth HCA points
  early season  last season's ratings (regressed 30% to average) count as K_PRIOR games until real games pile up
  prediction    possessions = tempo_h x tempo_a / league tempo; points = off x opp def / league x possessions / 100
backtest(): 2025-26 games vs Kalshi's spread and total lines (the rung priced nearest 50/50 before tip), ATS and O/U.
Box scores: sportsdataverse (ESPN), free.
"""
import csv, io, json, math, os, sys, time
from collections import defaultdict
from datetime import datetime, timedelta
from urllib.request import urlopen, Request

ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(os.environ.get("EDGE_DATA") or os.path.join(ROOT, "data", "cbb"), "box")
BOX = "https://github.com/sportsdataverse/sportsdataverse-data/releases/download/espn_mens_college_basketball_team_boxscores/team_box_{}.csv"
HCA = 3.0        # home court, points
K_PRIOR = 5.0    # games of last season's rating mixed in
REGRESS = 0.3    # last season's ratings pulled this far back to average
ITERS = 12


def f(x):
    try: return float(x)
    except (TypeError, ValueError): return None


def games(season, max_age=6 * 3600):
    """[{id, date, home, away, hs, as, poss, neutral?}] for a season (season = the year it ends), cached on disk."""
    os.makedirs(CACHE, exist_ok=True)
    p = os.path.join(CACHE, f"games_{season}.json")
    if os.path.exists(p) and time.time() - os.path.getmtime(p) < (10 ** 9 if season < 2027 else max_age): return json.load(open(p))
    with urlopen(Request(BOX.format(season), headers={"User-Agent": "edge-board"}), timeout=180) as r: txt = r.read().decode()
    by = defaultdict(dict)
    for r in csv.DictReader(io.StringIO(txt)):
        if r.get("season_type") not in ("2", "3") or f(r.get("team_score")) is None: continue
        poss = (f(r["field_goals_attempted"]) or 0) - (f(r["offensive_rebounds"]) or 0) + (f(r.get("turnovers")) or f(r.get("total_turnovers")) or 0) + 0.475 * (f(r["free_throws_attempted"]) or 0)
        by[r["game_id"]][r["team_home_away"]] = {"t": r["team_id"], "ab": r["team_abbreviation"], "pts": f(r["team_score"]), "poss": poss, "date": r["game_date"], "tt": r.get("season_type")}
    out = []
    for gid, g in by.items():
        if "home" not in g or "away" not in g: continue
        h, a = g["home"], g["away"]
        out.append({"id": gid, "date": h["date"], "home": h["t"], "away": a["t"], "hab": h["ab"], "aab": a["ab"], "hs": h["pts"], "as": a["pts"],
                    "poss": (h["poss"] + a["poss"]) / 2, "post": h["tt"] == "3"})
    out.sort(key=lambda g: g["date"])
    json.dump(out, open(p, "w"))
    return out


def ratings(gs, prior=None):
    """{team: (off, def, tempo)} from a list of games, opponent-adjusted; prior = last season's ratings."""
    if not gs: return {}, (100.0, 100.0, 68.0)
    lg_eff = sum(g["hs"] + g["as"] for g in gs) / max(1, sum(2 * g["poss"] for g in gs)) * 100
    lg_t = sum(g["poss"] for g in gs) / len(gs)
    teams = {t for g in gs for t in (g["home"], g["away"])}
    O = {t: lg_eff for t in teams}; D = {t: lg_eff for t in teams}; T = {t: lg_t for t in teams}
    per = defaultdict(list)
    for g in gs:
        hc = 0 if g.get("post") else HCA / 2 / max(g["poss"], 1) * 100   # tournament games: no home court
        per[g["home"]].append((g["away"], g["hs"] / g["poss"] * 100 - hc, g["as"] / g["poss"] * 100 + hc, g["poss"]))
        per[g["away"]].append((g["home"], g["as"] / g["poss"] * 100 + hc, g["hs"] / g["poss"] * 100 - hc, g["poss"]))
    for _ in range(ITERS):
        nO, nD, nT = {}, {}, {}
        for t, xs in per.items():
            n = len(xs)
            o = sum(off * lg_eff / D[opp] for opp, off, _, _ in xs) / n
            d = sum(de * lg_eff / O[opp] for opp, _, de, _ in xs) / n
            tp = sum(p * lg_t / T[opp] for opp, _, _, p in xs) / n
            if prior and t in prior:
                po, pd, pt = prior[t]
                o = (o * n + po * K_PRIOR) / (n + K_PRIOR); d = (d * n + pd * K_PRIOR) / (n + K_PRIOR); tp = (tp * n + pt * K_PRIOR) / (n + K_PRIOR)
            nO[t], nD[t], nT[t] = o, d, tp
        O, D, T = nO, nD, nT
    return {t: (O[t], D[t], T[t]) for t in teams}, (lg_eff, lg_eff, lg_t)


def regress(R, lg):
    return {t: tuple(v + (l - v) * REGRESS for v, l in zip(r, lg)) for t, r in R.items()}


def predict(R, lg, home, away, neutral=False):
    """(home margin, total) from ratings."""
    lo, _, lt = lg
    oh, dh, th = R.get(home, lg); oa, da, ta = R.get(away, lg)
    poss = th * ta / lt
    ph = oh * da / lo * poss / 100; pa = oa * dh / lo * poss / 100
    hc = 0 if neutral else HCA
    return ph - pa + hc, ph + pa


def live(season):
    """Ratings for today: this season's games with last season's regressed ratings as the prior."""
    prev = games(season - 1); R0, lg0 = ratings(prev); prior = regress(R0, lg0)
    try: cur = games(season) if season else []
    except Exception: cur = []   # season hasn't started: last season's (regressed) ratings stand in
    if not cur: return prior, lg0, 0
    R, lg = ratings(cur, prior)
    for t, r in prior.items(): R.setdefault(t, r)
    return R, lg, len(cur)


def backtest(season=2026, sample=320):
    sys.path.insert(0, ROOT)
    from td_backtest import kget, k_markets, k_candles
    gs = games(season); prev = games(season - 1); R0, lg0 = ratings(prev); prior = regress(R0, lg0)
    by_day = defaultdict(list)
    for g in gs: by_day[g["date"]].append(g)
    evs, cur = [], None
    while True:
        d = kget("/events?series_ticker=KXNCAAMBTOTAL&limit=200&status=settled" + (f"&cursor={cur}" if cur else "")); evs += d.get("events", []); cur = d.get("cursor")
        if not cur or not d.get("events"): break
    evs = [e for e in evs if e["event_ticker"].split("-")[1][:2] in ("25", "26")]
    evs = evs[::max(1, len(evs) // sample)][:sample]
    cache = {}

    def find(ev):
        code = ev.split("-")[1]
        try: day = datetime.strptime(code[:7], "%y%b%d")
        except ValueError: return None
        teams = code[7:]
        for dd in (0, -1, 1):
            ds = (day + timedelta(days=dd)).strftime("%Y-%m-%d")
            for g in by_day.get(ds, []):
                if teams in (g["aab"] + g["hab"], g["hab"] + g["aab"]): return g
        return None

    def line_of(series, ev, end, kind):
        try: ms = k_markets(f"{series}-{ev.split('-')[1]}")
        except Exception: return None, []
        best = None
        for m in sorted(ms, key=lambda m: m.get("floor_strike") or 0):
            if m.get("floor_strike") is None: continue
            try: cs = k_candles(series, m["ticker"], end - 24 * 3600, end)
            except Exception: cs = []
            if not cs: continue
            b, a = cs[-1]
            if not (0 < b and a < 0.99): continue
            mid = (b + a) / 2
            if best is None or abs(mid - 0.5) < abs(best[1] - 0.5): best = (m, mid)
        return best, ms

    out = []
    from concurrent.futures import ThreadPoolExecutor

    def one(ev):
        g = find(ev["event_ticker"])
        if not g or g["date"] < f"{season - 1}-11-25": return None
        before = [x for x in gs if x["date"] < g["date"]]
        key = g["date"]
        if key not in cache: R, lg = ratings(before, prior); [R.setdefault(t, r) for t, r in prior.items()]; cache[key] = (R, lg)
        R, lg = cache[key]
        m, t = predict(R, lg, g["home"], g["away"], g["post"])
        end = int(datetime.strptime(g["date"], "%Y-%m-%d").timestamp()) + 16 * 3600   # ~noon ET on game day: before most tips
        tl, _ = line_of("KXNCAAMBTOTAL", ev["event_ticker"], end, "total")
        sl, sms = line_of("KXNCAAMBSPREAD", ev["event_ticker"].replace("KXNCAAMBTOTAL", "KXNCAAMBSPREAD"), end, "spread")
        row = {"id": g["id"], "date": g["date"], "h": g["hab"], "a": g["aab"], "m": round(m, 1), "t": round(t, 1), "am": g["hs"] - g["as"], "at": g["hs"] + g["as"]}
        if tl: row["kt"] = tl[0]["floor_strike"]
        if sl:   # "X wins by over N": convert to the home team's margin line
            title = sl[0]["title"]; n = sl[0]["floor_strike"]
            home_side = g["hab"] in sl[0]["ticker"].split("-")[-1] or title.lower().startswith(g["hab"].lower())
            row["km"] = n if home_side else -n
        return row

    with ThreadPoolExecutor(4) as pool:
        for r in pool.map(one, evs):
            if r: out.append(r)
    return out


if __name__ == "__main__":
    rs = backtest(sample=int(sys.argv[1]) if len(sys.argv) > 1 else 320)
    os.makedirs(CACHE, exist_ok=True)
    json.dump(rs, open(os.path.join(CACHE, "cbb_backtest.json"), "w"))
    print(len(rs), "games matched")
    T = [r for r in rs if "kt" in r and r["at"] != r["kt"]]; M = [r for r in rs if "km" in r and r["am"] != r["km"]]
    mae = lambda xs: sum(xs) / len(xs) if xs else float("nan")
    print(f"totals n={len(T)}  MAE ours {mae([abs(r['t'] - r['at']) for r in T]):.2f}  Kalshi {mae([abs(r['kt'] - r['at']) for r in T]):.2f}")
    print(f"margins n={len(M)}  MAE ours {mae([abs(r['m'] - r['am']) for r in M]):.2f}  Kalshi {mae([abs(r['km'] - r['am']) for r in M]):.2f}")
    for th in (0, 2, 4):
        o = [r for r in T if abs(r["t"] - r["kt"]) >= max(th, 0.5)]; w = sum((r["t"] > r["kt"]) == (r["at"] > r["kt"]) for r in o)
        s = [r for r in M if abs(r["m"] - r["km"]) >= max(th, 0.5)]; ws = sum((r["m"] > r["km"]) == (r["am"] > r["km"]) for r in s)
        print(f"  edge {th}+: totals {w}-{len(o) - w}   sides {ws}-{len(s) - ws}")
