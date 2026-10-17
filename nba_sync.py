"""NBA sync for the Edge Board's NBA mode. Writes the same slate / props format as the NFL sync into its own folder
(out/nba locally, data/nba in the app), so the sports never mix. Free sources only:
  games, DraftKings lines, injuries   ESPN scoreboard + game summaries
  player props                        ESPN's DraftKings milestone ladders (points / rebounds / assists / threes "X+")
  Kalshi                              game lines and the same four player ladders (alt lines, market prices)
  model                               nba_model: minutes x per-minute rates x pace x opponent, blended with the market
Run:  python nba_sync.py
"""
import json, os, re, sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("EDGE_OUT", os.path.join(ROOT, "out", "nba"))
os.environ.setdefault("EDGE_DATA", os.path.join(ROOT, "data", "nba"))
import sync, kalshi, nba_model as M   # noqa: E402  (sync reads EDGE_OUT / EDGE_DATA on import)

sync.ESPN = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
sync.SUMMARY = "https://site.web.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event="
PROPS = "https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba/events/{0}/competitions/{0}/odds/100/propBets?limit=1000"
ESPN_TYPE = {"Points Milestones": "Points", "Rebounds Milestones": "Rebounds", "Assists Milestones": "Assists", "3-Point Field Goals Milestones": "Threes"}
LOG_KEY = {"Points": "points", "Rebounds": "rebounds", "Assists": "assists", "Threes": "threes"}
MKT_W = 0.8        # share of the market in the chance we show. Backtest (2025-26 Kalshi, 322 lines before tip): Brier ours 0.245, Kalshi 0.239, 60% market 0.2391, 80% 0.2388
VIG = 0.06         # DraftKings milestone ("X+", yes only) prices carry about this much margin

kalshi.PLAYER_SERIES = {"KXNBAPTS": "Points", "KXNBAREB": "Rebounds", "KXNBAAST": "Assists", "KXNBA3PT": "Threes"}
kalshi.GAME_SERIES = ("KXNBAGAME", "KXNBASPREAD", "KXNBATOTAL", "KXNBATEAMTOTAL")
kalshi.TO_ESPN = {"NYK": "NY", "SAS": "SA", "GSW": "GS", "NOP": "NO", "UTA": "UTAH", "WAS": "WSH"}
kalshi.FIRST_TD = None


def american(s):
    try: a = float(str(s).replace("+", ""))
    except (TypeError, ValueError): return None
    return 100 / (a + 100) if a > 0 else -a / (-a + 100)


def roster(team_id):
    try: d = sync.get(f"{sync.ESPN}/teams/{team_id}/roster")
    except Exception as e: print("roster failed", team_id, e, file=sys.stderr); return {}
    return {a["id"]: {"n": a["displayName"], "p": (a.get("position") or {}).get("abbreviation", "")} for a in d.get("athletes", [])}


def ladders(g):
    """{(athlete id, market): [{"l": 24.5, "dk": +odds, "pk": implied chance}]} from ESPN's DraftKings milestones."""
    try: items = sync.get(PROPS.format(g["id"])).get("items", [])
    except Exception as e: print("props failed", g["id"], e, file=sys.stderr); return {}
    out = {}
    for it in items:
        m = ESPN_TYPE.get((it.get("type") or {}).get("name")); ref = ((it.get("athlete") or {}).get("$ref") or "")
        aid = re.search(r"/athletes/(\d+)", ref)
        tgt = ((it.get("current") or {}).get("target") or {}).get("value")
        px = ((it.get("odds") or {}).get("american") or {}).get("value")
        if not (m and aid and tgt is not None and american(px)): continue
        out.setdefault((aid.group(1), m), []).append({"l": float(tgt) - 0.5, "dk": px, "pk": round(american(px), 3)})
    for v in out.values(): v.sort(key=lambda r: r["l"])
    return out


