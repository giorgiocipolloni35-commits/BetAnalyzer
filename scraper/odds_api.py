"""
Client per The Odds API — https://the-odds-api.com
Recupera le quote per Serie A, Premier League e La Liga
da tutti i bookmaker disponibili.
"""
import os
import json
import time
import logging
import requests
from datetime import datetime, timezone
from pathlib import Path
from models.match import Match, BookmakerOdds

logger = logging.getLogger(__name__)

# Mapping sport_key -> nome campionato
# Mappa chiavi frontend -> (odds_api_sport_key, display_name)
LEAGUES = {
    "italy_serie_a":           ("soccer_italy_serie_a", "Serie A"),
    "england_premier_league":  ("soccer_epl", "Premier League"),
    "spain_la_liga":           ("soccer_spain_la_liga", "La Liga"),
    "germany_bundesliga":      ("soccer_germany_bundesliga", "Bundesliga"),
    "france_ligue_1":          ("soccer_france_ligue_one", "Ligue 1"),
    "netherlands_eredivisie":  ("soccer_netherlands_eredivisie", "Eredivisie"),
    "champions_league":        ("soccer_uefa_champs_league", "Champions League"),
    "england_championship":    ("soccer_efl_champ", "Championship"),
    "portugal_primeira_liga":  ("soccer_portugal_primeira_liga", "Primeira Liga"),
    "denmark_superliga":       ("soccer_denmark_superliga", "Superliga"),
    "scotland_premiership":    ("soccer_spl", "Premiership"),
}

# Mapping nome bookmaker API -> nome visualizzato
BOOKMAKER_LABELS = {
    "pinnacle":        "Pinnacle",
    "williamhill":     "William Hill",
    "sport888":        "888sport",
    "betfair_ex_eu":   "Betfair Exchange",
    "unibet_fr":       "Unibet",
    "unibet_nl":       "Unibet NL",
    "unibet_se":       "Unibet SE",
    "betclic_fr":      "Betclic",
    "winamax_fr":      "Winamax",
    "winamax_de":      "Winamax DE",
    "nordicbet":       "NordicBet",
    "marathonbet":     "Marathonbet",
    "matchbook":       "Matchbook",
    "betsson":         "Betsson",
    "coolbet":         "Coolbet",
    "leovegas_se":     "LeoVegas",
    "codere_it":       "Codere",
    "tipico_de":       "Tipico",
    "pmu_fr":          "PMU",
    "onexbet":         "1xBet",
}

CACHE_DIR = Path(__file__).parent.parent / "cache"
CACHE_DIR.mkdir(exist_ok=True)


