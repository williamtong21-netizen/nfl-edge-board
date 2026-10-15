"""Yahoo Fantasy league pull for the Fantasy tab's trade finder (same shape the page builds from Sleeper).

One-time sign-in (keys live in config.json, typed by the user, never by the assistant):
  python yahoo.py auth            -> prints the Yahoo sign-in link
  python yahoo.py code <CODE>     -> trades the code for a refresh token, saved in config.json
Each sync (env YAHOO_CLIENT_ID / YAHOO_CLIENT_SECRET / YAHOO_REFRESH_TOKEN, or config.json):
  pull(out_dir, cache_dir)        -> out/yahoo_league.json  {league: {...}, teams: [{id, name, players: [sleeper ids]}]}
Players are matched to Sleeper ids through Sleeper's yahoo_id field (then name + position).
"""
import base64, json, os, re, sys
from urllib.parse import urlencode
from urllib.request import urlopen, Request

ROOT = os.path.dirname(os.path.abspath(__file__))
CFG = os.path.join(ROOT, "config.json")
AUTH = "https://api.login.yahoo.com/oauth2/request_auth"
TOKEN = "https://api.login.yahoo.com/oauth2/get_token"
API = "https://fantasysports.yahooapis.com/fantasy/v2/"
LEAGUE_ID = os.environ.get("YAHOO_LEAGUE_ID") or "1273976"
REDIRECT = "https://localhost"   # must match the Yahoo app; the code shows up in the address bar after sign-in


def cfg():
    try:
        with open(CFG, encoding="utf-8") as f: return json.load(f)
    except (FileNotFoundError, ValueError): return {}


def creds():
    c = cfg()
    return (os.environ.get("YAHOO_CLIENT_ID") or c.get("yahoo_client_id"), os.environ.get("YAHOO_CLIENT_SECRET") or c.get("yahoo_client_secret"),
            os.environ.get("YAHOO_REFRESH_TOKEN") or c.get("yahoo_refresh_token"))


def token(data):
    cid, sec, _ = creds()
    basic = base64.b64encode(f"{cid}:{sec}".encode()).decode()
    req = Request(TOKEN, data=urlencode({**data, "redirect_uri": REDIRECT}).encode(),
                  headers={"Authorization": "Basic " + basic, "Content-Type": "application/x-www-form-urlencoded"})
    with urlopen(req, timeout=30) as r: return json.loads(r.read())


def api(path, access):
    with urlopen(Request(API + path + ("&" if "?" in path else "?") + "format=json", headers={"Authorization": "Bearer " + access}), timeout=40) as r:
        return json.loads(r.read())


def flat(x):
    """Yahoo's JSON wraps fields as lists of one-key dicts: merge them into one dict."""
    out = {}
    for it in x if isinstance(x, list) else [x]:
        if isinstance(it, dict): out.update(it)
        elif isinstance(it, list): out.update(flat(it))
    return out


def norm(s): return re.sub(r"[^a-z]", "", re.sub(r"\b(jr|sr|ii|iii|iv|v)\b", "", (s or "").lower()))


def pull(out_dir, cache_dir):
    cid, sec, ref = creds()
    if not (cid and sec and ref): return None
    access = token({"grant_type": "refresh_token", "refresh_token": ref})["access_token"]
    lk = f"nfl.l.{LEAGUE_ID}"
    st = api(f"league/{lk}/settings", access)["fantasy_content"]["league"]
    meta, sets = flat(st[0]), flat(st[1]["settings"])
    rec = 0.0; stat_ids = {}
    for s in (sets.get("stat_categories") or {}).get("stats", []):
        s = s["stat"]; stat_ids[str(s["stat_id"])] = s.get("display_name") or s.get("name")
    for m in (sets.get("stat_modifiers") or {}).get("stats", []):
        m = m["stat"]
        if stat_ids.get(str(m["stat_id"])) in ("Rec", "Receptions"): rec = float(m["value"])
    slots = []
    for rp in sets.get("roster_positions", []):
        rp = rp["roster_position"]; pos = rp["position"]
        name = {"W/R/T": "FLEX", "W/R": "WRRB_FLEX", "W/T": "REC_FLEX", "Q/W/R/T": "SUPER_FLEX"}.get(pos, pos)
        if name in ("QB", "RB", "WR", "TE", "FLEX", "WRRB_FLEX", "REC_FLEX", "SUPER_FLEX"): slots += [name] * int(rp.get("count", 1))
    tm = api(f"league/{lk}/teams/roster", access)["fantasy_content"]["league"][1]["teams"]
    sys.path.insert(0, ROOT)
    import fantasy
    sp = fantasy.sleeper_players(cache_dir)
    by_yid = {v["yid"]: k for k, v in sp.items() if v.get("yid")}
    by_name = {(norm(v["n"]), v["pos"]): k for k, v in sp.items()}
    teams = []
    for k, t in tm.items():
        if k == "count": continue
        t = t["team"]; info = flat(t[0])
        mgr = flat(((info.get("managers") or [{}])[0] or {}).get("manager") or {})
        players = []
        for pk, p in (flat(t[1:]).get("roster") or {}).get("0", {}).get("players", {}).items():
            if pk == "count": continue
            pi = flat(p["player"][0])
            pos = pi.get("display_position", "").split(",")[0]
            sid = by_yid.get(str(pi.get("player_id"))) or by_name.get((norm((pi.get("name") or {}).get("full")), pos))
            if sid: players.append(sid)
        teams.append({"id": info.get("team_id"), "name": mgr.get("nickname") or info.get("name"), "players": players})
    doc = {"league": {"name": meta.get("name"), "num_teams": meta.get("num_teams"), "rec": rec, "slots": slots, "week": meta.get("current_week")}, "teams": teams}
    with open(os.path.join(out_dir, "yahoo_league.json"), "w", encoding="utf-8") as f: json.dump(doc, f, separators=(",", ":"))
    return len(teams)


if __name__ == "__main__":
    cid, sec, _ = creds()
    if len(sys.argv) > 1 and sys.argv[1] == "auth":
        if not (cid and sec): sys.exit("Add yahoo_client_id and yahoo_client_secret to config.json first.")
        print(AUTH + "?" + urlencode({"client_id": cid, "redirect_uri": REDIRECT, "response_type": "code", "scope": "openid fspt-r", "language": "en-us"}))
    elif len(sys.argv) > 2 and sys.argv[1] == "code":
        t = token({"grant_type": "authorization_code", "code": sys.argv[2].strip()})
        c = cfg(); c["yahoo_refresh_token"] = t["refresh_token"]
        with open(CFG, "w", encoding="utf-8") as f: json.dump(c, f, indent=2)
        print("Signed in: refresh token saved to config.json")
    else:
        print(pull(os.path.join(ROOT, "out"), os.path.join(ROOT, "data")), "teams")