def log_rows(book, pid, before, season):
    """His last 10 games, newest last, in the app's game-log shape."""
    out = []
    for r in book.player(pid, before)[-10:]:
        d = r["game_date"]
        out.append({"y": season if d >= f"{season - 1}-10-01" else season - 1, "w": None, "d": f"{int(d[5:7])}/{int(d[8:10])}", "o": r["opponent_team_abbreviation"],
                    "at": "vs" if r.get("home_away") == "home" else "@",
                    "s": {"points": M.f(r["points"]), "rebounds": M.f(r["rebounds"]), "assists": M.f(r["assists"]), "threes": M.f(r["three_point_field_goals_made"]),
                          "minutes": M.f(r["minutes"])}})
    return out


def mkt_chance(pr):
    """The market's chance of the over at our line: Kalshi's mid when it trades that rung, else DraftKings minus its margin."""
    k = next((r for r in pr.get("alt") or [] if r["l"] == pr["l"] and r.get("ya") is not None and r.get("na") is not None), None)
    if k: return (k["ya"] + 1 - k["na"]) / 2
    return pr["pk"] / (1 + VIG) if pr.get("pk") else None


def build_props(games, season):
    rows = []
    for yr in (season - 1, season):
        try: rows += M.box(yr)
        except Exception as e: print("box scores", yr, "not available:", e, file=sys.stderr)
    book = M.Book(rows)
    by_name = {}
    for r in rows: by_name[M.norm(r["athlete_display_name"])] = r["athlete_id"]
    tg = {}
    for r in rows: tg[r["game_id"]] = tg.get(r["game_id"], 0) + 1
    league = {}
    for st in M.STAT.values():
        tot = {}
        for r in rows: tot[r["game_id"]] = tot.get(r["game_id"], 0) + (M.f(r.get(st)) or 0)
        league[st] = sum(tot.values()) / max(1, len(tot)) / 2
    pre = [g for g in games if g["state"] == "pre" and (not g.get("pre_season") or os.environ.get("NBA_TEST_PRESEASON"))]   # preseason starters play ~20 minutes: no props
    with ThreadPoolExecutor(6) as pool:
        lads = dict(zip([g["id"] for g in pre], pool.map(ladders, pre)))
        rost = dict(zip([g[s]["id"] for g in pre for s in ("home", "away")], pool.map(roster, [g[s]["id"] for g in pre for s in ("home", "away")])))
    props = {}
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # ruled out: ESPN's injury list on the game, or a headline the 20-minute news check caught (state news_state.json)
    try: news_outs = set(json.load(open(os.path.join(sync.DATA, "news_state.json"), encoding="utf-8")).get("outs", {}))
    except (FileNotFoundError, ValueError): news_outs = set()
    for g in pre:
        side_of = {}
        for s in ("home", "away"):
            for aid, a in rost.get(g[s]["id"], {}).items(): side_of[aid] = (s, a)
        pls = {}
        out_names = news_outs | {i["n"] for sd in ("home", "away") for i in (g[sd].get("inj") or []) if i.get("s") in ("Out", "Doubtful")}
        for (aid, m), rungs in lads.get(g["id"], {}).items():
            if aid not in side_of: continue
            s, a = side_of[aid]
            if a["n"] in out_names: continue
            pl = pls.setdefault(aid, {"id": aid, "n": a["n"], "p": a["p"], "tm": g[s]["abbr"], "side": s, "props": []})
            main = min(rungs, key=lambda r: abs(r["pk"] / (1 + VIG) - 0.5))   # the rung nearest a coin flip is the line
            pl["props"].append({"m": m, "l": main["l"], "o": None, "pk": main["pk"], "dk": main["dk"], "books": [{"n": "DraftKings", "l": r["l"], "o": int(float(str(r["dk"]).replace("+", ""))), "u": None} for r in rungs]})
        for aid, pl in pls.items():
            pid = aid if aid in book.by_player else by_name.get(M.norm(pl["n"]))
            opp = g["away" if pl["side"] == "home" else "home"]["abbr"]
            pl["log"] = log_rows(book, pid, today, season) if pid else []
            for pr in pl["props"]:
                st = M.STAT[pr["m"]]
                pj = M.project(book, pid, st, today, (g.get("odds") or {}).get("t"), opp, league[st]) if pid else None
                if pj:
                    pr["proj"] = round(pj[0], 1); pr["min"] = round(pj[1], 1)
                    pr["pModel"] = round(max(0.03, min(0.97, M.p_over(st, pj[0], pr["l"]))), 3)
        props[g["id"]] = list(pls.values())
    return props


