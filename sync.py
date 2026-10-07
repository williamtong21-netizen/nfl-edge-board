"""Pull NFL lines, injuries, matchups, weather and (optionally) multi-book odds,
and write the three JSON documents the hosted Edge Board reads:

  out/slate.json    -> db slate/current     this week's games, everything the board shows
  out/history.json  -> db history/current   line snapshots for this week (one per run)
  out/results.json  -> db results/<season>  finals + closing lines, used to grade bets and CLV

Local archives live in data/ so nothing is lost when the week rolls over.
Run:  python sync.py
"""
import json, os, sys, time
from functools import reduce
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from urllib.request import urlopen, Request
from urllib.parse import urlencode

import analytics, projections, trends, traps, kalshi, grades, situational

ROOT = os.path.dirname(os.path.abspath(__file__))
# EDGE_OUT / EDGE_DATA let the GitHub Actions job write into the app repo's own folders
OUT = os.environ.get("EDGE_OUT") or os.path.join(ROOT, "out")
DATA = os.environ.get("EDGE_DATA") or os.path.join(ROOT, "data")
os.makedirs(OUT, exist_ok=True); os.makedirs(DATA, exist_ok=True)

ESPN = "https://site.api.espn.com/apis/site/v2/sports/football/nfl"
SUMMARY = "https://site.web.api.espn.com/apis/site/v2/sports/football/nfl/summary?event="
ODDS_API = "https://api.the-odds-api.com/v4/sports/americanfootball_nfl/odds"
ROLES = {"passingYards": "starting QB", "rushingYards": "leading rusher",
         "receivingYards": "leading receiver", "sacks": "sacks leader"}
BOOKS = "draftkings,fanduel,betmgm,caesars,espnbet,fanatics,betrivers,bovada,pinnacle"


def get(url, headers=False, tries=3):
    req = Request(url, headers={"User-Agent": "Mozilla/5.0 nfl-edge-board"})
    for n in range(tries):
        try:
            with urlopen(req, timeout=20) as r:
                body = json.loads(r.read().decode("utf-8"))
                return (body, dict(r.headers)) if headers else body
        except OSError:
            if n == tries - 1: raise
            time.sleep(1.5 * (n + 1))


def load(name, default):
    p = os.path.join(DATA, name)
    try:
        with open(p, encoding="utf-8") as f: return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError): return default


def save(path, obj):
    with open(path, "w", encoding="utf-8") as f: json.dump(obj, f, separators=(",", ":"), ensure_ascii=False)


def num(s):
    if s is None: return None
    try: return float(str(s).lstrip("ouOU").replace("+", "")) if str(s).strip() not in ("", "EVEN") else 100.0
    except ValueError: return None


def ml(s):
    if s is None: return None
    s = str(s).strip()
    if s.upper() == "EVEN": return 100
    try: return int(float(s))
    except ValueError: return None


# ---------------------------------------------------------------- ESPN
def parse_team(c):
    t = c["team"]
    rec = {r.get("type") or r.get("name"): r.get("summary") for r in c.get("records", [])}
    return {"id": t["id"], "abbr": t.get("abbreviation"), "name": t.get("shortDisplayName") or t.get("name"),
            "full": t.get("displayName"), "color": t.get("color"), "alt": t.get("alternateColor"),
            "score": c.get("score"), "rec": rec.get("total") or rec.get("overall") or "",
            "homeRec": rec.get("home", ""), "roadRec": rec.get("road", ""),
            "inj": [], "stats": None, "form": []}


def parse_game(ev, summ, week):
    c = ev["competitions"][0]
    side = {x["homeAway"]: x for x in c["competitors"]}
    g = {"id": ev["id"], "date": ev["date"], "week": week, "state": c["status"]["type"]["state"],
         "detail": c["status"]["type"].get("shortDetail"),
         "venue": c.get("venue", {}).get("fullName"), "indoor": bool(c.get("venue", {}).get("indoor")), "neutral": bool(c.get("neutralSite")),
         "city": c.get("venue", {}).get("address", {}),
         "tv": ", ".join(n for b in c.get("broadcasts", []) for n in b.get("names", [])),
         "home": parse_team(side["home"]), "away": parse_team(side["away"]), "odds": None, "model": None}
    o = (c.get("odds") or (summ or {}).get("pickcenter") or [None])[0]
    if o:
        ps, tt, m = o.get("pointSpread", {}), o.get("total", {}), o.get("moneyline", {})
        dig = lambda d, *k: reduce(lambda a, b: (a or {}).get(b), k, d)
        g["odds"] = {
            "book": (o.get("provider") or {}).get("name", "DraftKings"),
            "hs": num(dig(ps, "home", "close", "line")) if dig(ps, "home", "close", "line") else o.get("spread"),
            "hso": num(dig(ps, "home", "open", "line")),
            "hsp": ml(dig(ps, "home", "close", "odds")), "asp": ml(dig(ps, "away", "close", "odds")),
            "hspo": ml(dig(ps, "home", "open", "odds")), "aspo": ml(dig(ps, "away", "open", "odds")),
            "t": num(dig(tt, "over", "close", "line")) if dig(tt, "over", "close", "line") else o.get("overUnder"),
            "to": num(dig(tt, "over", "open", "line")),
            "op": ml(dig(tt, "over", "close", "odds")), "up": ml(dig(tt, "under", "close", "odds")),
            "opo": ml(dig(tt, "over", "open", "odds")), "upo": ml(dig(tt, "under", "open", "odds")),
            "hml": ml(dig(m, "home", "close", "odds")), "aml": ml(dig(m, "away", "close", "odds")),
            "hmlo": ml(dig(m, "home", "open", "odds")), "amlo": ml(dig(m, "away", "open", "odds")),
        }
    if not summ: return g
    pr = summ.get("predictor") or {}
    if (pr.get("homeTeam") or {}).get("gameProjection"):
        g["model"] = {"h": float(pr["homeTeam"]["gameProjection"]) / 100, "a": float(pr["awayTeam"]["gameProjection"]) / 100}
    by_id = {g["home"]["id"]: g["home"], g["away"]["id"]: g["away"]}
    roles = {}
    for t in summ.get("leaders", []):
        for cat in t.get("leaders", []):
            a = ((cat.get("leaders") or [{}])[0]).get("athlete")
            if a and cat.get("name") in ROLES: roles[a["id"]] = ROLES[cat["name"]]
    for t in summ.get("injuries", []):
        tm = by_id.get(t["team"]["id"])
        if tm: tm["inj"] = [{"n": i["athlete"]["displayName"], "p": (i["athlete"].get("position") or {}).get("abbreviation", ""),
                             "s": i.get("status", ""), "t": (i.get("details") or {}).get("type", ""),
                             "d": (i.get("details") or {}).get("detail", ""), "r": roles.get(i["athlete"]["id"])}
                            for i in t.get("injuries", [])]
    for t in (summ.get("boxscore") or {}).get("teams", []):
        tm = by_id.get(t["team"]["id"])
        if tm: tm["stats"] = {s["name"]: num(s.get("displayValue")) for s in t.get("statistics", [])}
    season_start = datetime(int(ev["date"][:4]) - (1 if int(ev["date"][5:7]) < 4 else 0), 9, 1, tzinfo=timezone.utc)
    for t in summ.get("lastFiveGames", []):
        tm = by_id.get((t.get("team") or {}).get("id"))
        if tm: tm["form"] = [{"r": e.get("gameResult"), "s": e.get("score"), "o": (e.get("opponent") or {}).get("abbreviation"),
                              "at": e.get("atVs")} for e in t.get("events", [])
                             if datetime.fromisoformat(e["gameDate"].replace("Z", "+00:00")) >= season_start]
    return g


