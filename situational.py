"""Game situations, coaching tenure, and how player/game outcomes move together (for parlays).

coaches(season)                 -> each team's head coach, seasons with the team, and who he replaced
situations(games, season, co)   -> g["sit"]: rest, travel/body clock, neutral site, divisional, weather; points per side,
                                   a total adjustment and weather multipliers for player props
correlations(season)            -> measured same-side rates for pairs of outcomes (player stats vs their usual, game results)
"""
import json, os, time
from collections import defaultdict
from analytics import rows, fix, f, i, CACHE

# ---------------------------------------------------------------- settings
REST_PT = 0.06        # points per extra day of rest over the opponent (each side; a bye vs a normal week is ~0.8 of margin)
BODY_CLOCK = 0.6      # west-coast team playing a 1 PM Eastern kickoff out east
TRAVEL = 0.3          # road team crossing 2+ time zones
NEUTRAL_KEEP = 0.3    # share of home field a "home" team keeps at a neutral or international site
DIV_SQUEEZE = 0.9     # division games: projected margin pulled 10% toward even (rivals know each other)
DIV_TOTAL = -0.4      # ...and a touch lower scoring
WIND_FROM = 12        # mph where wind starts to matter
NEW_COACH_PREV_W = 0.3  # weight of last season's player games when the team has a first-year head coach (normally 0.6)

TZ = {**{t: 0 for t in ("BUF", "MIA", "NE", "NYJ", "BAL", "CIN", "CLE", "PIT", "JAX", "IND", "DET", "ATL", "CAR", "TB", "NYG", "PHI", "WSH")},
      **{t: -1 for t in ("TEN", "HOU", "KC", "DAL", "CHI", "GB", "MIN", "NO")}, "DEN": -2,
      **{t: -3 for t in ("ARI", "LV", "LAC", "LAR", "SF", "SEA")}}   # hours from Eastern during football season (Arizona skips DST)


def _sched():
    return rows("schedules/games.csv.gz", 6 * 3600)


# Where each first-year head coach last called plays or ran a unit. Head-coach history comes from the schedule file;
# coordinator jobs aren't in any free data feed, so they're listed here (update each offseason).
COACH_SYSTEM = {
    "Todd Monken": ("off", "BAL", 2025, "offensive coordinator"),
    "Klint Kubiak": ("off", "SEA", 2025, "offensive coordinator"),
    "Mike McCarthy": ("off", "DAL", 2024, "head coach and play-caller"),
    "Jesse Minter": ("def", "LAC", 2025, "defensive coordinator"),
    "Jeff Hafley": ("def", "GB", 2025, "defensive coordinator"),
    "Robert Saleh": ("def", "SF", 2025, "defensive coordinator"),
    "John Harbaugh": ("both", "BAL", 2025, "head coach"),
}
SYSTEM_TRUST = 0.7   # how much of his old unit's lean we expect him to bring along


def _pass_rates(season):
    """Each team's dropback rate on offense and allowed on defense that season, minus the league rate (from weekly stats)."""
    try: ws = [w for w in rows(f"stats_player/stats_player_week_{season}.csv", 60 * 86400) if w.get("season_type") == "REG"]
    except Exception: return {}
    o, d = defaultdict(lambda: [0.0, 0.0]), defaultdict(lambda: [0.0, 0.0])
    for w in ws:
        db = (f(w.get("attempts")) or 0) + (f(w.get("sacks_suffered")) or 0); car = f(w.get("carries")) or 0
        for tbl, t in ((o, fix(w["team"])), (d, fix(w.get("opponent_team") or ""))):
            tbl[t][0] += db; tbl[t][1] += db + car
    lg = sum(v[0] for v in o.values()) / max(1, sum(v[1] for v in o.values()))
    return {t: {"op": o[t][0] / o[t][1] - lg if o[t][1] else 0, "dp": d[t][0] / d[t][1] - lg if d[t][1] else 0} for t in o}


def coaches(season):
    seen = defaultdict(dict)
    for g in _sched():
        if g["game_type"] != "REG": continue
        for s in ("home", "away"):
            if g.get(f"{s}_coach"): seen[fix(g[f"{s}_team"])][int(g["season"])] = g[f"{s}_coach"]
    out = {}
    for t, by in seen.items():
        cur = by.get(season)
        if not cur: continue
        yrs, y = 0, season
        while by.get(y) == cur: yrs += 1; y -= 1
        out[t] = {"n": cur, "yrs": yrs, "since": season - yrs + 1, "prev": by.get(season - yrs)}
    cache = {}
    for t, c in out.items():
        sysd = COACH_SYSTEM.get(c["n"]) if c["yrs"] == 1 else None
        if not sysd: continue
        side, team, yr, role = sysd
        if yr not in cache: cache[yr] = _pass_rates(yr)
        r = cache[yr].get(team)
        if not r: continue
        c["sys"] = {"side": side, "team": team, "yr": yr, "role": role,
                    "op": round(r["op"] * SYSTEM_TRUST, 3) if side in ("off", "both") else None,
                    "dp": round(r["dp"] * SYSTEM_TRUST, 3) if side in ("def", "both") else None}
    return out


