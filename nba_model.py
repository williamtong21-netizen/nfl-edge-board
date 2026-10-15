"""NBA player props: minutes x per-minute production, adjusted for pace and opponent, turned into the chance of clearing
each line (points, rebounds, assists, threes). Box scores: sportsdataverse (ESPN), free.

  minutes   recent minutes (each game back counts DECAY x the one after), starters vs bench, shrunk to his season average
  rates     points / rebounds / assists / threes per minute, recent-weighted, shrunk toward his longer history
  pace      the game's total vs the league's average total (more possessions, more of everything)
  opponent  how much the opponent allows of that stat per game vs the league (a soft, shrunk factor)
  chance    points ~ normal (sd grows with the mean); rebounds / assists / threes ~ overdispersed counts
backtest(): last season's Kalshi points / rebounds / assists / threes ladders, prices before tip-off, vs our chance.
"""
import csv, io, json, math, os, re, sys, time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from urllib.request import urlopen, Request

ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(os.environ.get("EDGE_DATA") or os.path.join(ROOT, "data", "nba"), "box")
BOX = "https://github.com/sportsdataverse/sportsdataverse-data/releases/download/espn_nba_player_boxscores/player_box_{}.csv"
STAT = {"Points": "points", "Rebounds": "rebounds", "Assists": "assists", "Threes": "three_point_field_goals_made"}
DECAY = 0.92          # weight per game back
N_RECENT = 15         # games that drive the recent rate
K_RATE = 6.0          # games of his season-long rate mixed into the recent rate
LEAGUE_TOTAL = 228.0  # average NBA game total, for pace


def norm(s): return re.sub(r"[^a-z]", "", re.sub(r"\b(jr|sr|ii|iii|iv)\b", "", (s or "").lower()))


def f(x):
    try: return float(x)
    except (TypeError, ValueError): return None


def box(season, max_age=6 * 3600):
    """Player box scores for an NBA season (season = the year it ends, e.g. 2026 = 2025-26), cached on disk."""
    os.makedirs(CACHE, exist_ok=True)
    p = os.path.join(CACHE, f"player_box_{season}.json")
    if os.path.exists(p) and time.time() - os.path.getmtime(p) < (10 ** 9 if season < 2027 else max_age):
        return json.load(open(p))
    with urlopen(Request(BOX.format(season), headers={"User-Agent": "edge-board"}), timeout=180) as r: txt = r.read().decode()
    keep = ("game_id", "season_type", "game_date", "game_date_time", "athlete_id", "athlete_display_name", "team_abbreviation", "opponent_team_abbreviation",
            "minutes", "points", "rebounds", "assists", "three_point_field_goals_made", "starter", "did_not_play", "home_away", "athlete_position_abbreviation")
    rows = [{k: r.get(k) for k in keep} for r in csv.DictReader(io.StringIO(txt)) if r.get("season_type") in ("2", "3") and r.get("did_not_play") != "true" and f(r.get("minutes"))]
    json.dump(rows, open(p, "w"))
    return rows


class Book:
    """Everything known before a date: each player's games, and each team's stats allowed."""
    def __init__(self, rows):
        self.by_player = defaultdict(list); self.allowed = defaultdict(lambda: defaultdict(list)); self.team_games = defaultdict(set)
        for r in sorted(rows, key=lambda r: r["game_date_time"] or r["game_date"]):
            self.by_player[r["athlete_id"]].append(r)
        tg = defaultdict(lambda: defaultdict(float))
        for r in rows:
            k = (r["game_id"], r["opponent_team_abbreviation"])
            for s in STAT.values(): tg[k][s] += f(r.get(s)) or 0
            tg[k]["date"] = r["game_date"]
        for (gid, opp), v in tg.items():
            for s in STAT.values(): self.allowed[opp][s].append((v["date"], v[s]))

    def player(self, pid, before):
        return [r for r in self.by_player.get(pid, []) if r["game_date"] < before]

    def opp_factor(self, opp, stat, before, lg):
        xs = [v for d, v in self.allowed.get(opp, {}).get(stat, []) if d < before]
        if not xs or not lg: return 1.0
        n = len(xs); m = sum(xs) / n
        return max(0.9, min(1.1, 1 + (m / lg - 1) * n / (n + 15)))


