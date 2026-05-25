#!/usr/bin/env python3
"""
Fetch player stats from Sofascore API for ALL leagues.

Scrapes season statistics for all players in all teams and saves
them into betanalyzer.db (player_info + player_stats_cache) using the
same schema as the Sportmonks nightly_sync, so scorers.py and cards.py
work without modifications.

Supports two modes:
  - Full:     python3 fetch_sofascore_stats.py           → all leagues
  - Single:   python3 fetch_sofascore_stats.py serie_a    → one league
  - Rotation: python3 fetch_sofascore_stats.py --rotate 3 → 3 leagues/day (round-robin)

Designed to run nightly via scheduler.py with --rotate for distributed load.
"""

import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [SS] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

BASE_DIR = Path(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = BASE_DIR / "data" / "betanalyzer.db"
ROTATION_FILE = BASE_DIR / "data" / "sofascore_rotation.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://www.sofascore.com/",
    "Origin": "https://www.sofascore.com",
    "Cache-Control": "no-cache",
}

# ── League configuration ─────────────────────────────────────────
# (tournament_id, season_id, db_league_id, db_season_id)
# season_id must be updated yearly when new season starts
LEAGUES = {
    "brasileirao": {
        "tournament_id": 325,
        "season_id": 87678,        # 2025/26
        "db_league_id": "brasileirao",
        "db_season_id": 99_325,
        "label": "Brasileirão",
    },
    "serie_a": {
        "tournament_id": 23,
        "season_id": 76457,        # 25/26
        "db_league_id": "serie_a",
        "db_season_id": 99_023,
        "label": "Serie A",
    },
    "premier_league": {
        "tournament_id": 17,
        "season_id": 76986,        # 25/26
        "db_league_id": "premier_league",
        "db_season_id": 99_017,
        "label": "Premier League",
    },
    "la_liga": {
        "tournament_id": 8,
        "season_id": 77559,        # 25/26
        "db_league_id": "la_liga",
        "db_season_id": 99_008,
        "label": "La Liga",
    },
    "bundesliga": {
        "tournament_id": 35,
        "season_id": 77333,        # 25/26
        "db_league_id": "bundesliga",
        "db_season_id": 99_035,
        "label": "Bundesliga",
    },
    "ligue_1": {
        "tournament_id": 34,
        "season_id": 77356,        # 25/26
        "db_league_id": "ligue_1",
        "db_season_id": 99_034,
        "label": "Ligue 1",
    },
    "eredivisie": {
        "tournament_id": 37,
        "season_id": 77012,        # 25/26
        "db_league_id": "eredivisie",
        "db_season_id": 99_037,
        "label": "Eredivisie",
    },
    "primeira_liga": {
        "tournament_id": 238,
        "season_id": 77806,        # 25/26
        "db_league_id": "primeira_liga",
        "db_season_id": 99_238,
        "label": "Primeira Liga",
    },
    "championship": {
        "tournament_id": 18,
        "season_id": 77347,        # 25/26
        "db_league_id": "championship",
        "db_season_id": 99_018,
        "label": "Championship",
    },
}

# Sofascore IDs offset to avoid clashing with Sportmonks IDs
SS_PLAYER_OFFSET = 90_000_000
SS_TEAM_OFFSET = 900_000

# Sofascore position → Sportmonks position_id
POS_MAP = {
    "G": 24,   # Goalkeeper
    "D": 25,   # Defender
    "M": 26,   # Midfielder
    "F": 27,   # Forward/Attacker
}


# ── HTTP helper ──────────────────────────────────────────────────
def _get(url: str, retries: int = 3) -> dict | None:
    """GET with retries and polite delay."""
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                wait = 30 * (attempt + 1)
                logger.warning("Rate-limited, waiting %ds...", wait)
                time.sleep(wait)
                continue
            if r.status_code == 404:
                return None
            logger.warning("HTTP %d for %s", r.status_code, url)
        except requests.RequestException as e:
            logger.warning("Request error: %s (attempt %d)", e, attempt + 1)
            time.sleep(3)
    return None