def situations(games, season, co=None):
    by_espn = {g.get("espn"): g for g in _sched() if int(g["season"] or 0) == season}
    for g in games:
        g.pop("sit", None)
        if g["state"] != "pre": continue
        row = by_espn.get(g["id"])
        H, A = g["home"]["abbr"], g["away"]["abbr"]
        pts, tot, notes = {"home": 0.0, "away": 0.0}, 0.0, []
        if row:
            hr, ar = f(row.get("home_rest")), f(row.get("away_rest"))
            if hr is not None and ar is not None and abs(hr - ar) >= 2:
                d = max(-7, min(7, hr - ar)); pts["home"] += REST_PT * d; pts["away"] -= REST_PT * d
                more, less = (H, A) if d > 0 else (A, H)
                notes.append({"k": "rest", "s": f"{more} comes in rested: {int(max(hr, ar))} days since its last game to {less}'s {int(min(hr, ar))}.", "pts": round(REST_PT * abs(d) * 2, 2), "team": more})
            if row.get("div_game") == "1": notes.append({"k": "div", "s": f"{A} at {H} is a division game. Rivals who meet twice a year tend to keep it closer and a bit lower scoring.", "pts": 0})
        neutral = bool(g.get("neutral"))
        if neutral:
            from projections import HFA
            k = HFA / 2 * (1 - NEUTRAL_KEEP); pts["home"] -= k; pts["away"] += k
            notes.append({"k": "neutral", "s": f"Neutral site ({g.get('venue') or 'away from home'}), so {H} gets little home-field edge.", "pts": round(2 * k, 2), "team": A})
        else:
            et = (row or {}).get("gametime") or ""
            venue_tz = TZ.get(H)
            for t in (A,):
                if TZ.get(t) == -3 and venue_tz is not None and venue_tz >= -1 and et and et < "14:00":
                    pts["away"] -= BODY_CLOCK
                    notes.append({"k": "clock", "s": f"West-coast {t} plays a {et} Eastern kickoff out east; body-clock games have been tough on them.", "pts": BODY_CLOCK, "team": t})
                elif TZ.get(t) is not None and venue_tz is not None and abs(TZ[t] - venue_tz) >= 2:
                    pts["away"] -= TRAVEL
                    notes.append({"k": "travel", "s": f"{t} travels {abs(TZ[t] - venue_tz)} time zones for this one.", "pts": TRAVEL, "team": t})
        # weather (outdoors only): wind and rain cut passing and scoring; cold trims scoring
        wx = g.get("wx") if not g.get("indoor") else None
        mult = {"pass": 1.0, "long": 1.0, "rush": 1.0}
        if wx:
            w = wx.get("wind") or 0
            if w > WIND_FROM:
                x = w - WIND_FROM
                tot -= min(4.0, 0.25 * x)
                mult["pass"] -= min(0.15, 0.012 * x); mult["long"] -= min(0.2, 0.016 * x); mult["rush"] += min(0.05, 0.005 * x)
            if (wx.get("pop") or 0) >= 60 and (wx.get("rain") or 0) >= 0.1:
                tot -= 1.0; mult["pass"] -= 0.03; mult["long"] -= 0.03; mult["rush"] += 0.02
            if wx.get("temp") is not None and wx["temp"] < 25: tot -= 1.0
            if tot: notes.append({"k": "wx", "s": "Weather should hold scoring down and tilt both offenses toward the run.", "pts": round(-tot, 2)})
        div = bool(row and row.get("div_game") == "1")
        if div: tot += DIV_TOTAL
        cn = []
        for side, t in (("home", H), ("away", A)):
            c = (co or {}).get(t)
            if c and c["yrs"] <= 2: cn.append({"team": t, "side": side, **c})
        g["sit"] = {"home": round(pts["home"] + tot / 2, 2), "away": round(pts["away"] + tot / 2, 2), "tot": round(tot, 2), "div": div,
                    "wx": {k: round(v, 3) for k, v in mult.items()}, "notes": notes, "coach": cn}


# ---------------------------------------------------------------- correlations
def _roles(ws):
    """Role per (season, team, player): QB1, RB1-2, WR1-3, TE1 by season volume."""
    vol = defaultdict(lambda: defaultdict(float)); pos = {}
    for w in ws:
        k = (w["season"], w["team"]); pid = w["player_id"]; pos[pid] = w.get("position")
        vol[k][pid] += {"QB": f(w.get("attempts")), "RB": f(w.get("carries")), "WR": f(w.get("targets")), "TE": f(w.get("targets"))}.get(w.get("position"), 0) or 0
    role = {}
    for k, v in vol.items():
        for p, n, lbl in (("QB", 1, "QB"), ("RB", 2, "RB"), ("WR", 3, "WR"), ("TE", 1, "TE")):
            ranked = sorted((x for x in v if pos.get(x) == p and v[x] > 0), key=lambda x: -v[x])[:n]
            for j, x in enumerate(ranked): role[(k[0], k[1], x)] = f"{lbl}{j + 1}"
    return role


