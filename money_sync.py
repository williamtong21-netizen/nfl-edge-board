"""Kalshi (and Polymarket) money only, every ~15 minutes (GitHub Actions money.yml): much lighter than the full sync.

Re-pulls Kalshi's game and player markets for the games already on the published slate (data/slate.json + its prop
parts), runs kalshi.flow (dollars traded, new money, which side), and writes a small <data>/money.json per sport:
  {"at": iso time, "games": {game id: {"ml" | "spread" | "total": {...}}}, "props": {game id: {"<player id>|<market>": {...}}}}
The app's Money tab reloads that file every 2 minutes while it's open. Kalshi blocks browsers from calling its API
directly (no CORS), so this is the closest to live the app can get.
Run:  python money_sync.py          (paths are the app repo's: data/..., state/...)
"""
import json, os, sys
from datetime import datetime, timezone

import kalshi

NFL_CFG = (dict(kalshi.PLAYER_SERIES), kalshi.GAME_SERIES, dict(kalshi.TO_ESPN), kalshi.FIRST_TD)
SPORTS = [   # sport, published data folder, state folder, Kalshi series (same as each sport's own sync)
    ("nfl", "data", "state", NFL_CFG),
    ("nba", "data/nba", "state/nba", ({"KXNBAPTS": "Points", "KXNBAREB": "Rebounds", "KXNBAAST": "Assists", "KXNBA3PT": "Threes"},
                                      ("KXNBAGAME", "KXNBASPREAD", "KXNBATOTAL", "KXNBATEAMTOTAL"),
                                      {"NYK": "NY", "SAS": "SA", "GSW": "GS", "NOP": "NO", "UTA": "UTAH", "WAS": "WSH"}, None)),
    ("cbb", "data/cbb", "state/cbb", ({}, ("KXNCAAMBGAME", "KXNCAAMBSPREAD", "KXNCAAMBTOTAL", "KXNCAAMBTEAMTOTAL"), {}, None)),
]


def load(path, default=None):
    try:
        with open(path, encoding="utf-8") as f: return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError): return default


def one(sport, out, data, cfg):
    slate = load(os.path.join(out, "slate.json"))
    if not slate: return 0
    now = datetime.now(timezone.utc)
    games = [g for g in slate.get("games", []) if g.get("state") == "pre" and datetime.fromisoformat(g["date"].replace("Z", "+00:00")) > now]
    if not games: return 0
    props = {}
    for m in load(os.path.join(out, "manifest.json"), []) or []:
        if m.get("collection") == "props":
            props.update((load(os.path.join(out, m["file"].split("/")[-1]), {}) or {}).get("games", {}))
    for pls in props.values():
        for pl in pls:
            for pr in pl.get("props", []): pr.pop("alt", None); pr.pop("kv", None)
    kalshi.PLAYER_SERIES, kalshi.GAME_SERIES, kalshi.TO_ESPN, kalshi.FIRST_TD = cfg
    os.environ["EDGE_DATA"] = data            # kalshi.flow keeps its snapshots in this sport's state folder
    kalshi.apply(games, props)
    money = {"at": now.isoformat(timespec="seconds"), "games": {}, "props": {}}
    for g in games:
        if (g.get("kalshi") or {}).get("money"): money["games"][g["id"]] = g["kalshi"]["money"]
        kv = {f'{pl["id"]}|{pr["m"]}': pr["kv"] for pl in props.get(g["id"], []) for pr in pl.get("props", []) if (pr.get("kv") or {}).get("usd", 0) >= 50}
        if kv: money["props"][g["id"]] = kv
    try:   # Polymarket: a second real-money exchange (prices, dollars traded, last-6h money by side)
        import polymarket; money["poly"] = polymarket.apply(games, sport)
    except Exception as e: print(sport, "polymarket failed:", e, file=sys.stderr)
    with open(os.path.join(out, "money.json"), "w", encoding="utf-8") as f: f.write(json.dumps(money, separators=(",", ":")))
    return len(money["games"]) + sum(len(v) for v in money["props"].values()) + len(money.get("poly") or {})


if __name__ == "__main__":
    for sport, out, data, cfg in SPORTS:
        try: print(sport, one(sport, out, data, cfg), "markets with money")
        except Exception as e: print(sport, "money failed:", e, file=sys.stderr)