# ── Get all teams from standings ─────────────────────────────────
def _get_teams(tournament_id: int, season_id: int) -> list[dict]:
    """Fetch all teams from Sofascore standings."""
    data = _get(
        f"https://www.sofascore.com/api/v1/unique-tournament/"
        f"{tournament_id}/season/{season_id}/standings/total"
    )
    if not data:
        logger.error("Failed to fetch standings")
        return []

    teams = []
    for group in data.get("standings", []):
        for row in group.get("rows", []):
            t = row.get("team", {})
            teams.append({
                "ss_id": t["id"],
                "name": t["name"],
                "short": t.get("shortName", t["name"]),
            })
    logger.info("Found %d teams in standings", len(teams))
    return teams


# ── Get players for a team ───────────────────────────────────────
def _get_team_players(team_ss_id: int) -> list[dict]:
    """Fetch player list for a team from Sofascore."""
    data = _get(f"https://www.sofascore.com/api/v1/team/{team_ss_id}/players")
    if not data:
        return []

    players = []
    for entry in data.get("players", []):
        p = entry.get("player", {})
        players.append({
            "ss_id": p["id"],
            "name": p.get("name", ""),
            "short_name": p.get("shortName", ""),
            "position": p.get("position", ""),
            "shirt": p.get("shirtNumber"),
            "country": p.get("country", {}).get("name", ""),
            "date_of_birth": p.get("dateOfBirthTimestamp"),
            "height": p.get("height"),
            "preferred_foot": p.get("preferredFoot"),
        })
    return players


# ── Get season stats for a player ────────────────────────────────
def _get_player_stats(player_ss_id: int, tournament_id: int, season_id: int) -> dict | None:
    """Fetch season statistics for a single player."""
    data = _get(
        f"https://www.sofascore.com/api/v1/player/{player_ss_id}/"
        f"unique-tournament/{tournament_id}/season/{season_id}/statistics/overall"
    )
    if not data:
        return None
    return data.get("statistics")


# ── Convert Sofascore stats to Sportmonks-compatible stats_json ──
def _to_sportmonks_format(ss: dict) -> dict:
    """Map Sofascore stat fields to the Sportmonks stats_json schema."""
    return {
        # Core stats
        "goals": ss.get("goals", 0),
        "assists": ss.get("assists", 0),
        "appearances": ss.get("appearances", 0),

        # Shooting
        "shots_total": ss.get("totalShots", 0),
        "shots_on_target": ss.get("shotsOnTarget", 0),

        # Passing
        "key_passes": ss.get("keyPasses", 0),
        "accurate_passes_pct": round(ss.get("accuratePassesPercentage", 0), 1),

        # Dribbling
        "dribbles_success": ss.get("successfulDribbles", 0),
        "dribbles_attempts": ss.get("totalContest", 0),

        # Defending
        "tackles": ss.get("tackles", 0),
        "interceptions": ss.get("interceptions", 0),
        "clearances": ss.get("clearances", 0),
        "blocks": ss.get("blockedShots", 0),
        "aerials_won": ss.get("aerialDuelsWon", 0),

        # Discipline
        "fouls_committed": ss.get("fouls", 0),
        "fouls_drawn": ss.get("wasFouled", 0),

        # Penalties
        "penalty_goals": ss.get("penaltyGoals", 0),
        "penalty_won": ss.get("penaltyWon", 0),
        "penalties_taken": ss.get("penaltiesTaken", 0),

        # Extended stats (Sofascore-only, enriches analysis)
        "big_chances_created": ss.get("bigChancesCreated", 0),
        "big_chances_missed": ss.get("bigChancesMissed", 0),
        "expected_goals": round(ss.get("expectedGoals", 0), 3),
        "expected_assists": round(ss.get("expectedAssists", 0), 3),
        "minutes_played": ss.get("minutesPlayed", 0),
        "matches_started": ss.get("matchesStarted", 0),
        "ground_duels_won": ss.get("groundDuelsWon", 0),
        "aerial_duels_won": ss.get("aerialDuelsWon", 0),
        "ball_recovery": ss.get("ballRecovery", 0),
        "possession_lost": ss.get("possessionLost", 0),
        "yellow_cards": ss.get("yellowCards", 0),
        "red_cards": ss.get("redCards", 0),
        "yellow_red_cards": ss.get("yellowRedCards", 0),
        "total_crosses": ss.get("totalCross", 0),
        "accurate_crosses": ss.get("accurateCrosses", 0),
        "long_balls": ss.get("totalLongBalls", 0),
        "accurate_long_balls": ss.get("accurateLongBalls", 0),
    }


