"""Group push alerts through ntfy (free: friends install the ntfy app and subscribe to the channel shown in the app).

The sync calls run() at the end of every pass. Bets live on each phone, so alerts are group-wide, not per bet:
  - a line moves (spread 1.5+ points, or through 3 or 7; total 2+ points) since the last alert for that game
  - a starting QB is ruled out
  - anytime-TD prices post at more books for a game
  - a finished week gets graded on the report card
Each alert goes out once (state in alerts_state.json). At most MAX_PER_RUN per pass, so a big sync never floods phones.
Channel: config.json "ntfy_topic" or env NTFY_TOPIC. No channel = no alerts.
"""
import json, os, sys
from urllib.request import Request, urlopen

APP_URL = "https://williamtong21-netizen.github.io/nfl-edge-board/"
MAX_PER_RUN = 4
KEY_NUMS = (3, 7)


def topic(cfg):
    return os.environ.get("NTFY_TOPIC") or (cfg or {}).get("ntfy_topic")


def send(top, title, body, tags="football", click=APP_URL, priority=3):
    req = Request(f"https://ntfy.sh/{top}", data=body.encode("utf-8"), method="POST",
                  headers={"Title": title.encode("ascii", "ignore").decode(), "Tags": tags, "Click": click, "Priority": str(priority)})
    with urlopen(req, timeout=15) as r: return r.status


def crossed(a, b):
    """The spread moved onto or through a key number (3 or 7), e.g. -2.5 -> -3."""
    return any((abs(a) < k) != (abs(b) < k) for k in KEY_NUMS)


def candidates(games, props, report, st, label="NFL"):
    out = []
    base = st.setdefault("lines", {})
    for g in games:
        if g.get("state") != "pre": continue
        o = g.get("odds") or {}
        A, H, gid = g["away"]["abbr"], g["home"]["abbr"], g["id"]
        name = f"{A} @ {H}"
        b = base.get(gid)
        if o.get("hs") is not None and o.get("t") is not None:
            if not b: base[gid] = {"hs": o["hs"], "t": o["t"]}
            else:
                ds, dt = o["hs"] - b["hs"], o["t"] - b["t"]
                if abs(ds) >= 1.5 or (ds and crossed(b["hs"], o["hs"])):
                    fav = lambda hs: f"{H} {hs:+g}" if hs else "pick'em"
                    toward = H if ds < 0 else A
                    out.append((2, f"line:{gid}:{o['hs']}", f"{label} line move: {name}", f"Spread moved {fav(b['hs'])} -> {fav(o['hs'])}, toward {toward}.", "chart_with_upwards_trend",
                                lambda gid=gid, o=o: base.__setitem__(gid, {**base[gid], "hs": o["hs"]})))
                if abs(dt) >= 2:
                    out.append((3, f"total:{gid}:{o['t']}", f"{label} total move: {name}", f"Total {'up' if dt > 0 else 'down'} {b['t']} -> {o['t']}.", "chart_with_upwards_trend",
                                lambda gid=gid, o=o: base.__setitem__(gid, {**base[gid], "t": o["t"]})))
        for side in ("home", "away"):
            t = g[side]
            for i in t.get("inj") or []:
                if i.get("r") == "starting QB" and str(i.get("s", "")).lower().startswith(("out", "injured", "doubtful", "suspend")):
                    out.append((1, f"qb:{gid}:{i['n']}", f"{t['abbr']} QB {i['n']}: {i['s']}", f"{name}. Lines and our reads update on the next sync.", "rotating_light", None))
        books = {b_["n"] for pl in (props or {}).get(gid, []) for pr in pl.get("props", []) if pr.get("m") == "Anytime TD" for b_ in pr.get("books", [])}
        if len(books) >= 2:
            out.append((4, f"td:{gid}", f"TD prices are up: {name}", f"Anytime TD prices from {len(books)} books. Check the TDs tab for value.", "football", None))
    for w, v in sorted(((report or {}).get("tdbt") or {}).items(), key=lambda x: int(x[0])):
        pl = (v.get("blend") or {}).get("pl")
        if pl is not None:
            out.append((5, f"recap:{w}", f"Week {w} graded", f"Touchdown value bets: {'+' if pl >= 0 else '-'}${abs(pl):.0f} on $10 each. Full report card in the Picks tab.", "trophy", None))
    return out


def run(games, props, report, cfg, state_path, label="NFL"):
    top = topic(cfg)
    if not top: return 0
    try:
        with open(state_path, encoding="utf-8") as f: st = json.load(f)
    except (FileNotFoundError, ValueError): st = {}
    first = "sent" not in st          # first run: remember everything as already sent, so a new channel isn't spammed with old news
    sent = set(st.get("sent", []))
    n = 0
    for pri, key, title, body, tags, after in sorted(candidates(games, props, report, st, label), key=lambda x: x[0]):
        if key in sent: continue
        if not first and n < MAX_PER_RUN:
            try: send(top, title, body, tags); n += 1
            except Exception as e: print("alert failed", e, file=sys.stderr); continue
        elif not first: continue      # over the cap: try again next pass
        sent.add(key)
        if after: after()
    st["sent"] = sorted(sent)[-2000:]
    with open(state_path, "w", encoding="utf-8") as f: json.dump(st, f)
    return n
