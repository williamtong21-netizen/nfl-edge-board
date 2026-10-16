"""College football sync for the Edge Board's College mode. Writes the same slate format as the NFL sync into its own
folder (out/cfb locally, data/cfb in the app), so the two sports never mix.

Covers Top 25 teams plus every Power 4 game (SEC, Big Ten, Big 12, ACC). Free ESPN data for games, odds, injuries and
win probabilities; the Odds API (key shared with the NFL app) for every book's game lines, at most every 12 hours,
about 3 credits a pull. No player props or nflverse analytics: college has no free play-by-play feed like nflverse.
Run:  python cfb_sync.py
"""
import json, os, sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("EDGE_OUT", os.path.join(ROOT, "out", "cfb"))
os.environ.setdefault("EDGE_DATA", os.path.join(ROOT, "data", "cfb"))
import sync   # noqa: E402  (reads EDGE_OUT / EDGE_DATA on import)

sync.ESPN = "https://site.api.espn.com/apis/site/v2/sports/football/college-football"
sync.SUMMARY = "https://site.web.api.espn.com/apis/site/v2/sports/football/college-football/summary?event="
sync.ODDS_API = "https://api.the-odds-api.com/v4/sports/americanfootball_ncaaf/odds"
POWER4 = {"8": "SEC", "5": "Big Ten", "4": "Big 12", "1": "ACC"}


def keep(ev):
    cs = ev["competitions"][0]["competitors"]
    return any((x.get("curatedRank") or {}).get("current", 99) <= 25 or str(x["team"].get("conferenceId")) in POWER4 for x in cs)


def ours(games, season):
    """Our own spread and total (cfb_model), shown as info: backtested even with the closing line, so not used for picks."""
    try:
        import cfb_model
        L = cfb_model.live_ratings(season)
    except Exception as e:
        print("cfb model skipped:", e, file=sys.stderr); return None
    R, ids = L["R"], L["ids"]
    try: qb = cfb_model.qb_changes(season, max(g["week"] for g in games))   # new starters (injury or benching), from last game's box score
    except Exception as e: print("qb check skipped:", e, file=sys.stderr); qb = {}
    for g in games:
        h, a = ids.get(str(g["home"]["id"])), ids.get(str(g["away"]["id"]))
        if h in R["mov"] and a in R["mov"]:
            m, t = cfb_model.predict(R, h, a, g.get("neutral"))
            qh, qa = qb.get(h), qb.get(a)
            if g["state"] == "pre" and (qh or qa):
                g["qb"] = {k: v for k, v in (("home", qh), ("away", qa)) if v}
                m += (qh or {}).get("adj", 0) - (qa or {}).get("adj", 0)
                t += 0.5 * ((qh or {}).get("adj", 0) + (qa or {}).get("adj", 0))   # a weaker passer scores less
            tb = t
            if L.get("U"):   # mixed total: half the points ratings, half the run / pass unit matchups (cfb_units)
                import cfb_units
                try: t = (1 - cfb_units.W_UNIT_TOT) * t + cfb_units.W_UNIT_TOT * (cfb_units.unit_total(L["U"], h, a, g.get("neutral"))
                          + 0.5 * ((qh or {}).get("adj", 0) + (qa or {}).get("adj", 0)) * (g["state"] == "pre"))
                except Exception as e: print("unit total skipped", h, a, e, file=sys.stderr)
            fb = set(L.get("fbs") or [])
            g["ours"] = {"m": round(m, 1), "t": round(t, 1), "tb": round(tb, 1), "fbs": h in fb and a in fb}
    return {"games": L["games"], "at": L["at"]}


def record(games, season, prev_events):
    """Finals, closing lines and our number for every college game, kept all season (grades My Bets and our record)."""
    res = sync.load(f"results_{season}.json", {"season": season, "games": {}})
    for g in games:
        r = res["games"].setdefault(g["id"], {})
        r.update({"w": g["week"], "d": g["date"], "h": g["home"]["abbr"], "a": g["away"]["abbr"], "st": g["state"]})
        if g["state"] == "pre" and g.get("odds"):   # keeps updating until kickoff, then freezes = the closing line
            r.update({"cs": g["odds"].get("hs"), "ct": g["odds"].get("t"), "chml": g["odds"].get("hml"), "caml": g["odds"].get("aml")})
            if g.get("ours"): r.update({"om": g["ours"]["m"], "ot": g["ours"]["t"]})
        if g["state"] == "post":
            r.update({"hs": sync.num(g["home"]["score"]), "as": sync.num(g["away"]["score"])})
    for e in prev_events:   # last week's games that finished after the scoreboard moved on
        r = res["games"].get(e["id"])
        c = e["competitions"][0]
        if not r or r.get("st") == "post" or c["status"]["type"]["state"] != "post": continue
        sc = {x["homeAway"]: sync.num(x.get("score")) for x in c["competitors"]}
        r.update({"st": "post", "hs": sc.get("home"), "as": sc.get("away")})
    sync.save(os.path.join(sync.DATA, f"results_{season}.json"), res)
    return res


