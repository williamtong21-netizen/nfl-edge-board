"""Possible "trap lines": spots where the line looks too good for the side casual bettors love, but the evidence
says the money that moves lines is on the other side.

Books don't publish public betting splits for free, so the public side is estimated from what casual bettors chase:
favorites, the better record, last week's winner, popular brands, and (for totals) the over. Then each signal that the
line is leaning against that side adds points:

  reverse line movement     the spread or total moved away from the public side since it opened
  key number                the move crossed 3 or 7 (or 41/44/47 on totals)
  price-only move           same number, but the public side got cheaper (books dangling it)
  too good to be true       the line is far cheaper than the records suggest, yet the models back the line
  models disagree           our projection or ESPN's model prefers the other side
  sharp vs retail           Pinnacle's number leans to the other side compared with the retail books

Moves that injury news explains (a starting QB or top skill player out on the public side) are discounted, and totals
are judged against how far totals moved league-wide that week. Score 0-100: 50+ "Possible trap", 30-49 "Trap watch". A lean, not a lock: fading flagged traps is tracked in results.
"""
POPULAR = {"DAL", "KC", "GB", "PIT", "SF", "PHI", "BUF", "BAL", "DET"}
KEY_SPREAD, KEY_TOTAL = (3, 7), (41, 44, 47)
SHARP = "pinnacle"


def _pct(rec):
    try:
        w, l, *t = [int(x) for x in str(rec).split("-")]
        g = w + l + (t[0] if t else 0)
        return (w + 0.5 * (t[0] if t else 0)) / g if g else None
    except (ValueError, TypeError):
        return None


def _crossed(a, b, keys):
    """Key numbers the line moved onto, off of, or through."""
    lo, hi = sorted((abs(a), abs(b)))
    return [k for k in keys if lo != hi and lo <= k <= hi]


def _median(xs):
    xs = sorted(xs); n = len(xs)
    return None if not n else xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def _news(t):
    """Key players out or doubtful: news, not sharp money, can explain a line move."""
    return [f"{i['n']} ({i['r']})" for i in t.get("inj", []) if i.get("r") in ("starting QB", "leading rusher", "leading receiver")
            and any(k in (i.get("s") or "").lower() for k in ("out", "reserve", "doubtful", "suspend"))]


def _appeal(g, side):
    t, o = g[side], g["home" if side == "away" else "away"]
    odds, s, why = g["odds"], 0.0, []
    hs = odds.get("hs")
    if hs is not None and ((side == "home" and hs < 0) or (side == "away" and hs > 0)):
        s += 2; why.append("favorite")
    pt, po = _pct(t.get("rec")), _pct(o.get("rec"))
    if pt is not None and po is not None and pt - po >= 0.25:
        s += 1; why.append("better record")
    if t.get("form") and t["form"][-1].get("r") == "W":
        s += 0.5; why.append("won last week")
    if t["abbr"] in POPULAR:
        s += 1; why.append("popular team")
    return s, why


def side_trap(g):
    o = g.get("odds") or {}
    hs, hso = o.get("hs"), o.get("hso")
    if hs is None or g["state"] != "pre": return None
    (ha, hwhy), (aa, awhy) = _appeal(g, "home"), _appeal(g, "away")
    if abs(ha - aa) < 1: return None                        # no clear public side
    pub = "home" if ha > aa else "away"; fade = "away" if pub == "home" else "home"
    P, Q = g[pub], g[fade]
    score, reasons = 0, []
    # 1) reverse line movement: hs going up means the line moved toward the away team
    if hso is not None and hs != hso:
        toward_fade = (hs > hso) if pub == "home" else (hs < hso)
        n = abs(hs - hso)
        if toward_fade:
            pts = 35 if n >= 1.5 else 25 if n >= 1 else 15
            news = _news(P)
            if news:
                pts //= 3
                reasons.append(f"The line has moved {n:g} toward the {Q['name']}, but {', '.join(news)} being out explains much of that.")
            else:
                reasons.append(f"The line has moved {n:g} point{'s' if n != 1 else ''} toward the {Q['name']} since it opened, even though the {P['name']} look like the popular side.")
            score += pts
            keys = _crossed(hso, hs, KEY_SPREAD)
            if keys and not news:
                score += 10; reasons.append(f"That move crossed the key number {keys[0]}, which books don't do lightly.")
    # 2) price-only move at the same number
    pub_price, pub_open = (o.get("hsp"), o.get("hspo")) if pub == "home" else (o.get("asp"), o.get("aspo"))
    if hso is not None and hs == hso and pub_price is not None and pub_open is not None and pub_price > pub_open + 4:
        score += 10; reasons.append(f"The number hasn't moved, but the {P['name']} price got cheaper ({pub_open} to {pub_price}). Books are inviting {P['name']} money.")
    # 3) too good to be true: record gap says a bigger spread than the book is hanging
    pp, qp = _pct(P.get("rec")), _pct(Q.get("rec"))
    p_line = hs if pub == "home" else -hs                   # negative = public side lays points
    if pp is not None and qp is not None:
        perceived = -(8 * (pp - qp) + (1.5 if pub == "home" else -1.5))
        if p_line - perceived >= 3:
            score += 15
            reasons.append(f"On records alone the {P['name']} look like {abs(perceived):.0f}-point favorites, but they're {'laying only ' + str(abs(p_line)) if p_line < 0 else 'getting ' + str(p_line)}. It looks too cheap.")
            proj = g.get("proj")
            if proj:
                ours = proj["margin"] if pub == "home" else -proj["margin"]
                if ours + p_line <= 1:
                    score += 10; reasons.append(f"Our projection ({P['abbr']} by {ours:.1f}) says the book's number is about right, so the bargain may be an illusion.")
    # 4) models prefer the other side
    proj, model, ml = g.get("proj"), g.get("model"), (o.get("hml"), o.get("aml"))
    if proj:
        ours_fade = proj["margin"] if fade == "home" else -proj["margin"]
        fade_line = hs if fade == "home" else -hs
        if ours_fade + fade_line >= 2.5:
            score += 10; reasons.append(f"Our projection likes the {Q['name']} at this number by {ours_fade + fade_line:.1f} points.")
    if model and all(ml):
        imp = lambda a: -a / (-a + 100) if a < 0 else 100 / (a + 100)
        fair_h = imp(ml[0]) / (imp(ml[0]) + imp(ml[1]))
        edge = (model["h"] - fair_h) * (1 if fade == "home" else -1)
        if edge >= 0.04:
            score += 10; reasons.append(f"ESPN's model gives the {Q['name']} a {edge * 100:.0f}-point better win chance than the market does.")
    # 5) sharp book vs retail
    books = g.get("books") or []
    sharp = next((b for b in books if b.get("k") == SHARP and b.get("hs") is not None), None)
    retail = [b["hs"] for b in books if b.get("k") != SHARP and b.get("hs") is not None]
    if sharp and retail:
        med = _median(retail)
        lean = (sharp["hs"] - med) * (1 if pub == "home" else -1)   # positive = sharp book is tougher on the public side
        if lean >= 0.5:
            score += 20; reasons.append(f"Pinnacle, the sharpest book, has {P['abbr']} {sharp['hs'] if pub == 'home' else -sharp['hs']:+g} while retail books sit at {med if pub == 'home' else -med:+g}. The sharp number leans {Q['name']}.")
    if score < 30: return None
    return {"pub": pub, "fade": fade, "score": min(100, score), "label": "Possible trap" if score >= 50 else "Trap watch",
            "why_public": hwhy if pub == "home" else awhy, "reasons": reasons, "fade_line": -hs if fade == "away" else hs}


