"""College basketball sync for the Edge Board's College BB mode. Same slate format as the other sports, its own folder
(out/cbb locally, data/cbb in the app). Free sources only: ESPN scoreboard (every D-I game, DraftKings lines), Kalshi
(KXNCAAMB game / spread / total), and our own number from cbb_model (tempo-free ratings), shown as info until it proves
itself against the closing line. No player props (books barely post them for college).
Run:  python cbb_sync.py
"""
import json, os, sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("EDGE_OUT", os.path.join(ROOT, "out", "cbb"))
os.environ.setdefault("EDGE_DATA", os.path.join(ROOT, "data", "cbb"))
import sync, kalshi   # noqa: E402

sync.ESPN = "https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball"
kalshi.PLAYER_SERIES = {}
kalshi.GAME_SERIES = ("KXNCAAMBGAME", "KXNCAAMBSPREAD", "KXNCAAMBTOTAL", "KXNCAAMBTEAMTOTAL")
kalshi.TO_ESPN = {}
kalshi.FIRST_TD = None


def ours(games, season):
    try:
        import cbb_model as M
        R, lg, n = M.live(season)
    except Exception as e:
        print("cbb model skipped:", e, file=sys.stderr); return None
    for g in games:
        h, a = str(g["home"]["id"]), str(g["away"]["id"])
        if h in R and a in R:
            m, t = M.predict(R, lg, h, a, g.get("neutral"))
            g["ours"] = {"m": round(m, 1), "t": round(t, 1)}
    return {"games": n}


def record(games, season):
    """Finals, closing lines and our number for every game, kept all season (grades My Bets and our record)."""
    res = sync.load(f"results_{season}.json", {"season": season, "games": {}})
    for g in games:
        r = res["games"].setdefault(g["id"], {})
        r.update({"d": g["date"], "h": g["home"]["abbr"], "a": g["away"]["abbr"], "st": g["state"]})
        if g["state"] == "pre" and g.get("odds"):   # keeps updating until tip-off, then freezes = the closing line
            r.update({"cs": g["odds"].get("hs"), "ct": g["odds"].get("t"), "chml": g["odds"].get("hml"), "caml": g["odds"].get("aml")})
            if g.get("ours"): r.update({"om": g["ours"]["m"], "ot": g["ours"]["t"]})
        if g["state"] == "post": r.update({"hs": sync.num(g["home"]["score"]), "as": sync.num(g["away"]["score"])})
    sync.save(os.path.join(sync.DATA, f"results_{season}.json"), res)
    return res


def report(res):
    """How our number did against the closing line: spreads and totals, by edge size."""
    blank = lambda: {"w": 0, "l": 0, "push": 0}
    out = {"ats": {k: blank() for k in ("all", "3+", "5+")}, "tot": {k: blank() for k in ("all", "3+", "5+")}, "weeks": {}, "games": 0}
    for r in res["games"].values():
        if r.get("st") != "post" or r.get("hs") is None or r.get("om") is None: continue
        out["games"] += 1
        for kind, edge, result in (("ats", None if r.get("cs") is None else r["om"] + r["cs"], None if r.get("cs") is None else r["hs"] - r["as"] + r["cs"]),
                                   ("tot", None if r.get("ct") is None else r["ot"] - r["ct"], None if r.get("ct") is None else r["hs"] + r["as"] - r["ct"])):
            if edge is None or abs(edge) < 0.5: continue
            k = "push" if result == 0 else "w" if (edge > 0) == (result > 0) else "l"
            for b, th in (("all", 0), ("3+", 3), ("5+", 5)):
                if abs(edge) >= th: out[kind][b][k] += 1
    return out


def main():
    now = datetime.now(timezone.utc)
    if os.environ.get("CBB_TEST_DATE"): now = datetime.strptime(os.environ["CBB_TEST_DATE"], "%Y-%m-%d").replace(hour=17, tzinfo=timezone.utc)   # dry runs on a past day
    events, season, stype = [], None, 2
    for d in range(-1, 3):   # yesterday (late finals) through two days out, US dates
        day = (now - timedelta(hours=5) + timedelta(days=d)).strftime("%Y%m%d")
        try: sb = sync.get(f"{sync.ESPN}/scoreboard?groups=50&limit=400&dates={day}")
        except Exception as e: print("scoreboard failed", day, e, file=sys.stderr); continue
        season = (sb.get("season") or {}).get("year") or season or (now.year + (1 if now.month >= 8 else 0))
        stype = (sb.get("season") or {}).get("type", stype)
        seen = {e["id"] for e in events}
        events += [e for e in sb.get("events", []) if e["id"] not in seen]
    games = []
    for e in events:
        g = sync.parse_game(e, None, None)
        g.pop("city", None); g["indoor"] = True
        for x in e["competitions"][0]["competitors"]:
            t = g["home" if x["homeAway"] == "home" else "away"]
            rank = (x.get("curatedRank") or {}).get("current", 99)
            t["rank"] = rank if rank <= 25 else None; t["logo"] = x["team"].get("logo")
        # only games anyone bets: ranked teams or a posted line
        if g.get("odds") or g["home"].get("rank") or g["away"].get("rank"): games.append(g)
    games.sort(key=lambda g: g["date"])
    model_note = ours(games, season)
    try: nk = kalshi.apply(games, {})
    except Exception as ex: print("kalshi failed", ex, file=sys.stderr); nk = 0
    res = record(games, season)
    stamp = now.isoformat(timespec="seconds")
    slate = {"updatedAt": stamp, "sport": "cbb", "propParts": 0, "analytics": None, "season": season, "week": None, "seasonType": stype,
             "odds": {"source": "ESPN (DraftKings)"}, "report": None, "cfbModel": model_note, "cfbReport": report(res), "games": games}
    sync.save(os.path.join(sync.OUT, "slate.json"), slate)
    sync.save(os.path.join(sync.OUT, "results.json"), res)
    sync.save(os.path.join(sync.OUT, "history.json"), sync.line_history(games, season))   # Moves tab
    sync.save(os.path.join(sync.OUT, "manifest.json"), [{"collection": "slate", "doc_id": "current", "file": "slate.json"},
                                                        {"collection": "results", "doc_id": str(season), "file": "results.json"}])
    print(json.dumps({"sport": "cbb", "season": season, "games": len(games), "with_odds": sum(1 for g in games if g.get("odds")), "kalshi": nk}))


if __name__ == "__main__":
    main()
