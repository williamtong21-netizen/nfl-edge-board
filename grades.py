"""Report card: every reading the board makes is frozen at kickoff, graded after the final whistle, and summed up
so we can see which kinds of reads actually hold up (and tune the ones that don't).

freeze(games, props, readings)       -> stores each upcoming game's readings (overwritten every run until kickoff)
grade(games, summaries, readings)    -> fills in final scores and every prop's actual stat once a game is final
report(readings, results)            -> the summary the page shows: hit rates by confidence, market, week and read type
"""
from collections import defaultdict

STAT = {  # prop market -> ESPN box score stats that add up to it
    "Pass yds": ["passingYards"], "Pass TD": ["passingTouchdowns"], "Completions": ["completions"], "Pass att": ["passingAttempts"],
    "INT": ["interceptions"], "Rush yds": ["rushingYards"], "Carries": ["rushingAttempts"], "Rec yds": ["receivingYards"],
    "Receptions": ["receptions"], "Rush+rec yds": ["rushingYards", "receivingYards"], "Pass+rush yds": ["passingYards", "rushingYards"],
    "Long rec": ["longReception"], "Anytime TD": ["rushingTouchdowns", "receivingTouchdowns"],
}
CONF = [(0.5, 0.55, "50–55%"), (0.55, 0.6, "55–60%"), (0.6, 0.65, "60–65%"), (0.65, 1.01, "65%+")]
TD_BINS = [(0, 0.15, "under 15%"), (0.15, 0.3, "15–30%"), (0.3, 0.45, "30–45%"), (0.45, 1.01, "45%+")]
EDGE_BINS = [(0, 1.5, "under 1.5 pts"), (1.5, 3, "1.5–3 pts"), (3, 99, "3+ pts")]


def freeze(games, props, readings):
    for g in games:
        if g["state"] != "pre" or not g.get("odds"): continue
        o, p = g["odds"], g.get("proj") or {}
        rec = {"w": g["week"], "d": g["date"], "h": g["home"]["abbr"], "a": g["away"]["abbr"],
               "line": {"hs": o.get("hs"), "t": o.get("t")},
               "proj": {"m": p.get("margin"), "t": p.get("total"), "pH": p.get("pHome")} if p.get("margin") is not None else None, "props": []}
        for pl in props.get(g["id"], []):
            tm = g["home"]["abbr"] if pl.get("side") == "home" else g["away"]["abbr"]
            for pr in pl["props"]:
                if pr.get("pOver") is None or pr["m"] not in STAT: continue
                rec["props"].append({"id": pl["id"], "n": pl["n"], "tm": tm, "m": pr["m"], "l": 0.5 if pr["m"] == "Anytime TD" else pr["l"], "p": pr["pOver"]})
        readings["games"][g["id"]] = rec


def grade(games, summaries, readings):
    from live import players as box      # same box-score parser the live tracker uses
    n = 0
    for g, summ in zip(games, summaries):
        rec = readings["games"].get(g["id"])
        if not rec or rec.get("res") or g["state"] != "post" or not summ: continue
        bx = box(summ)
        if not bx: continue
        for pr in rec["props"]:
            s = (bx.get(pr["id"]) or {}).get("s")
            pr["a"] = None if s is None else sum(s.get(k) or 0 for k in STAT[pr["m"]])   # None: didn't play, the bet is void
        rec["res"] = {"h": float(g["home"]["score"]), "a": float(g["away"]["score"])}
        n += 1
    return n


def _rate(w, l): return round(w / (w + l), 3) if w + l else None


def report(readings, results):
    bucket = lambda v, bins: next(lbl for lo, hi, lbl in bins if lo <= v < hi)
    props = defaultdict(lambda: [0, 0]); market = defaultdict(lambda: [0, 0]); week = defaultdict(lambda: [0, 0])
    td = defaultdict(lambda: [0, 0, 0.0])        # bin -> [scored, players, sum of our chances]
    sides = defaultdict(lambda: [0, 0, 0]); totals = defaultdict(lambda: [0, 0, 0])
    misses, graded = [], 0
    for gid, rec in readings["games"].items():
        res = rec.get("res")
        if not res: continue
        graded += 1
        margin, pts = res["h"] - res["a"], res["h"] + res["a"]
        for pr in rec["props"]:
            if pr.get("a") is None: continue
            if pr["m"] == "Anytime TD":
                b = td[bucket(pr["p"], TD_BINS)]; b[0] += pr["a"] > 0; b[1] += 1; b[2] += pr["p"]; continue
            if pr["a"] == pr["l"]: continue          # push
            over, conf = pr["p"] >= 0.5, max(pr["p"], 1 - pr["p"])
            hit = (pr["a"] > pr["l"]) == over
            for d in (props[bucket(conf, CONF)], market[pr["m"]], week[rec["w"]], props["all"]):
                d[0 if hit else 1] += 1
            if not hit and conf >= 0.6:
                misses.append({"g": f'{rec["a"]} @ {rec["h"]}', "w": rec["w"], "n": pr["n"], "m": pr["m"], "l": pr["l"],
                               "side": "Over" if over else "Under", "p": round(conf, 3), "a": pr["a"]})
        line, proj = rec.get("line") or {}, rec.get("proj")
        if proj and line.get("hs") is not None:
            edge = proj["m"] + line["hs"]            # how many points we think home beats the spread by
            cover = margin + line["hs"]
            if abs(edge) >= 0.5:
                d = sides[bucket(abs(edge), EDGE_BINS)]; da = sides["all"]
                k = 2 if cover == 0 else 0 if (cover > 0) == (edge > 0) else 1
                d[k] += 1; da[k] += 1
        if proj and line.get("t") is not None:
            edge = proj["t"] - line["t"]
            if abs(edge) >= 0.5:
                d = totals[bucket(abs(edge), EDGE_BINS)]; da = totals["all"]
                k = 2 if pts == line["t"] else 0 if (pts > line["t"]) == (edge > 0) else 1
                d[k] += 1; da[k] += 1
    # trap reads were already frozen into the results file at kickoff
    traps = {"side": [0, 0, 0], "total": [0, 0, 0]}
    for r in (results or {}).get("games", {}).values():
        if r.get("st") != "post" or r.get("hs") is None or not r.get("trap"): continue
        for k, v in r["trap"].items():
            if k == "side":
                m = (r["hs"] - r["as"]) if v["fade"] == r["h"] else (r["as"] - r["hs"])
                c = m + v["line"]
            else:
                c = v["line"] - (r["hs"] + r["as"])   # under wins when the game lands below the line
            traps[k][2 if c == 0 else 0 if c > 0 else 1] += 1
    wl = lambda d: {"w": d[0], "l": d[1], "pct": _rate(d[0], d[1])}
    return {
        "games": graded,
        "props": {k: wl(v) for k, v in props.items()},
        "markets": {k: wl(v) for k, v in sorted(market.items(), key=lambda x: -sum(x[1]))},
        "weeks": {str(k): wl(v) for k, v in sorted(week.items())},
        "td": [{"bin": lbl, "n": td[lbl][1], "scored": td[lbl][0], "ours": round(td[lbl][2] / td[lbl][1], 3) if td[lbl][1] else None}
               for _, _, lbl in TD_BINS if td[lbl][1]],
        "sides": {k: {**wl(v), "push": v[2]} for k, v in sides.items()},
        "totals": {k: {**wl(v), "push": v[2]} for k, v in totals.items()},
        "traps": {k: {**wl(v), "push": v[2]} for k, v in traps.items() if sum(v)},
        "misses": sorted(misses, key=lambda x: (-x["w"], -x["p"]))[:6],
    }
