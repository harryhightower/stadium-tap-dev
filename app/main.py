"""Stadium Tap - DEV environment (v3 multi-league)
Players attempt venues across many leagues.  Within 10km of the real spot
unlocks 3 trivia questions about the team/venue.  Leaderboard is per-league
plus a global total.
"""

from fastapi import FastAPI, HTTPException, Depends, Header, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from pathlib import Path
from typing import Optional, List
from datetime import datetime
from zoneinfo import ZoneInfo
import sqlite3
import json
import os
import uuid
import math
import hashlib
import random

APP_DIR = Path(__file__).parent
DATA_DIR = APP_DIR / "data"
STATIC_DIR = APP_DIR.parent / "static"
DB_PATH = os.environ.get("DB_PATH", "/app/data/game.db")
INVITE_CODE = os.environ.get("INVITE_CODE", "stadiumtap")
UNLOCK_RADIUS_KM = float(os.environ.get("UNLOCK_RADIUS_KM", "10.0"))
DAILY_QUIZ_SIZE = int(os.environ.get("DAILY_QUIZ_SIZE", "10"))
ET = ZoneInfo("America/New_York")

with open(DATA_DIR / "leagues.json") as f:
    LEAGUES = json.load(f)
with open(DATA_DIR / "venues.json") as f:
    VENUES = json.load(f)
with open(DATA_DIR / "trivia.json") as f:
    TRIVIA = json.load(f)

