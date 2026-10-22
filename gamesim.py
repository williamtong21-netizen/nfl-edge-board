"""Play-by-play game simulator: each upcoming game played 2,000 times, snap by snap.

Every snap: play call (down, distance, field, score, clock, team pass rate over expected) -> on a dropback the defense's
coverage (its 2025 mix) and pressure (both lines' 2026 QB-hit rates) -> sack / scramble / throwaway / interception /
target (target shares, adjusted for how each receiver does vs man or zone) -> catch and yards (real NFL yardage samples
by position and field zone, scaled per player) -> clock (seconds by outcome; hurry-up, milking, two-minute warning,
timeouts). Runs: carrier by carry share, inside or outside, the defense's 2026 run defense that way. 4th downs, field
goals, punts and kickoffs from real 2023-26 decisions and results.

Anchored to the market: before the 2,000 runs each team is calibrated (6 rounds of 300 games, same random numbers each
round) until the sim reproduces both teams' implied points (spread + total) and every player's main prop at our blended
chance (pOver): pass yards / attempts / completions / TDs / INTs, rush yards / carries, receptions / receiving yards,
anytime TD. So the sim doesn't claim a better side, total or prop than the line; what it adds is how everything moves
together (same-game parlays), full distributions (alt lines) and a watchable "most typical" game.

Output per game: data/sim/<game id>.json -> summary (win / cover / over, common final scores), each player's
distribution, the typical game's play-by-play, and the latent correlations between every main-line outcome (for
same-game parlay pricing in the app).
"""
import bisect, json, math, os, random, sys, time, zlib

ROOT = os.path.dirname(os.path.abspath(__file__))
TABLES = os.path.join(os.environ.get("EDGE_DATA") or os.path.join(ROOT, "data"), "sim_tables.json")
VERSION = 6   # bump when the engine changes so every game re-simulates
N_SIMS, CAL_ROUNDS, CAL_N, POLISH_N, POLISH_ROUNDS = 2000, 12, 500, 1500, 7
ST = ("att", "cmp", "pyd", "ptd", "int", "car", "ryd", "rtd", "tgt", "rec", "cyd", "ctd", "lng")
S = {k: i for i, k in enumerate(ST)}
MKT = {"Pass yds": ("pyd",), "Pass att": ("att",), "Completions": ("cmp",), "Pass TD": ("ptd",), "INT": ("int",),
       "Rush yds": ("ryd",), "Carries": ("car",), "Receptions": ("rec",), "Rec yds": ("cyd",), "Long rec": ("lng",),
       "Rush+rec yds": ("ryd", "cyd"), "Pass+rush yds": ("pyd", "ryd"), "Anytime TD": ("rtd", "ctd")}
MAN = {"COVER_0", "COVER_1", "2_MAN"}
COV_NAME = {"COVER_0": "Cover 0", "COVER_1": "Cover 1", "COVER_2": "Cover 2", "COVER_3": "Cover 3", "COVER_4": "Cover 4",
            "COVER_6": "Cover 6", "COVER_9": "Cover 9", "2_MAN": "2-Man"}
SUS_K = 1.12     # gains on 3rd / 4th down outside the red zone (sim 3rd-down rate -> real 2025 39.8%)
PREVENT_K = 1.2   # late, down 4-16: softer coverage (pushes final margins onto 3 and 7 like real games)
XP = 0.955
K_PE_NOLINE = 0.82   # yards-per-catch scale when a team has no pass-yds line: sim pass yds / team = real (sim_leans_backtest tuning)
TWO_PT_CHART = {-2, -5, -9, -10, -12, -16, 1, 5, 12}   # margin after the six, when trailing / leading late
TWO_PT_BASE, TWO_PT_P = 0.03, 0.48
SHOCK_SD, PACE_SD = 0.095, 0.0  # game-day form: one side plays better than expected, the other worse (sim margins were tighter than real; totals already matched)
FGMODE_S, FGMODE_P, FGTIE_S, OT_FG_P = 110, 0.8, 90, 0.9  # play-for-the-FG window (seconds left in the 4th) and make chance needed
K_FIN, K_SUS = (0.4, 2.0), (0.6, 1.5)   # points knob ranges (red-zone finishing, money-down sustain)
CLOCK_K = 1.08    # runoff per snap vs the table: penalties, replay reviews and injuries also run clock (tuned to 2025 snaps)
SQ2 = math.sqrt(2)
Phi = lambda x: 0.5 * math.erfc(-x / SQ2)


def yd(n):
    return f"{n} yard" if abs(n) == 1 else f"{n} yards"


def norm_name(s):
    import re, unicodedata
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"\b(jr|sr|ii|iii|iv|v)\b", "", re.sub(r"[^a-z ]", "", s)).strip()


class Player:
    __slots__ = ("id", "n", "short", "pos", "side", "gsis", "tw", "cw", "cr", "ym", "rm", "tdw", "out", "io", "mz", "lines", "idx")

    def __init__(self, **k):
        for s in self.__slots__: setattr(self, s, k.get(s))


class Team:
    def __init__(self, abbr, name, side):
        self.abbr, self.name, self.side = abbr, name, side
        self.qb = None; self.recv = []; self.rush = []
        self.k_pe = self.k_comp = self.k_int = self.k_fin = self.k_rzp = self.k_sus = 1.0; self.k_pr = 0.0
        self.pts_target = 21.0; self.tm = {}; self.cov = None; self.qbp = (1.0, 1.0)


def load_tables():
    import sim_tables
    return sim_tables.ensure()


