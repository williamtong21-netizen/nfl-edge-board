"""College football team model: schedule-adjusted ratings from scores and play-by-play efficiency (CollegeFootballData).

Ratings, solved together from every game so far (so beating good teams counts more):
  margin  r_home - r_away + home field = capped score margin
  points  pts = league avg + off_team - def_opponent (+ half home field)
  eff     e_home - e_away = net EPA per play (offense minus defense), turned into points
Each season starts from last season's ratings, kept more for teams returning more production, plus a recruiting-talent
term; this season's games take over as they come. (Preseason fix held out on 2025-26: off by 12.1-12.3 pts vs 12.8-14.0 before.)
Predictions: spread and total for any matchup. backtest() replays seasons week by week against the closing lines.
Free key: config.json "cfbd_api_key" or env CFBD_API_KEY.  Run:  python cfb_model.py   (backtest 2023-2025)
"""
import json, os, sys, time
from collections import defaultdict
from urllib.request import urlopen, Request

ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(os.environ.get("EDGE_DATA") or os.path.join(ROOT, "data", "cfb"), "cfbd")
API = "https://api.collegefootballdata.com"

HFA = 2.6          # home field, points
CAP = 28           # blowout cap on the margin rating
LAM = 3.0          # games-worth of prior pulled into each team's rating
REGRESS = 0.75     # share of last season's rating carried into the next (tuned 2022-24 with the two below)
FCS_PRIOR = -18.0  # FCS teams (rarely seen) start this far below an average FBS team
EFF_PTS = 70.0     # points per 1.0 EPA/play difference (about plays per game)
W_EFF = 0.45       # blend weight on the efficiency rating vs the margin rating
SEASONS = [2021, 2022, 2023, 2024, 2025, 2026]


def key():
    try:
        with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as f: k = json.load(f).get("cfbd_api_key")
    except Exception: k = None
    return k or os.environ.get("CFBD_API_KEY")


def fetch(name, path, year, max_age):
    """CFBD request with a disk cache: past seasons never change, the current one refreshes every max_age seconds."""
    os.makedirs(CACHE, exist_ok=True)
    p = os.path.join(CACHE, f"{name}_{year}.json")
    if os.path.exists(p) and (year < SEASONS[-1] or time.time() - os.path.getmtime(p) < max_age):
        with open(p, encoding="utf-8") as f: return json.load(f)
    req = Request(API + path, headers={"Authorization": f"Bearer {key()}", "Accept": "application/json"})
    with urlopen(req, timeout=90) as r: d = json.loads(r.read().decode())
    with open(p, "w", encoding="utf-8") as f: json.dump(d, f)
    return d