app = FastAPI(title="Stadium Tap (dev)")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = get_db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS players (
            token TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            player_token TEXT NOT NULL,
            league_key TEXT NOT NULL,
            venue_key TEXT NOT NULL,
            mode TEXT NOT NULL DEFAULT 'browse',
            play_date TEXT,
            guess_lat REAL NOT NULL,
            guess_lng REAL NOT NULL,
            distance_km REAL NOT NULL,
            base_score INTEGER NOT NULL,
            trivia_correct INTEGER NOT NULL DEFAULT 0,
            trivia_submitted INTEGER NOT NULL DEFAULT 0,
            multiplier REAL NOT NULL DEFAULT 1.0,
            final_score INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(player_token, venue_key, mode, play_date)
        );
        CREATE INDEX IF NOT EXISTS idx_attempts_league
          ON attempts(player_token, league_key);
        CREATE INDEX IF NOT EXISTS idx_attempts_mode_date
          ON attempts(mode, play_date);
        """
    )
    conn.commit()
    conn.close()


init_db()


def haversine(lat1, lng1, lat2, lng2):
    R = 6371.0
    lat1r, lng1r, lat2r, lng2r = map(math.radians, [lat1, lng1, lat2, lng2])
    dlat = lat2r - lat1r
    dlng = lng2r - lng1r
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1r) * math.cos(lat2r) * math.sin(dlng / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def calc_base_score(distance_km: float) -> int:
    return max(0, int(round(1000 * (1 - distance_km / 2000))))


def today_et_date() -> str:
    return datetime.now(ET).date().isoformat()


def todays_quiz_venues(
    date_str: str,
    size: int = DAILY_QUIZ_SIZE,
    region: Optional[str] = None,
) -> List[str]:
    seed = hashlib.md5(date_str.encode()).hexdigest()
    rng = random.Random(seed)
    populated = [k for k in VENUES.keys() if k in TRIVIA]
    if region:
        populated = [
            k for k in populated
            if LEAGUES.get(VENUES[k].get("league"), {}).get("region") == region
        ]
    populated.sort()
    return rng.sample(populated, min(size, len(populated)))


def get_player(authorization: Optional[str] = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing token")
    token = authorization[7:]
    conn = get_db()
    row = conn.execute("SELECT * FROM players WHERE token = ?", (token,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(401, "Invalid token")
    return dict(row)


def venues_by_league(league_key):
    return [
        (k, v) for k, v in VENUES.items()
        if v.get("league") == league_key
    ]


class JoinRequest(BaseModel):
    name: str
    invite_code: str


@app.post("/api/join")
def join(req: JoinRequest):
    if req.invite_code.strip() != INVITE_CODE:
        raise HTTPException(403, "Wrong invite code")
    name = req.name.strip()[:30]
    if not name:
        raise HTTPException(400, "Name required")
    token = str(uuid.uuid4())
    conn = get_db()
    conn.execute(
        "INSERT INTO players (token, name, created_at) VALUES (?, ?, ?)",
        (token, name, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()
    return {"token": token, "name": name}


@app.get("/api/leagues")
def leagues_list(player=Depends(get_player)):
    """Return all leagues that currently have venues, with counts and player progress."""
    conn = get_db()
    attempt_rows = conn.execute(
        "SELECT league_key, COUNT(*) as cnt, SUM(final_score) as total FROM attempts WHERE player_token = ? AND mode = 'browse' GROUP BY league_key",
        (player["token"],),
    ).fetchall()
    conn.close()
    by_league = {r["league_key"]: dict(r) for r in attempt_rows}

    out = []
    for key, info in LEAGUES.items():
        venue_count = len(venues_by_league(key))
        if venue_count == 0:
            # skip leagues without venues so the UI stays clean; show again once populated
            continue
        prog = by_league.get(key, {"cnt": 0, "total": 0})
        out.append({
            "key": key,
            "name": info["name"],
            "icon": info["icon"],
            "sort": info.get("sort", 99),
            "venue_count": venue_count,
            "played": prog["cnt"] or 0,
            "score": prog["total"] or 0,
        })
    out.sort(key=lambda x: x["sort"])
    return out


@app.get("/api/venues")
def venues_for_league(league: str = Query(...), player=Depends(get_player)):
    """Return venues for one league, in alphabetical order by team, with attempt state."""
    if league not in LEAGUES:
        raise HTTPException(404, "Unknown league")

    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM attempts WHERE player_token = ? AND league_key = ? AND mode = 'browse'",
        (player["token"], league),
    ).fetchall()
    conn.close()
    by_key = {r["venue_key"]: dict(r) for r in rows}

    league_venues = sorted(
        venues_by_league(league),
        key=lambda kv: kv[1]["team"]
    )
    out = []
    for i, (key, v) in enumerate(league_venues, start=1):
        entry = {
            "number": i,
            "key": key,
            "league": league,
            "team": v["team"],
            "stadium_name": v["name"],
            "city": v["city"],
            "played": key in by_key,
        }
        if key in by_key:
            a = by_key[key]
            entry["result"] = {
                "distance_km": a["distance_km"],
                "stadium_lat": v["lat"],
                "stadium_lng": v["lng"],
                "guess_lat": a["guess_lat"],
                "guess_lng": a["guess_lng"],
                "base_score": a["base_score"],
                "trivia_correct": a["trivia_correct"],
                "trivia_submitted": bool(a["trivia_submitted"]),
                "multiplier": a["multiplier"],
                "final_score": a["final_score"],
                "unlocked": a["distance_km"] <= UNLOCK_RADIUS_KM,
            }
        out.append(entry)
    return out


class GuessRequest(BaseModel):
    venue_key: str
    lat: float
    lng: float
    mode: Optional[str] = "browse"
    region: Optional[str] = None


@app.post("/api/guess")
def guess(req: GuessRequest, player=Depends(get_player)):
    if req.venue_key not in VENUES:
        raise HTTPException(400, "Unknown venue")
    mode = (req.mode or "browse").lower()
    if mode not in ("browse", "quiz"):
        raise HTTPException(400, "Invalid mode")
    venue = VENUES[req.venue_key]
    league_key = venue["league"]
    play_date = today_et_date() if mode == "quiz" else None
    region_filter = req.region if mode == "quiz" and req.region in ("us", "intl") else None

    if mode == "quiz" and req.venue_key not in todays_quiz_venues(play_date, region=region_filter):
        raise HTTPException(400, "Venue is not in today's quiz")

    conn = get_db()
    if mode == "quiz":
        existing = conn.execute(
            "SELECT id FROM attempts WHERE player_token = ? AND venue_key = ? AND mode = 'quiz' AND play_date = ?",
            (player["token"], req.venue_key, play_date),
        ).fetchone()
    else:
        existing = conn.execute(
            "SELECT id FROM attempts WHERE player_token = ? AND venue_key = ? AND mode = 'browse'",
            (player["token"], req.venue_key),
        ).fetchone()
    if existing:
        conn.close()
        raise HTTPException(400, "Already attempted this venue")

    distance = haversine(req.lat, req.lng, venue["lat"], venue["lng"])
    base_score = calc_base_score(distance)
    unlocked = distance <= UNLOCK_RADIUS_KM

    conn.execute(
        """INSERT INTO attempts
        (player_token, league_key, venue_key, mode, play_date, guess_lat, guess_lng, distance_km,
         base_score, trivia_correct, trivia_submitted, multiplier, final_score, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 1.0, ?, ?)""",
        (
            player["token"], league_key, req.venue_key, mode, play_date,
            req.lat, req.lng, distance, base_score, base_score,
            datetime.utcnow().isoformat(),
        ),
    )
    conn.commit()
    conn.close()

    resp = {
        "venue_key": req.venue_key,
        "league_key": league_key,
        "distance_km": round(distance, 2),
        "stadium_lat": venue["lat"],
        "stadium_lng": venue["lng"],
        "stadium_name": venue["name"],
        "team": venue["team"],
        "city": venue["city"],
        "base_score": base_score,
        "unlocked": unlocked,
        "unlock_radius_km": UNLOCK_RADIUS_KM,
    }
    if unlocked and req.venue_key in TRIVIA:
        resp["trivia"] = [
            {"difficulty": q["difficulty"], "question": q["question"], "options": q["options"]}
            for q in TRIVIA[req.venue_key]["questions"]
        ]
    return resp


class TriviaRequest(BaseModel):
    venue_key: str
    answers: List[str]
    mode: Optional[str] = "browse"


@app.post("/api/trivia")
def submit_trivia(req: TriviaRequest, player=Depends(get_player)):
    if req.venue_key not in VENUES:
        raise HTTPException(400, "Unknown venue")
    if req.venue_key not in TRIVIA:
        raise HTTPException(400, "No trivia available for this venue yet")
    if len(req.answers) != 3:
        raise HTTPException(400, "Need exactly 3 answers")
    mode = (req.mode or "browse").lower()
    if mode not in ("browse", "quiz"):
        raise HTTPException(400, "Invalid mode")
    play_date = today_et_date() if mode == "quiz" else None

    conn = get_db()
    if mode == "quiz":
        row = conn.execute(
            "SELECT * FROM attempts WHERE player_token = ? AND venue_key = ? AND mode = 'quiz' AND play_date = ?",
            (player["token"], req.venue_key, play_date),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM attempts WHERE player_token = ? AND venue_key = ? AND mode = 'browse'",
            (player["token"], req.venue_key),
        ).fetchone()
    if not row:
        conn.close()
        raise HTTPException(400, "Must guess first")
    attempt = dict(row)
    if attempt["trivia_submitted"]:
        conn.close()
        raise HTTPException(400, "Trivia already submitted for this venue")
    if attempt["distance_km"] > UNLOCK_RADIUS_KM:
        conn.close()
        raise HTTPException(400, f"Did not unlock trivia (not within {UNLOCK_RADIUS_KM}km)")

    questions = TRIVIA[req.venue_key]["questions"]
    correct = sum(1 for q, a in zip(questions, req.answers) if q["answer"] == a)
    multipliers = {0: 1.0, 1: 1.33, 2: 1.66, 3: 2.0}
    mult = multipliers[correct]
    final = int(round(attempt["base_score"] * mult))

    conn.execute(
        """UPDATE attempts SET trivia_correct = ?, trivia_submitted = 1,
           multiplier = ?, final_score = ? WHERE id = ?""",
        (correct, mult, final, attempt["id"]),
    )
    conn.commit()
    conn.close()

    feedback = [
        {
            "question": q["question"],
            "your_answer": a,
            "correct_answer": q["answer"],
            "correct": q["answer"] == a,
        }
        for q, a in zip(questions, req.answers)
    ]
    return {"correct": correct, "multiplier": mult, "final_score": final, "feedback": feedback}


@app.get("/api/leaderboard")
def leaderboard(
    league: Optional[str] = None,
    mode: str = "browse",
    scope: str = "alltime",
    date: Optional[str] = None,
    region: Optional[str] = None,
    player=Depends(get_player),
):
    """Leaderboard with optional filters.

    - mode: 'browse' (default) or 'quiz'
    - scope: 'alltime' (default) or 'daily'
    - date: only used when scope='daily'; defaults to today's ET date.
      For mode='quiz' it matches play_date; for mode='browse' it matches the
      created_at date.
    - league: optional league filter (only meaningful for browse mode).
    - region: optional region filter for mode='quiz' ('us' or 'intl');
      restricts attempts to venues whose league has that region.
    """
    if mode not in ("browse", "quiz"):
        raise HTTPException(400, "Invalid mode")
    if scope not in ("daily", "alltime"):
        raise HTTPException(400, "Invalid scope")
    if scope == "daily" and date is None:
        date = today_et_date()

    join_conditions = ["a.player_token = p.token", "a.mode = ?"]
    params: List = [mode]
    if league:
        if league not in LEAGUES:
            raise HTTPException(404, "Unknown league")
        join_conditions.append("a.league_key = ?")
        params.append(league)
    if scope == "daily":
        if mode == "quiz":
            join_conditions.append("a.play_date = ?")
        else:
            join_conditions.append("date(a.created_at) = ?")
        params.append(date)

    region_filter = region if mode == "quiz" and region in ("us", "intl") else None
    if region_filter:
        region_leagues = [k for k, v in LEAGUES.items() if v.get("region") == region_filter]
        if region_leagues:
            placeholders = ",".join("?" * len(region_leagues))
            join_conditions.append(f"a.league_key IN ({placeholders})")
            params.extend(region_leagues)

    sql = f"""
        SELECT p.name,
               COUNT(a.id) AS venues_played,
               COALESCE(SUM(a.final_score), 0) AS total_score
        FROM players p
        LEFT JOIN attempts a ON {' AND '.join(join_conditions)}
        GROUP BY p.token, p.name
        HAVING venues_played > 0
        ORDER BY total_score DESC
    """
    conn = get_db()
    rows = conn.execute(sql, params).fetchall()
    conn.close()

    if mode == "quiz":
        if scope == "daily":
            total_venues = len(todays_quiz_venues(date, region=region_filter))
        elif region_filter:
            total_venues = sum(
                1 for k, v in VENUES.items()
                if k in TRIVIA and LEAGUES.get(v.get("league"), {}).get("region") == region_filter
            )
        else:
            total_venues = DAILY_QUIZ_SIZE
    elif league:
        total_venues = len(venues_by_league(league))
    else:
        total_venues = sum(1 for v in VENUES.values())

    return {
        "league": league,
        "mode": mode,
        "scope": scope,
        "date": date if scope == "daily" else None,
        "region": region_filter,
        "total_venues": total_venues,
        "rows": [dict(r) for r in rows],
    }


@app.get("/api/daily-quiz")
def daily_quiz(
    region: Optional[str] = "us",
    player=Depends(get_player),
):
    """Today's region-scoped daily quiz. Same shape as /api/venues but with the
    sampled 10-venue set; played status reflects only today's quiz attempts.
    region='us' or 'intl' filters by league.region; anything else (or empty)
    falls back to all populated venues."""
    region_filter = region if region in ("us", "intl") else None
    play_date = today_et_date()
    venue_keys = todays_quiz_venues(play_date, region=region_filter)

    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM attempts WHERE player_token = ? AND mode = 'quiz' AND play_date = ?",
        (player["token"], play_date),
    ).fetchall()
    conn.close()
    by_key = {r["venue_key"]: dict(r) for r in rows}

    out = []
    for i, key in enumerate(venue_keys, start=1):
        v = VENUES[key]
        entry = {
            "number": i,
            "key": key,
            "league": v["league"],
            "team": v["team"],
            "stadium_name": v["name"],
            "city": v["city"],
            "played": key in by_key,
        }
        if key in by_key:
            a = by_key[key]
            entry["result"] = {
                "distance_km": a["distance_km"],
                "stadium_lat": v["lat"],
                "stadium_lng": v["lng"],
                "guess_lat": a["guess_lat"],
                "guess_lng": a["guess_lng"],
                "base_score": a["base_score"],
                "trivia_correct": a["trivia_correct"],
                "trivia_submitted": bool(a["trivia_submitted"]),
                "multiplier": a["multiplier"],
                "final_score": a["final_score"],
                "unlocked": a["distance_km"] <= UNLOCK_RADIUS_KM,
            }
        out.append(entry)
    return {"date": play_date, "venues": out}


@app.get("/api/history")
def history(player=Depends(get_player)):
    conn = get_db()
    rows = conn.execute(
        """SELECT created_at, league_key, venue_key, mode, play_date,
                  distance_km, base_score,
                  trivia_correct, trivia_submitted, multiplier, final_score
           FROM attempts WHERE player_token = ? ORDER BY created_at DESC""",
        (player["token"],),
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        v = VENUES.get(d["venue_key"], {})
        lg = LEAGUES.get(d["league_key"], {})
        d["stadium_name"] = v.get("name", d["venue_key"])
        d["team"] = v.get("team", "")
        d["league_name"] = lg.get("name", d["league_key"])
        d["league_icon"] = lg.get("icon", "")
        out.append(d)
    return out


@app.get("/api/me")
def me(player=Depends(get_player)):
    return {"name": player["name"]}


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def index():
    return FileResponse(str(STATIC_DIR / "index.html"))
