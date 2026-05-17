"""
Client per API-Football — https://www.api-football.com
Fallback gratuito per quote quando Odds API è esaurita.
Piano Free: 100 richieste/giorno, odds disponibili ±1 giorno.
"""
import os
import logging
import requests
from models.match import Match, BookmakerOdds

logger = logging.getLogger(__name__)

# Mapping league IDs API-Football
LEAGUE_MAP = {
    "serie_a":          135,
    "premier_league":   39,
    "la_liga":          140,
    "bundesliga":       78,
    "ligue_1":          61,
}

# Mapping dei nostri nomi interni → API-Football league IDs
DISPLAY_TO_APIFB = {
    "Serie A":          135,
    "Premier League":   39,
    "La Liga":          140,
    "Bundesliga":       78,
    "Ligue 1":          61,
}

# Market IDs che ci interessano
MARKET_MATCH_WINNER = 1       # 1X2
MARKET_OVER_UNDER = 5         # Goals Over/Under (contiene 1.5, 2.5, 3.5)
MARKET_BTTS = 8               # Both Teams Score
MARKET_DOUBLE_CHANCE = 12     # Double Chance


class APIFootballClient:
    BASE_URL = "https://v3.football.api-sports.io"

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.headers = {"x-apisports-key": api_key}
        self._remaining = None

    def get_remaining_requests(self) -> int | None:
        return self._remaining

    def _request(self, endpoint: str, params: dict) -> dict:
        """Esegue richiesta e traccia quota rimanente."""
        try:
            r = requests.get(
                f"{self.BASE_URL}/{endpoint}",
                headers=self.headers,
                params=params,
                timeout=15,
            )
            r.raise_for_status()
            remaining = r.headers.get("x-ratelimit-requests-remaining")
            if remaining is not None:
                self._remaining = int(remaining)
            return r.json()
        except Exception as e:
            logger.error(f"API-Football request error ({endpoint}): {e}")
            return {}

    def get_odds_for_date(self, date_str: str) -> dict:
        """
        Scarica tutte le odds disponibili per una data (formato YYYY-MM-DD).
        Ritorna dict: fixture_id → lista di BookmakerOdds.
        Piano Free: solo ±1 giorno dalla data corrente.
        """
        odds_by_fixture = {}
        page = 1
        total_pages = 1

        while page <= total_pages:
            data = self._request("odds", {
                "date": date_str,
                "page": page,
            })

            errors = data.get("errors", {})
            if errors:
                if "plan" in errors:
                    logger.warning(f"API-Football odds non disponibili per {date_str}: {errors['plan']}")
                else:
                    logger.warning(f"API-Football errors: {errors}")
                return odds_by_fixture

            total_pages = data.get("paging", {}).get("total", 1)

            for entry in data.get("response", []):
                fixture_id = entry.get("fixture", {}).get("id")
                league_id = entry.get("league", {}).get("id")

                if not fixture_id:
                    continue

                # Prendi solo le nostre leghe
                if league_id not in LEAGUE_MAP.values():
                    continue

                bookmaker_odds = self._parse_bookmakers(entry.get("bookmakers", []))
                if bookmaker_odds:
                    odds_by_fixture[fixture_id] = bookmaker_odds

            page += 1

        logger.info(f"API-Football: {len(odds_by_fixture)} fixture con odds per {date_str}")
        return odds_by_fixture

    def get_fixtures_with_odds(self, date_str: str) -> list[dict]:
        """
        Scarica fixtures + odds per le nostre leghe in una data.
        Ritorna lista di dict con info fixture + odds parsate.
        """
        results = []
        page = 1
        total_pages = 1

        while page <= total_pages:
            data = self._request("odds", {
                "date": date_str,
                "page": page,
            })

            errors = data.get("errors", {})
            if errors:
                if "plan" in errors:
                    logger.warning(f"API-Football odds non disponibili per {date_str}: {errors['plan']}")
                else:
                    logger.warning(f"API-Football errors: {errors}")
                return results

            total_pages = data.get("paging", {}).get("total", 1)

            for entry in data.get("response", []):
                fixture_id = entry.get("fixture", {}).get("id")
                league_id = entry.get("league", {}).get("id")

                if not fixture_id or league_id not in LEAGUE_MAP.values():
                    continue

                bookmaker_odds = self._parse_bookmakers(entry.get("bookmakers", []))
                if bookmaker_odds:
                    results.append({
                        "fixture_id": fixture_id,
                        "league_id": league_id,
                        "league_name": entry.get("league", {}).get("name", ""),
                        "odds": bookmaker_odds,
                    })

            page += 1

        logger.info(f"API-Football: {len(results)} fixture con odds per le nostre leghe ({date_str})")
        return results

    def get_fixture_mapping(self, date_str: str) -> dict:
        """
        Scarica fixtures per le nostre leghe e ritorna mapping:
        (home_team_lower, away_team_lower) → fixture_id
        Usata per matchare i nomi Sportmonks → API-Football fixture IDs.
        """
        mapping = {}

        for league_name, league_id in LEAGUE_MAP.items():
            data = self._request("fixtures", {
                "date": date_str,
                "league": league_id,
                "season": 2025,
            })

            errors = data.get("errors", {})
            if errors:
                # Free plan might not have the season, try without season
                data = self._request("fixtures", {
                    "date": date_str,
                    "league": league_id,
                })
                errors = data.get("errors", {})
                if errors:
                    continue

            for fix in data.get("response", []):
                fid = fix.get("fixture", {}).get("id")
                home = fix.get("teams", {}).get("home", {}).get("name", "").lower()
                away = fix.get("teams", {}).get("away", {}).get("name", "").lower()
                if fid and home and away:
                    mapping[(home, away)] = fid

        logger.info(f"API-Football fixture mapping: {len(mapping)} partite per {date_str}")
        return mapping

    def _parse_bookmakers(self, bookmakers: list) -> list[BookmakerOdds]:
        """Parsa i bookmaker di un fixture in lista di BookmakerOdds."""
        result = []

        for bk in bookmakers:
            bk_name = bk.get("name", "Unknown")
            bets = bk.get("bets", [])

            odds_obj = BookmakerOdds(bookmaker=bk_name)
            has_data = False

            for bet in bets:
                bet_id = bet.get("id")
                values = {str(v.get("value", "")).lower(): float(v.get("odd", 0))
                          for v in bet.get("values", [])
                          if v.get("odd")}

                # 1X2 Match Winner
                if bet_id == MARKET_MATCH_WINNER:
                    odds_obj.home = values.get("home")
                    odds_obj.draw = values.get("draw")
                    odds_obj.away = values.get("away")
                    if odds_obj.home:
                        has_data = True

                # Over/Under Goals (prende 1.5, 2.5, 3.5)
                elif bet_id == MARKET_OVER_UNDER:
                    # I values hanno formato: "Over 1.5", "Under 1.5", etc.
                    for v in bet.get("values", []):
                        val_str = str(v.get("value", ""))
                        odd = float(v.get("odd", 0)) if v.get("odd") else None
                        if not odd:
                            continue
                        if val_str == "Over 1.5":
                            odds_obj.over15 = odd
                        elif val_str == "Under 1.5":
                            odds_obj.under15 = odd
                        elif val_str == "Over 2.5":
                            odds_obj.over25 = odd
                            has_data = True
                        elif val_str == "Under 2.5":
                            odds_obj.under25 = odd
                        elif val_str == "Over 3.5":
                            odds_obj.over35 = odd
                        elif val_str == "Under 3.5":
                            odds_obj.under35 = odd

                # BTTS (Both Teams To Score)
                elif bet_id == MARKET_BTTS:
                    odds_obj.gg = values.get("yes")
                    odds_obj.ng = values.get("no")

                # Double Chance — calcolata automaticamente da BookmakerOdds
                # via @property dc_1x/dc_x2/dc_12 partendo dalle quote 1X2,
                # quindi non serve settarla manualmente.

            if has_data:
                result.append(odds_obj)

        return result


