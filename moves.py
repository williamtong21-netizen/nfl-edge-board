"""Why did the line move? For each upcoming game whose spread or total has moved meaningfully since it opened:

when    the biggest single jumps in the line history (between which two syncs it happened)
why     candidates from free data, strongest first:
          news      ESPN team headlines from the last 5 days about injuries, starters, suspensions, weather...,
                    flagged "right before the move" when published in the 12 hours before a jump was recorded
          injuries  key players listed Out / Doubtful (QBs and players the sync marked as key roles)
          lineup    QB changes (college: who actually started; NFL: lineup_changes)
          weather   strong wind / rain in the forecast when the total dropped
        nothing found = most likely betting action (sharp money), which the app says plainly
Result: g["why"] = {"jumps": [...], "items": [...]}, shown in the Moves tab. Free: one ESPN news call per team involved.
"""
import json, re, sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.request import Request, urlopen

NEWS = "https://site.api.espn.com/apis/site/v2/sports/{path}/news?team={tid}&limit=30"
# the headline itself has to be about someone's status (who plays, who starts, who's hurt, weather, a trade)
WORDS = re.compile(r"\b((?<!-)out|ruled out|won'?t play|will not play|not play|doubtful|questionable|injur\w*|injured reserve|IR|"
                   r"(?:gets?|first career|to|will) start|start(?:s|ing)? at QB|starter|benched|bench|suspend\w*|return\w*|"
                   r"activate\w*|cleared|illness|concussion|hamstring|ankle|knee|groin|shoulder|blood clot|surgery|inactive|"
                   r"sidelined|miss(?:es|ing)?|status|unsure|uncertain|weather|wind|rain|snow|storm|traded|trade with|released|"
                   r"arrested|fired|interim)\b", re.I)
NOISE = re.compile(r"\b(betting|odds|how to bet|lines and totals|picks|predictions?|power rankings?|fantasy|highlights|recruit\w*|"
                   r"mock draft|bubble watch|hot seat|never won|things .{0,30}thinks|roster holes|gameday|schedule|tracking records|"
                   r"what .{0,40} means|revisiting|playoff)\b", re.I)
STRONG = re.compile(r"\b(ruled out|(?<!-)out|won'?t play|will not play|to start|gets? start|first career start|starter|benched|suspended|traded)\b", re.I)
# a line move worth explaining (points since open): NFL / NBA vs college
MIN_MOVE = {"football/nfl": (1.5, 2.0), "football/college-football": (2.5, 3.0), "basketball/nba": (1.5, 3.0),
            "basketball/mens-college-basketball": (2.5, 4.0)}


def _ts(s):
    try: return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception: return None


def team_news(path, tids):
    """{team id: [{t, text, kind, url}]} for the last 5 days, keyword-filtered."""
    cut = datetime.now(timezone.utc) - timedelta(days=5)
    def one(tid):
        try:
            d = json.loads(urlopen(Request(NEWS.format(path=path, tid=tid), headers={"User-Agent": "edge-board"}), timeout=20).read())
        except Exception as e:
            print("news failed", tid, e, file=sys.stderr); return tid, []
        out = []
        for a in d.get("articles", []):
            t = _ts(a.get("published") or "")
            h = (a.get("headline") or "").strip()
            if not t or t < cut or not h or NOISE.search(h): continue
            if not WORDS.search(h): continue
            out.append({"t": t.isoformat(timespec="seconds"), "text": h, "kind": "news" if a.get("type") != "Media" else "media",
                        "url": ((a.get("links") or {}).get("web") or {}).get("href")})
        return tid, out
    with ThreadPoolExecutor(8) as pool:
        return dict(pool.map(one, sorted(set(tids))))


def jumps(gid, snaps, open_vals):
    """the biggest single moves between consecutive snapshots (open -> first snapshot counts, time unknown)."""
    pts = [(None, open_vals)] + [(s["t"], s["g"][gid]) for s in snaps if gid in s["g"]]
    out = []
    for (t0, a), (t1, b) in zip(pts, pts[1:]):
        sp = None if a[0] is None or b[0] is None else round(b[0] - a[0], 1)
        tt = None if a[1] is None or b[1] is None else round(b[1] - a[1], 1)
        size = abs(sp or 0) + 0.6 * abs(tt or 0)
        if size >= 1: out.append({"t0": t0, "t1": t1, "sp": sp, "tt": tt, "size": round(size, 1)})
    return sorted(out, key=lambda j: -j["size"])[:2]


