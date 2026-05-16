# Stadium Tap (dev)

Multi-league geo-guessing game.  Drop a pin at the venue, within 10km unlocks
3 trivia questions for a score multiplier.  Pure dev environment, expect breaking
changes.

## Stack

- FastAPI + SQLite (persistent volume on Railway)
- Leaflet + CartoDB Voyager tiles (realistic light basemap)
- Vanilla JS frontend, mobile first

## Data model

Three JSON files under `app/data/`:

- `leagues.json` — registry of all leagues (SEC, MLB, NBA, NFL, EPL, etc.) with icon + sort order.  Leagues with zero venues are hidden from the UI.
- `venues.json` — flat dict keyed by venue ID; each venue has `league`, `team`, `name`, `city`, `lat`, `lng`.
- `trivia.json` — keyed by venue ID; each entry has 3 questions (easy / medium / medium_hard) with 4 multiple-choice options.

To add a league worth of venues, just add entries to all three files.  No
backend changes required.

## Score math

- Base: 1000 at 0km distance, linear to 0 at 2000km
- Within 10km (`UNLOCK_RADIUS_KM` env var) unlocks trivia
- Multipliers: 0 correct = 1.0x, 1 = 1.33x, 2 = 1.66x, 3 = 2.0x

## Env vars

- `INVITE_CODE` — required to join (default `stadiumtap` in dev)
- `UNLOCK_RADIUS_KM` — trivia unlock threshold (default 10.0)
- `DB_PATH` — SQLite location (Railway sets this to the mounted volume)

## Local dev

```
pip install -r requirements.txt
DB_PATH=./game.db INVITE_CODE=test uvicorn app.main:app --reload
```

## Roadmap

- Populate venues + trivia for the remaining leagues (Big Ten, NFL, MLB, NBA, Bundesliga, Serie A, La Liga, PSG)
- Per-venue overrides for unlock radius (e.g. tighter radius for famous venues)
- Cross-league total leaderboard view
- Share-card export so players can post results on group chats
