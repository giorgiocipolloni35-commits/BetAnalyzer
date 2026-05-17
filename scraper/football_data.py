"""
Client per Football-Data.org API v4 (free tier)
Fornisce classifica e forma recente per le leghe principali.
"""
import logging
import requests
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

LEAGUE_CODES = {
    "italy_serie_a":          "SA",
    "england_premier_league": "PL",
    "spain_la_liga":          "PD",
    "germany_bundesliga":     "BL1",
    "denmark_superliga":      "DSL",
    "scotland_premiership":   "SPL",
}

BASE_URL = "https://api.football-data.org/v4"


class FootballDataClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.headers = {"X-Auth-Token": api_key}
        self._standings_cache = {}
        self._matches_cache = {}

    def _get(self, path: str, params: dict = None) -> dict | None:
        try:
            resp = requests.get(
                f"{BASE_URL}{path}",
                headers=self.headers,
                params=params,
                timeout=10,
            )
            if resp.status_code == 429:
                logger.warning("Football-Data.org rate limit raggiunto")
                return None
            if resp.status_code == 403:
                logger.warning(f"Football-Data.org accesso negato per {path} (piano free?)")
                return None
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"Football-Data.org errore: {e}")
            return None

    def get_standings(self, league_key: str) -> list[dict]:
        code = LEAGUE_CODES.get(league_key)
        if not code:
            return []

        if code in self._standings_cache:
            return self._standings_cache[code]

        data = self._get(f"/competitions/{code}/standings")
        if not data:
            return []

        standings = []
        for table in data.get("standings", []):
            if table.get("type") != "TOTAL":
                continue
            for row in table.get("table", []):
                team = row.get("team", {})
                standings.append({
                    "position": row.get("position"),
                    "team_name": team.get("name", ""),
                    "team_short": team.get("shortName", ""),
                    "team_id": team.get("id"),
                    "played": row.get("playedGames", 0),
                    "won": row.get("won", 0),
                    "draw": row.get("draw", 0),
                    "lost": row.get("lost", 0),
                    "points": row.get("points", 0),
                    "goals_for": row.get("goalsFor", 0),
                    "goals_against": row.get("goalsAgainst", 0),
                    "goals_diff": row.get("goalDifference", 0),
                })

        self._standings_cache[code] = standings
        logger.info(f"[Football-Data] Classifica {code}: {len(standings)} squadre")
        return standings

    def get_team_form(self, team_name: str, league_key: str, limit: int = 5) -> list[dict]:
        code = LEAGUE_CODES.get(league_key)
        if not code:
            return []

        cache_key = code
        if cache_key not in self._matches_cache:
            date_to = datetime.now().strftime("%Y-%m-%d")
            date_from = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")
            data = self._get(
                f"/competitions/{code}/matches",
                params={"status": "FINISHED", "dateFrom": date_from, "dateTo": date_to},
            )
            if not data:
                return []
            self._matches_cache[cache_key] = data.get("matches", [])

        matches = self._matches_cache[cache_key]
        team_lower = team_name.lower()

        team_matches = []
        for m in matches:
            home = m.get("homeTeam", {})
            away = m.get("awayTeam", {})
            home_name = home.get("name", "")
            away_name = away.get("name", "")
            score = m.get("score", {}).get("fullTime", {})
            h_goals = score.get("home")
            a_goals = score.get("away")

            if h_goals is None or a_goals is None:
                continue

            is_home = team_lower in home_name.lower() or home_name.lower() in team_lower
            is_away = team_lower in away_name.lower() or away_name.lower() in team_lower

            if not is_home and not is_away:
                continue

            if is_home:
                if h_goals > a_goals:
                    outcome = "W"
                elif h_goals == a_goals:
                    outcome = "D"
                else:
                    outcome = "L"
                text = f"{home_name} {h_goals}-{a_goals} {away_name}"
            else:
                if a_goals > h_goals:
                    outcome = "W"
                elif a_goals == h_goals:
                    outcome = "D"
                else:
                    outcome = "L"
                text = f"{home_name} {h_goals}-{a_goals} {away_name}"

            team_matches.append({
                "text": text,
                "outcome": outcome,
                "date": m.get("utcDate", ""),
            })

        team_matches.sort(key=lambda x: x["date"], reverse=True)
        return team_matches[:limit]

    def find_team_in_standings(self, team_name: str, standings: list[dict]) -> dict | None:
        team_lower = team_name.lower()
        for s in standings:
            if (team_lower in s["team_name"].lower() or
                s["team_name"].lower() in team_lower or
                team_lower in s.get("team_short", "").lower()):
                return s
        return None
