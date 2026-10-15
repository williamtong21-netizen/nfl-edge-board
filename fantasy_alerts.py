"""Personal fantasy alerts (your own ntfy channel, not the group's), run with the 20-minute news check.

Lineup (before each game): a starter of yours who is ruled out (Out / IR / Doubtful, or a "ruled out" headline the news
check caught), on bye, or projected 3+ points below a bench player who can fill his slot (this week's props + your
league's scoring and yardage bonuses). Each problem alerts once, only while his game hasn't started and within 30 hours
of it, and nothing between midnight and 8 AM Eastern.
Waivers (Wednesdays from 10 AM Eastern, once a week): the 3 best free agents for your team (rest-of-season points over a
replacement player, your scoring and bonuses, schedule-adjusted) that beat your weakest player at the position, and the
easiest drop.
Leagues: Sleeper (FF_SLEEPER_LEAGUE + FF_SLEEPER_USER, live) and Yahoo (FF_YAHOO_TEAM, from data/yahoo_league.json, as of
the last read). Channel: FF_NTFY_TOPIC. State: state/fantasy_alerts.json.
"""
import glob, json, math, os, re, sys
from datetime import datetime, timedelta, timezone
from urllib.request import urlopen, Request

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.environ.get("EDGE_OUT") or os.path.join(ROOT, "out")
DATA = os.environ.get("EDGE_DATA") or os.path.join(ROOT, "data")
SLP = "https://api.sleeper.app/v1/"
FLEX_OK = {"FLEX": ("RB", "WR", "TE"), "SUPER_FLEX": ("QB", "RB", "WR", "TE"), "REC_FLEX": ("WR", "TE"), "WRRB_FLEX": ("RB", "WR")}
TM = {"WAS": "WSH"}          # Sleeper team codes that differ from ESPN's
SWAP_MIN = 3.0
OUT_WORDS = ("Out", "IR", "Doubtful", "Sus", "PUP")


def get(path):
    with urlopen(Request(SLP + path, headers={"User-Agent": "edge-board"}), timeout=30) as r: return json.loads(r.read())


def load(p, default):
    try:
        with open(p, encoding="utf-8") as f: return json.load(f)
    except (FileNotFoundError, ValueError): return default


norm = lambda s: re.sub(r"(iii|ii|iv|jr|sr)$", "", re.sub(r"[^a-z]", "", (s or "").lower()))


def scoring_sleeper(ss):
    b = []
    for k, v in (ss or {}).items():
        m = re.match(r"^bonus_(pass|rush|rec|rush_rec)_yd_(\d+)$", k)
        if m and v: b.append(("rr" if m[1] == "rush_rec" else m[1], int(m[2]), v))
    return {"rec": ss.get("rec", 0), "te": ss.get("bonus_rec_te", 0), "ptd": ss.get("pass_td", 4), "int": ss.get("pass_int", -2), "b": b}


def ros(v, sc):
    """Rest-of-season points per game under a league's scoring (as the page computes it)."""
    tdint = ((sc["ptd"] - 4) * v.get("pt", 0) + (sc["int"] + 2) * v.get("it", 0)) if v["pos"] == "QB" else 0
    bon = sum(val * v.get("bz", {}).get(k + str(y), 0) for k, y, val in sc["b"])
    return (v["s"] + tdint + v["r"] * (sc["rec"] + (sc["te"] if v["pos"] == "TE" else 0)) + bon) * v.get("sf", 1)


def week_proj(pl, v, sc):
    """This week's points from the player's props, under the league's scoring (bonus chance from his season hit rate)."""
    sys.path.insert(0, ROOT)
    import fantasy
    pp = fantasy.prop_points(pl)
    if not pp: return None
    s, rec = pp
    bon = sum(val * v.get("bz", {}).get(k + str(y), 0) for k, y, val in sc["b"]) if v else 0
    return s + rec * (sc["rec"] + (sc["te"] if pl.get("p") == "TE" else 0)) + bon


def week_context():
    slate = load(os.path.join(OUT, "slate.json"), {})
    props = {}
    for fn in glob.glob(os.path.join(OUT, "props-*.json")): props.update(load(fn, {}).get("games", {}))
    kick, started, by_name = {}, set(), {}
    for g in slate.get("games", []):
        for side in ("home", "away"):
            t = g[side]["abbr"]; kick[t] = g["date"]
            if g.get("state") != "pre" or datetime.fromisoformat(g["date"].replace("Z", "+00:00")) < datetime.now(timezone.utc): started.add(t)
            for i in g[side].get("inj") or []:
                if str(i.get("s", "")).startswith(("Out", "Injured", "Doubtful", "Suspend")): by_name[norm(i["n"])] = i["s"]
        for pl in props.get(g["id"], []): by_name.setdefault("props:" + norm(pl["n"]), pl)
    outs = {norm(n) for n in (load(os.path.join(DATA, "news_state.json"), {}).get("outs") or {})}
    return slate, kick, started, by_name, outs