# ---------------------------------------------------------------- weather (Open-Meteo, free, no key)
def weather(games):
    geo = load("geo_cache.json", {})
    now = datetime.now(timezone.utc)
    for g in games:
        kick = datetime.fromisoformat(g["date"].replace("Z", "+00:00"))
        if g["indoor"] or g["state"] != "pre" or kick - now > timedelta(days=7): continue
        a = g["city"] or {}
        key = f'{a.get("city")}|{a.get("state")}|{a.get("country")}'
        try:
            if key not in geo:
                res = get("https://geocoding-api.open-meteo.com/v1/search?" + urlencode({"name": a.get("city"), "count": 10})).get("results", [])
                st = (a.get("state") or "").lower()
                pick = next((r for r in res if st and st in (r.get("admin1", "").lower(), r.get("admin1_code", "").lower())), None) \
                    or next((r for r in res if r.get("country_code") == "US"), None) or (res[0] if res else None)
                geo[key] = [pick["latitude"], pick["longitude"]] if pick else None
            if not geo[key]: continue
            lat, lon = geo[key]
            day = kick.strftime("%Y-%m-%d")
            w = get("https://api.open-meteo.com/v1/forecast?" + urlencode({
                "latitude": lat, "longitude": lon, "timezone": "UTC", "start_date": day, "end_date": day,
                "hourly": "temperature_2m,precipitation_probability,precipitation,wind_speed_10m,wind_gusts_10m",
                "wind_speed_unit": "mph", "temperature_unit": "fahrenheit", "precipitation_unit": "inch"}))["hourly"]
            # average over the ~3 hours of the game
            idx = [i for i, t in enumerate(w["time"]) if 0 <= (datetime.fromisoformat(t + ":00+00:00") - kick).total_seconds() < 3 * 3600]
            if not idx: continue
            avg = lambda k: round(sum((w[k][i] or 0) for i in idx) / len(idx), 1)
            g["wx"] = {"temp": avg("temperature_2m"), "wind": avg("wind_speed_10m"), "gust": max((w["wind_gusts_10m"][i] or 0) for i in idx),
                       "pop": max((w["precipitation_probability"][i] or 0) for i in idx), "rain": round(sum((w["precipitation"][i] or 0) for i in idx), 2)}
        except Exception as e:
            print("weather failed", g["id"], e, file=sys.stderr)
    save(os.path.join(DATA, "geo_cache.json"), geo)


# ---------------------------------------------------------------- The Odds API (optional key)
def multibook(games, cfg):
    key = (cfg.get("odds_api_key") or os.environ.get("ODDS_API_KEY") or "").strip()
    cache = load("odds_cache.json", {"t": 0, "events": [], "remaining": None})
    if key and time.time() - cache["t"] >= float(cfg.get("odds_api_min_hours", os.environ.get("ODDS_API_MIN_HOURS", 6))) * 3600:
        try:
            ev, hdr = get(ODDS_API + "?" + urlencode({"apiKey": key, "regions": "us", "markets": "h2h,spreads,totals",
                                                       "oddsFormat": "american", "bookmakers": BOOKS, "includeLinks": "true"}), headers=True)
            cache = {"t": time.time(), "events": ev, "remaining": hdr.get("x-requests-remaining") or hdr.get("X-Requests-Remaining")}
            save(os.path.join(DATA, "odds_cache.json"), cache)
        except Exception as e:
            print("odds api failed", e, file=sys.stderr)
    for g in games:
        kick = datetime.fromisoformat(g["date"].replace("Z", "+00:00"))
        ev = next((e for e in cache["events"] if e["home_team"] == g["home"]["full"] and e["away_team"] == g["away"]["full"]
                   and abs((datetime.fromisoformat(e["commence_time"].replace("Z", "+00:00")) - kick).total_seconds()) < 86400), None)
        if not ev: continue
        books = []
        for b in ev.get("bookmakers", []):
            row = {"k": b["key"], "n": b["title"]}
            lk = {"ev": b.get("link")} if b.get("link") else {}     # bet-slip links: tap one and the book opens with that bet in the slip
            for m in b.get("markets", []):
                for oc in m.get("outcomes", []):
                    if m["key"] == "h2h":
                        k = "hml" if oc["name"] == ev["home_team"] else "aml"; row[k] = oc["price"]
                    elif m["key"] == "spreads":
                        p = "h" if oc["name"] == ev["home_team"] else "a"; k = p + "s"
                        row[p + "s"], row[p + "sp"] = oc.get("point"), oc["price"]
                    elif m["key"] == "totals":
                        k = "op" if oc["name"] == "Over" else "up"
                        row["t"] = oc.get("point"); row[k] = oc["price"]
                    else: continue
                    if oc.get("link"): lk[k] = oc["link"]
            if lk: row["lk"] = lk
            books.append(row)
        g["books"] = books
    return {"at": cache["t"] or None, "remaining": cache.get("remaining"), "enabled": bool(key)}


