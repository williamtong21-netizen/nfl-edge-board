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


def main():
    try:
        with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as f: cfg = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError): cfg = {}
    sb = sync.get(sync.ESPN + "/scoreboard?groups=80&limit=300")
    season, week = sb["season"]["year"], (sb.get("week") or {}).get("number")
    events = [(e, week) for e in sb["events"] if keep(e)]
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
    sync.weather(games)
    odds_meta = sync.multibook(games, cfg)
    for g in games: g.pop("city", None)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    slate = {"updatedAt": now, "sport": "cfb", "propParts": 0, "analytics": None, "season": season, "week": week,
             "seasonType": sb["season"].get("type", 2), "odds": odds_meta, "report": None, "games": games}
    sync.save(os.path.join(sync.OUT, "slate.json"), slate)
    sync.save(os.path.join(sync.OUT, "history.json"), {"season": season, "snaps": []})
    sync.save(os.path.join(sync.OUT, "results.json"), {"season": season, "games": {}})
    sync.save(os.path.join(sync.OUT, "manifest.json"), [{"collection": "slate", "doc_id": "current", "file": "slate.json"}])
    print(json.dumps({"sport": "cfb", "season": season, "week": week, "games": len(games),
                      "with_books": sum(1 for g in games if g.get("books")), "multibook": odds_meta}))


if __name__ == "__main__":
    main()
