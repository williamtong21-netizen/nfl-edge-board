"""Game-sim report card: is the 2,000-game sim's win / cover / over chance trustworthy?

The sim is anchored to the market (spread + total), so it should land close to the market; this checks that it does and
that its chances are calibrated (games it calls 70% go about 70%), not that it beats anyone.

record   every sync, each upcoming game's sim summary (win / cover / over chance, the lines it was run on, the closing
         moneylines' no-vig win chance) is saved and kept updating until kickoff, then frozen
grade    once final: Brier scores (sim vs market for winners; vs a coin flip for cover / over, since a fair line is 50/50)
         and calibration buckets
backfill weeks played before the sim existed: lines-only sims (closing spread / total from nflverse, that game's players
         with usage from earlier games; team tables are this season's, so slightly after the fact) so the card has history from week 1; marked "bf"
State: state/sim_record_<season>.json (EDGE_DATA)
"""
import json, os, sys

import analytics

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("EDGE_DATA") or os.path.join(ROOT, "data")
BUCKETS = ((0, .3), (.3, .45), (.45, .55), (.55, .7), (.7, 1.01))


def path(season): return os.path.join(DATA, f"sim_record_{season}.json")


def load(season):
    try: return json.load(open(path(season), encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError): return {"season": season, "games": {}}


def novig(hml, aml):
    """home win chance from American moneylines, vig removed."""
    if hml is None or aml is None: return None
    imp = lambda o: 100 / (o + 100) if o > 0 else -o / (-o + 100)
    h, a = imp(hml), imp(aml)
    return h / (h + a)


def record(rec, games, sim_idx):
    """pre-kickoff: (re)write each upcoming game's entry from the slate's sim index."""
    for g in games:
        s = sim_idx.get(g["id"])
        if g.get("state") != "pre" or not s: continue
        o = g.get("odds") or {}
        rec["games"][g["id"]] = {"w": g.get("week"), "h": g["home"]["abbr"], "a": g["away"]["abbr"], "hw": s.get("hw"), "hc": s.get("hc"),
                                 "hpush": s.get("hpush") or 0, "ov": s.get("ov"), "tpush": s.get("tpush") or 0, "hs": (s.get("mkt") or {}).get("hs"),
                                 "t": (s.get("mkt") or {}).get("t"), "pm": novig(o.get("hml"), o.get("aml")), "basis": s.get("basis")}


def grade(rec, finals):
    """finals: {espn id: (home pts, away pts)}; adds the result to every recorded game that has finished."""
    for gid, r in rec["games"].items():
        f = finals.get(gid)
        if f is None or "res" in r: continue
        hp, ap = f; m = hp - ap
        r["res"] = {"hp": hp, "ap": ap,
                    "hw": None if m == 0 else int(m > 0),
                    "hc": None if r["hs"] is None or m + r["hs"] == 0 else int(m + r["hs"] > 0),
                    "ov": None if r["t"] is None or hp + ap == r["t"] else int(hp + ap > r["t"])}


def summary(rec):
    """the card: Brier sim vs market / coin flip, calibration, 55%+ calls' record, per week."""
    done = [r for r in rec["games"].values() if "res" in r]
    out = {"n": len(done), "bf": sum(1 for r in done if r.get("bf")), "weeks": {}}
    def block(rs):
        b = {}
        for k in ("hw", "hc", "ov"):
            rows = [r for r in rs if r["res"][k] is not None and r.get(k) is not None]
            if not rows: continue
            # cover / over: the sim's chance that the home side covers / the game goes over, with pushes left out
            push = {"hc": "hpush", "ov": "tpush"}.get(k)
            p = lambda r: min(1.0, r[k] / (1 - r.get(push, 0)) if push and r.get(push, 0) < 1 else r[k])
            sse = sum((p(r) - r["res"][k]) ** 2 for r in rows) / len(rows)
            if k == "hw":
                mk = [r for r in rows if r.get("pm") is not None]
                base = sum((r["pm"] - r["res"][k]) ** 2 for r in mk) / len(mk) if mk else None
                bsim = sum((p(r) - r["res"][k]) ** 2 for r in mk) / len(mk) if mk else None
            else:
                base, bsim = 0.25, sse
            calls = [r for r in rows if abs(p(r) - 0.5) >= 0.05]
            wins = sum((p(r) > 0.5) == bool(r["res"][k]) for r in calls)
            cal = []
            for lo, hi in BUCKETS:
                g = [r for r in rows if lo <= p(r) < hi]
                if g: cal.append([lo, hi, len(g), round(sum(p(r) for r in g) / len(g), 3), round(sum(r["res"][k] for r in g) / len(g), 3)])
            b[k] = {"n": len(rows), "sim": round(bsim, 4) if bsim is not None else None, "mkt": round(base, 4) if base is not None else None,
                    "calls": len(calls), "won": wins, "cal": cal}
        return b
    out.update(block(done))
    for w in sorted({r["w"] for r in done if r.get("w")}):
        rs = [r for r in done if r.get("w") == w]
        wb = block(rs)
        out["weeks"][str(w)] = {"n": len(rs), **{k: {"sim": v["sim"], "mkt": v["mkt"], "calls": v["calls"], "won": v["won"]} for k, v in wb.items()}}
    return out


def nflverse_finals(season):
    out = {}
    for g in analytics.rows("schedules/games.csv.gz", 6 * 3600):
        if g["season"] == str(season) and g["game_type"] == "REG" and g.get("espn") and g["result"] not in ("", "NA", None):
            out[g["espn"]] = (int(float(g["home_score"])), int(float(g["away_score"])))
    return out


def update(games, sim_idx, season, week=None, backfill_max=16):
    """sync hook: record upcoming games, backfill a few earlier games per run, grade finals, return the card."""
    rec = load(season)
    record(rec, games, sim_idx)
    if week:
        try: backfill_capped(rec, season, week, backfill_max)   # capped per sync (~3 s a game), oldest first
        except Exception as e:
            print("sim backfill failed", e, file=sys.stderr)
    finals = nflverse_finals(season)
    for g in games:   # ESPN finals come in before nflverse posts them
        if g.get("state") == "post":
            try: finals[g["id"]] = (int(float(g["home"]["score"])), int(float(g["away"]["score"])))
            except (KeyError, TypeError, ValueError): pass
    grade(rec, finals)
    os.makedirs(os.path.dirname(path(season)), exist_ok=True)
    with open(path(season), "w", encoding="utf-8") as fh: json.dump(rec, fh, separators=(",", ":"))
    return summary(rec)


def usage_rows(season, week, H, A, cur, prev):
    """the players who suited up in that game (nflverse box), with target / carry shares and catch rate from games
    before it (last season at half weight), one QB a side (most attempts): the sim's lines-only rows, point in time."""
    f = lambda v: float(v) if v not in ("", "NA", None) else 0.0
    games = [(r, 0.5) for r in prev] + [(r, 1.0) for r in cur if int(r["week"]) < week]
    tt, tc, hist = {}, {}, {}
    for r, _ in games:
        k = (r["season"], r["game_id"], r["team"]); tt[k] = tt.get(k, 0) + f(r.get("targets")); tc[k] = tc.get(k, 0) + f(r.get("carries"))
    for r, w in games: hist.setdefault(r["player_id"], []).append((int(r["season"]), int(r["week"]), w, r))
    def use(pid):
        h = sorted(hist.get(pid, []), key=lambda x: (x[0], x[1]), reverse=True)[:10]
        if not h: return {}
        sw = ts = cs = rec = tg = 0.0
        for age, (_, _, w, r) in enumerate(h):
            wt = w * 0.85 ** age; k = (r["season"], r["game_id"], r["team"]); sw += wt
            ts += wt * (f(r.get("targets")) / tt[k] if tt.get(k) else 0); cs += wt * (f(r.get("carries")) / tc[k] if tc.get(k) else 0)
            rec += wt * f(r.get("receptions")); tg += wt * f(r.get("targets"))
        return {"ts": ts / sw, "rsh": cs / sw, "cr": (rec + 10) / (tg + 15)}
    out = []
    for side, tm in (("home", H), ("away", A)):
        ps = [r for r in cur if int(r["week"]) == week and analytics.fix(r["team"]) == tm and r.get("position_group") in ("QB", "RB", "WR", "TE")]
        qbs = sorted((r for r in ps if r["position_group"] == "QB"), key=lambda r: -f(r.get("attempts")))
        for r in ps:
            if r["position_group"] == "QB" and r is not qbs[0]: continue
            u = use(r["player_id"])
            out.append({"id": r["player_id"], "n": r["player_display_name"], "p": r["position_group"], "side": side, "props": [],
                        "u": {"ts": u.get("ts", 0) if r["position_group"] != "QB" else 0, "rsh": u.get("rsh", 0), "cr": u.get("cr")}})
    return out


def backfill_capped(rec, season, week, cap):
    """lines-only sims for finished weeks before the sim existed, from nflverse's closing lines and box-score rosters."""
    import gamesim
    todo = sorted((g for g in analytics.rows("schedules/games.csv.gz", 6 * 3600)
                   if g["season"] == str(season) and g["game_type"] == "REG" and int(g["week"]) < week and g.get("espn")
                   and g["espn"] not in rec["games"] and g["result"] not in ("", "NA") and g["spread_line"] not in ("", "NA")),
                  key=lambda g: int(g["week"]))[:cap]
    if not todo: return 0
    T = gamesim.load_tables()
    reg = lambda y: [r for r in analytics.rows(f"stats_player/stats_player_week_{y}.csv", 6 * 3600) if r.get("season_type") == "REG"]
    cur, prev = reg(season), reg(season - 1)
    f = lambda v: float(v) if v not in ("", "NA", None) else None
    for s in todo:
        H, A = analytics.fix(s["home_team"]), analytics.fix(s["away_team"])
        hs, t = -float(s["spread_line"]), float(s["total_line"])
        g = {"id": s["espn"], "home": {"abbr": H, "name": H}, "away": {"abbr": A, "name": A}, "odds": {"hs": hs, "t": t}}
        rows = usage_rows(season, int(s["week"]), H, A, cur, prev)
        sm = gamesim.simulate_game(g, rows, T, {r["id"]: r["id"] for r in rows}, gamesim.N_SIMS)["sum"]
        rec["games"][s["espn"]] = {"w": int(s["week"]), "h": H, "a": A, "hw": sm["hw"], "hc": sm.get("hc"), "hpush": sm.get("hpush") or 0,
                                   "ov": sm.get("ov"), "tpush": sm.get("tpush") or 0, "hs": hs, "t": t,
                                   "pm": novig(f(s.get("home_moneyline")), f(s.get("away_moneyline"))), "basis": "lines", "bf": 1,
                                   "pts": sm.get("pts"), "tgt": sm.get("target")}
        print("backfill sim", s["week"], A, "@", H, sm["hw"], sm["ov"], sm.get("pts"), sm.get("target"), file=sys.stderr)
    return len(todo)


if __name__ == "__main__":
    # local: backfill everything finished (python sim_report.py 2026 6) and print the card
    season, week = int(sys.argv[1]), int(sys.argv[2])
    rec = load(season)
    backfill_capped(rec, season, week, 999)
    grade(rec, nflverse_finals(season))
    with open(path(season), "w", encoding="utf-8") as fh: json.dump(rec, fh, separators=(",", ":"))
    print(json.dumps(summary(rec), indent=1)[:4000])
