"""Fast news alerts: runs every ~20 minutes (its own small workflow), separate from the 3-hour data sync.

One ESPN call gets every team's injury report; another gets the latest headlines. We alert the group channel when a
player who matters this week changes status (Questionable / Doubtful / Out / IR, or cleared after being out), or a
headline about him says he's ruled out, inactive, activated, starting, benched or suspended.
"Matters" = a starting QB or lead back, or anyone with a 25%+ anytime-TD chance in this week's props. Out / Doubtful / IR
and "cleared" always alert; Questionable only for QBs and 40%+ TD scorers within 48 hours of kickoff (midweek practice noise).
State (what was already sent, last status seen) lives in state/news_state.json. Same quiet hours as alerts.py.
A headline that rules a key player out is saved under "outs" (sync.news_outs marks him Out before ESPN's status catches up)
and asks the workflow to start a full sync right away (state/.dispatch_sync).
NBA (EDGE_SPORT=nba, its own data/state folders): "matters" = a points line of 14.5+ in tonight's props; Out / Doubtful
always alert and start a sync (nba_sync drops them from props); Questionable / Day-To-Day only for 18+ point scorers within 8
hours of tip (shootaround noise before that).
Run:  python news.py        (EDGE_OUT = the published data folder, EDGE_DATA = the state folder, EDGE_SPORT = nfl | nba)
"""
import glob, json, os, re, sys
from datetime import datetime, timezone, timedelta
from urllib.request import urlopen, Request

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.environ.get("EDGE_OUT") or os.path.join(ROOT, "out")
DATA = os.environ.get("EDGE_DATA") or os.path.join(ROOT, "data")
SPORT = os.environ.get("EDGE_SPORT", "nfl")
PATH = "basketball/nba" if SPORT == "nba" else "football/nfl"
INJ = f"https://site.api.espn.com/apis/site/v2/sports/{PATH}/injuries"
NEWS = f"https://site.api.espn.com/apis/site/v2/sports/{PATH}/news?limit=50"
DOWN = ("Questionable", "Doubtful", "Out", "Injured Reserve", "Suspended")
RANK = {"Active": 0, "Questionable": 1, "Day-To-Day": 1, "Doubtful": 2, "Out": 3, "Injured Reserve": 4, "Suspended": 4}
WORDS = re.compile(r"\b(ruled out|won't play|will not play|inactive|out for|placed on (?:ir|injured reserve)|activated|will start|to start|benched|suspended|questionable|doubtful|week-to-week|season-ending|torn|carted)\b", re.I)
MAX_PER_RUN = 3
OUT_WORDS = re.compile(r"\b(ruled out|won't play|will not play|inactive|out for (?:sunday|monday|thursday|saturday|the game|week))\b", re.I)
if SPORT == "nba":
    WORDS = re.compile(r"\b(ruled out|won't play|will not play|out tonight|out for|resting|questionable|doubtful|game-time decision|will start|to start|starting|returns?|available|suspended|torn|sprain|strain)\b", re.I)
    OUT_WORDS = re.compile(r"\b(ruled out|won't play|will not play|out tonight|out for (?:tonight|the game|monday|tuesday|wednesday|thursday|friday|saturday|sunday)|to rest|resting)\b", re.I)


def get(url):
    with urlopen(Request(url, headers={"User-Agent": "nfl-edge-board"}), timeout=20) as r: return json.loads(r.read().decode())


