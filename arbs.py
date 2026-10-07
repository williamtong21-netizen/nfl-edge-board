"""Price gaps (arbitrage): both sides of the same bet at different places, priced so that backing both locks in a profit.

Sources and how fresh they are when the sync runs:
  - DraftKings via ESPN (g["odds"]) and Kalshi (g["kalshi"], props' "alt" rungs): fetched this sync
  - the other US books (g["books"], The Odds API): up to ~12 hours old on the free plan
Markets: moneyline, spreads (same number both ways), totals, anytime TD (a book's Yes vs Kalshi's No).
Kalshi: Yes on "TEAM wins by over 8.5" = TEAM -8.5, its No = the other side +8.5; Yes on "over 47.5" = over, No = under.
Prices that old mostly vanish before you get there, so every leg carries its age and the app says to check both apps first.
"""
import time

OFFSHORE = {"Pinnacle", "Bovada", "BetOnline.ag", "MyBookie.ag", "LowVig.ag", "BetUS", "Unibet"}
MIN_MARGIN = 0.005      # show gaps worth 0.5%+ of the stake


def dec(am): return 1 + (am / 100 if am > 0 else 100 / -am)
def kcost(p): return p + 0.07 * p * (1 - p)                    # Kalshi price plus its fee per contract
def am_from_dec(d): return round((d - 1) * 100) if d >= 2 else round(-100 / (d - 1))


def _offers(g, books_age):
    """{(market, line, side): [offer]} where an offer is {src, d (decimal odds), age (hours), url}."""
    out = {}
    def add(key, src, d, age, url=None):
        if d and d > 1.0001: out.setdefault(key, []).append({"src": src, "d": d, "age": age, "url": url})
    for b in g.get("books") or []:
        if b["n"] in OFFSHORE: continue
        lk = b.get("lk") or {}
        if b.get("hml") is not None: add(("ml", 0, "home"), b["n"], dec(b["hml"]), books_age, lk.get("hml") or lk.get("ev"))
        if b.get("aml") is not None: add(("ml", 0, "away"), b["n"], dec(b["aml"]), books_age, lk.get("aml") or lk.get("ev"))
        if b.get("hs") is not None and b.get("hsp") is not None: add(("spread", b["hs"], "home"), b["n"], dec(b["hsp"]), books_age, lk.get("hs") or lk.get("ev"))
        if b.get("hs") is not None and b.get("asp") is not None: add(("spread", b["hs"], "away"), b["n"], dec(b["asp"]), books_age, lk.get("as") or lk.get("ev"))
        if b.get("t") is not None and b.get("op") is not None: add(("total", b["t"], "over"), b["n"], dec(b["op"]), books_age, lk.get("op") or lk.get("ev"))
        if b.get("t") is not None and b.get("up") is not None: add(("total", b["t"], "under"), b["n"], dec(b["up"]), books_age, lk.get("up") or lk.get("ev"))
    o = g.get("odds") or {}
    if o.get("hml") is not None: add(("ml", 0, "home"), "DraftKings", dec(o["hml"]), 0)
    if o.get("aml") is not None: add(("ml", 0, "away"), "DraftKings", dec(o["aml"]), 0)
    if o.get("hs") is not None and o.get("hsp") is not None: add(("spread", o["hs"], "home"), "DraftKings", dec(o["hsp"]), 0)
    if o.get("hs") is not None and o.get("asp") is not None: add(("spread", o["hs"], "away"), "DraftKings", dec(o["asp"]), 0)
    if o.get("t") is not None and o.get("op") is not None: add(("total", o["t"], "over"), "DraftKings", dec(o["op"]), 0)
    if o.get("t") is not None and o.get("up") is not None: add(("total", o["t"], "under"), "DraftKings", dec(o["up"]), 0)
    k = g.get("kalshi") or {}; url = k.get("url") or {}
    H, A = g["home"]["abbr"], g["away"]["abbr"]
    for side, t in (("home", H), ("away", A)):
        p = (k.get("ml") or {}).get(t)
        if p and p.get("ya") and p["ya"] < 0.99: add(("ml", 0, side), "Kalshi", 1 / kcost(p["ya"]), 0, url.get("ml"))
    for t, rungs in (k.get("spread") or {}).items():
        home = t == H
        for r in rungs:
            line = -r["l"] if home else r["l"]          # home spread number for this rung (home -8.5 / away -8.5 = home +8.5)
            fav, dog = ("home", "away") if home else ("away", "home")
            if r.get("ya") and r["ya"] < 0.99: add(("spread", line, fav), "Kalshi", 1 / kcost(r["ya"]), 0, url.get("spread"))
            if r.get("na") and r["na"] < 0.99: add(("spread", line, dog), "Kalshi", 1 / kcost(r["na"]), 0, url.get("spread"))
    for r in k.get("total") or []:
        if r.get("ya") and r["ya"] < 0.99: add(("total", r["l"], "over"), "Kalshi", 1 / kcost(r["ya"]), 0, url.get("total"))
        if r.get("na") and r["na"] < 0.99: add(("total", r["l"], "under"), "Kalshi", 1 / kcost(r["na"]), 0, url.get("total"))
    return out