def league_lineup(name, sc, starters, slots, bench, V, ctx, now):
    """[(key, title, body)] lineup problems for one team."""
    slate, kick, started, by_name, outs = ctx
    week = slate.get("week")
    res = []

    def info(pid):
        if pid in V:
            v = V[pid]; t = TM.get(v["tm"], v["tm"])
            pl = by_name.get("props:" + norm(v["n"]))
            return v["n"], v["pos"], t, v.get("inj"), (week_proj(pl, v, sc) if pl else None)
        if re.fullmatch(r"[A-Z]{2,3}", pid or ""): return pid + " D/ST", "DEF", TM.get(pid, pid), None, None
        return None

    def soon(t):
        k = kick.get(t)
        if not k: return False
        dt = datetime.fromisoformat(k.replace("Z", "+00:00")) - now
        return timedelta(0) < dt < timedelta(hours=30)

    first_kick = min((datetime.fromisoformat(k.replace("Z", "+00:00")) for k in kick.values()), default=None)
    for pid, slot in zip(starters, slots):
        x = info(pid)
        if not x: continue
        n, pos, t, inj, proj = x
        if t in started: continue
        out = (inj in OUT_WORDS) or norm(n) in outs or by_name.get(norm(n), "").startswith(("Out", "Injured", "Doubtful", "Suspend"))
        if t not in kick:   # bye: warn once the week's games are within 30 hours
            if first_kick and timedelta(0) < first_kick - now < timedelta(hours=30):
                res.append((f"bye:{name}:{week}:{pid}", f"{name}: {n} is on bye", f"Your {slot} {n} ({t}) has no game this week. Swap him out before kickoff."))
            continue
        if out and soon(t):
            why = inj if inj in OUT_WORDS else "ruled out"
            res.append((f"out:{name}:{week}:{pid}", f"{name}: bench {n}", f"Your {slot} {n} ({t}) is {why}. Swap him out before {kick_time(kick[t])}."))
            continue
        if proj is None or not soon(t): continue
        ok = FLEX_OK.get(slot, (slot,))
        best = None
        for b in bench:
            y = info(b)
            if not y or y[1] not in ok or y[2] in started or y[2] not in kick or y[4] is None: continue
            if (y[3] in OUT_WORDS) or norm(y[0]) in outs: continue
            if y[4] - proj >= SWAP_MIN and (not best or y[4] > best[1][4]): best = (b, y)
        if best:
            b, y = best
            res.append((f"swap:{name}:{week}:{pid}:{b}", f"{name}: start {y[0]}?", f"Our projections: {y[0]} {y[4]:.1f} vs your {slot} {n} {proj:.1f} this week (your scoring + bonuses)."))
    return res


def kick_time(iso):
    try:
        from zoneinfo import ZoneInfo
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(ZoneInfo("America/New_York")).strftime("%a %-I:%M %p ET")
    except Exception: return iso


def waivers(name, sc, mine, owned, positions, n_teams, V):
    slots = [p for p in positions if p in ("QB", "RB", "WR", "TE") or p in FLEX_OK]
    cnt = {"QB": 0, "RB": 0, "WR": 0, "TE": 0}
    for x in slots:
        if x in cnt: cnt[x] += 1
        else:
            ok = FLEX_OK[x]; rw = [q for q in ok if q in ("RB", "WR")]
            for q in ok: cnt[q] += 0.1 if q == "TE" else 0.8 if q == "QB" else (1 - (0.1 if "TE" in ok else 0) - (0.8 if "QB" in ok else 0)) / len(rw)
    repl = {}
    for pos in cnt:
        xs = sorted((ros(v, sc) for v in V.values() if v["pos"] == pos), reverse=True)
        repl[pos] = xs[min(len(xs) - 1, round(n_teams * cnt[pos]))] if xs else 0
    worst = {}
    for pid in mine:
        v = V.get(pid)
        if v and v["pos"] in cnt: worst[v["pos"]] = min(worst.get(v["pos"], 99), ros(v, sc))
    fa = [(pid, v) for pid, v in V.items() if v["pos"] in cnt and pid not in owned and not re.search(r"IR|Out|Sus|PUP", v.get("inj") or "")
          and ros(v, sc) > worst.get(v["pos"], 0) + 0.5]
    fa.sort(key=lambda kv: -(ros(kv[1], sc) - repl[kv[1]["pos"]]))
    if not fa: return None
    val = lambda pid: max(0, ros(V[pid], sc) - repl.get(V[pid]["pos"], 0))
    keep = sorted((p for p in mine if p in V and V[p]["pos"] in cnt), key=lambda p: -ros(V[p], sc))
    drop = min(keep[len(slots):] or keep, key=lambda p: (val(p), ros(V[p], sc)), default=None) if keep else None
    lines = [f"{v['n']} ({v['pos']}, {v['tm'] or 'FA'}) {ros(v, sc):.1f}/wk" for _, v in fa[:3]]
    return f"Top pickups: " + "; ".join(lines) + (f". Easiest drop: {V[drop]['n']} ({ros(V[drop], sc):.1f}/wk)." if drop else ".")


