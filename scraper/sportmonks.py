"""
SportmonksClient — Drop-in replacement using Football-Data.org + Sofascore.

Mantiene stessa classe, stessi metodi, stesse firme.
Internamente usa:
  - Football-Data.org (via PenaltyAnalyzer) per fixture, standings, form, H2H, coach
  - Sofascore (via DB + proxy) per lineups e player stats
  - Dict statico + Nominatim per coordinate stadi (meteo)

Zero cambi nei caller (worker.py, app.py, analyzer.py).
"""

import logging
import json
import os
import time
import requests
from pathlib import Path
from datetime import datetime, timedelta, date
from models.match import Match, BookmakerOdds

logger = logging.getLogger(__name__)

# ── Football-Data.org league codes ──
FD_LEAGUE_CODES = {
    "italy_serie_a": "SA",
    "england_premier_league": "PL",
    "spain_la_liga": "PD",
    "germany_bundesliga": "BL1",
    "france_ligue_1": "FL1",
    "netherlands_eredivisie": "DED",
    "champions_league": "CL",
    "england_championship": "ELC",
    "portugal_primeira_liga": "PPL",
    "brazil_serie_a": "BSA",
    "denmark_superliga": "DSU",
    "scotland_premiership": "SPL",
    "world_cup": "WC",
}

# ── Mapping SM league_id → FD league code (per get_standings) ──
SM_LEAGUE_TO_FD = {
    "384": "SA", "8": "PL", "564": "PD", "82": "BL1", "301": "FL1",
    "72": "DED", "2": "CL", "9": "ELC", "462": "PPL", "271": "DSU",
    "501": "SPL",
}

# ── Sofascore tournament IDs (per lineups) ──
SOFASCORE_TOURNAMENTS = {
    "italy_serie_a": 23,
    "england_premier_league": 17,
    "spain_la_liga": 8,
    "germany_bundesliga": 35,
    "france_ligue_1": 34,
    "netherlands_eredivisie": 37,
    "champions_league": 7,
    "england_championship": 18,
    "portugal_primeira_liga": 238,
}