def project(book, pid, stat, before, total=None, opp=None, lg=None):
    """Expected stat for his next game: minutes x per-minute rate x pace x opponent."""
    gs = book.player(pid, before)[-40:]
    if len(gs) < 3: return None
    recent = gs[-N_RECENT:][::-1]
    w = [DECAY ** k for k in range(len(recent))]
    mins = sum(f(r["minutes"]) * x for r, x in zip(recent, w)) / sum(w)
    rate_r = sum((f(r.get(stat)) or 0) * x for r, x in zip(recent, w)) / max(1e-6, sum(f(r["minutes"]) * x for r, x in zip(recent, w)))
    tot_min = sum(f(r["minutes"]) for r in gs); rate_s = sum(f(r.get(stat)) or 0 for r in gs) / max(1e-6, tot_min)
    rate = (rate_r * len(recent) + rate_s * K_RATE) / (len(recent) + K_RATE)
    pace = max(0.92, min(1.08, (total / LEAGUE_TOTAL) ** 0.8)) if total else 1.0
    of = book.opp_factor(opp, stat, before, lg) if opp and lg else 1.0
    return max(0.0, mins * rate * pace * of), mins


def p_over(stat, mean, line):
    """Chance of finishing above `line` (a half-point line, e.g. 24.5 for "25+")."""
    if mean <= 0: return 0.0
    if stat == "points":
        sd = 2.0 + 0.3 * mean
        return 1 - 0.5 * (1 + math.erf((line - mean) / (sd * math.sqrt(2))))
    # counts: negative binomial with variance = mean * (1 + mean / r)
    r = {"rebounds": 8.0, "assists": 6.0, "three_point_field_goals_made": 5.0}.get(stat, 6.0)
    p = r / (r + mean); k = int(math.floor(line)); cdf, pk = 0.0, p ** r
    for j in range(k + 1):
        cdf += pk; pk *= (j + r) / (j + 1) * (1 - p)
    return max(0.0, 1 - cdf)


K_CODE = {"GS": "GSW", "NO": "NOP", "NY": "NYK", "SA": "SAS", "UTAH": "UTA", "WSH": "WAS", "PHX": "PHX"}