# ---------------------------------------------------------------- building the two teams from the slate + props
def build_teams(g, rows, T, gsis_of):
    out = {}
    injured = {}
    for side in ("home", "away"):
        for i in (g[side].get("inj") or []):
            if i.get("s") in ("Out", "Doubtful", "Injured Reserve"): injured[norm_name(i["n"])] = i["s"]
    imp = g.get("proj") or {}
    o = g.get("odds") or {}
    for side in ("home", "away"):
        tm = Team(g[side]["abbr"], g[side].get("name") or g[side]["abbr"], side)
        if imp.get("h") is not None: tm.pts_target = imp["h"] if side == "home" else imp["a"]
        elif o.get("t") is not None and o.get("hs") is not None: tm.pts_target = (o["t"] - o["hs"]) / 2 if side == "home" else (o["t"] + o["hs"]) / 2
        tm.pts_target += 0.2   # totals are set near the median; real NFL scoring averages 0.4 above the closing total (2015-25)
        tm.tm = T["team"].get(tm.abbr, {})
        ps = []
        for r in rows:
            if r.get("side") != side or norm_name(r["n"]) in injured: continue
            u = r.get("u") or {}
            lines = {}
            for pr in r.get("props", []):
                if pr["m"] in MKT and pr.get("l") is not None and pr.get("pOver") is not None: lines[pr["m"]] = (pr["l"], pr["pOver"])
            pos = r.get("p") or "WR"
            gs = gsis_of.get(str(r["id"]))
            last = r["n"].replace(" Jr.", "").replace(" Sr.", "").replace(" III", "").replace(" II", "").split(" ")[-1]
            ps.append(Player(id=str(r["id"]), n=r["n"], short=last, pos=pos, side=side, gsis=gs, lines=lines,
                             tw=max(0.0, u.get("ts") or (0.02 if pos in ("WR", "TE", "RB") else 0)),
                             cw=max(0.0, u.get("rsh") or 0.0), cr=u.get("cr"), ym=1.0, rm=1.0, tdw=1.0,
                             out=(T["rusher"].get(gs) or {}).get("out", 0.4), io=(T["rusher"].get(gs) or {}).get("io", [1.0, 1.0]),
                             mz=T["rcv_mz"].get(gs) or {"man": [1, 1], "zone": [1, 1]}))
        if not any(p.lines for p in ps):
            # lines-only sim (no props yet), so nothing calibrates the players: hand the shares of anyone ruled out to the
            # teammates who are playing, in proportion, so the "others" bucket doesn't absorb a missing lead back / WR1
            # (same rule as sim_leans_backtest)
            for attr, want in (("tw", 0.88), ("cw", 0.93)):
                tot = sum(getattr(p, attr) * (0.6 if p.pos == "QB" and attr == "cw" else 1) for p in ps)
                if 0 < tot < want:
                    k = min(1.6, want / tot)
                    for p in ps: setattr(p, attr, getattr(p, attr) * k)
        qbs = [p for p in ps if p.pos == "QB"]
        qbs.sort(key=lambda p: ("Pass yds" not in p.lines, "Pass att" not in p.lines))
        if qbs: tm.qb = qbs[0]
        else: tm.qb = Player(id=f"{tm.abbr}-QB", n=f"{tm.abbr} QB", short="QB", pos="QB", side=side, lines={}, tw=0, cw=0.06, ym=1, rm=1, tdw=1, out=0.5, io=[1, 1], mz={})
        tm.qb.cw = max(0.02, (tm.qb.cw or 0.08) * 0.6)    # designed runs; scrambles come on dropbacks
        q = T["qb_press"].get(tm.qb.gsis) or {}
        tm.qbp = (q.get("clean", 1.0), q.get("press", 1.0))
        recv = [p for p in ps if p.pos in ("WR", "TE", "RB") and (p.tw > 0 or "Receptions" in p.lines or "Rec yds" in p.lines)]
        rush = [p for p in ps if p.pos in ("RB", "WR") and (p.cw > 0.02 or "Carries" in p.lines or "Rush yds" in p.lines)]
        for p in rush:
            if p.cw <= 0.02: p.cw = 0.15 if p.pos == "RB" else 0.03
        st = sum(p.tw for p in recv); sc = sum(p.cw for p in rush) + tm.qb.cw
        recv.append(Player(id=f"{tm.abbr}-o", n=f"{tm.name} others", short="", pos="WR", side=side, lines={}, tw=max(0.10, 1 - st), cw=0,
                           cr=None, ym=1, rm=1, tdw=1, out=0.4, io=[1, 1], mz={"man": [1, 1], "zone": [1, 1]}))
        rush.append(Player(id=f"{tm.abbr}-r", n=f"{tm.name} others", short="", pos="RB", side=side, lines={}, tw=0, cw=max(0.04, 1 - sc),
                           cr=None, ym=1, rm=1, tdw=1, out=0.4, io=[1, 1], mz={}))
        tm.recv, tm.rush = recv, rush
        seen, tm.all = set(), []
        for p in [tm.qb] + recv + rush:
            if id(p) not in seen: seen.add(id(p)); tm.all.append(p)
        out[side] = tm
    for side, opp in (("home", "away"), ("away", "home")):
        mix = T["cov_mix"].get(out[opp].abbr) or T["cov_mix"]["NFL"]
        nfl = T["cov_mix"]["NFL"]["cov"]
        w = min(1.0, mix.get("n", 0) / 400)
        cov = {c: w * mix["cov"].get(c, 0) + (1 - w) * nfl.get(c, 0) for c in set(nfl) | set(mix["cov"])}
        tot = sum(cov.values()); keys = sorted(cov); acc = 0; cum = []
        for c in keys: acc += cov[c] / tot; cum.append(acc)
        out[side].cov = (keys, cum)
    idx = 0
    for side in ("home", "away"):
        tm = out[side]
        for p in tm.all:
            p.idx = idx; idx += 1
    return out["home"], out["away"], idx