def report(res):
    """How our number did against the closing line: spreads and totals, by edge size and by week."""
    blank = lambda: {"w": 0, "l": 0, "push": 0}
    out = {"ats": {k: blank() for k in ("all", "3+", "5+")}, "tot": {k: blank() for k in ("all", "3+", "5+")}, "weeks": {}, "games": 0}
    for r in res["games"].values():
        if r.get("st") != "post" or r.get("hs") is None or r.get("om") is None: continue
        out["games"] += 1
        wk = out["weeks"].setdefault(str(r["w"]), {"ats": blank(), "tot": blank()})
        for kind, edge, result in (("ats", None if r.get("cs") is None else r["om"] + r["cs"], None if r.get("cs") is None else r["hs"] - r["as"] + r["cs"]),
                                   ("tot", None if r.get("ct") is None else r["ot"] - r["ct"], None if r.get("ct") is None else r["hs"] + r["as"] - r["ct"])):
            if edge is None or abs(edge) < 0.5: continue   # no lean
            k = "push" if result == 0 else "w" if (edge > 0) == (result > 0) else "l"
            for b, th in (("all", 0), ("3+", 3), ("5+", 5)):
                if abs(edge) >= th: out[kind][b][k] += 1
            wk[kind][k] += 1
    return out


def main():
    try:
        with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as f: cfg = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError): cfg = {}
    sb = sync.get(sync.ESPN + "/scoreboard?groups=80&limit=300")
    season, week = sb["season"]["year"], (sb.get("week") or {}).get("number")
    events = [(e, week) for e in sb["events"] if keep(e)]
    prev = sync.get(sync.ESPN + f"/scoreboard?groups=80&limit=300&week={week - 1}")["events"] if week and week > 1 else []
    # early in the week the current week can be mostly done: add next week's games too
    if week and sum(1 for e, _ in events if e["competitions"][0]["status"]["type"]["state"] == "pre") <= 3:
        nxt = sync.get(sync.ESPN + f"/scoreboard?groups=80&limit=300&week={week + 1}")
        events += [(e, week + 1) for e in nxt["events"] if keep(e)]
    with ThreadPoolExecutor(8) as pool:
        sums = list(pool.map(sync.summary, [e for e, _ in events]))
    games = []
    for (e, w), sm in zip(events, sums):
        g = sync.parse_game(e, sm, w)
        for x in e["competitions"][0]["competitors"]:
            t = g["home" if x["homeAway"] == "home" else "away"]
            rank = (x.get("curatedRank") or {}).get("current", 99)
            t["rank"] = rank if rank <= 25 else None
            t["logo"] = x["team"].get("logo")
            t["conf"] = POWER4.get(str(x["team"].get("conferenceId")))
        games.append(g)
    model_note = ours(games, season)
    sync.weather(games)
    odds_meta = sync.multibook(games, cfg)
    for g in games: g.pop("city", None)
    res = record(games, season, prev)
    flags = None
    try:   # early-week leans: Power 4 games where our number is 3+ off the opener, frozen, graded, one alert digest
        import cfb_flags
        fl = cfb_flags.find(games)
        for f_ in fl: next(g for g in games if g["id"] == f_["gid"]).setdefault("lean", []).append({k: f_[k] for k in ("kind", "text", "open", "edge")})
        st = cfb_flags.track(games, fl, res, os.path.join(sync.DATA, f"cfb_flags_{season}.json"))
        flags = {"bt": cfb_flags.BACKTEST, "rec": cfb_flags.grade(st), "edge": cfb_flags.EDGE,
                 "past": sorted((v for v in st["flags"].values() if v.get("hs") is not None), key=lambda v: v["d"], reverse=True)[:40]}
        cfb_flags.alert(st, games, cfg, os.path.join(sync.DATA, f"cfb_flags_{season}.json"))
    except Exception as e:
        import traceback; traceback.print_exc(); print("cfb flags failed", e, file=sys.stderr)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    slate = {"updatedAt": now, "sport": "cfb", "propParts": 0, "analytics": None, "season": season, "week": week,
             "seasonType": sb["season"].get("type", 2), "odds": odds_meta, "report": None, "cfbModel": model_note, "cfbReport": report(res), "cfbFlags": flags, "games": games}
    sync.save(os.path.join(sync.OUT, "slate.json"), slate)
    sync.save(os.path.join(sync.OUT, "history.json"), {"season": season, "snaps": []})
    sync.save(os.path.join(sync.OUT, "results.json"), res)
    sync.save(os.path.join(sync.OUT, "manifest.json"), [{"collection": "slate", "doc_id": "current", "file": "slate.json"}])
    print(json.dumps({"sport": "cfb", "season": season, "week": week, "games": len(games),
                      "with_books": sum(1 for g in games if g.get("books")), "multibook": odds_meta}))


if __name__ == "__main__":
    main()
