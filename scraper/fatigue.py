"""
Fatigue & Calendar Analyzer.

Evaluates travel fatigue, fixture congestion, and European competition
impact for each team in a match. Uses Football-Data.org /teams/{id}/matches
endpoint to get cross-competition schedule.

Key metrics:
- Rest days since last match
- European midweek involvement (CL/EL/ECL)
- Fixture density (matches in last 14 days)
- Fatigue score (0-100)
"""

import logging
import requests
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

BASE_URL = "https://api.football-data.org/v4"

# European competitions
EUROPEAN_COMPS = {
    "UEFA Champions League", "Champions League",
    "UEFA Europa League", "Europa League",
    "UEFA Europa Conference League", "Conference League",
}

# Rough city coordinates for travel distance estimation (lat, lon)
# Football-Data uses team IDs; we map common European cities
TEAM_CITIES = {
    # Serie A
    100: ("Roma", 41.9, 12.5),
    108: ("Milano", 45.5, 9.2),   # Inter
    98:  ("Milano", 45.5, 9.2),   # Milan
    109: ("Torino", 45.1, 7.7),   # Juventus
    113: ("Napoli", 40.9, 14.3),
    99:  ("Firenze", 43.8, 11.3), # Fiorentina
    102: ("Roma", 41.9, 12.5),    # Lazio
    103: ("Bergamo", 45.7, 9.7),  # Atalanta
    # Premier League
    57:  ("London", 51.5, -0.1),  # Arsenal
    65:  ("Manchester", 53.5, -2.2), # Man City
    66:  ("Manchester", 53.5, -2.2), # Man Utd
    64:  ("Liverpool", 53.4, -3.0),
    61:  ("London", 51.5, -0.1),  # Chelsea
    73:  ("London", 51.5, -0.1),  # Spurs
    # La Liga
    86:  ("Madrid", 40.4, -3.7),  # Real Madrid
    81:  ("Barcelona", 41.4, 2.2),
    78:  ("Madrid", 40.4, -3.7),  # Atletico
    # Bundesliga
    5:   ("München", 48.1, 11.6), # Bayern
    4:   ("Dortmund", 51.5, 7.5), # BVB
    # Ligue 1
    524: ("Paris", 48.9, 2.3),    # PSG
}

# European cities for away travel estimation
EUROPEAN_CITY_COORDS = {
    "istanbul": (41.0, 28.9), "budapest": (47.5, 19.0),
    "athens": (37.9, 23.7), "moscow": (55.8, 37.6),
    "kyiv": (50.5, 30.5), "belgrade": (44.8, 20.5),
    "bucharest": (44.4, 26.1), "prague": (50.1, 14.4),
    "warsaw": (52.2, 21.0), "lisbon": (38.7, -9.1),
    "london": (51.5, -0.1), "madrid": (40.4, -3.7),
    "paris": (48.9, 2.3), "roma": (41.9, 12.5),
    "milano": (45.5, 9.2), "münchen": (48.1, 11.6),
    "amsterdam": (52.4, 4.9), "barcelona": (41.4, 2.2),
}


def _haversine_km(lat1, lon1, lat2, lon2):
    """Approximate distance in km between two points."""
    import math
    R = 6371
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))