PAIRS = {"home": "away", "over": "under"}


def find(games, props, books_at):
    """Every gap across the slate, best first. Also puts each game's own list on g["arbs"]."""
    books_age = round((time.time() - books_at) / 3600, 1) if books_at else 99
    found = []
    for g in games:
        if g.get("state") != "pre": continue
        H, A = g["home"]["abbr"], g["away"]["abbr"]
        offers = _offers(g, books_age)
        gl = []
        for (mkt, line, side), xs in offers.items():
            if side not in PAIRS: continue
            ys = offers.get((mkt, line, PAIRS[side]))
            if not ys: continue
            a = max(xs, key=lambda x: x["d"]); b = max((y for y in ys if y["src"] != a["src"]), key=lambda y: y["d"], default=None)
            if not b: continue
            s = 1 / a["d"] + 1 / b["d"]
            if s >= 1 - MIN_MARGIN: continue
            nm = {"ml": lambda sd: f"{H if sd == 'home' else A} ML",
                  "spread": lambda sd: f"{H} {line:+g}" if sd == "home" else f"{A} {-line:+g}",
                  "total": lambda sd: f"{'Over' if sd == 'over' else 'Under'} {line:g}"}[mkt]
            legs = [{"pick": nm(sd), "src": x["src"], "odds": am_from_dec(x["d"]), "age": x["age"], "url": x.get("url"), "share": round((1 / x["d"]) / s, 4)}
                    for sd, x in ((side, a), (PAIRS[side], b))]
            gl.append({"gid": g["id"], "game": f"{A} @ {H}", "mkt": mkt, "margin": round(1 / s - 1, 4), "legs": legs,
                       "fresh": all(l["age"] == 0 for l in legs), "kick": g["date"]})
        # anytime TD: a book's Yes vs Kalshi's No on "1+ touchdowns"
        for pl in (props or {}).get(g["id"], []):
            pr = next((x for x in pl.get("props", []) if x.get("m") == "Anytime TD"), None)
            if not pr: continue
            k = next((r for r in pr.get("alt") or [] if r.get("l") == 0.5 and r.get("na") and r["na"] < 0.99), None)
            bks = [x for x in pr.get("books") or [] if x.get("o") is not None and x["n"] not in OFFSHORE]
            if not k or not bks: continue
            bk = max(bks, key=lambda x: x["o"]); dy, dn = dec(bk["o"]), 1 / kcost(k["na"])
            s = 1 / dy + 1 / dn
            if s >= 1 - MIN_MARGIN: continue
            gl.append({"gid": g["id"], "game": f"{A} @ {H}", "mkt": "td", "margin": round(1 / s - 1, 4), "kick": g["date"], "fresh": False,
                       "legs": [{"pick": f"{pl['n']} scores", "src": bk["n"], "odds": bk["o"], "age": books_age, "url": bk.get("lo"), "share": round((1 / dy) / s, 4)},
                                {"pick": f"{pl['n']} doesn't score", "src": "Kalshi", "odds": am_from_dec(dn), "age": 0, "url": pr.get("kx"), "share": round((1 / dn) / s, 4)}]})
        # one per market and line is plenty (the best pairing); keep the top few per game
        gl.sort(key=lambda x: -x["margin"])
        g["arbs"] = gl[:4]
        found += gl[:4]
    return sorted(found, key=lambda x: -x["margin"])