class OddsAPIClient:
    BASE_URL = "https://api.the-odds-api.com/v4"

    def __init__(self, api_key: str, cache_minutes: int = 30):
        self.api_key = api_key
        self.cache_minutes = cache_minutes
        self._requests_remaining = "?"
        self._requests_used = "?"

    # ------------------------------------------------------------------ #
    #  Public API                                                          #
    # ------------------------------------------------------------------ #

    def get_all_matches(self, league_keys: list[str] = None) -> list[Match]:
        """Recupera le partite solo per le leghe specificate. Se vuoto, non recupera nulla."""
        all_matches: list[Match] = []
        
        if not league_keys:
            logger.warning("Nessuna lega selezionata per Odds API")
            return []

        for key in league_keys:
            if key not in LEAGUES: continue
            sport_key, league_name = LEAGUES[key]
            try:
                matches = self._get_league_odds(sport_key, league_name)
                all_matches.extend(matches)
                logger.info(f"[{league_name}] {len(matches)} partite recuperate")
            except Exception as e:
                logger.error(f"Errore recupero {league_name}: {e}")
        return all_matches

    def get_quota_usage(self) -> dict:
        return {
            "remaining": self._requests_remaining,
            "used": self._requests_used,
        }

    # ------------------------------------------------------------------ #
    #  Private helpers                                                     #
    # ------------------------------------------------------------------ #

    def _get_league_odds(self, sport_key: str, league_name: str) -> list[Match]:
        cache_file = CACHE_DIR / f"{sport_key}.json"
        cached = self._load_cache(cache_file)
        if cached is not None:
            logger.debug(f"[{league_name}] Cache valida")
            return self._parse_response(cached, league_name)

        data = self._fetch(sport_key)
        if data is None:
            # prova a caricare cache scaduta come fallback
            cached_stale = self._load_cache(cache_file, ignore_ttl=True)
            if cached_stale:
                logger.warning(f"[{league_name}] Uso cache scaduta come fallback")
                return self._parse_response(cached_stale, league_name)
            return []

        self._save_cache(cache_file, data)
        return self._parse_response(data, league_name)

    def _fetch(self, sport_key: str) -> list | None:
        url = f"{self.BASE_URL}/sports/{sport_key}/odds"
        params = {
            "apiKey":     self.api_key,
            "regions":    "eu",
            "markets":    "h2h,totals",
            "additionalMarkets": "btts",
            "oddsFormat": "decimal",
            "dateFormat": "iso",
        }
        try:
            resp = requests.get(url, params=params, timeout=15)
            self._requests_remaining = resp.headers.get("x-requests-remaining", "?")
            self._requests_used      = resp.headers.get("x-requests-used", "?")

            if resp.status_code == 401:
                raise ValueError("API Key non valida o scaduta")
            if resp.status_code == 429:
                raise ValueError("Rate limit superato — riprova tra qualche minuto")
            if resp.status_code == 422:
                logger.warning(f"Nessun evento disponibile per {sport_key} (422)")
                return None
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.ConnectionError:
            logger.error("Nessuna connessione a internet")
            return None
        except requests.exceptions.Timeout:
            logger.error(f"Timeout richiesta {sport_key}")
            return None

    # ------------------------------------------------------------------ #
    #  Parsing                                                             #
    # ------------------------------------------------------------------ #

    def _parse_response(self, data: list, league_name: str) -> list[Match]:
        matches = []
        for event in data:
            try:
                match = self._parse_event(event, league_name)
                matches.append(match)
            except Exception as e:
                logger.warning(f"Errore parsing evento {event.get('id', '?')}: {e}")
        # ordina per data
        matches.sort(key=lambda m: m.commence_time)
        return matches

    def _parse_event(self, event: dict, league_name: str) -> Match:
        match = Match(
            id=event["id"],
            home_team=event["home_team"],
            away_team=event["away_team"],
            league=league_name,
            commence_time=event["commence_time"],
        )

        for bk_data in event.get("bookmakers", []):
            bk_key   = bk_data["key"]
            bk_label = BOOKMAKER_LABELS.get(bk_key, bk_data.get("title", bk_key))
            odds_obj = BookmakerOdds(bookmaker=bk_label)

            for market in bk_data.get("markets", []):
                mkey = market["key"]
                outcomes = {o["name"]: o["price"] for o in market.get("outcomes", [])}

                if mkey == "h2h":
                    odds_obj.home = outcomes.get(event["home_team"])
                    odds_obj.draw = outcomes.get("Draw")
                    odds_obj.away = outcomes.get(event["away_team"])

                elif mkey == "totals":
                    for outcome in market.get("outcomes", []):
                        point = outcome.get("point")
                        name = outcome["name"]
                        price = outcome["price"]
                        if point == 1.5:
                            if name == "Over":  odds_obj.over15 = price
                            elif name == "Under": odds_obj.under15 = price
                        elif point == 2.5:
                            if name == "Over":  odds_obj.over25 = price
                            elif name == "Under": odds_obj.under25 = price
                        elif point == 3.5:
                            if name == "Over":  odds_obj.over35 = price
                            elif name == "Under": odds_obj.under35 = price

                elif mkey == "btts":
                    odds_obj.gg = outcomes.get("Yes")
                    odds_obj.ng = outcomes.get("No")

            # Stima mercati mancanti da Over/Under 2.5
            if odds_obj.over25 and odds_obj.under25:
                p_over25 = 1 / odds_obj.over25
                p_under25 = 1 / odds_obj.under25
                margin = p_over25 + p_under25
                fair_over25 = p_over25 / margin
                fair_under25 = p_under25 / margin

                if not odds_obj.over15:
                    # O1.5 ~ O2.5 prob + ~20% (quasi tutte le partite hanno 2+ gol)
                    fair_o15 = min(0.95, fair_over25 + 0.20)
                    odds_obj.over15 = round(1 / fair_o15, 2)
                    odds_obj.under15 = round(1 / (1 - fair_o15), 2)

                if not odds_obj.over35:
                    # O3.5 ~ O2.5 prob - ~18%
                    fair_o35 = max(0.08, fair_over25 - 0.18)
                    odds_obj.over35 = round(1 / fair_o35, 2)
                    odds_obj.under35 = round(1 / (1 - fair_o35), 2)

                if not odds_obj.gg:
                    # GG correlato a O2.5: GG ~ O2.5 prob + 5%
                    fair_gg = min(0.90, fair_over25 + 0.05)
                    odds_obj.gg = round(1 / fair_gg, 2)
                    odds_obj.ng = round(1 / (1 - fair_gg), 2)

            match.odds.append(odds_obj)

        return match

    # ------------------------------------------------------------------ #
    #  Cache                                                               #
    # ------------------------------------------------------------------ #

    def _load_cache(self, path: Path, ignore_ttl: bool = False) -> list | None:
        if not path.exists():
            return None
        try:
            with open(path) as f:
                payload = json.load(f)
            age_minutes = (time.time() - payload["ts"]) / 60
            if ignore_ttl or age_minutes < self.cache_minutes:
                return payload["data"]
        except Exception:
            pass
        return None

    def _save_cache(self, path: Path, data: list):
        try:
            with open(path, "w") as f:
                json.dump({"ts": time.time(), "data": data}, f)
        except Exception as e:
            logger.warning(f"Impossibile salvare cache {path}: {e}")