def backtest(season=2026, series=("KXNBAPTS", "KXNBAREB", "KXNBAAST", "KXNBA3PT"), limit_days=None):
    sys.path.insert(0, ROOT)
    from td_backtest import kget, k_markets, k_candles
    rows = box(season); book = Book(rows)
    by_name_day = {(norm(r["athlete_display_name"]), r["game_date"]): r for r in rows}
    tip = {}
    for r in rows: tip.setdefault(r["game_id"], r["game_date_time"])
    league = {}
    tot_by_game = defaultdict(float)
    for r in rows: tot_by_game[r["game_id"]] += f(r.get("points")) or 0
    lg_tot = sum(tot_by_game.values()) / max(1, len(tot_by_game))
    out = []
    for ser in series:
        stat = STAT[{"KXNBAPTS": "Points", "KXNBAREB": "Rebounds", "KXNBAAST": "Assists", "KXNBA3PT": "Threes"}[ser]]
        evs, cur = [], None
        while True:
            d = kget(f"/events?series_ticker={ser}&limit=200&status=settled" + (f"&cursor={cur}" if cur else "")); evs += d.get("events", []); cur = d.get("cursor")
            if not cur or not d.get("events"): break
        evs = [e for e in evs if e["event_ticker"].split("-")[1][:2] in ("25", "26") and not e["event_ticker"].split("-")[1].startswith(("26JUL", "26AUG", "26SEP", "26OCT"))]
        if limit_days: evs = evs[::max(1, len(evs) // limit_days)][:limit_days]   # spread across the season
        n0 = len(out)
        if stat not in league:
            vals = defaultdict(float)
            for x in rows: vals[x["game_id"]] += f(x.get(stat)) or 0
            league[stat] = sum(vals.values()) / max(1, len(vals)) / 2   # per team per game

        def one(e):
            res = []
            try: ms = k_markets(e["event_ticker"])
            except Exception as ex: print("kalshi", e["event_ticker"], ex, file=sys.stderr); return res
            # one ladder per player; price the rung nearest our number
            ladders = defaultdict(list)
            for m in ms:
                if m.get("floor_strike") is not None and m.get("result") in ("yes", "no") and ":" in m["title"]: ladders[m["title"].split(":")[0]].append(m)
            for name, rungs in ladders.items():
                day = None
                for dd in range(0, 2):   # event date vs box date (late games roll past midnight UTC)
                    tk = e["event_ticker"].split("-")[1][:7]
                    try: dt = datetime.strptime(tk, "%y%b%d") + timedelta(days=dd)
                    except ValueError: break
                    if (norm(name), dt.strftime("%Y-%m-%d")) in by_name_day: day = dt.strftime("%Y-%m-%d"); break
                if not day: continue
                r = by_name_day[(norm(name), day)]
                t = datetime.fromisoformat(tip[r["game_id"]].replace("Z", "+00:00")) if "T" in (tip[r["game_id"]] or "") else None
                if not t: continue
                end = int(t.timestamp())
                pj = project(book, r["athlete_id"], stat, day, None, r["opponent_team_abbreviation"], league[stat])
                if not pj: continue
                mean, mins = pj
                near = sorted(rungs, key=lambda m: abs(m["floor_strike"] - mean))[:1]
                for m in near:
                    try: cs = k_candles(ser, m["ticker"], end - 24 * 3600, end)
                    except Exception: cs = []
                    if not cs: continue
                    bid, ask = cs[-1]
                    if ask >= 0.99 or bid <= 0.01: continue
                    p = max(0.03, min(0.97, p_over(stat, mean, m["floor_strike"])))
                    res.append({"n": name, "stat": stat, "day": day, "line": m["floor_strike"], "mean": round(mean, 2), "min": round(mins, 1), "p": round(p, 3),
                                "bid": bid, "ask": ask, "mid": (bid + ask) / 2, "scored": m["result"] == "yes", "actual": f(r.get(stat))})
            return res
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(4) as pool:
            for res in pool.map(one, evs): out += res
        print(f"{ser}: {len(out) - n0} rungs", file=sys.stderr, flush=True)
    return out


if __name__ == "__main__":
    sys.path.insert(0, ROOT)
    from td_backtest import bets
    lim = int(sys.argv[1]) if len(sys.argv) > 1 else None
    rs = backtest(limit_days=lim)
    os.makedirs(CACHE, exist_ok=True)
    json.dump(rs, open(os.path.join(CACHE, "nba_backtest.json"), "w"))
    mean = lambda xs: sum(xs) / len(xs) if xs else float("nan")
    print(len(rs), "rungs")
    for st in sorted({r["stat"] for r in rs}) + ["ALL"]:
        g = [r for r in rs if st == "ALL" or r["stat"] == st]
        lean = [r for r in g if abs(r["p"] - 0.5) >= 0.05]; h = sum((r["p"] > 0.5) == r["scored"] for r in lean)
        out = f"{st:30} n={len(g):5}  Brier ours {mean([(r['p'] - r['scored']) ** 2 for r in g]):.4f}  Kalshi {mean([(r['mid'] - r['scored']) ** 2 for r in g]):.4f}  leans {h}-{len(lean) - h}"
        for wt in (0.6, 0.8):
            for r in g: r["b"] = wt * r["mid"] + (1 - wt) * r["p"]
            out += f"  | blend {wt}: {mean([(r['b'] - r['scored']) ** 2 for r in g]):.4f}"
        print(out)