FEAT = {"QB": {"P": ["passing_yards"], "PT": ["passing_tds"]},
        "RB": {"R": ["rushing_yards"], "Y": ["receiving_yards"], "C": ["receptions"]},
        "WR": {"Y": ["receiving_yards"], "C": ["receptions"]}, "TE": {"Y": ["receiving_yards"], "C": ["receptions"]}}


def correlations(season, years=3):
    path = os.path.join(CACHE, "corr.json")
    try:
        with open(path, encoding="utf-8") as fh: hit = json.load(fh)
        if hit.get("season") == season and time.time() - hit.get("t", 0) < 14 * 86400: return hit
    except (FileNotFoundError, json.JSONDecodeError): pass
    ws = []
    for y in range(season - years, season):
        try: ws += [w for w in rows(f"stats_player/stats_player_week_{y}.csv", 60 * 86400) if w.get("season_type") == "REG"]
        except Exception: pass
    sched = {g["game_id"]: g for g in _sched() if g["game_type"] == "REG"}
    role = _roles(ws)
    # each player's values per team-season, for leave-one-out averages (a stand-in for the book's line)
    vals = defaultdict(list)
    for w in ws:
        r = role.get((w["season"], w["team"], w["player_id"]))
        if not r: continue
        for m, cols in FEAT[r[:2]].items(): vals[(w["season"], w["team"], w["player_id"], m)].append(sum(f(w.get(c)) or 0 for c in cols))
    feats = defaultdict(dict)   # (game_id, team) -> feature -> bool
    for w in ws:
        r = role.get((w["season"], w["team"], w["player_id"]))
        if not r: continue
        key = (w["game_id"], w["team"])
        for m, cols in FEAT[r[:2]].items():
            allv = vals[(w["season"], w["team"], w["player_id"], m)]
            if len(allv) < 4: continue
            v = sum(f(w.get(c)) or 0 for c in cols)
            loo = (sum(allv) - v) / (len(allv) - 1)
            if v != loo: feats[key][f"{r}.{m}"] = v > loo
        if r[:2] != "QB": feats[key][f"{r}.T"] = ((f(w.get("rushing_tds")) or 0) + (f(w.get("receiving_tds")) or 0)) > 0
    for (gid, team), d in list(feats.items()):
        g = sched.get(gid)
        if not g or g.get("result") in ("", "NA", None) or g.get("spread_line") in ("", "NA"): continue
        home = team == g["home_team"]
        res, line, tot, tl = f(g["result"]), f(g["spread_line"]), f(g["total"]), f(g["total_line"])
        margin, exp = (res, line) if home else (-res, -line)
        if margin: d["G.W"] = margin > 0
        if margin != exp: d["G.S"] = margin > exp
        if tot is not None and tl is not None and tot != tl: d["G.O"] = tot > tl
        if tl is not None:
            pts, imp = (f(g["home_score"]) if home else f(g["away_score"])), (tl + exp) / 2
            if pts is not None and pts != imp: d["G.TT"] = pts > imp
    cnt = defaultdict(lambda: [0, 0, 0, 0])   # n11, n10, n01, n00
    def add(rel, a, va, b, vb):
        if a > b: a, va, b, vb = b, vb, a, va   # name order only, so identical pairs keep both value orders
        c = cnt[f"{a}|{b}|{rel}"]; c[0 if va and vb else 1 if va else 2 if vb else 3] += 1
    by_game = defaultdict(dict)
    for (gid, team), d in feats.items(): by_game[gid][team] = d
    for gid, teams in by_game.items():
        ts = list(teams)
        for t in ts:
            items = list(teams[t].items())
            for x in range(len(items)):
                for y in range(x + 1, len(items)): add("same", *items[x], *items[y])
        if len(ts) == 2:
            for a, va in teams[ts[0]].items():
                if a == "G.O": continue
                for b, vb in teams[ts[1]].items():
                    if b != "G.O": add("opp", a, va, b, vb)
    out = {}
    for k, (n11, n10, n01, n00) in cnt.items():
        n = n11 + n10 + n01 + n00
        if n < 150: continue
        den = ((n11 + n10) * (n01 + n00) * (n11 + n01) * (n10 + n00)) ** 0.5
        if not den: continue
        out[k] = {"r": round((n11 * n00 - n10 * n01) / den, 3), "same": round((n11 + n00) / n, 3), "n": n}
    res = {"season": season, "t": time.time(), "years": f"{season - years}-{season - 1}", "pairs": out}
    with open(path, "w", encoding="utf-8") as fh: json.dump(res, fh)
    return res