PROP_API_MARKETS = {  # The Odds API market -> our prop name
    "player_pass_yds": "Pass yds", "player_pass_tds": "Pass TD", "player_pass_completions": "Completions", "player_pass_attempts": "Pass att",
    "player_pass_interceptions": "INT", "player_rush_yds": "Rush yds", "player_rush_attempts": "Carries", "player_reception_yds": "Rec yds",
    "player_receptions": "Receptions", "player_rush_reception_yds": "Rush+rec yds", "player_anytime_td": "Anytime TD",
}
norm_name = lambda n: " ".join(w for w in "".join(c for c in (n or "").lower() if c.isalnum() or c == " ").split() if w not in ("jr", "sr", "ii", "iii", "iv", "v"))


def multibook_props(games, props, cfg):
    """Every book's player-prop prices, attached to our props as pr['books'] = [{n, l, o, u}].
    Costs ~11 API credits per game, so it is off unless config "odds_api_props" is true, and only covers games
    kicking off within 3 days, refreshed at most every odds_api_props_min_hours (default 12)."""
    key = (cfg.get("odds_api_key") or os.environ.get("ODDS_API_KEY") or "").strip()
    on = cfg.get("odds_api_props", os.environ.get("ODDS_API_PROPS") == "1")
    cache = load("odds_props_cache.json", {})
    now = datetime.now(timezone.utc)
    if key and on:
        try:
            events = get("https://api.the-odds-api.com/v4/sports/americanfootball_nfl/events?" + urlencode({"apiKey": key}))  # free call
        except Exception as e:
            print("odds api events failed", e, file=sys.stderr); events = []
        for g in games:
            kick = datetime.fromisoformat(g["date"].replace("Z", "+00:00"))
            if g["state"] != "pre" or kick - now > timedelta(days=3) or g["id"] not in props: continue
            hit = cache.get(g["id"])
            if hit and time.time() - hit["t"] < cfg.get("odds_api_props_min_hours", 12) * 3600: continue
            ev = next((e for e in events if e["home_team"] == g["home"]["full"] and e["away_team"] == g["away"]["full"]), None)
            if not ev: continue
            try:
                data = get(f"https://api.the-odds-api.com/v4/sports/americanfootball_nfl/events/{ev['id']}/odds?" + urlencode(
                    {"apiKey": key, "regions": "us", "markets": ",".join(PROP_API_MARKETS), "oddsFormat": "american", "bookmakers": BOOKS, "includeLinks": "true"}))
                cache[g["id"]] = {"t": time.time(), "data": data}
            except Exception as e:
                print("odds api props failed", g["id"], e, file=sys.stderr)
        save(os.path.join(DATA, "odds_props_cache.json"), cache)
    attach_prop_books(props, cache)


def attach_prop_books(props, cache, only=None):
    """Adds each book's line, prices and bet-slip links to our props: pr['books'] = [{n, l, o, u, lo, lu}]."""
    for gid, rows in props.items():
        data = (cache.get(gid) or {}).get("data")
        if not data: continue
        by_player = {norm_name(pl["n"]): pl for pl in rows}
        for b in data.get("bookmakers", []):
            for m in b.get("markets", []):
                ours = PROP_API_MARKETS.get(m["key"])
                if not ours or (only and ours != only): continue
                lines = {}
                for oc in m.get("outcomes", []):
                    pl = by_player.get(norm_name(oc.get("description")))
                    if not pl: continue
                    row = lines.setdefault(pl["n"], {"n": b["title"], "l": oc.get("point", 0.5)})
                    if oc["name"] in ("Over", "Yes"):
                        row["o"] = oc["price"]
                        if oc.get("link"): row["lo"] = oc["link"]
                    elif oc["name"] in ("Under", "No"):
                        row["u"] = oc["price"]
                        if oc.get("link"): row["lu"] = oc["link"]
                for name, row in lines.items():
                    pl = by_player[norm_name(name)]
                    pr = next((p for p in pl["props"] if p["m"] == ours), None)
                    if pr is not None and not any(x["n"] == row["n"] for x in pr.get("books", [])): pr.setdefault("books", []).append(row)


TD_RESERVE = 80   # credits kept back each month: below this, TD prices refresh on game day only


def multibook_tds(games, props, cfg):
    """Anytime-TD prices and bet-slip links from every book, 1 credit per game per pull. Starting the morning of the
    week's first game (Thursday), every game that week is pulled, then refreshed at least every 48 hours, daily while FanDuel or DraftKings hasn't posted it yet,
    and once more on game day (kickoff within 14 hours). If the month's credits run low (under TD_RESERVE), only
    game-day pulls run. On by default with an Odds API key; config "odds_api_tds": false or env ODDS_API_TDS=0 turns it off.
    Env ODDS_API_TDS_NOW=1 refreshes every upcoming game right away."""
    key = (cfg.get("odds_api_key") or os.environ.get("ODDS_API_KEY") or "").strip()
    on = cfg.get("odds_api_tds", os.environ.get("ODDS_API_TDS", "1") != "0")
    now_all = os.environ.get("ODDS_API_TDS_NOW") == "1"
    cache = load("odds_td_cache.json", {})
    meta = cache.pop("_meta", {})
    now = datetime.now(timezone.utc)
    if key and on:
        low = meta.get("remaining") is not None and int(meta["remaining"]) < TD_RESERVE
        # each week's pulls start the morning of that week's first game (usually Thursday night), for every game that week
        kicks = lambda g: datetime.fromisoformat(g["date"].replace("Z", "+00:00"))
        opens = {}
        for g in games: opens[g["week"]] = min(opens.get(g["week"], kicks(g)), kicks(g))
        due = []
        for g in games:
            kick = kicks(g)
            if g["state"] != "pre" or g["id"] not in props: continue
            if now < opens[g["week"]] - timedelta(hours=14) and not now_all: continue   # that week hasn't started yet
            hit = cache.get(g["id"])
            age = (time.time() - hit["t"]) / 3600 if hit else 1e9
            books = {b.get("key") for b in ((hit or {}).get("data") or {}).get("bookmakers", [])}
            game_day = kick - now < timedelta(hours=14)
            if now_all and age >= 1: due.append(g); continue
            if game_day and age >= 6: due.append(g); continue          # last look before kickoff
            if low: continue                                            # saving credits: game day only
            if age >= 48 or (age >= 20 and not {"fanduel", "draftkings"} <= books): due.append(g)
        if due:
            try: events = get("https://api.the-odds-api.com/v4/sports/americanfootball_nfl/events?" + urlencode({"apiKey": key}))  # free call
            except Exception as e: print("odds api events failed", e, file=sys.stderr); events = []
            for g in due:
                ev = next((e for e in events if e["home_team"] == g["home"]["full"] and e["away_team"] == g["away"]["full"]), None)
                if not ev: continue
                try:
                    data, hdr = get(f"https://api.the-odds-api.com/v4/sports/americanfootball_nfl/events/{ev['id']}/odds?" + urlencode(
                        {"apiKey": key, "regions": "us", "markets": "player_anytime_td", "oddsFormat": "american", "bookmakers": BOOKS, "includeLinks": "true"}), headers=True)
                    cache[g["id"]] = {"t": time.time(), "data": data}
                    rem = hdr.get("x-requests-remaining") or hdr.get("X-Requests-Remaining")
                    if rem is not None: meta = {"remaining": int(float(rem)), "t": time.time()}
                except Exception as e:
                    print("odds api tds failed", g["id"], e, file=sys.stderr)
        # drop finished games so the cache stays small
        live_ids = {g["id"] for g in games if g["state"] != "post"}
        for gid in [k for k in cache if k not in live_ids]: cache.pop(gid, None)
        save(os.path.join(DATA, "odds_td_cache.json"), {**cache, "_meta": meta})
    attach_prop_books(props, cache, only="Anytime TD")
    return sum(1 for g in games if g["id"] in cache)