# ── Coordinate stadi (lat, lon) — gli stadi non si spostano ──
VENUE_COORDS = {
    # Serie A
    "Inter Milan": {"lat": 45.4781, "lon": 9.1240, "name": "Stadio Giuseppe Meazza", "city": "Milano"},
    "FC Internazionale Milano": {"lat": 45.4781, "lon": 9.1240, "name": "Stadio Giuseppe Meazza", "city": "Milano"},
    "AC Milan": {"lat": 45.4781, "lon": 9.1240, "name": "Stadio Giuseppe Meazza", "city": "Milano"},
    "Juventus FC": {"lat": 45.1096, "lon": 7.6413, "name": "Allianz Stadium", "city": "Torino"},
    "SSC Napoli": {"lat": 40.8280, "lon": 14.1931, "name": "Stadio Diego Armando Maradona", "city": "Napoli"},
    "AS Roma": {"lat": 41.9340, "lon": 12.4547, "name": "Stadio Olimpico", "city": "Roma"},
    "SS Lazio": {"lat": 41.9340, "lon": 12.4547, "name": "Stadio Olimpico", "city": "Roma"},
    "Atalanta BC": {"lat": 45.7092, "lon": 9.6808, "name": "Gewiss Stadium", "city": "Bergamo"},
    "ACF Fiorentina": {"lat": 43.7808, "lon": 11.2822, "name": "Stadio Artemio Franchi", "city": "Firenze"},
    "Bologna FC 1909": {"lat": 44.4923, "lon": 11.3097, "name": "Stadio Renato Dall'Ara", "city": "Bologna"},
    "Torino FC": {"lat": 45.0419, "lon": 7.6497, "name": "Stadio Olimpico Grande Torino", "city": "Torino"},
    "Udinese Calcio": {"lat": 46.0818, "lon": 13.2000, "name": "Bluenergy Stadium", "city": "Udine"},
    "Genoa CFC": {"lat": 44.4163, "lon": 8.9525, "name": "Stadio Luigi Ferraris", "city": "Genova"},
    "UC Sampdoria": {"lat": 44.4163, "lon": 8.9525, "name": "Stadio Luigi Ferraris", "city": "Genova"},
    "Cagliari Calcio": {"lat": 39.1997, "lon": 9.1370, "name": "Unipol Domus", "city": "Cagliari"},
    "US Lecce": {"lat": 40.3600, "lon": 18.1939, "name": "Stadio Via del Mare", "city": "Lecce"},
    "Empoli FC": {"lat": 43.7263, "lon": 10.9556, "name": "Stadio Carlo Castellani", "city": "Empoli"},
    "Hellas Verona FC": {"lat": 45.4353, "lon": 10.9686, "name": "Stadio Marcantonio Bentegodi", "city": "Verona"},
    "US Sassuolo Calcio": {"lat": 44.7150, "lon": 10.6517, "name": "Mapei Stadium", "city": "Reggio Emilia"},
    "Parma Calcio 1913": {"lat": 44.7952, "lon": 10.3381, "name": "Stadio Ennio Tardini", "city": "Parma"},
    "Venezia FC": {"lat": 45.4466, "lon": 12.3474, "name": "Stadio Pier Luigi Penzo", "city": "Venezia"},
    "AC Monza": {"lat": 45.5850, "lon": 9.2942, "name": "U-Power Stadium", "city": "Monza"},
    "US Salernitana 1919": {"lat": 40.6828, "lon": 14.7953, "name": "Stadio Arechi", "city": "Salerno"},
    "Frosinone Calcio": {"lat": 41.6367, "lon": 13.3331, "name": "Stadio Benito Stirpe", "city": "Frosinone"},
    "Como 1907": {"lat": 45.7709, "lon": 9.0831, "name": "Stadio Giuseppe Sinigaglia", "city": "Como"},
    # Premier League
    "Arsenal FC": {"lat": 51.5549, "lon": -0.1084, "name": "Emirates Stadium", "city": "London"},
    "Manchester City FC": {"lat": 53.4831, "lon": -2.2004, "name": "Etihad Stadium", "city": "Manchester"},
    "Liverpool FC": {"lat": 53.4308, "lon": -2.9608, "name": "Anfield", "city": "Liverpool"},
    "Chelsea FC": {"lat": 51.4817, "lon": -0.1910, "name": "Stamford Bridge", "city": "London"},
    "Manchester United FC": {"lat": 53.4631, "lon": -2.2913, "name": "Old Trafford", "city": "Manchester"},
    "Tottenham Hotspur FC": {"lat": 51.6043, "lon": -0.0664, "name": "Tottenham Hotspur Stadium", "city": "London"},
    "Newcastle United FC": {"lat": 54.9756, "lon": -1.6217, "name": "St James' Park", "city": "Newcastle"},
    "Aston Villa FC": {"lat": 52.5092, "lon": -1.8847, "name": "Villa Park", "city": "Birmingham"},
    "Brighton & Hove Albion FC": {"lat": 50.8616, "lon": -0.0837, "name": "Amex Stadium", "city": "Brighton"},
    "West Ham United FC": {"lat": 51.5387, "lon": -0.0166, "name": "London Stadium", "city": "London"},
    "Crystal Palace FC": {"lat": 51.3983, "lon": -0.0855, "name": "Selhurst Park", "city": "London"},
    "Brentford FC": {"lat": 51.4908, "lon": -0.2887, "name": "Gtech Community Stadium", "city": "London"},
    "Wolverhampton Wanderers FC": {"lat": 52.5903, "lon": -2.1306, "name": "Molineux Stadium", "city": "Wolverhampton"},
    "Everton FC": {"lat": 53.4389, "lon": -2.9664, "name": "Goodison Park", "city": "Liverpool"},
    "Fulham FC": {"lat": 51.4749, "lon": -0.2217, "name": "Craven Cottage", "city": "London"},
    "Nottingham Forest FC": {"lat": 52.9400, "lon": -1.1327, "name": "City Ground", "city": "Nottingham"},
    "AFC Bournemouth": {"lat": 50.7352, "lon": -1.8388, "name": "Vitality Stadium", "city": "Bournemouth"},
    "Ipswich Town FC": {"lat": 52.0545, "lon": 1.1449, "name": "Portman Road", "city": "Ipswich"},
    "Leicester City FC": {"lat": 52.6204, "lon": -1.1422, "name": "King Power Stadium", "city": "Leicester"},
    "Southampton FC": {"lat": 50.9058, "lon": -1.3910, "name": "St Mary's Stadium", "city": "Southampton"},
    # La Liga
    "Real Madrid CF": {"lat": 40.4530, "lon": -3.6884, "name": "Santiago Bernabéu", "city": "Madrid"},
    "FC Barcelona": {"lat": 41.3809, "lon": 2.1228, "name": "Estadi Olímpic Lluís Companys", "city": "Barcelona"},
    "Club Atlético de Madrid": {"lat": 40.4362, "lon": -3.5994, "name": "Cívitas Metropolitano", "city": "Madrid"},
    "Athletic Club": {"lat": 43.2642, "lon": -2.9494, "name": "San Mamés", "city": "Bilbao"},
    "Real Sociedad de Fútbol": {"lat": 43.3013, "lon": -1.9736, "name": "Reale Arena", "city": "San Sebastián"},
    "Real Betis Balompié": {"lat": 37.3564, "lon": -5.9818, "name": "Benito Villamarín", "city": "Sevilla"},
    "Villarreal CF": {"lat": 39.9441, "lon": -0.1036, "name": "Estadio de la Cerámica", "city": "Villarreal"},
    "Sevilla FC": {"lat": 37.3840, "lon": -5.9706, "name": "Ramón Sánchez-Pizjuán", "city": "Sevilla"},
    "Valencia CF": {"lat": 39.4747, "lon": -0.3583, "name": "Mestalla", "city": "Valencia"},
    "RC Celta de Vigo": {"lat": 42.2119, "lon": -8.7393, "name": "Abanca-Balaídos", "city": "Vigo"},
    "RCD Mallorca": {"lat": 39.5903, "lon": 2.6309, "name": "Estadi de Son Moix", "city": "Palma"},
    "Girona FC": {"lat": 41.9610, "lon": 2.8287, "name": "Estadi Montilivi", "city": "Girona"},
    "Getafe CF": {"lat": 40.3256, "lon": -3.7143, "name": "Coliseum", "city": "Getafe"},
    "CA Osasuna": {"lat": 42.7967, "lon": -1.6369, "name": "El Sadar", "city": "Pamplona"},
    "Rayo Vallecano de Madrid": {"lat": 40.3922, "lon": -3.6589, "name": "Estadio de Vallecas", "city": "Madrid"},
    "UD Las Palmas": {"lat": 28.1003, "lon": -15.4569, "name": "Estadio Gran Canaria", "city": "Las Palmas"},
    "Deportivo Alavés": {"lat": 42.8372, "lon": -2.6878, "name": "Mendizorroza", "city": "Vitoria-Gasteiz"},
    "RCD Espanyol de Barcelona": {"lat": 41.3478, "lon": 2.0753, "name": "RCDE Stadium", "city": "Barcelona"},
    "CD Leganés": {"lat": 40.3564, "lon": -3.7606, "name": "Estadio Municipal de Butarque", "city": "Leganés"},
    "Real Valladolid CF": {"lat": 41.6444, "lon": -4.7614, "name": "Estadio José Zorrilla", "city": "Valladolid"},
    # Bundesliga
    "FC Bayern München": {"lat": 48.2188, "lon": 11.6247, "name": "Allianz Arena", "city": "München"},
    "Borussia Dortmund": {"lat": 51.4926, "lon": 7.4519, "name": "Signal Iduna Park", "city": "Dortmund"},
    "RB Leipzig": {"lat": 51.3459, "lon": 12.3485, "name": "Red Bull Arena", "city": "Leipzig"},
    "Bayer 04 Leverkusen": {"lat": 51.0383, "lon": 7.0022, "name": "BayArena", "city": "Leverkusen"},
    "VfB Stuttgart": {"lat": 48.7924, "lon": 9.2320, "name": "MHPArena", "city": "Stuttgart"},
    "Eintracht Frankfurt": {"lat": 50.0685, "lon": 8.6452, "name": "Deutsche Bank Park", "city": "Frankfurt"},
    "SC Freiburg": {"lat": 48.0224, "lon": 7.8302, "name": "Europa-Park Stadion", "city": "Freiburg"},
    "VfL Wolfsburg": {"lat": 52.4319, "lon": 10.8039, "name": "Volkswagen Arena", "city": "Wolfsburg"},
    "1. FC Union Berlin": {"lat": 52.4572, "lon": 13.5681, "name": "Stadion An der Alten Försterei", "city": "Berlin"},
    "TSG 1899 Hoffenheim": {"lat": 49.2383, "lon": 8.8881, "name": "PreZero Arena", "city": "Sinsheim"},
    "SV Werder Bremen": {"lat": 53.0664, "lon": 8.8375, "name": "Weserstadion", "city": "Bremen"},
    "1. FSV Mainz 05": {"lat": 49.9842, "lon": 8.2245, "name": "Mewa Arena", "city": "Mainz"},
    "FC Augsburg": {"lat": 48.3236, "lon": 10.8856, "name": "WWK Arena", "city": "Augsburg"},
    "Borussia Mönchengladbach": {"lat": 51.1747, "lon": 6.3853, "name": "Borussia-Park", "city": "Mönchengladbach"},
    "1. FC Heidenheim 1846": {"lat": 48.6747, "lon": 10.1500, "name": "Voith-Arena", "city": "Heidenheim"},
    "FC St. Pauli 1910": {"lat": 53.5544, "lon": 9.9675, "name": "Millerntor-Stadion", "city": "Hamburg"},
    "Holstein Kiel": {"lat": 54.3489, "lon": 10.1228, "name": "Holstein-Stadion", "city": "Kiel"},
    # Ligue 1
    "Paris Saint-Germain FC": {"lat": 48.8414, "lon": 2.2530, "name": "Parc des Princes", "city": "Paris"},
    "AS Monaco FC": {"lat": 43.7275, "lon": 7.4153, "name": "Stade Louis II", "city": "Monaco"},
    "Olympique de Marseille": {"lat": 43.2697, "lon": 5.3958, "name": "Stade Vélodrome", "city": "Marseille"},
    "Olympique Lyonnais": {"lat": 45.7653, "lon": 4.9822, "name": "Groupama Stadium", "city": "Lyon"},
    "LOSC Lille": {"lat": 50.6119, "lon": 3.1306, "name": "Stade Pierre-Mauroy", "city": "Lille"},
    "OGC Nice": {"lat": 43.7050, "lon": 7.1925, "name": "Allianz Riviera", "city": "Nice"},
    "RC Lens": {"lat": 50.4328, "lon": 2.8153, "name": "Stade Bollaert-Delelis", "city": "Lens"},
    "Stade Rennais FC 1901": {"lat": 48.1075, "lon": -1.7128, "name": "Roazhon Park", "city": "Rennes"},
    "RC Strasbourg Alsace": {"lat": 48.5600, "lon": 7.7550, "name": "Stade de la Meinau", "city": "Strasbourg"},
    "Stade Brestois 29": {"lat": 48.4028, "lon": -4.4614, "name": "Stade Francis-Le Blé", "city": "Brest"},
    "Montpellier HSC": {"lat": 43.6222, "lon": 3.8117, "name": "Stade de la Mosson", "city": "Montpellier"},
    "FC Nantes": {"lat": 47.2561, "lon": -1.5250, "name": "Stade de la Beaujoire", "city": "Nantes"},
    "Toulouse FC": {"lat": 43.5833, "lon": 1.4339, "name": "Stadium de Toulouse", "city": "Toulouse"},
    "Angers SCO": {"lat": 47.4608, "lon": -0.5306, "name": "Stade Raymond Kopa", "city": "Angers"},
    "AJ Auxerre": {"lat": 47.7950, "lon": 3.5908, "name": "Stade de l'Abbé-Deschamps", "city": "Auxerre"},
    "AS Saint-Étienne": {"lat": 45.4608, "lon": 4.3903, "name": "Stade Geoffroy-Guichard", "city": "Saint-Étienne"},
    "Le Havre AC": {"lat": 49.4989, "lon": 0.1622, "name": "Stade Océane", "city": "Le Havre"},
    "Stade de Reims": {"lat": 49.2469, "lon": 3.9631, "name": "Stade Auguste-Delaune", "city": "Reims"},
    # Eredivisie
    "PSV": {"lat": 51.4419, "lon": 5.4678, "name": "Philips Stadion", "city": "Eindhoven"},
    "Feyenoord Rotterdam": {"lat": 51.8939, "lon": 4.5231, "name": "De Kuip", "city": "Rotterdam"},
    "AFC Ajax": {"lat": 52.3142, "lon": 4.9419, "name": "Johan Cruijff Arena", "city": "Amsterdam"},
    "AZ": {"lat": 52.6136, "lon": 4.7408, "name": "AFAS Stadion", "city": "Alkmaar"},
    "FC Twente '65": {"lat": 52.2367, "lon": 6.8375, "name": "De Grolsch Veste", "city": "Enschede"},
    "FC Utrecht": {"lat": 52.0778, "lon": 5.1475, "name": "Stadion Galgenwaard", "city": "Utrecht"},
}