def key_players_nba():
    """{espn athlete id: {n, team, game, kick, pts, pos}}: players with a 14.5+ points line in the next 36 hours."""
    try:
        with open(os.path.join(OUT, "slate.json"), encoding="utf-8") as f: slate = json.load(f)
    except (FileNotFoundError, ValueError): return {}, {}
    now = datetime.now(timezone.utc)
    games = {g["id"]: g for g in slate.get("games", []) if g.get("state") == "pre" and timedelta(0) < datetime.fromisoformat(g["date"].replace("Z", "+00:00")) - now < timedelta(hours=36)}
    keys = {}
    for fn in glob.glob(os.path.join(OUT, "props-*.json")):
        with open(fn, encoding="utf-8") as f: parts = json.load(f).get("games", {})
        for gid, pls in parts.items():
            if gid not in games: continue
            g = games[gid]
            for pl in pls:
                pts = next((p["l"] for p in pl.get("props", []) if p.get("m") == "Points"), None)
                if pts is not None and pts >= 14.5:
                    keys[str(pl["id"])] = {"n": pl["n"], "team": pl.get("tm"), "game": f'{g["away"]["abbr"]} @ {g["home"]["abbr"]}', "kick": g["date"], "pts": pts, "pos": pl.get("p"), "td": None}
    return keys, games


def key_players():
    """{espn athlete id: {n, team, why}} for this week's games that haven't kicked off."""
    if SPORT == "nba": return key_players_nba()
    try:
        with open(os.path.join(OUT, "slate.json"), encoding="utf-8") as f: slate = json.load(f)
    except (FileNotFoundError, ValueError): return {}, {}
    now = datetime.now(timezone.utc)
    games = {g["id"]: g for g in slate.get("games", []) if g.get("state") == "pre" and datetime.fromisoformat(g["date"].replace("Z", "+00:00")) > now}
    keys = {}
    for g in games.values():
        for side in ("home", "away"):
            for i in g[side].get("inj") or []:
                if i.get("r") in ("starting QB", "leading rusher"): keys.setdefault(str(i.get("id") or ""), None)
    for fn in glob.glob(os.path.join(OUT, "props-*.json")):
        with open(fn, encoding="utf-8") as f: parts = json.load(f).get("games", {})
        for gid, pls in parts.items():
            if gid not in games: continue
            g = games[gid]
            for pl in pls:
                td = next((p for p in pl.get("props", []) if p.get("m") == "Anytime TD" and p.get("pOver")), None)
                big = pl.get("p") == "QB" and any(p.get("m") == "Pass yds" for p in pl.get("props", []))
                if big or (td and td["pOver"] >= 0.25):
                    keys[str(pl["id"])] = {"n": pl["n"], "team": pl.get("tm"), "game": f'{g["away"]["abbr"]} @ {g["home"]["abbr"]}', "kick": g["date"],
                                           "td": td["pOver"] if td else None, "pos": pl.get("p")}
    return {k: v for k, v in keys.items() if v}, games


def athlete_id(a):
    for l in (a or {}).get("links") or []:
        m = re.search(r"/id/(\d+)", l.get("href", ""))
        if m: return m.group(1)
    return None