ALT_MARKETS = {"player_pass_yds_alternate": "Pass yds", "player_rush_yds_alternate": "Rush yds", "player_reception_yds_alternate": "Rec yds",
               "player_receptions_alternate": "Receptions", "player_rush_reception_yds_alternate": "Rush+rec yds", "player_pass_tds_alternate": "Pass TD"}
ALT_GAME_MARKETS = ("alternate_spreads", "alternate_totals", "alternate_team_totals")


def multibook_alts(games, props, cfg):
    """Every sportsbook's alternate-line ladders (FanDuel, DraftKings and the rest) for props, spreads, totals and team totals.
    Costs ~9 API requests per game, so it is off unless config "odds_api_alts" (or env ODDS_API_ALTS=1) is on, covers games
    within odds_api_alts_hours of kickoff (default 48), and refreshes at most every odds_api_alts_min_hours (default 12).
    Writes pr["abooks"] = {book: [{l, o, u}]} and g["abooks"] = {book: {"spread": {abbr: [{l, p}]}, "total": [{l, o, u}], "tt": {abbr: [...]}}}."""
    key = (cfg.get("odds_api_key") or os.environ.get("ODDS_API_KEY") or "").strip()
    on = cfg.get("odds_api_alts", os.environ.get("ODDS_API_ALTS") == "1")
    hours = float(cfg.get("odds_api_alts_hours", os.environ.get("ODDS_API_ALTS_HOURS", 48)))
    cache = load("odds_alts_cache.json", {})
    now = datetime.now(timezone.utc)
    if key and on:
        try: events = get("https://api.the-odds-api.com/v4/sports/americanfootball_nfl/events?" + urlencode({"apiKey": key}))
        except Exception as e:
            print("odds api events failed", e, file=sys.stderr); events = []
        for g in games:
            kick = datetime.fromisoformat(g["date"].replace("Z", "+00:00"))
            if g["state"] != "pre" or kick - now > timedelta(hours=hours): continue
            hit = cache.get(g["id"])
            if hit and time.time() - hit["t"] < float(cfg.get("odds_api_alts_min_hours", 12)) * 3600: continue
            ev = next((e for e in events if e["home_team"] == g["home"]["full"] and e["away_team"] == g["away"]["full"]), None)
            if not ev: continue
            try:
                data = get(f"https://api.the-odds-api.com/v4/sports/americanfootball_nfl/events/{ev['id']}/odds?" + urlencode(
                    {"apiKey": key, "regions": "us", "markets": ",".join(list(ALT_MARKETS) + list(ALT_GAME_MARKETS)), "oddsFormat": "american", "bookmakers": BOOKS}))
                cache[g["id"]] = {"t": time.time(), "data": data}
            except Exception as e:
                print("odds api alternates failed", g["id"], e, file=sys.stderr)
        save(os.path.join(DATA, "odds_alts_cache.json"), cache)
    by_id = {g["id"]: g for g in games}
    for gid, hit in cache.items():
        g, data = by_id.get(gid), (hit or {}).get("data")
        if not g or not data or g["state"] != "pre": continue
        full = {g["home"]["full"]: g["home"]["abbr"], g["away"]["full"]: g["away"]["abbr"]}
        players = {norm_name(pl["n"]): pl for pl in props.get(gid, [])}
        gb = {}
        for b in data.get("bookmakers", []):
            book = b["title"]
            for m in b.get("markets", []):
                if m["key"] in ALT_MARKETS:
                    ladders = {}
                    for oc in m.get("outcomes", []):
                        pl = players.get(norm_name(oc.get("description")))
                        if not pl or oc.get("point") is None: continue
                        row = ladders.setdefault(pl["n"], {}).setdefault(oc["point"], {"l": oc["point"]})
                        row["o" if oc["name"] == "Over" else "u"] = oc["price"]
                    for name, rungs in ladders.items():
                        pl = players[norm_name(name)]
                        pr = next((x for x in pl["props"] if x["m"] == ALT_MARKETS[m["key"]]), None)
                        if pr is not None: pr.setdefault("abooks", {})[book] = sorted(rungs.values(), key=lambda r: r["l"])
                elif m["key"] == "alternate_spreads":
                    for oc in m.get("outcomes", []):
                        t = full.get(oc["name"])
                        if t and oc.get("point") is not None: gb.setdefault(book, {}).setdefault("spread", {}).setdefault(t, []).append({"l": oc["point"], "p": oc["price"]})
                elif m["key"] == "alternate_totals":
                    rows = {}
                    for oc in m.get("outcomes", []):
                        if oc.get("point") is None: continue
                        rows.setdefault(oc["point"], {"l": oc["point"]})["o" if oc["name"] == "Over" else "u"] = oc["price"]
                    gb.setdefault(book, {})["total"] = sorted(rows.values(), key=lambda r: r["l"])
                elif m["key"] == "alternate_team_totals":
                    rows = {}
                    for oc in m.get("outcomes", []):
                        t = full.get(oc.get("description"))
                        if t and oc.get("point") is not None: rows.setdefault(t, {}).setdefault(oc["point"], {"l": oc["point"]})["o" if oc["name"] == "Over" else "u"] = oc["price"]
                    gb.setdefault(book, {})["tt"] = {t: sorted(v.values(), key=lambda r: r["l"]) for t, v in rows.items()}
        for book in gb:
            for t in gb[book].get("spread", {}): gb[book]["spread"][t].sort(key=lambda r: r["l"])
        if gb: g["abooks"] = gb