def explain(games, snaps, path):
    lo_sp, lo_tt = MIN_MOVE.get(path, (2.0, 3.0))
    todo = []
    for g in games:
        o = g.get("odds") or {}
        if g.get("state") != "pre" or not o: continue
        sp = None if o.get("hs") is None or o.get("hso") is None else o["hs"] - o["hso"]
        tt = None if o.get("t") is None or o.get("to") is None else o["t"] - o["to"]
        if abs(sp or 0) >= lo_sp or abs(tt or 0) >= lo_tt: todo.append((g, sp, tt))
    if not todo: return 0
    tids = [g[s].get("id") for g, _, _ in todo for s in ("home", "away") if g[s].get("id")]
    news = team_news(path, tids)
    for g, sp, tt in todo:
        o = g["odds"]
        jl = jumps(g["id"], snaps, [o.get("hso"), o.get("to")])
        items = []
        # "right before the move": news from 6 hours before the last sync that still had the old line, up to the sync
        # that saw the new one (moves from the open to the first recorded line have no known start, so they don't count)
        win = [(_ts(j["t0"]) - timedelta(hours=6), _ts(j["t1"])) for j in jl if j["t0"] and j["t1"]]
        # a headline counts only if it names one of the two teams (the team feeds also carry other teams' stories)
        names = set()
        for s in ("away", "home"):
            t_ = g[s]; full = t_.get("full") or ""; nm = t_.get("name") or ""
            names |= {x for x in (nm, full.replace(nm, "").strip(), t_.get("abbr")) if x and len(x) >= 3}
        about = re.compile(r"\b(" + "|".join(re.escape(x) for x in sorted(names, key=len, reverse=True)) + r")", re.I) if names else None
        for s in ("away", "home"):
            for n in news.get(g[s].get("id"), []):
                if about and not about.search(n["text"]): continue
                t = _ts(n["t"])
                near = any(a <= t <= b for a, b in win)
                items.append({**n, "team": g[s]["abbr"], "near": near})
            for i in g[s].get("inj") or []:
                st = (i.get("s") or "").lower()
                if ("out" in st or "doubtful" in st or "reserve" in st) and (i.get("p") == "QB" or i.get("r")):
                    items.append({"kind": "inj", "team": g[s]["abbr"], "text": f"{i['n']} ({i.get('p', '')}) {i.get('s', '').lower()}" + (f", {i['t']}" if i.get("t") else "")})
            q = (g.get("qb") or {}).get(s)
            if q: items.append({"kind": "lineup", "team": g[s]["abbr"], "text": f"QB change: {q['new']} started over {q['old']} last game"})
        for s, ch in (g.get("lineup") or {}).items():
            for k in ch:
                if (k.get("role") == "QB" or k.get("k") == "qb") and k.get("main") and k.get("n") and k["n"] != k["main"]:
                    items.append({"kind": "lineup", "team": g[s]["abbr"], "text": f"QB change: {k['n']} expected to start for {k['main']}"})
        wx = g.get("wx") or {}
        if not g.get("indoor") and (tt or 0) <= -1 and ((wx.get("wind") or 0) >= 15 or (wx.get("gust") or 0) >= 28 or (wx.get("pop") or 0) >= 50):
            items.append({"kind": "weather", "team": "", "text": f"Forecast: wind {round(wx.get('wind') or 0)} mph, gusts {round(wx.get('gust') or 0)}"
                                                               + (f", {wx['pop']}% chance of rain" if (wx.get("pop") or 0) >= 50 else "") + " (wind and rain push totals down)"})
        # strongest first: news right before a jump, injuries / lineup, other news (newest first), weather
        # strongest first: news right before a jump; then headlines saying who's out / starting and injury listings;
        # then last game's lineup changes; other news (newest first); weather
        def rank(x):
            if x.get("near"): return 0
            if x["kind"] == "inj" or x["kind"] in ("news", "media") and STRONG.search(x["text"]): return 1
            return {"lineup": 1.5, "weather": 3}.get(x["kind"], 2)
        items = sorted(items, key=lambda x: (rank(x), -(_ts(x["t"]).timestamp() if x.get("t") else 0)))
        seen, keep = set(), []
        for x in items:
            k = re.sub(r"\W+", " ", x["text"].lower())[:60]
            if k in seen: continue
            seen.add(k); keep.append(x)
        g["why"] = {"jumps": jl, "items": keep[:5]}
    return len(todo)