def finish(props):
    """After Kalshi's ladders are attached: the market's chance and the blend we show."""
    for pls in props.values():
        for pl in pls:
            for pr in pl["props"]:
                pm = mkt_chance(pr)
                if pm is not None: pr["pMkt"] = round(pm, 3)
                if pr.get("pModel") is not None:
                    pr["pOver"] = round(MKT_W * pm + (1 - MKT_W) * pr["pModel"], 3) if pm is not None else pr["pModel"]
                pr.pop("pk", None)
            pl["props"].sort(key=lambda p: list(LOG_KEY).index(p["m"]) if p["m"] in LOG_KEY else 9)
        pls.sort(key=lambda pl: -max([abs((p.get("pOver") or 0.5) - 0.5) for p in pl["props"]] or [0]))


def record(games, season):
    """Finals and closing lines for every game, kept all season (grades My Bets)."""
    res = sync.load(f"results_{season}.json", {"season": season, "games": {}})
    for g in games:
        if g.get("pre_season"): continue
        r = res["games"].setdefault(g["id"], {})
        r.update({"d": g["date"], "h": g["home"]["abbr"], "a": g["away"]["abbr"], "st": g["state"]})
        if g["state"] == "pre" and g.get("odds"):   # keeps updating until tip-off, then freezes = the closing line
            r.update({"cs": g["odds"].get("hs"), "ct": g["odds"].get("t"), "chml": g["odds"].get("hml"), "caml": g["odds"].get("aml")})
        if g["state"] == "post": r.update({"hs": sync.num(g["home"]["score"]), "as": sync.num(g["away"]["score"])})
    sync.save(os.path.join(sync.DATA, f"results_{season}.json"), res)
    return res


def record_lines(games, props, now):
    """Save every prop line this sync saw (DraftKings price, Kalshi's price at that line, our numbers, who's out), one file per
    day in state/nba/lines/. Weeks of these show whether lines lag behind injury news (the "teammate out" test, nba_lineup.py)."""
    d = os.path.join(sync.DATA, "lines"); os.makedirs(d, exist_ok=True)
    try: outs_news = set(json.load(open(os.path.join(sync.DATA, "news_state.json"), encoding="utf-8")).get("outs", {}))
    except (FileNotFoundError, ValueError): outs_news = set()
    t = now.isoformat(timespec="seconds"); n = 0
    with open(os.path.join(d, now.strftime("%Y-%m-%d") + ".jsonl"), "a", encoding="utf-8") as fh:
        for g in games:
            if g["state"] != "pre" or (g.get("pre_season") and not os.environ.get("NBA_TEST_PRESEASON")) or g["id"] not in props: continue
            outs = {sd: sorted({i["n"] for i in (g[sd].get("inj") or []) if i.get("s") in ("Out", "Doubtful")} | {x for x in outs_news if x in {pl["n"] for pl in props[g["id"]] if pl["side"] == sd}})
                    for sd in ("home", "away")}
            fh.write(json.dumps({"t": t, "g": g["id"], "tip": g["date"], "outs": outs}, separators=(",", ":")) + "\n")
            for pl in props[g["id"]]:
                for pr in pl["props"]:
                    k = next((r for r in pr.get("alt") or [] if r["l"] == pr["l"] and r.get("ya") is not None and r.get("na") is not None), None)
                    fh.write(json.dumps({"t": t, "g": g["id"], "pid": pl["id"], "n": pl["n"], "tm": pl["tm"], "m": pr["m"], "l": pr["l"], "dk": pr.get("dk"),
                                         "km": round((k["ya"] + 1 - k["na"]) / 2, 3) if k else None, "po": pr.get("pOver"), "pm": pr.get("pModel"), "pr": pr.get("proj")},
                                        separators=(",", ":")) + "\n"); n += 1
    # keep about 60 days
    for fn in sorted(os.listdir(d))[:-60]: os.remove(os.path.join(d, fn))
    return n