def summary(ev):
    try: return get(SUMMARY + ev["id"])
    except Exception as e:
        print("summary failed", ev["id"], e, file=sys.stderr); return None


# ---------------------------------------------------------------- player props (DraftKings lines via ESPN) + game logs
CORE = "https://sports.core.api.espn.com/v2/sports/football/leagues/nfl"
GAMELOG = "https://site.web.api.espn.com/apis/common/v3/sports/football/nfl/athletes/{}/gamelog"
PROP_MARKETS = {
    "Total Passing Yards (incl. overtime)": ("Pass yds", ["passingYards"]),
    "Total Passing Touchdowns (incl. overtime)": ("Pass TD", ["passingTouchdowns"]),
    "Total Pass Completions (incl. overtime)": ("Completions", ["completions"]),
    "Total Passing Attempts (incl. overtime)": ("Pass att", ["passingAttempts"]),
    "Total Passing Interceptions (incl. overtime)": ("INT", ["interceptions"]),
    "Total Rushing Yards (incl. overtime)": ("Rush yds", ["rushingYards"]),
    "Total Carries (incl. overtime)": ("Carries", ["rushingAttempts"]),
    "Total Receiving Yards (incl. overtime)": ("Rec yds", ["receivingYards"]),
    "Total Receptions (incl. overtime)": ("Receptions", ["receptions"]),
    "Total Rushing Plus Receiving Yards (incl. overtime)": ("Rush+rec yds", ["rushingYards", "receivingYards"]),
    "Total Passing Plus Rushing Yards (incl. overtime)": ("Pass+rush yds", ["passingYards", "rushingYards"]),
    "Longest Reception (incl. overtime)": ("Long rec", ["longReception"]),
    "Anytime Touchdown Scorer": ("Anytime TD", ["rushingTouchdowns", "receivingTouchdowns"]),
}
MARKET_STATS = {v[0]: v[1] for v in PROP_MARKETS.values()}


def roster(team_id, cache):
    hit = cache.get(team_id)
    if hit and time.time() - hit["t"] < 86400: return hit["p"]
    d = get(f"{ESPN}/teams/{team_id}/roster")
    p = {a["id"]: [a["displayName"], (a.get("position") or {}).get("abbreviation", ""), a.get("jersey", "")]
         for grp in d.get("athletes", []) for a in grp.get("items", [])}
    cache[team_id] = {"t": time.time(), "p": p}
    return p


def game_props(g):
    try:
        items, page = [], 1
        while True:
            d = get(f"{CORE}/events/{g['id']}/competitions/{g['id']}/odds/100/propBets?limit=1000&page={page}")
            items += d.get("items", [])
            if page >= d.get("pageCount", 1): break
            page += 1
    except Exception as e:
        print("props failed", g["id"], e, file=sys.stderr); return {}
    out = {}
    for i in items:
        m = PROP_MARKETS.get((i.get("type") or {}).get("name"))
        ref = (i.get("athlete") or {}).get("$ref", "")
        if not m or "/athletes/" not in ref: continue
        aid = ref.split("/athletes/")[1].split("?")[0]
        line = ((i.get("current") or {}).get("target") or {}).get("value")
        opn = ((i.get("open") or {}).get("target") or {}).get("value")
        if m[0] == "Anytime TD": line = 0.5
        if line is None: continue
        out.setdefault(aid, {})[m[0]] = {"m": m[0], "l": line, "o": opn}
    return {a: list(v.values()) for a, v in out.items()}


def gamelog(aid, season):
    """Last 5 regular/postseason games, reaching into last season when this one is short."""
    path = os.path.join(DATA, "gamelogs", f"{aid}.json")
    try:
        with open(path, encoding="utf-8") as f: hit = json.load(f)
        if time.time() - hit["t"] < 12 * 3600: return hit["g"]
    except (FileNotFoundError, json.JSONDecodeError): pass
    games = []
    for yr in (season, season - 1):
        d = get(GAMELOG.format(aid) + ("" if yr == season else f"?season={yr}"))
        names, meta, rows = d.get("names", []), d.get("events", {}), []
        for st in d.get("seasonTypes", []):
            if "Preseason" in st.get("displayName", ""): continue
            for c in st.get("categories", []):
                for e in c.get("events", []):
                    m = meta.get(e["eventId"], {})
                    rows.append({"y": yr, "w": m.get("week"), "d": m.get("gameDate", "")[:10], "o": (m.get("opponent") or {}).get("abbreviation"),
                                 "at": m.get("atVs"), "r": m.get("gameResult"),
                                 "s": {n: num(v) for n, v in zip(names, e.get("stats", []))}})
        rows.sort(key=lambda r: r["d"], reverse=True)
        games += rows
        if len(games) >= 5: break
    games = games[:5]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    save(path, {"t": time.time(), "g": games})
    return games