def load(year, max_age=12 * 3600, with_lines=True):
    games = fetch("games", f"/games?year={year}&seasonType=regular", year, max_age)
    lines = fetch("lines", f"/lines?year={year}&seasonType=regular", year, max_age) if with_lines else []
    adv = fetch("adv", f"/stats/game/advanced?year={year}&seasonType=regular", year, max_age)
    close = {}
    for g in lines:   # consensus closing line: the median across books
        sp = sorted(l["spread"] for l in g.get("lines", []) if l.get("spread") is not None)
        tt = sorted(l["overUnder"] for l in g.get("lines", []) if l.get("overUnder") is not None)
        if sp: close[g["id"]] = {"spread": sp[len(sp) // 2], "total": tt[len(tt) // 2] if tt else None}
    eff = defaultdict(dict)
    for a in adv:
        o, d = a.get("offense") or {}, a.get("defense") or {}
        if o.get("ppa") is not None and d.get("ppa") is not None: eff[a["gameId"]][a["team"]] = o["ppa"] - d["ppa"]
    out = []
    for g in games:
        h, a = g.get("homeTeam"), g.get("awayTeam")
        out.append({"id": g["id"], "season": year, "week": g.get("week"), "home": h, "away": a, "neutral": bool(g.get("neutralSite")),
                    "hp": g.get("homePoints"), "ap": g.get("awayPoints"), "done": g.get("completed") and g.get("homePoints") is not None,
                    "fbs": {h: g.get("homeClassification") == "fbs", a: g.get("awayClassification") == "fbs"},
                    "eff": eff.get(g["id"], {}), "line": close.get(g["id"])})
    return out


def solve(games, prior, lam=LAM, iters=60):
    """Ridge-style ratings by iteration: each team's rating = (prior x lam + sum over its games of what that game says) / (lam + games)."""
    r = dict(prior)
    adj = defaultdict(list)
    for g in games:
        adj[g["h"]].append((g["a"], g["v"], +1)); adj[g["a"]].append((g["h"], g["v"], -1))
    for _ in range(iters):
        for t, gs in adj.items():
            p = prior.get(t, 0.0)
            s = sum((v * sgn) + r.get(o, prior.get(o, 0.0)) for o, v, sgn in gs)
            r[t] = (lam * p + s) / (lam + len(gs))
    return r


def ratings(games, prior):
    """Margin, efficiency and points (offense / defense) ratings from finished games."""
    done = [g for g in games if g["done"]]
    mg = [{"h": g["home"], "a": g["away"], "v": max(-CAP, min(CAP, g["hp"] - g["ap"])) - (0 if g["neutral"] else HFA)} for g in done]
    eg = [{"h": g["home"], "a": g["away"], "v": (g["eff"][g["home"]] - g["eff"][g["away"]]) * EFF_PTS / 2 - (0 if g["neutral"] else HFA / 2)}
          for g in done if g["home"] in g["eff"] and g["away"] in g["eff"]]
    mov = solve(mg, prior.get("mov", {}), LAM)
    eff = solve(eg, prior.get("eff", {}), LAM)
    # points: offense o and defense d (points allowed vs average), mu = league average points per team
    pts = [(g["home"], g["away"], g["hp"] - (0 if g["neutral"] else HFA / 2)) for g in done] + [(g["away"], g["home"], g["ap"] + (0 if g["neutral"] else HFA / 2)) for g in done]
    mu = sum(p for *_, p in pts) / max(1, len(pts))
    o, d = dict(prior.get("off", {})), dict(prior.get("def", {}))
    by_o, by_d = defaultdict(list), defaultdict(list)
    for t, opp, p in pts: by_o[t].append((opp, p)); by_d[opp].append((t, p))
    for _ in range(40):
        for t, gs in by_o.items(): o[t] = (LAM * prior.get("off", {}).get(t, 0) + sum(p - mu - d.get(x, 0) for x, p in gs)) / (LAM + len(gs))
        for t, gs in by_d.items(): d[t] = (LAM * prior.get("def", {}).get(t, 0) + sum(p - mu - o.get(x, 0) for x, p in gs)) / (LAM + len(gs))
    return {"mov": mov, "eff": eff, "off": o, "def": d, "mu": mu}


K_RET = 0.8        # extra carry-over per 1.0 of returning production above average (offense PPA share)
K_TAL = 6.0        # points per standard deviation of recruiting talent, added to the starting rating
RET_AVG = 0.55


def team_extras(year):
    """Returning production and recruiting talent for a season (CFBD, 2 calls a year, cached)."""
    try:
        ret = {r["team"]: r for r in fetch("returning", f"/player/returning?year={year}", year, 30 * 86400)}
        tal = {r["team"]: float(r["talent"]) for r in fetch("talent", f"/talent?year={year}", year, 30 * 86400)}
    except Exception: return {}, {}
    return ret, tal


def carry(final, fbs, year=None, k_ret=None, k_tal=None, reg=None):
    """Next season's prior: last season's ratings regressed toward average (FCS teams start well below).
    With returning production, teams that kept more of last year's offense carry more of their rating; recruiting talent
    nudges every team toward what its roster should be."""
    k_ret = K_RET if k_ret is None else k_ret; k_tal = K_TAL if k_tal is None else k_tal; reg = REGRESS if reg is None else reg
    ret, tal = team_extras(year) if year and (k_ret or k_tal) else ({}, {})
    tv = [v for t, v in tal.items() if fbs.get(t)]
    tm = sum(tv) / len(tv) if tv else 0; tsd = (sum((v - tm) ** 2 for v in tv) / len(tv)) ** 0.5 if tv else 1
    pr = {}
    for k in ("mov", "eff", "off", "def"):
        pr[k] = {}
        for t, v in final.get(k, {}).items():
            rp = ret.get(t, {}).get("percentPPA")
            keep = reg + (k_ret * (rp - RET_AVG) if rp is not None and k != "def" else 0)
            tz = (tal[t] - tm) / tsd if t in tal and tsd else 0
            add = k_tal * tz * {"mov": 1, "eff": 1, "off": 0.5, "def": -0.5}[k]
            pr[k][t] = max(0, keep) * v + add
        for t, is_fbs in fbs.items():
            if not is_fbs and t not in pr[k]: pr[k][t] = FCS_PRIOR if k in ("mov", "eff") else (FCS_PRIOR / 2 if k == "off" else -FCS_PRIOR / 2)
    return pr


def predict(R, home, away, neutral=False, w_eff=W_EFF):
    hf = 0 if neutral else HFA
    mov = R["mov"].get(home, 0) - R["mov"].get(away, 0)
    eff = R["eff"].get(home, 0) - R["eff"].get(away, 0)
    margin = (1 - w_eff) * mov + w_eff * eff + hf
    mu = R.get("mu", 28)
    total = 2 * mu + R["off"].get(home, 0) + R["off"].get(away, 0) + R["def"].get(home, 0) + R["def"].get(away, 0)
    return margin, total


def season_priors(all_games):
    """Final ratings of each season, chained: season S starts from season S-1's final, regressed."""
    finals, prior = {}, {"mov": {}, "eff": {}, "off": {}, "def": {}}
    for s in SEASONS:
        gs = [g for g in all_games if g["season"] == s]
        fbs = {t: v for g in gs for t, v in g["fbs"].items()}
        prior = carry(finals.get(s - 1, {}), fbs, s) if s - 1 in finals else prior
        finals[s] = ratings(gs, prior)
        finals[s]["prior"] = prior
    return finals


def backtest(all_games, seasons=(2023, 2024, 2025), w_eff=W_EFF):
    finals = season_priors(all_games)
    rows = []
    for s in seasons:
        gs = [g for g in all_games if g["season"] == s]
        prior = finals[s]["prior"]
        for w in sorted({g["week"] for g in gs if g["week"]}):
            R = ratings([g for g in gs if g["week"] < w], prior)
            for g in gs:
                if g["week"] != w or not g["done"] or not g["line"] or g["line"]["spread"] is None: continue
                if not (g["fbs"].get(g["home"]) and g["fbs"].get(g["away"])): continue   # FBS vs FBS only
                m, t = predict(R, g["home"], g["away"], g["neutral"], w_eff)
                rows.append({"s": s, "w": w, "m": m, "t": t, "line": -g["line"]["spread"], "tl": g["line"]["total"],   # line as expected home margin
                             "res": g["hp"] - g["ap"], "tot": g["hp"] + g["ap"]})
    return rows


def report(rows, label=""):
    mean = lambda xs: sum(xs) / len(xs) if xs else float("nan")
    mae_us, mae_mk = mean([abs(r["m"] - r["res"]) for r in rows]), mean([abs(r["line"] - r["res"]) for r in rows])
    out = [f"{label}{len(rows)} games | margin off by: ours {mae_us:.2f}, closing line {mae_mk:.2f}"]
    for th in (2, 3, 5, 7):
        w = l = 0
        for r in rows:
            e, a = r["m"] - r["line"], r["res"] - r["line"]
            if abs(e) < th or a == 0: continue
            w += (e > 0) == (a > 0); l += (e > 0) != (a > 0)
        tw = tl_ = 0
        for r in rows:
            if r["tl"] is None: continue
            e, a = r["t"] - r["tl"], r["tot"] - r["tl"]
            if abs(e) < th or a == 0: continue
            tw += (e > 0) == (a > 0); tl_ += (e > 0) != (a > 0)
        out.append(f"   edge {th}+: ATS {w}-{l} ({w / max(1, w + l) * 100:.1f}%)   totals {tw}-{tl_} ({tw / max(1, tw + tl_) * 100:.1f}%)")
    return "\n".join(out)


PRIOR_FILE = os.path.join(ROOT, "cfb_prior.json")   # last season's final ratings, shipped with the code (the history is 50MB)


def export_prior(all_games, season=SEASONS[-1] - 1):
    f = season_priors(all_games)[season]
    with open(PRIOR_FILE, "w", encoding="utf-8") as fh:
        json.dump({"season": season, **{k: {t: round(v, 2) for t, v in f[k].items()} for k in ("mov", "eff", "off", "def")}}, fh)


def live_ratings(season, max_age=12 * 3600):
    """This season's ratings for the sync, recomputed at most every 12 hours (2 CFBD calls); saved small in the data folder."""
    out = os.path.join(os.path.dirname(CACHE), "cfb_ratings.json")
    try:
        with open(out, encoding="utf-8") as fh: L = json.load(fh)
        if L.get("season") == season and time.time() - L["at"] < max_age: return L
    except (FileNotFoundError, ValueError, KeyError): pass
    with open(PRIOR_FILE, encoding="utf-8") as fh: last = json.load(fh)
    gs = load(season, max_age, with_lines=False)
    fbs = {t: v for g in gs for t, v in g["fbs"].items()}
    raw = fetch("games", f"/games?year={season}&seasonType=regular", season, max_age)
    ids = {str(g[k + "Id"]): g[k + "Team"] for g in raw for k in ("home", "away")}   # CFBD team ids are ESPN's
    R = ratings(gs, carry(last, fbs, season))
    L = {"at": time.time(), "season": season, "games": sum(1 for g in gs if g["done"]), "ids": ids,
         "R": {"mu": R["mu"], **{k: {t: round(v, 2) for t, v in R[k].items()} for k in ("mov", "eff", "off", "def")}}}
    with open(out, "w", encoding="utf-8") as fh: json.dump(L, fh)
    return L


# ------------------------------------------------------------ quarterback changes (no free college injury feed: we see who started)
BACKUP_YPA = 6.3    # a typical backup's yards per attempt, the prior for a QB with little or no tape
QB_PTS = 1.8        # points per game per 1.0 yard-per-attempt gap (about 30 throws a game; starter -> backup lands near 3-6 points)
QB_CAP = 8.0


def qb_weeks(season, week, max_age=12 * 3600):
    """Each team's starting QB (most attempts) in every finished week, kept small in the data folder: one CFBD call per new week."""
    path = os.path.join(os.path.dirname(CACHE), "cfb_qb_weeks.json")
    try:
        with open(path, encoding="utf-8") as fh: W = json.load(fh)
    except (FileNotFoundError, ValueError): W = {}
    if W.get("season") != season: W = {"season": season, "weeks": {}, "at": {}}
    for w in range(1, week):
        k = str(w)
        # past weeks are final; the latest finished week refreshes until its stats settle
        if k in W["weeks"] and (w < week - 1 or time.time() - W["at"].get(k, 0) < max_age): continue
        req = Request(f"{API}/games/players?year={season}&week={w}&seasonType=regular&category=passing", headers={"Authorization": f"Bearer {key()}"})
        with urlopen(req, timeout=90) as r: d = json.loads(r.read().decode())
        rows = {}
        for g in d:
            for t in g.get("teams", []):
                qbs = {}
                for typ in t["categories"][0]["types"] if t.get("categories") else []:
                    for a in typ["athletes"]:
                        q = qbs.setdefault(a["id"], {"n": a["name"]})
                        if typ["name"] == "C/ATT" and "/" in str(a["stat"]): q["att"] = int(str(a["stat"]).split("/")[1] or 0)
                        if typ["name"] == "YDS": q["yds"] = float(a["stat"] or 0)
                if qbs:
                    sid, q = max(qbs.items(), key=lambda kv: kv[1].get("att", 0))
                    rows[t["team"]] = {"id": sid, "n": q["n"], "att": q.get("att", 0), "yds": q.get("yds", 0), "all": {i: [v.get("att", 0), v.get("yds", 0)] for i, v in qbs.items()}}
        W["weeks"][k] = rows; W["at"][k] = time.time()
    with open(path, "w", encoding="utf-8") as fh: json.dump(W, fh)
    return W


def qb_changes(season, week):
    """Teams whose last game was started by someone other than their usual starter, with a points adjustment for our number."""
    W = qb_weeks(season, week)
    weeks = sorted(W["weeks"], key=int)
    out = {}
    teams = {t for w in weeks for t in W["weeks"][w]}
    for t in teams:
        played = [(int(w), W["weeks"][w][t]) for w in weeks if t in W["weeks"][w]]
        if len(played) < 2: continue
        starts = {}
        for _, r in played: starts[r["id"]] = starts.get(r["id"], 0) + 1
        usual = max(starts, key=starts.get)
        last_w, last = played[-1]
        if last["id"] == usual: continue
        # season passing for both, from every game they threw in
        tot = lambda pid: [sum(r["all"].get(pid, [0, 0])[0] for _, r in played), sum(r["all"].get(pid, [0, 0])[1] for _, r in played)]
        (oa, oy), (na, ny) = tot(usual), tot(last["id"])
        old_ypa = (oy + BACKUP_YPA * 40) / (oa + 40)                  # regressed a little
        new_ypa = (ny + BACKUP_YPA * 80) / (na + 80)                  # regressed hard toward a backup
        share = starts[usual] / len(played)                            # how much of the rating was built with the usual starter
        adj = max(-QB_CAP, min(QB_CAP / 2, (new_ypa - old_ypa) * QB_PTS * share))
        usual_name = next(r["n"] for _, r in played if r["id"] == usual)
        out[t] = {"new": last["n"], "old": usual_name, "wk": last_w, "starts": starts[usual], "games": len(played), "adj": round(adj, 1),
                  "newYpa": round(ny / na, 1) if na else None, "oldYpa": round(oy / oa, 1) if oa else None, "newAtt": na}
    return out


if __name__ == "__main__":
    allg = []
    for s in SEASONS: allg += load(s)
    print(f"loaded {len(allg)} games")
    export_prior(allg)
    for w_eff in (0.0, 0.45, 0.7, 1.0):
        rows = backtest(allg, w_eff=w_eff)
        print(report(rows, f"[efficiency weight {w_eff}] "))