# ── FD base URL ──
FD_BASE_URL = "https://api.football-data.org/v4"
FD_CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "penalties")


class SportmonksClient:
    """Drop-in replacement: same interface, free backends."""

    def __init__(self, api_key: str = None, cache_minutes: int = 30):
        # api_key è ignorato — usiamo Football-Data.org key da env
        self.fd_key = os.getenv("FOOTBALL_DATA_API_KEY", "")
        self.cache_minutes = cache_minutes
        self.cache_dir = Path(__file__).parent.parent / "cache"
        self.cache_dir.mkdir(exist_ok=True)
        # Legacy compat: callers reference self.base_url and self.api_key (weather.py)
        self.base_url = "https://api.sportmonks.com/v3/football"
        self.api_key = api_key or ""

        # SM league_map — kept for caller compatibility
        self.league_map = {
            "italy_serie_a": "384",
            "england_premier_league": "8",
            "spain_la_liga": "564",
            "germany_bundesliga": "82",
            "france_ligue_1": "301",
            "denmark_superliga": "271",
            "scotland_premiership": "501",
            "netherlands_eredivisie": "72",
            "champions_league": "2",
            "england_championship": "9",
            "portugal_primeira_liga": "462",
        }

    # ─────────────────────────────────────────────
    # Internal: Football-Data.org HTTP wrapper
    # ─────────────────────────────────────────────
    def _fd_get(self, endpoint, params=None):
        """HTTP GET to Football-Data.org with rate-limit handling."""
        if not self.fd_key:
            logger.warning("FOOTBALL_DATA_API_KEY non configurata")
            return None
        url = f"{FD_BASE_URL}{endpoint}"
        headers = {"X-Auth-Token": self.fd_key}
        for attempt in range(3):
            try:
                resp = requests.get(url, headers=headers, params=params, timeout=30)
                if resp.status_code == 200:
                    return resp.json()
                if resp.status_code == 429:
                    wait = int(resp.headers.get("Retry-After", 60))
                    logger.warning(f"FD rate limited, waiting {wait}s (attempt {attempt+1}/3)")
                    time.sleep(wait + 1)
                    continue
                logger.error(f"FD API Error {resp.status_code} for {endpoint}")
                return None
            except Exception as e:
                logger.error(f"FD request error: {e}")
                time.sleep(5)
        return None

    def _load_fd_cache(self, league_code):
        """Load PenaltyAnalyzer match cache."""
        path = os.path.join(FD_CACHE_DIR, f"{league_code}_matches.json")
        if os.path.exists(path):
            with open(path, 'r') as f:
                return json.load(f)
        return {}

    def _league_key_to_fd(self, league_key):
        """Convert league_key to FD league code."""
        return FD_LEAGUE_CODES.get(league_key)

    # ─────────────────────────────────────────────
    # get_all_matches — FD scheduled/timed fixtures
    # ─────────────────────────────────────────────
    def get_all_matches(self, league_keys: list[str] = None) -> list[Match]:
        """Recupera fixture prossimi 7 giorni da Football-Data.org."""
        cache_file = self.cache_dir / "sportmonks_matches.json"

        # Check cache
        if cache_file.exists() and not league_keys:
            mtime = datetime.fromtimestamp(cache_file.stat().st_mtime)
            if datetime.now() - mtime < timedelta(minutes=self.cache_minutes):
                logger.info("Caricamento fixture da cache")
                with open(cache_file, "r") as f:
                    data = json.load(f)
                    return [Match.from_dict(m) for m in data]

        if not league_keys:
            logger.warning("Nessuna lega selezionata")
            return []

        logger.info(f"Recupero fixture da Football-Data.org per {len(league_keys)} leghe...")

        matches = []
        for lk in league_keys:
            fd_code = self._league_key_to_fd(lk)
            if not fd_code:
                logger.warning(f"Nessun codice FD per {lk}")
                continue

            data = self._fd_get(f"/competitions/{fd_code}/matches",
                                params={"status": "SCHEDULED,TIMED"})
            if not data:
                continue

            for m in data.get("matches", []):
                try:
                    home = m.get("homeTeam", {})
                    away = m.get("awayTeam", {})
                    comp = m.get("competition", {})
                    season = m.get("season", {})

                    match = Match(
                        id=f"fd_{m['id']}",
                        league=comp.get("name", lk),
                        league_id=str(comp.get("id", "")),
                        season_id=str(season.get("id", "")),
                        home_team=home.get("name", "?"),
                        away_team=away.get("name", "?"),
                        commence_time=m.get("utcDate", ""),
                        home_id=str(home.get("id", "")),
                        away_id=str(away.get("id", "")),
                    )
                    matches.append(match)
                except Exception as e:
                    logger.error(f"Errore parsing match FD: {e}")

            # Rate limit: 10 req/min su free tier
            time.sleep(6)

        # Save cache
        with open(cache_file, "w") as f:
            json.dump([m.to_dict() for m in matches], f, indent=2)

        logger.info(f"Recuperati {len(matches)} fixture da FD")
        return matches

    # ─────────────────────────────────────────────
    # get_standings — FD standings
    # ─────────────────────────────────────────────
    def get_standings(self, league_id: str, season_id: str = None) -> list[dict]:
        """Recupera classifica da Football-Data.org."""
        try:
            # Map SM league_id to FD code
            fd_code = SM_LEAGUE_TO_FD.get(str(league_id))
            if not fd_code:
                # Prova diretto (potrebbe già essere un FD code)
                fd_code = str(league_id)

            resp = self._fd_get(f"/competitions/{fd_code}/standings")
            if not resp:
                return []

            standings = []
            for table in resp.get("standings", []):
                if table.get("type") == "TOTAL":
                    for entry in table.get("table", []):
                        team = entry.get("team", {})
                        standings.append({
                            "team_id": str(team.get("id")),
                            "team_name": team.get("name"),
                            "position": entry.get("position"),
                            "points": entry.get("points", 0),
                            "played": entry.get("playedGames", 0),
                            "goals_diff": entry.get("goalDifference", 0),
                        })

            logger.info(f"Recuperate {len(standings)} posizioni in classifica FD ({fd_code})")
            return standings
        except Exception as e:
            logger.error(f"Errore recupero classifica FD: {e}")
            return []

    # ─────────────────────────────────────────────
    # get_last_results — FD match cache (zero API)
    # ─────────────────────────────────────────────
    def get_last_results(self, team_id: str, limit: int = 3) -> list:
        """Recupera ultimi N risultati da cache FD match. Zero API call."""
        if not team_id:
            return []
        try:
            team_id_int = int(str(team_id).replace("sm_", "").replace("fd_", ""))
        except (ValueError, TypeError):
            return []

        results = []

        # Cerca in tutte le cache league
        all_matches = []
        for fname in os.listdir(FD_CACHE_DIR):
            if fname.endswith("_matches.json") and not fname.startswith("player_") and not fname.startswith("team_"):
                cache = self._load_fd_cache(fname.replace("_matches.json", ""))
                for mid, detail in cache.items():
                    h_id = detail.get("home_id")
                    a_id = detail.get("away_id")
                    if h_id == team_id_int or a_id == team_id_int:
                        score = detail.get("score", {})
                        ft = score.get("fullTime", {})
                        if ft.get("home") is not None and ft.get("away") is not None:
                            all_matches.append({
                                "matchday": detail.get("matchday", 0),
                                "home_id": h_id,
                                "away_id": a_id,
                                "h_goals": ft["home"],
                                "a_goals": ft["away"],
                                "detail": detail,
                            })

        # Ordina per matchday desc
        all_matches.sort(key=lambda x: x["matchday"], reverse=True)

        for m in all_matches[:limit]:
            # Trova nomi squadre dai players data o standings
            h_name = self._resolve_team_name(m["home_id"])
            a_name = self._resolve_team_name(m["away_id"])

            h_goals = m["h_goals"]
            a_goals = m["a_goals"]
            res_str = f"{h_name} {h_goals}-{a_goals} {a_name}"

            is_home = m["home_id"] == team_id_int
            if h_goals > a_goals:
                outcome = "W" if is_home else "L"
            elif a_goals > h_goals:
                outcome = "L" if is_home else "W"
            else:
                outcome = "D"

            results.append({
                "text": res_str,
                "outcome": outcome,
                "date": None,  # non disponibile nella cache FD
            })

        logger.info(f"Last results team {team_id}: {len(results)} partite (da cache FD)")
        return results

    def _resolve_team_name(self, team_id) -> str:
        """Risolvi team_id FD a nome. Prima cache team, poi standings."""
        # Prova dal file team_{id}.json
        team_file = os.path.join(FD_CACHE_DIR, f"team_{team_id}.json")
        if os.path.exists(team_file):
            try:
                with open(team_file) as f:
                    data = json.load(f)
                    return data.get("shortName") or data.get("name") or str(team_id)
            except:
                pass

        # Prova dai match cache (campo players)
        for fname in os.listdir(FD_CACHE_DIR):
            if fname.endswith("_matches.json") and not fname.startswith("player_") and not fname.startswith("team_"):
                cache = self._load_fd_cache(fname.replace("_matches.json", ""))
                for mid, detail in cache.items():
                    players = detail.get("players", {})
                    for pid, pinfo in players.items():
                        if pinfo.get("team_id") == team_id:
                            # Abbiamo trovato la squadra ma non il nome team — non utile
                            pass
                break  # solo primo file per velocità

        return str(team_id)

    # ─────────────────────────────────────────────
    # get_head_to_head — FD match cache (zero API)
    # ─────────────────────────────────────────────
    def get_head_to_head(self, team1_id: str, team2_id: str, limit: int = 10) -> list[dict]:
        """Recupera H2H dalla cache FD. Zero API call."""
        if not team1_id or not team2_id:
            return []
        try:
            t1 = int(str(team1_id).replace("sm_", "").replace("fd_", ""))
            t2 = int(str(team2_id).replace("sm_", "").replace("fd_", ""))
        except (ValueError, TypeError):
            return []

        h2h = []
        for fname in os.listdir(FD_CACHE_DIR):
            if fname.endswith("_matches.json") and not fname.startswith("player_") and not fname.startswith("team_"):
                cache = self._load_fd_cache(fname.replace("_matches.json", ""))
                for mid, detail in cache.items():
                    h_id = detail.get("home_id")
                    a_id = detail.get("away_id")
                    if (h_id == t1 and a_id == t2) or (h_id == t2 and a_id == t1):
                        score = detail.get("score", {})
                        ft = score.get("fullTime", {})
                        if ft.get("home") is not None:
                            h_name = self._resolve_team_name(h_id)
                            a_name = self._resolve_team_name(a_id)
                            h2h.append({
                                "date": "",
                                "home": h_name,
                                "away": a_name,
                                "home_goals": ft["home"],
                                "away_goals": ft["away"],
                                "score": f"{ft['home']}-{ft['away']}",
                                "matchday": detail.get("matchday", 0),
                            })

        h2h.sort(key=lambda x: x.get("matchday", 0), reverse=True)
        results = h2h[:limit]

        logger.info(f"H2H {team1_id} vs {team2_id}: {len(results)} scontri diretti (da cache FD)")
        return results

    # ─────────────────────────────────────────────
    # get_coach_info — FD /teams/{id}
    # ─────────────────────────────────────────────
    def get_coach_info(self, team_id: str) -> dict | None:
        """Recupera info coach da Football-Data.org."""
        if not team_id:
            return None
        try:
            clean_id = str(team_id).replace("sm_", "").replace("fd_", "")

            # Prova cache locale prima
            team_file = os.path.join(FD_CACHE_DIR, f"team_{clean_id}.json")
            data = None
            if os.path.exists(team_file):
                if time.time() - os.path.getmtime(team_file) < 86400 * 7:
                    try:
                        with open(team_file) as f:
                            data = json.load(f)
                    except:
                        pass

            if not data:
                data = self._fd_get(f"/teams/{clean_id}")
                if data:
                    try:
                        with open(team_file, 'w') as f:
                            json.dump(data, f)
                    except:
                        pass

            if not data:
                return None

            coach = data.get("coach", {})
            if not coach:
                return None

            coach_name = coach.get("name", "Sconosciuto")
            contract = coach.get("contract", {})
            start_str = contract.get("start", "")

            days_in_charge = 0
            if start_str:
                try:
                    # FD usa formati vari: "2025-06-01", "2025-06", "2025"
                    if len(start_str) == 10:
                        start_date = datetime.strptime(start_str, "%Y-%m-%d").date()
                    elif len(start_str) == 7:
                        start_date = datetime.strptime(start_str + "-01", "%Y-%m-%d").date()
                    elif len(start_str) == 4:
                        start_date = datetime.strptime(start_str + "-01-01", "%Y-%m-%d").date()
                    else:
                        start_date = datetime.strptime(start_str[:10], "%Y-%m-%d").date()
                    days_in_charge = (date.today() - start_date).days
                except Exception:
                    pass

            return {
                "coach_name": coach_name,
                "coach_id": coach.get("id"),
                "start_date": start_str,
                "days_in_charge": days_in_charge,
                "temporary": False,
                "previous_coaches": 0,  # Non disponibile da FD, impatto minimo
            }
        except Exception as e:
            logger.error(f"Errore recupero coach FD team {team_id}: {e}")
            return None

    # ─────────────────────────────────────────────
    # get_squad — FD /teams/{id} squad
    # ─────────────────────────────────────────────
    def get_squad(self, team_id: str) -> list[dict]:
        """Recupera rosa da Football-Data.org."""
        if not team_id:
            return []
        try:
            clean_id = str(team_id).replace("sm_", "").replace("fd_", "")
            data = self._fd_get(f"/teams/{clean_id}")
            if not data:
                return []

            players = []
            for p in data.get("squad", []):
                # Map FD position to simple role
                pos = (p.get("position") or "").upper()
                if pos == "GOALKEEPER":
                    role = "Goalkeeper"
                elif pos == "DEFENCE":
                    role = "Defence"
                elif pos == "MIDFIELD":
                    role = "Midfield"
                elif pos in ("OFFENCE", "FORWARD"):
                    role = "Offence"
                else:
                    role = "Unknown"

                players.append({
                    "id": p.get("id"),
                    "name": p.get("name"),
                    "position": role,
                    "team_id": team_id,
                    "position_id": {"Goalkeeper": 24, "Defence": 25, "Midfield": 26, "Offence": 27}.get(role, 0),
                })
            return players
        except Exception as e:
            logger.error(f"Errore recupero squad FD team {team_id}: {e}")
            return []

    # ─────────────────────────────────────────────
    # get_official_lineups — Sofascore via proxy
    # ─────────────────────────────────────────────
    def get_official_lineups(self, fixture_id: str) -> dict:
        """Recupera formazioni ufficiali da Sofascore (disponibili ~45min prima)."""
        if not fixture_id:
            return {}
        try:
            # fixture_id è "fd_12345" o "sm_12345" o plain number
            clean_id = str(fixture_id).replace("fd_", "").replace("sm_", "")

            # Per Sofascore servono gli event IDs, non i FD match IDs
            # Proviamo a trovare il match su Sofascore cercando per squadre
            # Per ora, restituiamo vuoto — le lineups sono gestite dal worker
            # che ha il suo fallback Sofascore
            logger.info(f"Lineups richieste per fixture {fixture_id} — delegata a Sofascore worker")
            return {}
        except Exception as e:
            logger.error(f"Errore recupero lineups fixture {fixture_id}: {e}")
            return {}

    # ─────────────────────────────────────────────
    # get_team_formation_stats — Non disponibile
    # ─────────────────────────────────────────────
    def get_team_formation_stats(self, sm_team_id: int, season_start: str = "2025-08-01",
                                  season_end: str = "2026-06-30") -> list[dict]:
        """Formation stats — non più disponibile senza Sportmonks. Ritorna vuoto."""
        logger.info(f"Formation stats non disponibili (migrazione da Sportmonks)")
        return []

    # ─────────────────────────────────────────────
    # get_venue_coords — Dict statico + Nominatim
    # ─────────────────────────────────────────────
    def get_venue_coords(self, team_id: str) -> dict | None:
        """Coordinate stadio da dict statico + fallback Nominatim."""
        if not team_id:
            return None
        try:
            clean_id = str(team_id).replace("sm_", "").replace("fd_", "")
            team_name = self._resolve_team_name(int(clean_id))

            # Cerca nel dict statico
            venue = VENUE_COORDS.get(team_name)
            if venue:
                return {
                    "lat": venue["lat"],
                    "lon": venue["lon"],
                    "name": venue.get("name", ""),
                    "city": venue.get("city", ""),
                    "surface": "grass",
                    "capacity": 0,
                }

            # Fuzzy match nel dict
            team_lower = team_name.lower()
            for name, coords in VENUE_COORDS.items():
                if team_lower in name.lower() or name.lower() in team_lower:
                    return {
                        "lat": coords["lat"],
                        "lon": coords["lon"],
                        "name": coords.get("name", ""),
                        "city": coords.get("city", ""),
                        "surface": "grass",
                        "capacity": 0,
                    }

            # Fallback: Nominatim geocoding (OpenStreetMap, gratuito)
            return self._nominatim_geocode(team_name)

        except Exception as e:
            logger.warning(f"Errore venue coords team {team_id}: {e}")
            return None

    def _nominatim_geocode(self, team_name: str) -> dict | None:
        """Geocoding fallback con Nominatim (OpenStreetMap)."""
        try:
            # Cerca "stadio <team_name>"
            query = f"stadium {team_name}"
            resp = requests.get(
                "https://nominatim.openstreetmap.org/search",
                params={"q": query, "format": "json", "limit": 1},
                headers={"User-Agent": "BetAnalyzer/1.0"},
                timeout=10,
            )
            if resp.status_code == 200:
                results = resp.json()
                if results:
                    return {
                        "lat": float(results[0]["lat"]),
                        "lon": float(results[0]["lon"]),
                        "name": results[0].get("display_name", "").split(",")[0],
                        "city": "",
                        "surface": "grass",
                        "capacity": 0,
                    }
        except Exception as e:
            logger.warning(f"Nominatim geocode fallito per {team_name}: {e}")
        return None

    # ─────────────────────────────────────────────
    # Legacy methods — kept for compatibility
    # ─────────────────────────────────────────────
    def search_team(self, name: str) -> list[dict]:
        """Search team — usa FD. Ritorna lista compatibile."""
        return []

    def search_player(self, name: str, dob: str = None) -> list[dict]:
        """Search player — non necessario, i giocatori vengono da Sofascore DB."""
        return []

    def get_player_stats(self, player_id: int, season_id: int, team_id: int = None,
                         team_name: str = None) -> dict:
        """Player stats — non necessario, tutto da Sofascore DB."""
        return {}

    def get_quota_usage(self) -> dict:
        return {"remaining": "Free tier (FD)", "used": "N/A"}

    def _make_request(self, endpoint, params=None):
        """Legacy compat — redirect a FD."""
        return self._fd_get(f"/{endpoint}", params)