def merge_apifootball_odds(matches: list[Match], date_str: str, api_key: str) -> int:
    """
    Funzione principale: scarica odds da API-Football e le assegna ai match
    che non hanno già odds (fallback).
    Ritorna il numero di match arricchiti.
    """
    client = APIFootballClient(api_key)

    # 1. Scarica tutte le odds per la data
    fixtures_with_odds = client.get_fixtures_with_odds(date_str)
    if not fixtures_with_odds:
        logger.info("API-Football: nessuna odds disponibile per il fallback")
        return 0

    # 2. Costruisci lookup per nome squadra
    # API-Football usa nomi diversi da Sportmonks, serve fuzzy matching
    odds_lookup = {}
    for fw in fixtures_with_odds:
        odds_lookup[fw["fixture_id"]] = fw["odds"]

    # 3. Scarica fixture mapping per matchare i nomi
    fixture_data = client._request("fixtures", {"date": date_str})
    name_to_odds = {}
    for fix in fixture_data.get("response", []):
        fid = fix.get("fixture", {}).get("id")
        if fid in odds_lookup:
            home = fix.get("teams", {}).get("home", {}).get("name", "")
            away = fix.get("teams", {}).get("away", {}).get("name", "")
            if home and away:
                name_to_odds[(home.lower(), away.lower())] = odds_lookup[fid]

    # 4. Assegna odds ai match senza quote
    enriched = 0
    for match in matches:
        if match.odds:  # Ha già le odds
            continue

        mh = match.home_team.lower()
        ma = match.away_team.lower()

        # Prova match esatto
        match_odds = name_to_odds.get((mh, ma))

        # Prova fuzzy: containment
        if not match_odds:
            for (h, a), odds_list in name_to_odds.items():
                if (mh in h or h in mh) and (ma in a or a in ma):
                    match_odds = odds_list
                    break

        # Prova fuzzy: parole significative
        if not match_odds:
            mh_words = set(w for w in mh.split() if len(w) > 3)
            ma_words = set(w for w in ma.split() if len(w) > 3)
            SKIP = {"club", "real", "sporting", "athletic", "city", "united"}
            mh_words -= SKIP
            ma_words -= SKIP

            for (h, a), odds_list in name_to_odds.items():
                h_words = set(w for w in h.split() if len(w) > 3) - SKIP
                a_words = set(w for w in a.split() if len(w) > 3) - SKIP
                if mh_words & h_words and ma_words & a_words:
                    match_odds = odds_list
                    break

        if match_odds:
            match.odds = match_odds
            enriched += 1
            logger.info(f"✅ API-Football odds: {match.home_team} vs {match.away_team} ({len(match_odds)} bookmaker)")

    remaining = client.get_remaining_requests()
    logger.info(f"API-Football fallback: {enriched}/{len(matches)} match arricchiti (richieste rimanenti: {remaining})")
    return enriched