def player_props(games, season):
    now = datetime.now(timezone.utc)
    targets = [g for g in games if g["state"] == "pre"
               and datetime.fromisoformat(g["date"].replace("Z", "+00:00")) - now < timedelta(days=8)]
    with ThreadPoolExecutor(6) as pool:
        lines = dict(zip([g["id"] for g in targets], pool.map(game_props, targets)))
    rc, jobs = load("roster_cache_v2.json", {}), []
    for g in targets:
        people = {}
        for side in ("home", "away"):
            try:
                for aid, (n, pos, *_) in roster(g[side]["id"], rc).items(): people[aid] = (n, pos, g[side]["abbr"], side)
            except Exception as e:
                print("roster failed", g[side]["abbr"], e, file=sys.stderr)
        jobs += [(g["id"], aid, people[aid], props) for aid, props in lines.get(g["id"], {}).items() if aid in people]
    save(os.path.join(DATA, "roster_cache_v2.json"), rc)

    def build(job):
        gid, aid, (n, pos, tm, side), props = job
        try: log = gamelog(aid, season)
        except Exception as e:
            print("gamelog failed", aid, e, file=sys.stderr); log = []
        keys = sorted({k for p in props for k in MARKET_STATS[p["m"]]})
        return gid, {"id": aid, "n": n, "p": pos, "tm": tm, "side": side, "props": props,
                     "log": [{**{k: r[k] for k in ("y", "w", "o", "at", "r")}, "s": {k: r["s"].get(k) for k in keys}} for r in log]}
    out = {}
    with ThreadPoolExecutor(8) as pool:
        for gid, row in pool.map(build, jobs): out.setdefault(gid, []).append(row)
    order = {"QB": 0, "RB": 1, "WR": 2, "TE": 3}
    for rows in out.values(): rows.sort(key=lambda r: (order.get(r["p"], 9), -len(r["props"])))
    return out



# ---------------------------------------------------------------- lineups: depth charts + game-day inactives
OUT_RE = ("out", "reserve", "suspend", "pup", "nfi", "doubtful")


def depth_chart(team_id):
    path = os.path.join(DATA, "depth", f"{team_id}.json")
    try:
        with open(path, encoding="utf-8") as f: hit = json.load(f)
        if time.time() - hit["t"] < 6 * 3600: return hit["d"]
    except (FileNotFoundError, json.JSONDecodeError): pass
    d = get(f"{ESPN}/teams/{team_id}/depthcharts").get("depthchart", [])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    save(path, {"t": time.time(), "d": d})
    return d


def game_roster(g, side):
    """ESPN's game-day roster: who started and who did not play (inactives)."""
    tid = g[side]["id"]
    try: d = get(f"{CORE}/events/{g['id']}/competitions/{g['id']}/competitors/{tid}/roster")
    except Exception: return None
    e = d.get("entries") or []
    if not e: return None
    return {"inactive": [str(x["playerId"]) for x in e if x.get("didNotPlay")],
            "starters": [str(x["playerId"]) for x in e if x.get("starter")]}


def team_lineup(g, side, rc):
    jerseys = {aid: v[2] if len(v) > 2 else "" for aid, v in roster(g[side]["id"], rc).items()}
    inj = {i["n"]: i for i in g[side]["inj"]}
    out = {}
    for f in depth_chart(g[side]["id"]):
        name = f.get("name", "")
        unit = "st" if "Special" in name else "def" if name.rstrip().endswith("D") or " D" in name else "off"
        slots = {}
        for key, pos in f.get("positions", {}).items():
            rows = []
            for a in pos.get("athletes", [])[:4]:
                ij = (a.get("injuries") or [{}])[0]
                st = ij.get("status") or inj.get(a.get("displayName"), {}).get("s", "")
                rows.append({"id": a["id"], "n": a.get("displayName"), "j": jerseys.get(a["id"], ""), "s": st,
                             "c": (ij.get("shortComment") or "")[:220] if st else ""})
            slots[key] = rows
        out[unit] = {"f": name, "slots": slots}
    return out


def depth_info(games):
    """Starting QB and the offensive depth chart (top two at each skill spot) for every team with an upcoming game."""
    out = {}
    for g in games:
        if g["state"] != "pre": continue
        for side in ("home", "away"):
            t = g[side]
            if t["abbr"] in out: continue
            try: d = depth_chart(t["id"])
            except Exception: continue
            names, qb, qbs, qbname = set(), None, [], {}
            for f in d:
                pos = f.get("positions", {})
                if "qb" not in pos: continue
                for key in ("qb", "rb", "wr1", "wr2", "wr3", "te"):
                    ath = (pos.get(key) or {}).get("athletes", [])
                    if key == "qb" and ath and not qb:
                        qb = ath[0].get("displayName")
                        qbs = [analytics.norm(x.get("displayName")) for x in ath]
                        qbname = {analytics.norm(x.get("displayName")): x.get("displayName") for x in ath}
                    names.update(analytics.norm(a.get("displayName")) for a in ath[:2])
            out[t["abbr"]] = {"qb": qb, "qbs": qbs, "qbname": qbname, "names": names}
    return out


def lineups(games):
    now = datetime.now(timezone.utc)
    rc, res = load("roster_cache_v2.json", {}), {}
    for g in games:
        kick = datetime.fromisoformat(g["date"].replace("Z", "+00:00"))
        if g["state"] == "post" or kick - now > timedelta(days=8): continue
        try:
            row = {"home": team_lineup(g, "home", rc), "away": team_lineup(g, "away", rc)}
        except Exception as e:
            print("lineup failed", g["id"], e, file=sys.stderr); continue
        # official inactives are announced ~90 minutes before kickoff
        if g["state"] != "pre" or kick - now < timedelta(hours=36):
            gr = {side: game_roster(g, side) for side in ("home", "away")}
            for side, r in gr.items():
                if not r: continue
                ros = roster(g[side]["id"], rc)
                r["inactive"] = [[aid, *(ros.get(aid) or ["Unknown", "", ""])] for aid in r["inactive"]]
            row["gr"] = gr
            row["official"] = g["state"] != "pre" or now >= kick - timedelta(minutes=95)
        res[g["id"]] = row
    save(os.path.join(DATA, "roster_cache_v2.json"), rc)
    return res


def chunk(rows, limit=180_000):
    """Split a {game_id: data} map into dicts that stay under the db's 256 KiB document limit."""
    chunks, cur = [], {}
    for gid, v in rows.items():
        cur[gid] = v
        if len(json.dumps(cur, separators=(",", ":"))) > limit and len(cur) > 1:
            last = cur.pop(gid); chunks.append(cur); cur = {gid: last}
    if cur: chunks.append(cur)
    return chunks


