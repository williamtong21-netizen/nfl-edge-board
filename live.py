"""Live game tracker: scores, clock, down & distance, win probability and every player's live box score.

Writes out/live.json for the hosted board's db doc live/current. Prints "idle" (and writes nothing new)
when no game is live, about to start, or just finished, so the scheduled job can skip the push.
The page's local mode builds the exact same shape straight from ESPN in the browser (parseLive in edge-board.html).
Run:  python live.py
"""
import json, os, sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

from sync import get, ESPN, SUMMARY, OUT, save

LIVE_KEYS = {  # ESPN box score column -> our stat names
    "passing": ["completions/passingAttempts", "passingYards", None, "passingTouchdowns", "interceptions"],
    "rushing": ["rushingAttempts", "rushingYards", None, "rushingTouchdowns", "longRushing"],
    "receiving": ["receptions", "receivingYards", None, "receivingTouchdowns", "longReception", "receivingTargets"],
}


def num(v):
    try: return float(v)
    except (TypeError, ValueError): return None


def players(summary):
    out = {}
    for team in (summary.get("boxscore") or {}).get("players", []):
        abbr = team["team"].get("abbreviation")
        for cat in team.get("statistics", []):
            keys = cat.get("keys") or []
            if cat.get("name") not in LIVE_KEYS: continue
            for a in cat.get("athletes", []):
                p = out.setdefault(a["athlete"]["id"], {"n": a["athlete"].get("displayName"), "tm": abbr, "s": {}})
                for k, v in zip(keys, a.get("stats", [])):
                    if k == "completions/passingAttempts" and "/" in str(v):
                        c, att = str(v).split("/"); p["s"]["completions"], p["s"]["passingAttempts"] = num(c), num(att)
                    elif k in ("passingYards", "passingTouchdowns", "interceptions", "rushingAttempts", "rushingYards", "rushingTouchdowns",
                               "longRushing", "receptions", "receivingYards", "receivingTouchdowns", "longReception", "receivingTargets"):
                        p["s"][k] = num(v)
    return out


def game(ev, summary):
    c = ev["competitions"][0]
    side = {x["homeAway"]: x for x in c["competitors"]}
    st, sit = c["status"], c.get("situation") or {}
    by_id = {side[s]["team"]["id"]: side[s]["team"]["abbreviation"] for s in side}
    wp = None
    if summary and summary.get("winprobability"): wp = summary["winprobability"][-1].get("homeWinPercentage")
    elif (sit.get("lastPlay") or {}).get("probability"): wp = sit["lastPlay"]["probability"].get("homeWinPercentage")
    return {"id": ev["id"], "date": ev["date"], "state": st["type"]["state"], "period": st.get("period"), "clock": st.get("displayClock"),
            "detail": st["type"].get("shortDetail"),
            "home": {"abbr": side["home"]["team"]["abbreviation"], "score": side["home"].get("score")},
            "away": {"abbr": side["away"]["team"]["abbreviation"], "score": side["away"].get("score")},
            "poss": by_id.get(sit.get("possession")), "dd": sit.get("downDistanceText") or sit.get("shortDownDistanceText"),
            "rz": bool(sit.get("isRedZone")), "last": (sit.get("lastPlay") or {}).get("text"), "wp": wp,
            "players": players(summary) if summary else {}}


def main():
    now = datetime.now(timezone.utc)
    sb = get(ESPN + "/scoreboard")
    evs = [e for e in sb["events"] if abs(datetime.fromisoformat(e["date"].replace("Z", "+00:00")) - now) < timedelta(hours=14)]
    def summ(e):
        if e["competitions"][0]["status"]["type"]["state"] == "pre": return None
        try: return get(SUMMARY + e["id"])
        except Exception as ex:
            print("summary failed", e["id"], ex, file=sys.stderr); return None
    with ThreadPoolExecutor(8) as pool: sums = list(pool.map(summ, evs))
    games = [game(e, s) for e, s in zip(evs, sums)]
    active = any(g["state"] == "in" for g in games) or any(
        g["state"] == "pre" and datetime.fromisoformat(g["date"].replace("Z", "+00:00")) - now < timedelta(minutes=15) for g in games)
    just_done = any(g["state"] == "post" and now - datetime.fromisoformat(g["date"].replace("Z", "+00:00")) < timedelta(hours=4, minutes=30) for g in games)
    save(os.path.join(OUT, "live.json"), {"at": now.isoformat(timespec="seconds"), "games": games})
    print("live" if active or just_done else "idle", json.dumps({g["away"]["abbr"] + "@" + g["home"]["abbr"]: g["detail"] for g in games}))


if __name__ == "__main__":
    main()