def total_trap(g, drift=0.0):
    """drift = how far totals fell on average across the slate; only the drop beyond that counts."""
    o = g.get("odds") or {}
    t, to = o.get("t"), o.get("to")
    if t is None or g["state"] != "pre": return None
    score, reasons = 0, []
    if to is not None and t < to:
        n, excess = to - t, (to - t) - max(drift, 0)
        news = _news(g["home"]) + _news(g["away"])
        if excess >= 1:
            pts = 35 if excess >= 3 else 25 if excess >= 2 else 15
            if news:
                pts //= 3; reasons.append(f"The total has dropped {n:g} since it opened at {to}, but {', '.join(news)} being out explains much of that.")
            else:
                reasons.append(f"The total has dropped {n:g} since it opened at {to}{f' (about {excess:.1f} more than totals moved league-wide this week)' if drift > 0.25 else ''}, even though casual bettors love the over.")
            score += pts
            keys = _crossed(to, t, KEY_TOTAL)
            if keys and not news: score += 5; reasons.append(f"It fell through {keys[0]}, one of the most common final combined scores.")
    if o.get("op") is not None and o.get("opo") is not None and to == t and o["op"] > o["opo"] + 4:
        score += 10; reasons.append(f"Same number, but the over got cheaper ({o['opo']} to {o['op']}). Books are inviting over bets.")
    ppg = [(g[s].get("stats") or {}).get("totalPointsPerGame") for s in ("home", "away")]
    if all(p is not None for p in ppg) and sum(ppg) - t >= 5:
        score += 15; reasons.append(f"These offenses average {sum(ppg):.0f} combined points, so {t} looks low. That's the kind of number that draws over money.")
    if g.get("proj") and g["proj"]["total"] <= t - 2.5:
        score += 10; reasons.append(f"Our projection ({g['proj']['total']:.1f}) agrees the under is the better side.")
    books = g.get("books") or []
    sharp = next((b for b in books if b.get("k") == SHARP and b.get("t") is not None), None)
    retail = [b["t"] for b in books if b.get("k") != SHARP and b.get("t") is not None]
    if sharp and retail and _median(retail) - sharp["t"] >= 0.5:
        score += 20; reasons.append(f"Pinnacle is at {sharp['t']} while retail books sit at {_median(retail)}. Sharp money looks like it's on the under.")
    if score < 30: return None
    return {"pub": "over", "fade": "under", "score": min(100, score), "label": "Possible trap" if score >= 50 else "Trap watch", "reasons": reasons, "fade_line": t}


def apply(games):
    moves = [g["odds"]["to"] - g["odds"]["t"] for g in games if g["state"] == "pre" and g.get("odds") and g["odds"].get("to") is not None and g["odds"].get("t") is not None]
    drift = sorted(moves)[len(moves) // 2] if moves else 0.0           # median move, so one huge drop doesn't set the bar
    for g in games:
        g.pop("trap", None)
        tr = {"side": side_trap(g), "total": total_trap(g, drift)}
        if tr["side"] or tr["total"]: g["trap"] = tr