# ---------------------------------------------------------------- main
def main():
    try:
        with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as f: cfg = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError): cfg = {}
    sb = get(ESPN + "/scoreboard")
    season, week = sb["season"]["year"], (sb.get("week") or {}).get("number")
    stype = sb["season"].get("type", 2)
    events = [(e, week) for e in sb["events"]]
    # Monday/Tuesday: the current week is nearly done, so pull next week's lines too
    pre = sum(1 for e in sb["events"] if e["competitions"][0]["status"]["type"]["state"] == "pre")
    if stype == 2 and week and week < 18 and pre <= 3:
        nxt = get(ESPN + f"/scoreboard?week={week + 1}&seasontype=2")
        events += [(e, week + 1) for e in nxt["events"]]
    with ThreadPoolExecutor(8) as pool:
        sums = list(pool.map(summary, [e for e, _ in events]))
    games = [parse_game(e, s, w) for (e, w), s in zip(events, sums)]
    weather(games)
    odds_meta = multibook(games, cfg)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    ids = {g["id"] for g in games}

    # line history: one snapshot per run, only when a line changed; keyed by game id
    hist = load(f"history_{season}.json", {"season": season, "snaps": []})
    lines = {g["id"]: [g["odds"]["hs"], g["odds"]["t"], g["odds"]["hml"], g["odds"]["aml"]]
             for g in games if g["odds"] and g["state"] == "pre"}
    last = {}
    for sn in hist["snaps"]: last.update(sn["g"])
    changed = {k: v for k, v in lines.items() if last.get(k) != v}
    if changed: hist["snaps"].append({"t": now, "g": changed})
    save(os.path.join(DATA, f"history_{season}.json"), hist)
    # the hosted copy only carries games on the current slate
    snaps = [{"t": sn["t"], "g": {k: v for k, v in sn["g"].items() if k in ids}} for sn in hist["snaps"]]
    hist_out = {"season": season, "snaps": [sn for sn in snaps if sn["g"]]}

    # results + closing lines (close keeps updating until kickoff, then freezes)
    res = load(f"results_{season}.json", {"season": season, "games": {}})
    for g in games:
        r = res["games"].setdefault(g["id"], {})
        r.update({"w": g["week"], "d": g["date"], "h": g["home"]["abbr"], "a": g["away"]["abbr"], "st": g["state"]})
        if g["state"] == "pre" and g["odds"]:
            r.update({"cs": g["odds"]["hs"], "ct": g["odds"]["t"], "chml": g["odds"]["hml"], "caml": g["odds"]["aml"]})
        if g["state"] == "post":
            r.update({"hs": num(g["home"]["score"]), "as": num(g["away"]["score"])})
    save(os.path.join(DATA, f"results_{season}.json"), res)

    props = player_props(games, season)
    # nflverse analytics: team efficiency + ranks, defense vs position, player usage (snaps, shares, EPA)
    team_an = None
    try:
        team_an = analytics.team_analytics(season)
        ids = {pl["id"] for rows in props.values() for pl in rows}
        use = analytics.player_usage(season, ids)
        for rows in props.values():
            for pl in rows:
                pl["u"] = use["season"].get(pl["id"])
                wk = use["weekly"].get(pl["id"], {})
                for r in pl["log"]:
                    w = wk.get(f'{r["y"]}-{r["w"]}')
                    if w: r["us"] = {k: w.get(k) for k in ("snap", "tgt", "ts", "car", "rs") if w.get(k) is not None}
    except Exception as e:
        print("analytics failed", e, file=sys.stderr)
    # key players back from injury (or newly out): the team's numbers and teammates' logs were built without (or with) them
    try:
        if team_an:
            dep = depth_info(games)
            # the books know first: if they only post passing props for one QB, he's the starter (beats a stale depth chart)
            for g in games:
                if g["state"] != "pre": continue
                for side in ("home", "away"):
                    d = dep.get(g[side]["abbr"])
                    qbs = [pl["n"] for pl in props.get(g["id"], []) if pl.get("side") == side and pl.get("p") == "QB" and any(pr["m"] == "Pass yds" for pr in pl["props"])]
                    if d is None or len(qbs) != 1: continue
                    nm = analytics.norm(qbs[0])
                    if d["qbs"][:1] != [nm]:
                        d["qbs"] = [nm] + [q for q in d["qbs"] if q != nm]; d["qbname"][nm] = qbs[0]; d["qb_from_books"] = True
            lc = analytics.lineup_changes(season, games, dep)
            team_an["lineup"] = lc
            for g in games:
                ch = {s: lc[g[s]["abbr"]] for s in ("home", "away") if g["state"] == "pre" and g[s]["abbr"] in lc}
                if ch: g["lineup"] = {s: v["keys"] + [{**d, "role": "DEF"} for d in v.get("def", [])] for s, v in ch.items()}
    except Exception as e:
        print("lineup changes failed", e, file=sys.stderr)
    # our own projections, betting trends and player pages
    pages = {}
    multibook_props(games, props, cfg)
    multibook_tds(games, props, cfg)    # anytime-TD prices + bet-slip links, 1 credit per game on game day
    multibook_alts(games, props, cfg)   # every book's alternate ladders (paid-plan volume; off by default)
    try:
        kalshi_n = kalshi.apply(games, props)   # alternate-line ladders with real prices
    except Exception as e:
        kalshi_n = 0; print("kalshi failed", e, file=sys.stderr)
    # game situations (rest, travel, neutral site, division, weather), coaching tenure, parlay correlations
    try:
        co = situational.coaches(season)
        situational.situations(games, season, co)
        if team_an is not None:
            team_an["coaches"] = co
            cr = situational.correlations(season)
            team_an["corr"] = {"years": cr["years"], "pairs": cr["pairs"]}
    except Exception as e:
        import traceback; traceback.print_exc()
        print("situations failed", e, file=sys.stderr)
    try:
        projections.team_projections(games, team_an)
        projections.prop_projections(props, games, team_an, season)
        try:   # first touchdown scorer chances, from the anytime chances just blended
            import firsttd; firsttd.attach(games, props)
        except Exception as e: print("first td failed", e, file=sys.stderr)
    except Exception as e:
        import traceback; traceback.print_exc()
        print("projections failed", e, file=sys.stderr)
    # possible trap lines; the read at kickoff is frozen into results so fading them can be tracked
    try:
        traps.apply(games)
        for g in games:
            if g["state"] != "pre": continue
            r = res["games"].setdefault(g["id"], {})
            tr = g.get("trap") or {}
            r["trap"] = {k: {"fade": (g[v["fade"]]["abbr"] if k == "side" else "under"), "line": v["fade_line"], "score": v["score"]}
                         for k, v in tr.items() if v}
        save(os.path.join(DATA, f"results_{season}.json"), res)
    except Exception as e:
        print("traps failed", e, file=sys.stderr)
    try:
        if team_an is not None: team_an["trends"] = trends.team_trends(season, games, coaches=team_an.get("coaches"))
        pages = trends.player_pages(season, games)
    except Exception as e:
        print("trends/pages failed", e, file=sys.stderr)
    # report card: grade finished games against the readings frozen at kickoff, then freeze the upcoming ones
    report = None
    try:
        rd = load(f"readings_{season}.json", {"season": season, "games": {}})
        graded = grades.grade(games, sums, rd)
        grades.freeze(games, props, rd)
        save(os.path.join(DATA, f"readings_{season}.json"), rd)
        report = grades.report(rd, res)
        # touchdown backtest: once a week is final, grade our TD model, Kalshi's prices and the app's blend (free, ~2 min a week)
        try:
            import td_backtest
            bt = load("td_backtest.json", {})
            todo = [w for w in range(1, (week or 1) + 1) if str(w) not in bt and td_backtest.ready(w)][:2]
            for w in todo:
                bt[str(w)] = td_backtest.summarize(td_backtest.build(w), projections.MKT_TD_W)
            if todo: save(os.path.join(DATA, "td_backtest.json"), bt)
            if report is not None: report["tdbt"] = bt
        except Exception as e:
            print("td backtest failed", e, file=sys.stderr)
        # yardage / receptions backtest: same idea vs Kalshi's yardage ladders (slower, so one week per run)
        try:
            import td_backtest, prop_backtest
            pb = load("prop_backtest.json", {})
            todo = [w for w in range(1, (week or 1) + 1) if str(w) not in pb and td_backtest.ready(w)][:1]
            for w in todo:
                pb[str(w)] = td_backtest.summarize(prop_backtest.build(w), projections.MKT_PROP_W)
            if todo: save(os.path.join(DATA, "prop_backtest.json"), pb)
            if report is not None: report["pbt"] = pb
        except Exception as e:
            print("prop backtest failed", e, file=sys.stderr)
        # the rest of the board (attempts, completions, TDs, INTs, carries, rush+rec) the same way, one week per run
        try:
            import td_backtest, prop_backtest
            pb2 = load("prop_backtest2.json", {})
            todo = [w for w in range(1, (week or 1) + 1) if str(w) not in pb2 and td_backtest.ready(w)][:1]
            for w in todo:
                rs = prop_backtest.build(w, prop_backtest.SERIES2)
                pb2[str(w)] = {**td_backtest.summarize(rs, projections.MKT_PROP_W), "mk": prop_backtest.by_market(rs)}
            if todo: save(os.path.join(DATA, "prop_backtest2.json"), pb2)
            if report is not None: report["pbt2"] = pb2
        except Exception as e:
            print("prop backtest (other markets) failed", e, file=sys.stderr)
        if graded: print("graded", graded, "games", file=sys.stderr)
    except Exception as e:
        import traceback; traceback.print_exc()
        print("report card failed", e, file=sys.stderr)
    lu = lineups(games)
    chunks, lchunks, pchunks = chunk(props), chunk(lu), chunk(pages)
    for f in os.listdir(OUT):
        if f.startswith(("props-", "lineups-", "players-")): os.remove(os.path.join(OUT, f))
    manifest = [{"collection": "slate", "doc_id": "current", "file": "out/slate.json"},
                {"collection": "history", "doc_id": "current", "file": "out/history.json"},
                {"collection": "results", "doc_id": str(season), "file": "out/results.json"}]
    for n, ch in enumerate(chunks):
        save(os.path.join(OUT, f"props-{n}.json"), {"sync": now, "part": n, "games": ch})
        manifest.append({"collection": "props", "doc_id": f"part-{n}", "file": f"out/props-{n}.json"})
    for n, ch in enumerate(lchunks):
        save(os.path.join(OUT, f"lineups-{n}.json"), {"sync": now, "part": n, "games": ch})
        manifest.append({"collection": "lineups", "doc_id": f"part-{n}", "file": f"out/lineups-{n}.json"})
    for n, ch in enumerate(pchunks):
        save(os.path.join(OUT, f"players-{n}.json"), {"sync": now, "part": n, "games": ch})
        manifest.append({"collection": "players", "doc_id": f"part-{n}", "file": f"out/players-{n}.json"})
    save(os.path.join(OUT, "manifest.json"), manifest)

    for g in games: g.pop("city", None)
    try:   # price gaps across books and Kalshi (arbitrage), each leg with how old its price is
        import arbs; gaps = arbs.find(games, props, (odds_meta or {}).get("at"))
    except Exception as e: print("arbs failed", e, file=sys.stderr); gaps = []
    slate = {"updatedAt": now, "propParts": len(chunks), "analytics": team_an, "season": season, "week": week, "seasonType": stype, "odds": odds_meta, "report": report, "games": games, "arbs": gaps}
    save(os.path.join(OUT, "slate.json"), slate)
    try:   # group push alerts (ntfy): line moves, QBs out, TD prices posting, weekly recap
        import alerts
        sent = alerts.run(games, props, report, cfg, os.path.join(DATA, "alerts_state.json"), gaps=slate.get("arbs"))
        if sent: print("alerts sent", sent, file=sys.stderr)
    except Exception as e:
        print("alerts failed", e, file=sys.stderr)
    save(os.path.join(OUT, "history.json"), hist_out)
    save(os.path.join(OUT, "results.json"), res)
    sizes = {f: os.path.getsize(os.path.join(OUT, f)) // 1024 for f in os.listdir(OUT)}
    print(json.dumps({"season": season, "weeks": sorted({g["week"] for g in games}), "games": len(games),
                      "snapshots": len(hist_out["snaps"]), "propPlayers": sum(len(v) for v in props.values()), "lineupGames": len(lu), "playerPages": len(pages), "kalshiRungs": kalshi_n, "multibook": odds_meta, "kb": sizes}))


if __name__ == "__main__":
    main()