def main():
    now = datetime.now(timezone.utc)
    events = []
    for d in range(0, 3):   # today and the next two days (US dates)
        day = (now - timedelta(hours=5) + timedelta(days=d)).strftime("%Y%m%d")
        try: sb = sync.get(f"{sync.ESPN}/scoreboard?dates={day}&limit=100")
        except Exception as e: print("scoreboard failed", day, e, file=sys.stderr); continue
        events += [e for e in sb.get("events", []) if e["id"] not in {x["id"] for x in events}]
        season = sb.get("season", {}).get("year") or (now.year + (1 if now.month >= 8 else 0))
        stype = sb.get("season", {}).get("type", 2)
    with ThreadPoolExecutor(8) as pool:
        sums = list(pool.map(sync.summary, events))
    games = []
    for e, sm in zip(events, sums):
        g = sync.parse_game(e, sm, None)
        g.pop("city", None); g["indoor"] = True; g["pre_season"] = (e.get("season") or {}).get("type") == 1
        for x in e["competitions"][0]["competitors"]:
            t = g["home" if x["homeAway"] == "home" else "away"]; t["logo"] = x["team"].get("logo")
        games.append(g)
    props = build_props(games, season)
    try: nk = kalshi.apply(games, props)
    except Exception as ex: print("kalshi failed", ex, file=sys.stderr); nk = 0
    finish(props)
    try: nl = record_lines(games, props, now)
    except Exception as ex: print("line log failed", ex, file=sys.stderr); nl = 0
    try:   # price gaps: DraftKings (ESPN) vs Kalshi on moneylines, spreads and totals
        import arbs; gaps = arbs.find(games, props, None)
    except Exception as ex: print("arbs failed", ex, file=sys.stderr); gaps = []
    try:   # group alerts: line moves and fresh price gaps, same channel and rules as the NFL
        import alerts
        try: cfg = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
        except (FileNotFoundError, ValueError): cfg = {}
        alerts.run([g for g in games if not g.get("pre_season")], props, None, cfg, os.path.join(sync.DATA, "alerts_state.json"), label="NBA", gaps=gaps)
    except Exception as ex: print("alerts failed", ex, file=sys.stderr)
    hist = sync.line_history(games, season)   # Moves tab
    try: import moves; moves.explain(games, hist["snaps"], "basketball/nba")
    except Exception as ex: print("move reasons failed", ex, file=sys.stderr)
    stamp = now.isoformat(timespec="seconds")
    slate = {"updatedAt": stamp, "sport": "nba", "propParts": 1, "analytics": None, "season": season, "week": None, "seasonType": stype,
             "odds": {"source": "ESPN (DraftKings)"}, "report": None, "nbaModel": {"mktW": MKT_W}, "games": games, "arbs": gaps}
    sync.save(os.path.join(sync.OUT, "slate.json"), slate)
    sync.save(os.path.join(sync.OUT, "props-0.json"), {"sync": stamp, "part": 0, "games": props})
    sync.save(os.path.join(sync.OUT, "history.json"), hist)
    res = record(games, season)
    sync.save(os.path.join(sync.OUT, "results.json"), res)
    sync.save(os.path.join(sync.OUT, "manifest.json"), [{"collection": "slate", "doc_id": "current", "file": "slate.json"},
                                                        {"collection": "results", "doc_id": str(season), "file": "results.json"},
                                                        {"collection": "props", "doc_id": "part-0", "file": "props-0.json"}])
    print(json.dumps({"sport": "nba", "season": season, "games": len(games), "players": sum(len(v) for v in props.values()),
                      "props": sum(len(p["props"]) for v in props.values() for p in v), "kalshi": nk, "logged": nl}))


if __name__ == "__main__":
    main()
