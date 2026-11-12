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
                row = {"id": pl["id"], "n": pl["n"], "tm": tm, "m": pr["m"], "l": 0.5 if pr["m"] == "Anytime TD" else pr["l"], "p": pr["pOver"]}
                # Kalshi money at freeze time (kalshi.flow): dollars traded, new dollars, side split, price move; kept so we
                # can test later whether "the money says over" predicts anything beyond the price itself
                kv = pr.get("kv") or {}
                if kv: row["kv"] = {k: kv[k] for k in ("usd", "dusd", "ys", "dp") if kv.get(k) is not None}
                # the market's own chance of the over at kickoff (left off when it sat at 50/50), so leans can be graded vs the price
                mk = pr.get("pMkt") if pr["m"] == "Anytime TD" else pr.get("pMkt", 0.5)
                if mk is not None: row["mk"] = mk
                rec["props"].append(row)
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


# "key bets": the props people actually bet, on the players who matter. A player counts when his own line says he's a
# featured piece that week (starting QB, lead back, real receiver); backups and 1.5-catch tight ends are left out.
KEY_LINE = {"Pass yds": 175.5, "Rush yds": 44.5, "Rec yds": 39.5}            # his line must be at least this
KEY_BY = {"Pass yds": "Pass yds", "Pass TD": "Pass yds", "Rush yds": "Rush yds", "Rec yds": "Rec yds", "Receptions": "Rec yds"}


def key_rows(rec):
    """The graded rows of one game that are key bets (market in KEY_BY and the player's matching line is big enough)."""
    lines = {(r["id"], r["m"]): r["l"] for r in rec["props"]}
    return [r for r in rec["props"] if r["m"] in KEY_BY and (lines.get((r["id"], KEY_BY[r["m"]])) or 0) >= KEY_LINE[KEY_BY[r["m"]]]]


VIG = 1.0476   # a -110 / -110 line: you pay 52.4% for a 50/50 bet


def vs_price(rows, lean=0.55):
    """Leans graded against the price: record, how often the market said our side would hit, and profit at that price.
    rows: (our chance of over, market chance of over or None, over hit, groups). A lean only counts when the market
    priced it, so "hit 58%" can be read next to "the price said 57%". mkt = what you'd actually pay (the fair market
    chance plus a normal book's cut), roi = profit per $1 bet at that price."""
    out = defaultdict(lambda: {"w": 0, "l": 0, "mkt": 0.0, "pl": 0.0})
    for p, mk, over, grps in rows:
        c = max(p, 1 - p)
        if c < lean or mk is None or not 0.03 < mk < 0.97: continue
        side_mk = min(0.97, (mk if p >= 0.5 else 1 - mk) * VIG)
        hit = over == (p >= 0.5)
        for k in ("all", *grps, "60%+" if c >= 0.6 else "55–60%"):
            d = out[k]; d["w" if hit else "l"] += 1; d["mkt"] += side_mk; d["pl"] += (1 / side_mk - 1) if hit else -1
    res = {}
    for k, d in out.items():
        n = d["w"] + d["l"]
        res[k] = {"w": d["w"], "l": d["l"], "pct": round(d["w"] / n, 3), "mkt": round(d["mkt"] / n, 3), "roi": round(d["pl"] / n, 3)}
    return res


def _rate(w, l): return round(w / (w + l), 3) if w + l else None


def report(readings, results):
    bucket = lambda v, bins: next(lbl for lo, hi, lbl in bins if lo <= v < hi)
    props = defaultdict(lambda: [0, 0]); market = defaultdict(lambda: [0, 0]); week = defaultdict(lambda: [0, 0])
    td = defaultdict(lambda: [0, 0, 0.0])        # bin -> [scored, players, sum of our chances]
    sides = defaultdict(lambda: [0, 0, 0]); totals = defaultdict(lambda: [0, 0, 0])
    misses, graded = [], 0
    key = []                                     # key bets, for the vs-the-price record
    for gid, rec in readings["games"].items():
        res = rec.get("res")
        if not res: continue
        graded += 1
        margin, pts = res["h"] - res["a"], res["h"] + res["a"]
        key += [(pr["p"], pr.get("mk"), pr["a"] > pr["l"], (pr["m"], "Week " + str(rec["w"]))) for pr in key_rows(rec)
                if pr.get("a") is not None and pr["a"] != pr["l"]]
        for pr in rec["props"]:
            if pr.get("a") is None: continue
            if pr["m"] == "Anytime TD":
                b = td[bucket(pr["p"], TD_BINS)]; b[0] += pr["a"] > 0; b[1] += 1; b[2] += pr["p"]; continue
            if pr["a"] == pr["l"]: continue          # push
            over, conf = pr["p"] >= 0.5, max(pr["p"], 1 - pr["p"])
            hit = (pr["a"] > pr["l"]) == over
            # a lean = 55%+ (50-55% reads are coin flips and only show in the by-confidence table), same as the NBA card
            for d in (props[bucket(conf, CONF)],) + ((market[pr["m"]], week[rec["w"]], props["all"]) if conf >= 0.55 else ()):
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
        "key": vs_price(key),
        "misses": sorted(misses, key=lambda x: (-x["w"], -x["p"]))[:6],
    }