# ---------------------------------------------------------------- one game
class Sim:
    def __init__(self, T, H, A, nplayers):
        self.T, self.H, self.A, self.NP = T, H, A, nplayers
        self.pr = T["pass_rate"]; self.fourth = T["fourth"]; self.fg = T["fg"]
        self.run = {k: v for k, v in T["run"].items()}; self.catch = T["catch"]; self.comp = T["comp"]
        self.clock = T["clock"]; self.rate = T["rate"]; self.cres = T["cov_res"]
        lg_press = T["press"]
        ps = self.cres["press|True"]["sack"]
        self.p_sack_press = ps
        c = self.rate["scr"] / (1 - lg_press + lg_press * 2.2)
        self.p_scr = (c, 2.2 * c)
        cl, pr_ = self.cres["press|False"]["ypd"], self.cres["press|True"]["ypd"]
        # pressured dropbacks: lower completion and shorter gains (sacks handled separately)
        self.press_eff = {False: (min(1.12, math.sqrt(cl)), min(1.12, math.sqrt(cl))), True: (0.80, 0.78)}
        self.cov_eff = {c[4:]: math.sqrt(v["ypd"]) for c, v in self.cres.items() if c.startswith("cov|")}
        self.punt = {int(k): v for k, v in T["punt"].items()}; self.kick = T["kick"]
        pn = T.get("pen") or {}
        self.pen_d, self.pen_dy, self.pen_dfd = (pn.get("def") or {}).get("p", 0), (pn.get("def") or {}).get("y", [5]), (pn.get("def") or {}).get("fd", 0.7)
        self.pen_o, self.pen_oy = (pn.get("off") or {}).get("p", 0), (pn.get("off") or {}).get("y", [5])

    def prep(self):
        """cumulative target / carry weights per team for man vs zone and red zone vs open field."""
        for tm in (self.H, self.A):
            tm.tcum = {}
            for mz in ("man", "zone"):
                for rz in (False, True):
                    ws = [p.tw * (p.mz.get(mz, [1, 1])[0] if p.mz else 1) * (p.tdw if rz else 1) for p in tm.recv]
                    acc, cum = 0, []
                    for w in ws: acc += w; cum.append(acc)
                    tm.tcum[(mz, rz)] = cum
            tm.ccum = {}
            for rz in (False, True):
                ws = [p.cw * (p.tdw if rz else 1) for p in [tm.qb] + tm.rush]
                acc, cum = 0, []
                for w in ws: acc += w; cum.append(acc)
                tm.ccum[rz] = cum
            opp = self.A if tm is self.H else self.H
            ot = opp.tm
            tm.press_p = min(0.6, self.T["press"] * tm.tm.get("hit_o", 1.0) * ot.get("hit_d", 1.0))
            tm.int_p = self.rate["int"] * ot.get("int_d", 1.0) * tm.k_int
            tm.run_d = ot.get("run_d", [1.0, 1.0])
            tm.pace = tm.tm.get("pace", 1.0)

    def fgp(self, ydl):
        d = ydl + 17
        return 1 / (1 + math.exp(-(self.fg[0] + self.fg[1] * d))) if d <= 66 else 0.0

    def game(self, rng, log=False):
        H, A, T = self.H, self.A, self.T
        rnd = rng.random
        st = [[0] * 13 for _ in range(self.NP)]
        teams = (H, A)
        score = [0, 0]; qpts = [[0, 0, 0, 0, 0], [0, 0, 0, 0, 0]]
        plays = [] if log else None
        freak = False
        recv0 = 0 if rnd() < 0.5 else 1
        poss = recv0
        ydl = self.kick[int(rnd() * len(self.kick))]
        qtr, qs = 1, 900
        down, togo = 1, 10
        tos = [3, 3]
        ot_poss = None; snaps = 0
        dlt = rng.gauss(0, SHOCK_SD); form = [math.exp(dlt - SHOCK_SD ** 2 / 2), math.exp(-dlt - SHOCK_SD ** 2 / 2)]
        tempo = math.exp(rng.gauss(0, PACE_SD) - PACE_SD ** 2 / 2)
        names = (H.abbr, A.abbr)

        def spot(o, y):
            return f"{names[o]} {100 - y}" if y > 50 else f"{names[1 - o]} {y}" if y < 50 else "50"

        def L(kind, text, yds=0, cov="", prs=False, extra="", a="", b=""):
            # [qtr, clock, offense (0 home / 1 away), down, to go, yards to the goal, kind, caption, yards, coverage, pressured,
            #  home score, away score, TD/FG/INT/FUM/SAF, passer or runner, receiver]
            if log: plays.append([qtr, qs, poss, down, togo, ydl, kind, text, yds, cov, 1 if prs else 0, score[0], score[1], extra, a, b])

        def score_pts(o, pts):
            score[o] += pts; qpts[o][min(qtr, 5) - 1] += pts

        def kickoff(to):
            return to, self.kick[int(rnd() * len(self.kick))]

        while True:
            if qs <= 0:
                if qtr in (1, 3): qtr += 1; qs = 900
                elif qtr == 2:
                    qtr, qs = 3, 900; tos = [3, 3]
                    poss, ydl = kickoff(1 - recv0); down, togo = 1, 10
                    L("half", "Second half kickoff")
                elif qtr == 4:
                    if score[0] != score[1]: break
                    qtr, qs = 5, 600; tos = [2, 2]
                    poss, ydl = kickoff(0 if rnd() < 0.5 else 1); down, togo = 1, 10
                    ot_poss = [0, 0]
                    L("ot", "Overtime")
                else: break
            off, de = teams[poss], teams[1 - poss]
            o = poss
            sd = score[o] - score[1 - o]
            late = qtr >= 4 and qs <= 300 or qtr == 5
            if qtr == 4 and qs <= 300: md = "hurry" if sd < 0 or (sd == 0 and qs <= 150) else "milk" if sd > 0 else "normal"
            elif qtr == 5: md = "hurry" if sd <= 0 else "normal"
            elif qtr == 2 and qs <= 120: md = "hurry"
            else: md = "normal"
            # kneel out the clock: each kneel burns ~40 s unless the defense still has a timeout
            if sd > 0 and qtr >= 4:
                left = 4 - down + 1
                if qs <= 40 * left - 40 * min(tos[1 - o], left - 1):
                    L("kneel", f"{off.qb.short or off.abbr} kneels", -1)
                    if tos[1 - o] > 0: tos[1 - o] -= 1; qs -= 2
                    else: qs -= 40
                    ydl = min(99, ydl + 1); down += 1; togo += 1
                    if down > 4: qs = 0
                    continue
            # field goal to end a half / win it
            kick_now = False
            if (qtr in (2, 4, 5) and qs <= 20 and self.fgp(ydl) > 0.35 and (qtr == 2 or -3 <= sd <= 0)): kick_now = True
            # playing for the field goal: tied or down 1-2 late and already in easy range -> run, let the clock go, kick on
            # 3rd down (or with the clock nearly out) instead of chasing a touchdown. Real 2023-25: 4.8% of games end by 3 on
            # a last-2-minutes FG (the sim had 2.8% without this); overtime FGs follow from the extra ties
            # overtime once the other side has had the ball: any field goal ties or wins, so the same (real OTs end tied 2%)
            fg_mode = ((qtr == 4 and qs <= FGMODE_S or qtr == 5 and ot_poss is not None and ot_poss[1 - o] >= 1)
                       and -2 <= sd <= 0 and down < 4 and self.fgp(ydl) >= FGMODE_P)
            if fg_mode and (down == 3 or qs <= 12): kick_now = True
            # sudden death (both sides have had the ball, still tied): any field goal wins, so kick once it's a likely make
            if qtr == 5 and sd == 0 and ot_poss is not None and ot_poss[1 - o] >= 1 and self.fgp(ydl) >= OT_FG_P: kick_now = True
            choice = None
            if down == 4 or kick_now:
                if kick_now: choice = "fg"
                elif sd == 0 and (qtr == 5 or qtr == 4 and qs <= 150) and self.fgp(ydl) >= 0.5:
                    choice = "fg"
                elif late and sd < 0:
                    if -sd <= 3 and self.fgp(ydl) >= 0.55 and (qs <= FGTIE_S or togo >= 4): choice = "fg"   # kick to tie / take the lead
                    elif ydl > 70 and qs > 240 and togo > 3: choice = "punt"
                    else: choice = "go"
                else:
                    d4 = "1" if togo <= 1 else "2" if togo <= 3 else "4" if togo <= 6 else "7"
                    pv = self.fourth.get(f"{d4}|{min(ydl // 10, 9)}")
                    if pv:
                        r = rnd(); choice = "go" if r < pv[0] else "punt" if r < pv[0] + pv[1] else "fg"
                    else: choice = "fg" if ydl <= 35 else "punt"
                    if choice == "fg" and self.fgp(ydl) < 0.25: choice = "punt" if ydl > 40 else "go"
                    if choice == "punt" and ydl < 35: choice = "fg" if self.fgp(ydl) > 0.4 else "go"
            if choice == "fg":
                dist = ydl + 17
                if rnd() < self.fgp(ydl):
                    L("fg", f"{dist}-yard field goal is good", 0, extra="FG"); score_pts(o, 3)
                    qs -= 5
                    if ot_poss is not None:
                        ot_poss[o] += 1
                        if ot_poss[1 - o] >= 1 and score[o] != score[1 - o]: break   # a tying FG goes to sudden death
                    poss, ydl = kickoff(1 - o); down, togo = 1, 10
                else:
                    L("fgx", f"{dist}-yard field goal is no good", 0)
                    qs -= 5
                    if ot_poss is not None: ot_poss[o] += 1
                    poss = 1 - o; ydl = min(80, 100 - (ydl + 7)); down, togo = 1, 10
                continue
            if choice == "punt":
                lst = self.punt.get(min(ydl // 5, 19)) or self.punt[max(self.punt)]
                ny = lst[int(rnd() * len(lst))]
                L("punt", f"Punt, {names[1 - o]} ball at {spot(1 - o, ny)}", 0)
                qs -= 8
                if ot_poss is not None: ot_poss[o] += 1
                poss = 1 - o; ydl = ny; down, togo = 1, 10
                continue
            # a penalty wipes out the snap
            r = rnd()
            if r < self.pen_d + self.pen_o:
                if r < self.pen_d:
                    y = self.pen_dy[int(rnd() * len(self.pen_dy))]
                    if y >= ydl: y = max(1, ydl // 2)
                    ydl -= y
                    if rnd() < self.pen_dfd or y >= togo: down, togo = 1, min(10, ydl)
                    else: togo -= y
                    L("pen", f"Penalty on {names[1 - o]}, {yd(y)}" + (", automatic first down" if down == 1 else ""), y)
                else:
                    y = self.pen_oy[int(rnd() * len(self.pen_oy))]
                    if ydl + y >= 100: y = max(1, (100 - ydl) // 2)
                    ydl += y; togo += y
                    L("pen", f"Penalty on {names[o]}, {yd(y)}", -y)
                qs -= max(1, int(6 + 6 * rnd()))
                if qtr in (2, 4) and qs < 120 and qs + 12 > 120: qs = max(qs, 120)
                continue
            # play call
            key = f"{min(down, 3)}{'s' if togo <= 2 else 'm' if togo <= 6 else 'l' if togo <= 10 else 'x'}" \
                  f"{sum(1 for hi in (-17, -9, -4, -1, 0, 3, 8, 16) if sd > hi)}{2 if (qtr >= 4 and qs <= 300) else 1 if (qtr == 2 and qs <= 120) else 0}"
            pp = self.pr.get(key, 0.58)
            if down == 4: pp = 0.7 if togo > 2 else 0.45
            lo = math.log(pp / (1 - pp)) + off.tm.get("proe", 0) + off.k_pr
            if ydl <= 20: lo += math.log(off.k_rzp)
            is_pass = rnd() < 1 / (1 + math.exp(-lo)) and not fg_mode; snaps += 1
            if fg_mode and qtr == 4: md = "milk"
            zn = "gl" if ydl <= 5 else "rz" if ydl <= 20 else "op"     # completion-rate zones
            zb = "g2" if ydl <= 2 else "g5" if ydl <= 5 else "r10" if ydl <= 10 else "r15" if ydl <= 15 else "r20" if ydl <= 20 else "op"
            rz = ydl <= 20                # the market's points knob (k_fin) works on red-zone finishing
            sus = SUS_K * off.k_sus if (down >= 3 and not rz) else 1.0
            # prevent defense: a team down 4-16 late moves the ball underneath more easily
            prev_d = PREVENT_K if (qtr >= 4 and qs <= 300 and -16 <= sd <= -4) else 1.0
            sus *= prev_d
            gain = 0; outc = "run"; turnover = False; td = False; ret_td = False
            q = off.qb
            if is_pass:
                prs = rnd() < off.press_p
                r = rnd(); cum = off.cov[1]; ci = bisect.bisect_left(cum, r * cum[-1]); cov = off.cov[0][min(ci, len(cum) - 1)]
                mz = "man" if cov in MAN else "zone"
                cvn = COV_NAME.get(cov, cov)
                if prs and rnd() < self.p_sack_press:
                    sk = T["sack_yds"]; gain = sk[int(rnd() * len(sk))]; outc = "sack"
                    L("sack", f"{q.short or off.abbr} sacked for {yd(gain)}", gain, cvn, True, "", q.n)
                elif rnd() < self.p_scr[prs]:
                    sc = T["scr"]; gain = sc[int(rnd() * len(sc))]
                    if gain > 0: gain = round(gain * q.rm)
                    s_ = st[q.idx]; s_[S["car"]] += 1
                    outc = "run"
                    if gain >= ydl: gain = ydl; td = True; s_[S["rtd"]] += 1
                    s_[S["ryd"]] += gain
                    L("scr", f"{q.short or off.abbr} scrambles for {yd(gain)}", gain, cvn, prs, "TD" if td else "", q.n)
                else:
                    s_ = st[q.idx]; s_[S["att"]] += 1
                    if rnd() < self.rate["away"] * (1.6 if prs else 0.75):
                        outc = "inc"; L("away", f"{q.short or off.abbr} throws it away", 0, cvn, prs, "", q.n)
                    elif rnd() < off.int_p * (1.5 if prs else 0.8):
                        outc = "inc"; turnover = True; s_[S["int"]] += 1
                        if rnd() < 0.035: ret_td = True; freak = True
                        L("int", f"{q.short or off.abbr} intercepted", 0, cvn, prs, "INT", q.n)
                    else:
                        cum = off.tcum[(mz, ydl <= 20)]
                        ti = bisect.bisect_left(cum, rnd() * cum[-1]); ti = min(ti, len(cum) - 1)
                        p = off.recv[ti]; ps_ = st[p.idx]; ps_[S["tgt"]] += 1
                        pos = p.pos if p.pos in ("WR", "TE", "RB") else "WR"
                        base = self.comp.get(f"{pos}|{zn}", 0.62)
                        crf = 1.0
                        if p.cr is not None:
                            avg = {"WR": 0.64, "TE": 0.73, "RB": 0.78}.get(pos, 0.65)
                            crf = max(0.8, min(1.2, 0.5 + 0.5 * p.cr / avg))
                        pe = self.press_eff[prs]
                        qf = math.sqrt(off.qbp[1] if prs else off.qbp[0])
                        ce = self.cov_eff.get(cov, 1.0)
                        pc = min(0.97, base * crf * off.k_comp * pe[0] * math.sqrt(qf) * math.sqrt(ce) * (1.06 if prev_d > 1 else 1.0))
                        if rnd() < pc:
                            lst = self.catch.get(f"{pos}|{zb}") or self.catch.get(f"WR|{zb}") or self.catch[f"WR|op"]
                            y = lst[int(rnd() * len(lst))]
                            if y == 999: y = ydl
                            elif y > 0:
                                f_ = form[o] * p.ym * off.k_pe * pe[1] * math.sqrt(qf) * math.sqrt(ce) * (p.mz.get(mz, [1, 1])[1] if p.mz else 1)
                                f_ *= off.k_fin * prev_d if rz else sus
                                y = int(y * f_ + rnd())
                            gain = y
                            if gain >= ydl: gain = ydl; td = True
                            outc = "comp_oob" if (rnd() < (0.30 if md != "normal" else 0.12)) else "comp_in"
                            s_[S["cmp"]] += 1; s_[S["pyd"]] += gain; ps_[S["rec"]] += 1; ps_[S["cyd"]] += gain
                            if gain > ps_[S["lng"]]: ps_[S["lng"]] = gain
                            if td: s_[S["ptd"]] += 1; ps_[S["ctd"]] += 1
                            if gain >= 60: freak = True
                            nm = p.short or "a teammate"
                            if not td and rnd() < self.rate["fum_c"]: turnover = True
                            L("pass", f"{q.short or off.abbr} to {nm} for {yd(gain)}" + (", touchdown" if td else ""), gain, cvn, prs, "TD" if td else ("FUM" if turnover else ""), q.n, p.n)
                        else:
                            outc = "inc"
                            L("inc", f"{q.short or off.abbr} to {p.short or 'a teammate'}, incomplete", 0, cvn, prs, "", q.n, p.n)
            else:
                cum = off.ccum[ydl <= 20]
                ri = bisect.bisect_left(cum, rnd() * cum[-1]); ri = min(ri, len(cum) - 1)
                p = ([q] + off.rush)[ri]; ps_ = st[p.idx]; ps_[S["car"]] += 1
                pos = "QB" if p is q else ("RB" if p.pos == "RB" else "WR")
                outside = rnd() < (p.out or 0.4)
                lst = self.run.get(f"{pos}|{zb}") or self.run.get(f"RB|{zb}") or self.run["RB|op"]
                y = lst[int(rnd() * len(lst))]
                if y == 999: y = ydl
                elif y > 0:
                    f_ = form[o] * p.rm * p.io[1 if outside else 0] * off.run_d[1 if outside else 0]
                    f_ *= off.k_fin * prev_d if rz else sus
                    y = int(y * f_ + rnd())
                gain = y
                if gain >= ydl: gain = ydl; td = True; ps_[S["rtd"]] += 1
                ps_[S["ryd"]] += gain
                if gain >= 60: freak = True
                if not td and rnd() < self.rate["fum_r"]: turnover = True
                L("run", f"{p.short or off.abbr} runs {'outside' if outside else 'up the middle'} for {yd(gain)}" + (", touchdown" if td else ""), gain, "", False, "TD" if td else ("FUM" if turnover else ""), p.n)
            # clock
            e = self.clock.get(f"{outc}|{md}") or self.clock.get(f"{outc}|normal", 30)
            if md == "normal": e *= off.pace * CLOCK_K
            elif md == "milk": e *= CLOCK_K
            e *= (0.85 + 0.3 * rnd()) * tempo
            if td or turnover: e = 6
            if not td and not turnover and outc in ("run", "comp_in", "sack"):
                if md == "hurry" and tos[o] > 0 and (qtr in (2, 4, 5)) and qs - e < 110:
                    tos[o] -= 1; e = 6
                elif md == "milk" and tos[1 - o] > 0 and qs - e > 0:
                    tos[1 - o] -= 1; e = 6
            if qtr in (2, 4) and qs > 120 and qs - e <= 120: e = qs - 120
            qs -= max(1, int(e))
            # result
            if ret_td:
                score_pts(1 - o, 7); L("ptd", f"Returned for a {names[1 - o]} touchdown", 0, extra="TD")
                if ot_poss is not None: break
                poss, ydl = kickoff(o); down, togo = 1, 10; continue
            if td:
                score_pts(o, 6)
                # a walk-off TD in overtime (the other side already had the ball) ends it on the spot: no extra point
                # (real 2023-25 overtimes: 35% end by 6, 2% by 7)
                walkoff = ot_poss is not None and ot_poss[1 - o] >= 1 and score[o] > score[1 - o]
                if walkoff: xp = 0
                else:
                    # two-point tries: the usual late-game chart in the 4th (down 2 / 5 / 9 / 10, up 1 / 5 ...), a few earlier
                    # (real 2023-25: 0.24 tries per team-game, ~48% good); they move 1/2/4-point finals onto ties and 3s
                    d = score[o] - score[1 - o]
                    go2 = (qtr >= 4 and d in TWO_PT_CHART) or rnd() < TWO_PT_BASE
                    xp = (2 if rnd() < TWO_PT_P else 0) if go2 else (1 if rnd() < XP else 0)
                score_pts(o, xp)
                if ot_poss is not None:
                    ot_poss[o] += 1
                    if ot_poss[1 - o] >= 1 and score[o] != score[1 - o]: break   # a tying TD goes to sudden death
                poss, ydl = kickoff(1 - o); down, togo = 1, 10
                continue
            if turnover:
                if ot_poss is not None: ot_poss[o] += 1
                if outc == "inc":   # interception downfield, short return
                    ny = max(1, min(99, 100 - max(1, ydl - 12) - int(rnd() * 15)))
                else:
                    ny = max(1, min(99, 100 - (ydl - gain)))
                poss = 1 - o; ydl = ny; down, togo = 1, 10
                L("chg", f"{names[1 - o]} ball at {spot(1 - o, ny)}")
                continue
            ydl -= gain
            if ydl >= 100:
                score_pts(1 - o, 2); freak = True; L("saf", "Safety", 0, extra="SAF")
                poss, ydl = 1 - o, 62; down, togo = 1, 10; continue
            if gain >= togo:
                down, togo = 1, min(10, ydl)
            elif down == 4:
                if ot_poss is not None: ot_poss[o] += 1
                poss = 1 - o; ydl = 100 - ydl; down, togo = 1, 10
                L("chg", f"Turnover on downs, {names[poss]} ball at {spot(poss, ydl)}")
            else:
                down += 1; togo -= gain
            if ot_poss is not None and ot_poss[0] >= 1 and ot_poss[1] >= 1 and score[0] != score[1]: break
        self.last_snaps = snaps
        return score, st, qpts, plays, freak


# ---------------------------------------------------------------- calibration to the market
def _quant(vals, p, jitter, rng):
    xs = sorted(v + (rng.random() - 0.5 if jitter else 0) for v in vals)
    i = (1 - p) * (len(xs) - 1)
    lo = int(i); hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (i - lo)


def stat_of(st, p, m):
    return sum(st[p.idx][S[k]] for k in MKT[m])


def pois_mean(line, p):
    """mean of a Poisson whose P(X > line) is p."""
    k = int(math.floor(line))
    lo, hi = 0.001, 15.0
    for _ in range(40):
        lam = (lo + hi) / 2
        cdf = sum(math.exp(-lam) * lam ** j / math.factorial(j) for j in range(k + 1))
        if 1 - cdf < p: lo = lam
        else: hi = lam
    return (lo + hi) / 2


def _pts_knobs(tm, f):
    """move a team's points by factor f: red-zone finishing first, whatever its range can't take through drive sustain.
    The ranges are wide on purpose: with the old 0.6-1.6 / 0.8-1.35 a big underdog's knobs sat on the floor (and a big
    favorite's on the ceiling) while the dog still scored 1-2 points too many, so sim margins came up ~0.7 short of the
    spread and every 55%+ spread call landed on the dog."""
    want = tm.k_fin * f
    tm.k_fin = max(K_FIN[0], min(K_FIN[1], want))
    if want != tm.k_fin: tm.k_sus = max(K_SUS[0], min(K_SUS[1], tm.k_sus * want / tm.k_fin))


def calibrate(sim, seed, rounds=CAL_ROUNDS, n=CAL_N):
    H, A = sim.H, sim.A
    cl = lambda x, a, b: max(a, min(b, x))
    for tm in (H, A):   # no passing line to tune against: start from the league-wide pass-efficiency scale (the raw
        if "Pass yds" not in tm.qb.lines and "Pass+rush yds" not in tm.qb.lines: tm.k_pe = K_PE_NOLINE   # engine runs hot)
    for rd in range(rounds):
        sim.prep()
        rng = random.Random(seed)
        nn = n if rd < rounds - 4 else 2 * n     # last rounds: more games, less noise in the points knob
        res = [sim.game(rng) for _ in range(nn)]
        jr = random.Random(seed + 7)
        dmp = 0.85 / (1 + 0.25 * rd)        # steps shrink so the knobs settle instead of chasing noise
        for side, tm in (("home", H), ("away", A)):
            ti = 0 if side == "home" else 1
            pts = sum(r[0][ti] for r in res) / len(res)
            # points: red-zone finishing first (a realistic 0.6-1.6 range), the rest through drive sustain on money downs
            _pts_knobs(tm, cl((tm.pts_target / max(1, pts)) ** (1.4 * dmp), 0.75, 1.35))
            q = tm.qb
            def fac(p, m, jitter=False):
                l, po = p.lines[m]
                vals = [stat_of(r[1], p, m) for r in res]
                qv = _quant(vals, po, jitter, jr)
                return cl((l / qv) if qv > 0.5 else 1.4, 0.7, 1.4) ** dmp
            if "Pass att" in q.lines: tm.k_pr = cl(tm.k_pr + 1.8 * math.log(fac(q, "Pass att", True)), -1.5, 1.5)
            if "Completions" in q.lines: tm.k_comp = cl(tm.k_comp * fac(q, "Completions", True) ** 0.8, 0.6, 1.5)
            if "Pass yds" in q.lines: tm.k_pe = cl(tm.k_pe * fac(q, "Pass yds"), 0.4, 2.5)
            elif "Pass+rush yds" in q.lines: tm.k_pe = cl(tm.k_pe * fac(q, "Pass+rush yds"), 0.4, 2.5)
            if "INT" in q.lines:
                l, po = q.lines["INT"]; m_t = pois_mean(l, po); m_s = sum(stat_of(r[1], q, "INT") for r in res) / len(res)
                tm.k_int = cl(tm.k_int * cl(m_t / max(0.05, m_s), 0.7, 1.4) ** dmp, 0.3, 3)
            if "Pass TD" in q.lines:
                l, po = q.lines["Pass TD"]; m_t = pois_mean(l, po); m_s = sum(stat_of(r[1], q, "Pass TD") for r in res) / len(res)
                tm.k_rzp = cl(tm.k_rzp * cl(m_t / max(0.05, m_s), 0.7, 1.4) ** (0.8 * dmp), 0.3, 3)
            for p in tm.all:
                L = p.lines
                if "Carries" in L: p.cw = cl(p.cw * fac(p, "Carries", True), 0.002, 3)
                if "Rush yds" in L: p.rm = cl(p.rm * fac(p, "Rush yds"), 0.3, 3)
                elif "Rush+rec yds" in L and "Rec yds" not in L and p.pos in ("RB", "QB"): p.rm = cl(p.rm * fac(p, "Rush+rec yds"), 0.3, 3)
                if p is q: continue
                if "Receptions" in L: p.tw = cl(p.tw * fac(p, "Receptions", True), 0.002, 3)
                if "Rec yds" in L: p.ym = cl(p.ym * fac(p, "Rec yds"), 0.3, 3)
                elif "Rush+rec yds" in L and "Rush yds" not in L: p.ym = cl(p.ym * fac(p, "Rush+rec yds"), 0.3, 3)
            for p in tm.all:
                if "Anytime TD" in p.lines and not p.id.endswith(("-o", "-r")):
                    l, po = p.lines["Anytime TD"]
                    ps = (sum(1 for r in res if stat_of(r[1], p, "Anytime TD") > l) + 0.5) / (len(res) + 1)
                    orr = (po / (1 - po)) / (ps / (1 - ps))
                    p.tdw = cl(p.tdw * cl(orr, 0.6, 1.6) ** (0.8 * dmp), 0.2, 5)
    # polish: points only (red-zone finishing, then drive sustain), with the player knobs now fixed; the same 1,500 games
    # every round so each change reflects the knob, not new random games
    for rd in range(POLISH_ROUNDS):
        sim.prep()
        rng = random.Random(seed + 100)
        res = [sim.game(rng) for _ in range(POLISH_N)]
        for ti, tm in ((0, H), (1, A)):
            pts = sum(r[0][ti] for r in res) / len(res)
            _pts_knobs(tm, cl((tm.pts_target / max(1, pts)) ** 1.3, 0.8, 1.25))
    sim.prep()


# ---------------------------------------------------------------- latent correlations for same-game parlays
GL = [(-0.9931285991850949, 0.017614007139152118), (-0.9639719272779138, 0.04060142980038694), (-0.912234428251326, 0.06267204833410907),
      (-0.8391169718222188, 0.08327674157670475), (-0.7463319064601508, 0.10193011981724044), (-0.636053680726515, 0.11819453196151841),
      (-0.5108670019508271, 0.13168863844917664), (-0.37370608871541955, 0.14209610931838204), (-0.2277858511416451, 0.14917298647260374),
      (-0.07652652113349734, 0.15275338713072584), (0.07652652113349734, 0.15275338713072584), (0.2277858511416451, 0.14917298647260374),
      (0.37370608871541955, 0.14209610931838204), (0.5108670019508271, 0.13168863844917664), (0.636053680726515, 0.11819453196151841),
      (0.7463319064601508, 0.10193011981724044), (0.8391169718222188, 0.08327674157670475), (0.912234428251326, 0.06267204833410907),
      (0.9639719272779138, 0.04060142980038694), (0.9931285991850949, 0.017614007139152118)]


def bvn_upper(h, k, r):
    """P(X > h, Y > k) for standard bivariate normal with correlation r (|r| < 1)."""
    base = Phi(-h) * Phi(-k)
    if r == 0: return base
    a = math.asin(r); hk2 = h * h + k * k; hk = h * k
    s = 0.0
    for x, w in GL:
        th = a * (x + 1) / 2; sn = math.sin(th); c2 = 1 - sn * sn
        s += w * math.exp(-(hk2 - 2 * hk * sn) / (2 * c2))
    return base + s * a / 2 / (2 * math.pi)


def ninv(p):
    """inverse standard normal CDF (Acklam)."""
    p = min(max(p, 1e-9), 1 - 1e-9)
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02, 1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02, 6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00, -2.549732984343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00, 3.754408661907416e+00]
    if p < 0.02425:
        q = math.sqrt(-2 * math.log(p)); return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    if p > 1 - 0.02425:
        q = math.sqrt(-2 * math.log(1 - p)); return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    q = p - 0.5; r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)


def tetra(n11, p1, p2, n):
    """latent normal correlation that reproduces the joint 'both over' rate."""
    if p1 <= 0.01 or p2 <= 0.01 or p1 >= 0.99 or p2 >= 0.99: return 0.0
    h, k = ninv(1 - p1), ninv(1 - p2)
    tgt = n11 / n
    lo, hi = -0.97, 0.97
    if tgt <= bvn_upper(h, k, lo): return lo
    if tgt >= bvn_upper(h, k, hi): return hi
    for _ in range(22):
        m = (lo + hi) / 2
        if bvn_upper(h, k, m) < tgt: lo = m
        else: hi = m
    return (lo + hi) / 2


# ---------------------------------------------------------------- one game end to end
def simulate_game(g, rows, T, gsis_of, n=N_SIMS):
    H, A, NP = build_teams(g, rows, T, gsis_of)
    sim = Sim(T, H, A, NP)
    seed = input_hash(g, rows)
    calibrate(sim, seed)
    rng = random.Random(seed + 1)
    seeds = [rng.getrandbits(32) for _ in range(n)]
    res = []
    for s in seeds:
        sc, st, qp, _, fr = sim.game(random.Random(s))
        res.append((sc, st, qp, fr))
    o = g.get("odds") or {}
    hs, tl = o.get("hs"), o.get("t")
    margins = [r[0][0] - r[0][1] for r in res]; totals = [r[0][0] + r[0][1] for r in res]
    summ = {"n": n, "hw": round(sum(m > 0 for m in margins) / n, 3), "tie": round(sum(m == 0 for m in margins) / n, 3),
            "pts": [round(sum(r[0][0] for r in res) / n, 1), round(sum(r[0][1] for r in res) / n, 1)],
            "target": [round(H.pts_target, 1), round(A.pts_target, 1)], "mkt": {"hs": hs, "t": tl, "pHome": (g.get("proj") or {}).get("pHome")},
            # "props": players tuned to their prop lines; "lines": no props posted yet, so only the spread / total anchor the
            # game and players run on their season usage (it re-simulates once props post)
            "basis": "props" if any(r.get("props") for r in rows) else "lines"}
    if hs is not None:
        summ["hc"] = round(sum(m + hs > 0 for m in margins) / n, 3); summ["hpush"] = round(sum(m + hs == 0 for m in margins) / n, 3)
    if tl is not None:
        summ["ov"] = round(sum(t > tl for t in totals) / n, 3); summ["tpush"] = round(sum(t == tl for t in totals) / n, 3)
    cnt = {}
    for r in res: k = f"{r[0][0]}-{r[0][1]}"; cnt[k] = cnt.get(k, 0) + 1
    summ["scores"] = sorted(([k, round(v / n, 3)] for k, v in cnt.items()), key=lambda x: -x[1])[:8]
    mc = {}
    for m in margins: mc[m] = mc.get(m, 0) + 1
    summ["margins"] = {str(k): round(v / n, 4) for k, v in sorted(mc.items()) if -40 <= k <= 40}
    # players: distribution + sim chance over the main line
    players = []
    for tm in (H, A):
        for p in tm.all:
            if not p.lines: continue
            d = {"id": p.id, "n": p.n, "side": p.side, "pos": p.pos, "m": {}}
            for m, (l, po) in p.lines.items():
                vals = sorted(stat_of(r[1], p, m) for r in res)
                d["m"][m] = {"l": l, "p": po, "sim": round(sum(v > l for v in vals) / n, 3),
                             "q": [vals[int(n * f)] for f in (0.1, 0.25, 0.5, 0.75, 0.9)], "avg": round(sum(vals) / n, 1)}
            # fantasy points per simulated game (no fumbles / 2-pt), 10th-90th percentiles for PPR, half and standard: the
            # real floor / ceiling, since yards, catches and TDs move together inside a game
            if p.pos in ("QB", "RB", "WR", "TE"):
                fp = {}
                for ppr in (1, 0.5, 0):
                    v = sorted(round(st[p.idx][S["pyd"]] * 0.04 + st[p.idx][S["ptd"]] * 4 - st[p.idx][S["int"]] * 2 + (st[p.idx][S["ryd"]] + st[p.idx][S["cyd"]]) * 0.1
                                     + st[p.idx][S["rec"]] * ppr + (st[p.idx][S["rtd"]] + st[p.idx][S["ctd"]]) * 6, 1) for _, st, _, _ in res)
                    fp[str(ppr)] = [v[int(n * q)] for q in (0.1, 0.25, 0.5, 0.75, 0.9)]
                d["fp"] = fp
            players.append(d)
    # typical game: closest to the median on the score and the key players, no freak plays
    keys = [lambda r: r[0][0], lambda r: r[0][1]]
    for tm in (H, A):
        keys.append(lambda r, p=tm.qb: stat_of(r[1], p, "Pass yds"))
        top = sorted([p for p in tm.recv if "Rec yds" in p.lines], key=lambda p: -p.lines["Rec yds"][0])[:2]
        keys += [lambda r, p=p: stat_of(r[1], p, "Rec yds") for p in top]
        rb = sorted([p for p in tm.rush if "Rush yds" in p.lines], key=lambda p: -p.lines["Rush yds"][0])[:1]
        keys += [lambda r, p=p: stat_of(r[1], p, "Rush yds") for p in rb]
    cols = [[k(r) for r in res] for k in keys]
    meds = [sorted(c)[n // 2] for c in cols]
    sds = [max(1.0, (sum((x - sum(c) / n) ** 2 for x in c) / n) ** 0.5) for c in cols]
    best, bi = None, 0
    for i, r in enumerate(res):
        if r[3]: continue
        if (r[0][0] > r[0][1]) != (meds[0] > meds[1]) and meds[0] != meds[1]: continue   # the typical winner wins
        dd = sum(((cols[j][i] - meds[j]) / sds[j]) ** 2 for j in range(len(cols)))
        if best is None or dd < best: best, bi = dd, i
    sc, st, qp, plays, _ = sim.game(random.Random(seeds[bi]), log=True)
    box = []
    for tm in (H, A):
        for p in tm.all:
            s_ = st[p.idx]
            if any(s_): box.append([p.side[0], p.n, p.pos] + s_)
    typical = {"i": bi, "score": sc, "q": qp, "plays": plays, "box": box}
    # latent correlations between main-line outcomes (players + game)
    colmeta, colvals = [], []
    for tm in (H, A):
        for p in tm.all:
            for m, (l, po) in p.lines.items():
                colmeta.append([p.id, m, l]); colvals.append([stat_of(r[1], p, m) > l for r in res])
    if hs is not None:
        colmeta.append(["G", "home_cover", hs]); colvals.append([m + hs > 0 for m in margins])
        colmeta.append(["G", "home_win", 0]); colvals.append([m > 0 for m in margins])
    if tl is not None:
        colmeta.append(["G", "over", tl]); colvals.append([t > tl for t in totals])
        colmeta.append(["G", "home_tt", round(H.pts_target, 1)]); colvals.append([r[0][0] > H.pts_target for r in res])
        colmeta.append(["G", "away_tt", round(A.pts_target, 1)]); colvals.append([r[0][1] > A.pts_target for r in res])
    ps_ = [sum(c) / n for c in colvals]
    corr = []
    for i in range(len(colvals)):
        ci = colvals[i]
        for j in range(i + 1, len(colvals)):
            cj = colvals[j]
            n11 = sum(1 for a, b in zip(ci, cj) if a and b)
            corr.append(round(tetra(n11, ps_[i], ps_[j], n), 2))
    return {"id": g["id"], "home": H.abbr, "away": A.abbr, "hn": H.name, "an": A.name, "sum": summ, "players": players,
            "typical": typical, "cols": colmeta, "corr": corr,
            "cal": {s: {"k_fin": round(t.k_fin, 3), "k_sus": round(t.k_sus, 3), "k_pe": round(t.k_pe, 3), "k_pr": round(t.k_pr, 3), "k_comp": round(t.k_comp, 3)} for s, t in (("home", H), ("away", A))}}


def input_hash(g, rows):
    """same lines, chances and injuries -> same seed -> identical output (no file churn between syncs)."""
    key = [g["id"], VERSION, [g.get("odds", {}).get(k) for k in ("hs", "t")], (g.get("proj") or {}).get("h"), (g.get("proj") or {}).get("a"),
           sorted(i.get("n", "") + i.get("s", "") for s in ("home", "away") for i in (g[s].get("inj") or [])),
           [[r["id"], [(p["m"], p.get("l"), p.get("pOver")) for p in r.get("props", [])]] for r in rows]]
    return zlib.crc32(json.dumps(key, sort_keys=True).encode())


def gsis_map():
    import analytics
    return {p["espn_id"]: p["gsis_id"] for p in analytics.rows("players/players.csv", 30 * 86400) if p.get("espn_id") and p.get("gsis_id")}


def index_entry(d):
    """the slate's per-game summary: enough for the Sims page without loading each game's file."""
    s = d["sum"]
    return {**{k: s.get(k) for k in ("hw", "hc", "ov", "tie", "hpush", "tpush", "pts", "target", "mkt", "basis")}, "scores": (s.get("scores") or [])[:5], "at": d.get("at")}


def run(games, props, out_dir, n=N_SIMS, only=None):
    """simulate every upcoming game; one file per game plus an index. Returns the index."""
    T = load_tables(); gs = gsis_map()
    os.makedirs(out_dir, exist_ok=True)
    idx = {}
    for g in games:
        if g.get("state") != "pre" or (only and g["id"] not in only): continue
        rows = props.get(g["id"]) or []
        if not rows: continue
        path = os.path.join(out_dir, f"{g['id']}.json")
        h = input_hash(g, rows)
        try:
            old = json.load(open(path, encoding="utf-8"))
            if old.get("h") == h:
                idx[g["id"]] = index_entry(old); continue
        except (FileNotFoundError, json.JSONDecodeError, KeyError): pass
        t0 = time.time()
        try: d = simulate_game(g, rows, T, gs, n)
        except Exception as e:
            import traceback; traceback.print_exc(); print("sim failed", g["id"], e, file=sys.stderr); continue
        d["at"] = time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime()); d["h"] = h
        with open(path, "w", encoding="utf-8") as fh: json.dump(d, fh, separators=(",", ":"))
        idx[g["id"]] = index_entry(d)
        print("sim", d["away"], "@", d["home"], round(time.time() - t0, 1), "s", json.dumps(idx[g["id"]]), file=sys.stderr)
    if only: return idx   # a partial (local) run leaves the other games and the index alone
    for f in os.listdir(out_dir):   # games that kicked off or left the slate
        if f.endswith(".json") and f != "index.json" and f[:-5] not in idx: os.remove(os.path.join(out_dir, f))
    with open(os.path.join(out_dir, "index.json"), "w", encoding="utf-8") as fh: json.dump(idx, fh, separators=(",", ":"))
    return idx


if __name__ == "__main__":
    # local run on the last sync's output: python gamesim.py [game id ...]
    out = os.environ.get("SIM_SRC") or os.path.join(ROOT, "out")
    slate = json.load(open(os.path.join(out, "slate.json"), encoding="utf-8"))
    props = {}
    for f in sorted(os.listdir(out)):
        if f.startswith("props-"): props.update(json.load(open(os.path.join(out, f), encoding="utf-8"))["games"])
    run(slate["games"], props, os.path.join(ROOT, "out", "sim"), int(os.environ.get("SIM_N", N_SIMS)), set(sys.argv[1:]) or None)