def candidates(keys, st):
    out = []
    seen = st.setdefault("status", {})
    try: inj = get(INJ)
    except Exception as e: print("injuries failed", e, file=sys.stderr); inj = {}
    for t in inj.get("injuries", []):
        for i in t.get("injuries", []):
            aid = athlete_id(i.get("athlete")); k = keys.get(aid)
            if not k: continue
            new, old = i.get("status") or "Active", seen.get(aid, "Active")
            seen[aid] = new
            if new == old: continue
            worse, cleared = RANK.get(new, 0) > RANK.get(old, 0), RANK.get(old, 0) >= 2 and RANK.get(new, 0) == 0
            if not (worse or cleared): continue
            # Questionable is mostly midweek practice noise: only for QBs and big scorers (40%+ anytime TD), within 48 hours
            if new in ("Questionable", "Day-To-Day") and not cleared:
                try: left = datetime.fromisoformat(k["kick"].replace("Z", "+00:00")) - datetime.now(timezone.utc)
                except Exception: left = timedelta(days=9)
                if SPORT == "nba":
                    if not (left < timedelta(hours=8) and (k.get("pts") or 0) >= 18.5): continue
                elif not (left < timedelta(hours=48) and (k.get("pos") == "QB" or (k.get("td") or 0) >= 0.40)): continue
            if SPORT == "nba" and new in ("Out", "Doubtful") and not cleared:   # ruled out on the injury report: the props need a rebuild now
                st.setdefault("outs", {})[k["n"]] = {"tm": k["team"], "kick": k["kick"], "pos": k.get("pos"), "why": f"injury report: {new}", "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
                st["dispatch"] = True
            note = (i.get("shortComment") or "").strip()
            title = f'{k["n"]} ({k["team"]}): {"cleared to play" if cleared else new}'
            why = f'{k["pts"]} pts line' if k.get("pts") else f'{k["pos"]}, {round(k["td"] * 100)}% to score' if k.get("td") else k.get("pos") or ""
            out.append((1 if new in ("Out", "Doubtful", "Injured Reserve") or cleared else 2, f'inj:{aid}:{new}:{i.get("date", "")[:10]}', title,
                        f'{k["game"]} · {why}. {note[:220]}', "rotating_light" if worse else "white_check_mark"))
    try: news = get(NEWS).get("articles", [])
    except Exception as e: print("news failed", e, file=sys.stderr); news = []
    names = {v["n"].lower(): (aid, v) for aid, v in keys.items()}
    for a in news:
        if a.get("type") not in ("HeadlineNews", "Story", "Recap", None): continue
        hl = a.get("headline") or ""
        try: pub = datetime.fromisoformat(a["published"].replace("Z", "+00:00"))
        except Exception: continue
        if datetime.now(timezone.utc) - pub > timedelta(hours=6) or not WORDS.search(hl): continue
        hit = next(((aid, v) for nm, (aid, v) in names.items() if nm in hl.lower()), None)
        if not hit: continue
        aid, v = hit
        if OUT_WORDS.search(hl) and v["n"] not in st.setdefault("outs", {}):   # ruled out in the news: tell the next sync, and start one now
            st["outs"][v["n"]] = {"tm": v["team"], "kick": v["kick"], "pos": v.get("pos"), "why": hl[:160], "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            st["dispatch"] = True
        link = ((a.get("links") or {}).get("web") or {}).get("href")
        out.append((2, f'news:{a.get("id") or hl}', f'{v["n"]} news', f'{hl}. {v["game"]}.', "newspaper", link))
    return out


def main():
    sys.path.insert(0, ROOT)
    import alerts
    try:
        with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as f: cfg = json.load(f)
    except (FileNotFoundError, ValueError): cfg = {}
    top = alerts.topic(cfg)
    path = os.path.join(DATA, "news_state.json")
    try:
        with open(path, encoding="utf-8") as f: st = json.load(f)
    except (FileNotFoundError, ValueError): st = {}
    keys, _ = key_players()
    first = "sent" not in st           # first run: learn current statuses quietly
    sent = set(st.get("sent", []))
    try:
        from zoneinfo import ZoneInfo
        quiet = datetime.now(ZoneInfo("America/New_York")).hour < 8
    except Exception: quiet = False
    n = 0
    for c in sorted(candidates(keys, st), key=lambda x: x[0]):
        pri, key, title, body, tags = c[:5]; click = c[5] if len(c) > 5 and c[5] else alerts.APP_URL
        if key in sent: continue
        if not first and top and not quiet:
            if n >= MAX_PER_RUN: continue
            try: alerts.send(top, title, body, tags, click=click); n += 1
            except Exception as e: print("alert failed", e, file=sys.stderr); continue
        elif not first: continue
        sent.add(key)
    st["sent"] = sorted(sent)[-3000:]
    # drop "ruled out" notes once the game has started; ask the workflow to run a full sync when there's a new one
    now = datetime.now(timezone.utc)
    st["outs"] = {k: v for k, v in (st.get("outs") or {}).items() if datetime.fromisoformat(v["kick"].replace("Z", "+00:00")) > now}
    if st.pop("dispatch", False):
        with open(os.path.join(DATA, ".dispatch_sync"), "w") as fh: fh.write(now.isoformat())
    with open(path, "w", encoding="utf-8") as f: json.dump(st, f)
    print(json.dumps({"key_players": len(keys), "sent": n, "first": first, "quiet": quiet}))


if __name__ == "__main__":
    main()
