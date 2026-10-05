# NFL Edge Board

Lines and line movement, storylines, player props with last-5 trends and "due" reads, lineups with inactives,
team analytics (EPA, defense vs position), live scores and prop tracking, and a personal bet tracker.

**Install it:** open the site on your phone, then Share > Add to Home Screen (iPhone) or menu > Install app (Android).

**How it stays fresh:** a GitHub Action (`.github/workflows/sync.yml`) runs `sync.py` every few hours and on game days
around when inactives post, and commits the results to `data/`. Live scores and player stats come straight from ESPN
in your browser every 15 seconds. Bets are saved on each person's own device.

**Optional:** add an `ODDS_API_KEY` repository secret (key from the-odds-api.com) to compare game lines across sportsbooks. To also pull player-prop prices from every book, add a repository variable `ODDS_API_PROPS` = `1` (uses ~11 API credits per game, so it needs a paid plan).

Data: ESPN's public feeds (unofficial), nflverse, Open-Meteo. For personal use among friends; not betting advice.