# ── Save to database ─────────────────────────────────────────────
def _save_to_db(team: dict, players_with_stats: list[tuple],
                db_league_id: str, db_season_id: int):
    """Save player info + stats to betanalyzer.db."""
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    cursor = conn.cursor()
    now = datetime.now(timezone.utc).isoformat()

    db_team_id = team["ss_id"] + SS_TEAM_OFFSET

    saved = 0
    for player, stats in players_with_stats:
        db_player_id = player["ss_id"] + SS_PLAYER_OFFSET
        pos_id = POS_MAP.get(player["position"])
        # Sofascore ratings are 0-10, Sportmonks are 0-100 → multiply by 10
        raw_rating = stats.get("rating", 0) if stats else 0
        rating = round(raw_rating * 10, 1)

        # Upsert player_info
        cursor.execute("""
            INSERT OR REPLACE INTO player_info
                (player_id, name, team_id, team_name, league_id, position_id, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            db_player_id,
            player["name"],
            db_team_id,
            team["name"],
            db_league_id,
            pos_id,
            now,
        ))

        # Build stats_json in Sportmonks format
        if stats:
            stats_json = json.dumps(_to_sportmonks_format(stats), ensure_ascii=False)
        else:
            stats_json = json.dumps({
                "goals": 0, "assists": 0, "appearances": 0,
                "shots_total": 0, "shots_on_target": 0,
                "fouls_committed": 0, "fouls_drawn": 0,
                "tackles": 0, "interceptions": 0,
                "key_passes": 0, "dribbles_success": 0,
                "dribbles_attempts": 0, "aerials_won": 0,
                "clearances": 0, "blocks": 0,
                "accurate_passes_pct": 0, "big_chances_created": 0,
            })

        # Upsert player_stats_cache
        cursor.execute("""
            INSERT OR REPLACE INTO player_stats_cache
                (player_id, season_id, team_id, stats_json, rating, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            db_player_id,
            db_season_id,
            db_team_id,
            stats_json,
            round(rating, 2),
            now,
        ))

        saved += 1

    conn.commit()
    conn.close()
    return saved


