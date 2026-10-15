"""NBA fantasy draft board for a season-points league (default: NBA Hoopas, Yahoo 245728: PTS + REB + AST + STL + BLK - TO,
8 teams, 10 starters + 3 bench). Season points = points per game x games, so availability counts as much as talent.

  per game   Yahoo's 2025-26 fantasy points per game under the league's scoring, blended 75/25 with 2024-25 (box scores)
             to smooth a fluky year; players who missed all of 2025-26 use 2024-25
  games      2025-26 games, 2024-25 games and a 70-game baseline (50 / 30 / 20), capped at 80; a full lost season counts as
             a risk, not a zero (Achilles / ACL returns get 55)
  value      season points over replacement (the 81st-best player: 8 teams x 10 starters)
  tiers      a new tier wherever the value drops 3%+ of the top player's value between neighbours
  age        young players improve, older ones fade: per game x (+7% at 21 and under ... -12% at 37+), games x 0.92 at 34+ and
             0.88 at 37+ (more rest days); ages from ESPN (data/nba/ages.json)
  vs Yahoo   our rank 12+ spots ahead of Yahoo's preseason rank = sleeper; 12+ behind = risk (usually games played)
Rookies have no NBA stats: they keep Yahoo's rank and are flagged.
Run:  python nba_draft.py      -> data/nba/draft.json
"""
import csv, io, json, os, re, sys
from collections import defaultdict
from urllib.request import urlopen, Request

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data", "nba")
BOX = "https://github.com/sportsdataverse/sportsdataverse-data/releases/download/espn_nba_player_boxscores/player_box_{}.csv"
TEAMS, STARTERS = 8, 10
import unicodedata
norm = lambda s: re.sub(r"(iii|ii|iv|jr|sr)$", "", re.sub(r"[^a-z]", "", unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()))


def season_fp(season):
    """{name key: (fantasy points per game, games)} for a season, league scoring."""
    p = os.path.join(DATA, "box", f"fp_{season}.json")
    if os.path.exists(p): return json.load(open(p, encoding="utf-8"))
    t = urlopen(Request(BOX.format(season), headers={"User-Agent": "edge-board"}), timeout=240).read().decode()
    tot, g = defaultdict(float), defaultdict(int)
    f = lambda x: float(x) if x not in (None, "", "NA") else 0.0
    for r in csv.DictReader(io.StringIO(t)):
        if r.get("season_type") != "2" or r.get("did_not_play") == "true" or not f(r.get("minutes")): continue
        k = norm(r["athlete_display_name"])
        tot[k] += f(r["points"]) + f(r["rebounds"]) + f(r["assists"]) + f(r["steals"]) + f(r["blocks"]) - f(r["turnovers"]); g[k] += 1
    out = {k: (round(tot[k] / g[k], 2), g[k]) for k in g}
    os.makedirs(os.path.dirname(p), exist_ok=True); json.dump(out, open(p, "w", encoding="utf-8"))
    return out


def build(yahoo_path=os.path.join(DATA, "yahoo_ranks_hoopas.json")):
    yr = json.load(open(yahoo_path, encoding="utf-8"))
    prev = season_fp(2025)
    try: ages = json.load(open(os.path.join(DATA, "ages.json")))
    except FileNotFoundError: ages = {}
    def curve(a):
        if not a: return 1.0, 1.0
        per = 1.07 if a <= 21 else 1.04 if a <= 23 else 1.01 if a <= 27 else 1.0 if a <= 30 else 0.98 if a <= 32 else 0.95 if a <= 34 else 0.92 if a <= 36 else 0.88
        return per, 0.88 if a >= 37 else 0.92 if a >= 34 else 1.0
    rows = []
    for name, tm, pos, gp, fpg, yrank in yr:
        p25 = prev.get(norm(name))
        rookie = gp == 0 and not p25
        if gp:
            per = 0.75 * fpg + 0.25 * p25[0] if p25 and p25[1] >= 20 else fpg
            games = 0.5 * gp + 0.3 * (p25[1] if p25 else 60) + 0.2 * 70
        elif p25:   # missed all of last season: his 2024-25 rate, a cautious games number
            per, games = p25[0], 55
        else: per, games = None, None
        age = ages.get(norm(name)); cp, cg = curve(age)
        if per: per *= cp
        games = min(80, games * cg) if games else None
        notes = []
        if rookie: notes.append("rookie: no NBA stats")
        elif gp == 0: notes.append("missed all of 2025-26")
        elif gp < 50: notes.append(f"played {gp} games last season")
        if age and (age <= 22 or age >= 34): notes.append(f"age {age}")
        rows.append({"n": name, "tm": tm, "pos": pos, "age": age, "gp": gp, "fpg": fpg, "yr": yrank, "per": round(per, 1) if per else None,
                     "g": round(games) if games else None, "pts": round(per * games) if per else None, "note": "; ".join(notes)})
    have = sorted((r for r in rows if r["pts"]), key=lambda r: -r["pts"])
    repl = have[TEAMS * STARTERS]["pts"] if len(have) > TEAMS * STARTERS else 0
    for r in rows: r["val"] = round(r["pts"] - repl) if r["pts"] else None
    # order: by value; rookies slot in at Yahoo's rank
    ranked = sorted(have, key=lambda r: -r["val"])
    for r in sorted((r for r in rows if not r["pts"]), key=lambda r: r["yr"]): ranked.insert(min(len(ranked), r["yr"] - 1), r)
    top = ranked[0]["val"] or 1; tier = 1; last = None
    for i, r in enumerate(ranked):
        r["rk"] = i + 1
        if r["val"] is not None:
            if last is not None and last - r["val"] >= 0.03 * top: tier += 1
            last = r["val"]
        r["tier"] = tier
        d = r["yr"] - r["rk"]
        r["tag"] = "sleeper" if d >= 12 and r["val"] is not None else "risk" if d <= -12 and r["val"] is not None else ""
    doc = {"league": "NBA Hoopas", "teams": TEAMS, "starters": STARTERS, "scoring": "PTS + REB + AST + STL + BLK - TO", "repl": repl, "players": ranked}
    json.dump(doc, open(os.path.join(DATA, "draft.json"), "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    return ranked


if __name__ == "__main__":
    R = build()
    for r in R[:30]: print(r["rk"], r["n"], r["pos"], r["per"], r["g"], r["pts"], r["val"], "T" + str(r["tier"]), "Y" + str(r["yr"]), r["tag"], r["note"])
    print("sleepers:", [(r["n"], r["rk"], r["yr"]) for r in R if r["tag"] == "sleeper"][:12])
    print("risks:", [(r["n"], r["rk"], r["yr"]) for r in R if r["tag"] == "risk"][:12])