def main():
    top = os.environ.get("FF_NTFY_TOPIC")
    if not top: print("no FF_NTFY_TOPIC"); return
    sys.path.insert(0, ROOT)
    import alerts
    vals = load(os.path.join(OUT, "ff_values.json"), {}); V = vals.get("players", {})
    if not V: print("no ff_values"); return
    st_path = os.path.join(DATA, "fantasy_alerts.json"); st = load(st_path, {})
    first = "sent" not in st; sent = set(st.get("sent", []))
    now = datetime.now(timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        et = now.astimezone(ZoneInfo("America/New_York"))
    except Exception: et = now - timedelta(hours=4)
    quiet = et.hour < 8
    ctx = week_context(); week = ctx[0].get("week")
    msgs = []
    # Sleeper: live roster and lineup
    lg_id, user = os.environ.get("FF_SLEEPER_LEAGUE"), os.environ.get("FF_SLEEPER_USER")
    if lg_id and user:
        try:
            lg = get(f"league/{lg_id}"); u = get(f"user/{user}"); rosters = get(f"league/{lg_id}/rosters")
            me = next(r for r in rosters if r["owner_id"] == u["user_id"])
            sc = scoring_sleeper(lg.get("scoring_settings")); pos = [p for p in lg.get("roster_positions", []) if p not in ("BN", "IR")]
            starters = me.get("starters") or []; bench = [p for p in me.get("players") or [] if p not in starters and p not in (me.get("reserve") or [])]
            msgs += league_lineup("GSO", sc, starters, pos, bench, V, ctx, now)
            if et.weekday() == 2 and et.hour >= 10:
                w = waivers("GSO", sc, me.get("players") or [], {p for r in rosters for p in (r.get("players") or [])}, lg.get("roster_positions", []), len(rosters), V)
                if w: msgs.append((f"wv:GSO:{week}", "GSO waiver tips", w))
        except Exception as e: print("sleeper failed", e, file=sys.stderr)
    # Yahoo: as of the last read
    yt = os.environ.get("FF_YAHOO_TEAM"); yl = load(os.path.join(OUT, "yahoo_league.json"), None)
    if yt and yl:
        try:
            me = next(t for t in yl["teams"] if str(t["id"]) == yt); s = yl["league"].get("score", {})
            sc = {"rec": yl["league"].get("rec", 0), "te": 0, "ptd": s.get("ptd", 4), "int": s.get("int", -2), "b": [tuple(x) for x in s.get("b", [])]}
            pos = yl["league"].get("positions") or yl["league"]["slots"]
            bench = [p for p in me["players"] if p not in me["starters"] and p not in me.get("reserve", [])]
            msgs += league_lineup("Degens", sc, me["starters"], pos, bench, V, ctx, now)
            if et.weekday() == 2 and et.hour >= 10:
                w = waivers("Degens", sc, me["players"], {p for t in yl["teams"] for p in t["players"]}, pos, yl["league"].get("num_teams", 10), V)
                if w: msgs.append((f"wv:Degens:{week}", "Fantasy Degens waiver tips", w + f" (rosters as of {yl['league'].get('at', 'the last read')})"))
        except Exception as e: print("yahoo failed", e, file=sys.stderr)
    n = 0
    for key, title, body in msgs:
        if key in sent: continue
        if not first:
            if quiet or n >= 4: continue
            try: alerts.send(top, title, body, "fantasy" if False else "clipboard", click=alerts.APP_URL); n += 1
            except Exception as e: print("alert failed", e, file=sys.stderr); continue
        sent.add(key)
    st["sent"] = sorted(sent)[-1000:]
    with open(st_path, "w", encoding="utf-8") as f: json.dump(st, f)
    print(json.dumps({"checked": len(msgs), "sent": n, "first": first, "quiet": quiet}))


if __name__ == "__main__":
    main()