# ── Fetch one league ─────────────────────────────────────────────
def fetch_league(league_key: str) -> int:
    """Scrape all teams for a single league. Returns total players saved."""
    cfg = LEAGUES.get(league_key)
    if not cfg:
        logger.error("Unknown league: %s (available: %s)", league_key, ", ".join(LEAGUES.keys()))
        return 0

    tournament_id = cfg["tournament_id"]
    season_id = cfg["season_id"]
    db_league_id = cfg["db_league_id"]
    db_season_id = cfg["db_season_id"]
    label = cfg["label"]

    logger.info("═" * 50)
    logger.info("  %s (tournament=%d, season=%d)", label, tournament_id, season_id)
    logger.info("═" * 50)

    teams = _get_teams(tournament_id, season_id)
    if not teams:
        logger.error("No teams found for %s, skipping", label)
        return 0

    total_players = 0
    total_with_stats = 0

    for i, team in enumerate(teams, 1):
        logger.info("[%d/%d] %s (SS ID: %d)...", i, len(teams), team["name"], team["ss_id"])

        # 1. Get player list
        players = _get_team_players(team["ss_id"])
        if not players:
            logger.warning("  No players found for %s", team["name"])
            time.sleep(2)
            continue

        logger.info("  %d players in squad", len(players))
        time.sleep(1)

        # 2. Get stats for each player
        players_with_stats = []
        for pi, player in enumerate(players):
            stats = _get_player_stats(player["ss_id"], tournament_id, season_id)
            players_with_stats.append((player, stats))

            if stats and stats.get("appearances", 0) > 0:
                total_with_stats += 1

            # Polite delay: 0.5s between players, extra pause every 20
            time.sleep(0.5)
            if (pi + 1) % 20 == 0:
                time.sleep(2)

        # 3. Save to DB
        saved = _save_to_db(team, players_with_stats, db_league_id, db_season_id)
        total_players += saved

        apps_list = [(p["name"], s.get("appearances", 0), s.get("goals", 0))
                     for p, s in players_with_stats if s and s.get("appearances", 0) > 0]
        apps_list.sort(key=lambda x: -x[1])
        top3 = ", ".join(f"{n}({a}app/{g}g)" for n, a, g in apps_list[:3])
        logger.info("  ✓ Saved %d players | Top: %s", saved, top3)

        # Extra pause every 5 teams
        if i % 5 == 0 and i < len(teams):
            logger.info("  (pause 5s)")
            time.sleep(5)

    logger.info("─" * 50)
    logger.info("%s done: %d players (%d with stats)", label, total_players, total_with_stats)
    logger.info("─" * 50)
    return total_players


# ── Rotation logic ───────────────────────────────────────────────
def _get_rotation_leagues(count: int) -> list[str]:
    """Pick the next `count` leagues in round-robin order.

    Persists rotation state in a JSON file so each run picks up
    where the previous one left off.
    """
    all_keys = list(LEAGUES.keys())

    # Load last index
    last_index = 0
    if ROTATION_FILE.exists():
        try:
            with open(ROTATION_FILE) as f:
                state = json.load(f)
                last_index = state.get("next_index", 0) % len(all_keys)
        except Exception:
            pass

    # Pick next `count` leagues (wrap around)
    selected = []
    for i in range(count):
        idx = (last_index + i) % len(all_keys)
        selected.append(all_keys[idx])

    # Save next starting point
    next_index = (last_index + count) % len(all_keys)
    try:
        with open(ROTATION_FILE, "w") as f:
            json.dump({
                "next_index": next_index,
                "last_run": datetime.now(timezone.utc).isoformat(),
                "last_leagues": selected,
            }, f)
    except Exception:
        pass

    return selected


# ── Main ─────────────────────────────────────────────────────────
def fetch_all(leagues: list[str] | None = None):
    """Scrape specified leagues (or all) and save player stats to DB."""
    if leagues is None:
        leagues = list(LEAGUES.keys())

    logger.info("Starting Sofascore player stats scrape")
    logger.info("Leagues to process: %s", ", ".join(leagues))

    grand_total = 0
    for li, league_key in enumerate(leagues):
        total = fetch_league(league_key)
        grand_total += total

        # Pause between leagues (30s)
        if li < len(leagues) - 1:
            logger.info("(pause 30s before next league)")
            time.sleep(30)

    logger.info("=" * 50)
    logger.info("ALL DONE! %d total players saved across %d leagues", grand_total, len(leagues))
    logger.info("=" * 50)


if __name__ == "__main__":
    args = sys.argv[1:]

    if "--rotate" in args:
        # Rotation mode: --rotate N
        idx = args.index("--rotate")
        count = int(args[idx + 1]) if idx + 1 < len(args) else 3
        leagues_to_run = _get_rotation_leagues(count)
        logger.info("Rotation mode: running %d leagues → %s", count, ", ".join(leagues_to_run))
        fetch_all(leagues_to_run)
    elif args and args[0] in LEAGUES:
        # Single league mode
        fetch_all([args[0]])
    elif args and args[0] == "--all":
        # Force all leagues
        fetch_all()
    else:
        # Default: all leagues
        fetch_all()