def analyze_fatigue(home_team_id: int, away_team_id: int,
                    match_date: str, api_key: str,
                    home_name: str = "", away_name: str = "") -> dict:
    """
    Analyze fatigue for both teams before a given match.

    Returns:
        {
            "home": { fatigue data },
            "away": { fatigue data },
            "insight": "text summary",
            "advantage": "home" | "away" | "neutral"
        }
    """
    if not api_key:
        return {}

    try:
        match_dt = datetime.fromisoformat(match_date.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        try:
            match_dt = datetime.strptime(match_date[:10], "%Y-%m-%d")
        except Exception:
            return {}

    home_data = _get_team_schedule(home_team_id, api_key)
    away_data = _get_team_schedule(away_team_id, api_key)

    home_fatigue = _compute_fatigue(home_data, match_dt, home_team_id, home_name)
    away_fatigue = _compute_fatigue(away_data, match_dt, away_team_id, away_name)

    # Determine advantage
    h_score = home_fatigue.get("fatigue_score", 0)
    a_score = away_fatigue.get("fatigue_score", 0)
    diff = a_score - h_score  # positive = away more fatigued = home advantage

    if diff >= 20:
        advantage = "home"
    elif diff <= -20:
        advantage = "away"
    else:
        advantage = "neutral"

    # Build insight text
    insights = []
    for side, name, fat in [("home", home_name, home_fatigue), ("away", away_name, away_fatigue)]:
        parts = []
        rest = fat.get("rest_days")
        if rest is not None:
            if rest <= 2:
                parts.append(f"solo {rest} giorni di riposo")
            elif rest <= 3:
                parts.append(f"{rest} giorni di riposo")
        if fat.get("played_europe_midweek"):
            comp = fat.get("europe_comp", "Europa")
            parts.append(f"ha giocato in {comp} a metà settimana")
        if fat.get("europe_away"):
            parts.append(f"trasferta europea ({fat.get('europe_opponent', '?')})")
        density = fat.get("matches_14d", 0)
        if density >= 4:
            parts.append(f"{density} partite in 14 giorni")
        if parts:
            insights.append(f"{name}: {', '.join(parts)}")

    summary = ""
    if insights:
        summary = " | ".join(insights)
        if advantage == "home":
            summary += f" → Vantaggio {home_name} (ospiti più stanchi)"
        elif advantage == "away":
            summary += f" → Vantaggio {away_name} (padroni di casa più stanchi)"

    return {
        "home": home_fatigue,
        "away": away_fatigue,
        "insight": summary,
        "advantage": advantage,
        "fatigue_diff": diff,
    }


def _get_team_schedule(team_id: int, api_key: str) -> list:
    """Fetch recent + upcoming matches for a team from Football-Data.org."""
    if not team_id:
        return []

    try:
        headers = {"X-Auth-Token": api_key}
        r = requests.get(
            f"{BASE_URL}/teams/{team_id}/matches",
            params={"status": "SCHEDULED,FINISHED,TIMED", "limit": 20},
            headers=headers,
            timeout=10,
        )
        if r.status_code == 200:
            return r.json().get("matches", [])
        elif r.status_code == 429:
            logger.warning(f"[Fatigue] Rate limited for team {team_id}")
        else:
            logger.warning(f"[Fatigue] API error {r.status_code} for team {team_id}")
    except Exception as e:
        logger.error(f"[Fatigue] Error fetching schedule for team {team_id}: {e}")
    return []


def _compute_fatigue(matches: list, target_dt: datetime,
                     team_id: int, team_name: str) -> dict:
    """Compute fatigue metrics for a team relative to a target match date."""
    if not matches:
        return {"fatigue_score": 0, "rest_days": None, "matches_14d": 0}

    # Parse and sort matches by date
    parsed = []
    for m in matches:
        try:
            dt = datetime.fromisoformat(m["utcDate"].replace("Z", "+00:00"))
        except (ValueError, KeyError):
            continue
        comp = m.get("competition", {}).get("name", "")
        is_europe = comp in EUROPEAN_COMPS

        home_id = m.get("homeTeam", {}).get("id")
        away_id = m.get("awayTeam", {}).get("id")
        is_away = (away_id != team_id) if home_id == team_id else True
        opponent = m.get("awayTeam", {}).get("shortName", "?") if home_id == team_id else m.get("homeTeam", {}).get("shortName", "?")

        parsed.append({
            "dt": dt.replace(tzinfo=None),
            "comp": comp,
            "is_europe": is_europe,
            "is_away": is_away,
            "opponent": opponent,
            "status": m.get("status"),
        })

    parsed.sort(key=lambda x: x["dt"])
    target_naive = target_dt.replace(tzinfo=None) if target_dt.tzinfo else target_dt

    # Find matches BEFORE target date
    before = [p for p in parsed if p["dt"] < target_naive]
    # Find matches AFTER target date (upcoming density)
    after = [p for p in parsed if p["dt"] > target_naive]

    # --- Rest days since last match ---
    rest_days = None
    last_match = None
    if before:
        last_match = before[-1]
        rest_days = (target_naive - last_match["dt"]).days

    # --- European midweek (did they play in Europe in the last 7 days?) ---
    played_europe_midweek = False
    europe_away = False
    europe_comp = ""
    europe_opponent = ""
    week_ago = target_naive - timedelta(days=7)
    for p in before:
        if p["dt"] >= week_ago and p["is_europe"]:
            played_europe_midweek = True
            europe_comp = p["comp"]
            europe_opponent = p["opponent"]
            if p["is_away"]:
                europe_away = True

    # --- Fixture density: matches in last 14 days ---
    two_weeks_ago = target_naive - timedelta(days=14)
    matches_14d = sum(1 for p in before if p["dt"] >= two_weeks_ago)

    # --- Upcoming congestion: next match after target ---
    next_rest = None
    if after:
        next_rest = (after[0]["dt"] - target_naive).days

    # --- FATIGUE SCORE (0-100) ---
    score = 0

    # Rest days component (max 40 points)
    if rest_days is not None:
        if rest_days <= 2:
            score += 40
        elif rest_days <= 3:
            score += 25
        elif rest_days <= 4:
            score += 10
        # 5+ days = 0 (well rested)

    # European midweek component (max 30 points)
    if played_europe_midweek:
        score += 15
        if europe_away:
            score += 15  # long travel adds more fatigue

    # Fixture density component (max 20 points)
    if matches_14d >= 5:
        score += 20
    elif matches_14d >= 4:
        score += 15
    elif matches_14d >= 3:
        score += 5

    # Upcoming congestion (max 10 points) — managers may rotate
    if next_rest is not None and next_rest <= 3:
        score += 10
    elif next_rest is not None and next_rest <= 4:
        score += 5

    score = min(100, score)

    return {
        "fatigue_score": score,
        "rest_days": rest_days,
        "last_match": {
            "opponent": last_match["opponent"] if last_match else None,
            "comp": last_match["comp"] if last_match else None,
            "is_away": last_match["is_away"] if last_match else None,
            "days_ago": rest_days,
        } if last_match else None,
        "played_europe_midweek": played_europe_midweek,
        "europe_away": europe_away,
        "europe_comp": europe_comp if played_europe_midweek else None,
        "europe_opponent": europe_opponent if played_europe_midweek else None,
        "matches_14d": matches_14d,
        "next_rest_days": next_rest,
    }
