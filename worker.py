import os
import json
import logging
import sqlite3
import time
import requests
from datetime import datetime, timedelta, timezone
import pytz
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

# Configurazione logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

class BetAnalyzerWorker:
    """Worker autonomo per monitoraggio formazioni e invio alert.

    Logica:
    1. Legge le impostazioni dal DB (worker_enabled, active_leagues, alert_email)
    2. Recupera i match delle prossime 24h per i campionati attivi (via Odds API)
    3. Per ogni match nella finestra 70→0 minuti prima del kickoff:
       - Controlla se le formazioni ufficiali sono disponibili su Sportmonks
       - Se le trova e l'alert non è già stato inviato → genera analisi AI + invia email
       - Se non le trova → riprova al prossimo ciclo (ogni 5 min)
    4. Fuori dalla finestra (>45 min o partita iniziata) → skip
    """

    # Finestra di monitoraggio formazioni (minuti prima del kickoff)
    LINEUP_WINDOW_START = 45   # inizia a cercare formazioni 45 min prima (formazioni più affidabili)
    LINEUP_WINDOW_END = 0      # smette quando la partita inizia
    CHECK_INTERVAL = 300       # secondi tra un ciclo e l'altro (5 min)

    def __init__(self):
        from scraper.penalties import PenaltyAnalyzer
        from scraper.sportmonks import SportmonksClient
        from logic.notifications import EmailService

        self.pa = PenaltyAnalyzer(os.getenv("FOOTBALL_DATA_API_KEY"))
        self.sm = SportmonksClient(os.getenv("SPORTMONKS_API_KEY"))
        self.mail = EmailService()
        self.db_path = "data/betanalyzer.db"

    # ------------------------------------------------------------------ #
    #  Impostazioni dal DB (lette OGNI ciclo, non cachate)
    # ------------------------------------------------------------------ #

    def _get_worker_setting(self, key, default=""):
        try:
            conn = sqlite3.connect(self.db_path, timeout=30)
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM worker_settings WHERE key = ?", (key,))
            row = cursor.fetchone()
            conn.close()
            return row[0] if row else default
        except:
            return default

    def _is_enabled(self):
        return self._get_worker_setting("worker_enabled", "off") == "on"

    def _get_alert_email(self):
        return self._get_worker_setting("alert_email", "giorgio.cipolloni@gmail.com")

    def _get_active_leagues(self):
        raw = self._get_worker_setting("active_leagues", "[]")
        try:
            leagues = json.loads(raw)
            return leagues if isinstance(leagues, list) else []
        except:
            return []

    # ------------------------------------------------------------------ #
    #  Smart Odds Rate Limiting (risparmio quota 500/mese)
    # ------------------------------------------------------------------ #

    _ODDS_FETCH_CACHE_KEY = "last_odds_fetch"

    def _should_fetch_odds(self) -> bool:
        """Decide if we should fetch fresh odds or reuse cache.

        Strategy:
        - If matches today: every 2 hours
        - If matches tomorrow: every 4 hours
        - Otherwise: every 6 hours
        This uses ~8-12 requests/day instead of 288.
        """
        last_fetch_str = self._get_worker_setting(self._ODDS_FETCH_CACHE_KEY, "")
        if not last_fetch_str:
            return True  # First time, always fetch

        try:
            last_fetch = datetime.fromisoformat(last_fetch_str)
        except (ValueError, TypeError):
            return True

        now = datetime.now(timezone.utc)
        hours_since = (now - last_fetch).total_seconds() / 3600

        # Check if there are matches today
        today_str = now.strftime("%Y-%m-%d")
        tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")

        # Read cached odds to check match dates
        cached = self._load_cached_odds()
        has_today = any(getattr(m, 'commence_time', '')[:10] == today_str for m in (cached or []))
        has_tomorrow = any(getattr(m, 'commence_time', '')[:10] == tomorrow for m in (cached or []))

        if has_today:
            interval = 2  # Match day: ogni 2 ore
        elif has_tomorrow:
            interval = 4  # Day before: ogni 4 ore
        else:
            interval = 6  # No imminent matches: ogni 6 ore

        if hours_since >= interval:
            logger.info(f"📊 Odds fetch: {hours_since:.1f}h dall'ultimo (intervallo: {interval}h) → FETCH")
            return True
        else:
            logger.debug(f"📊 Odds fetch: {hours_since:.1f}h dall'ultimo (intervallo: {interval}h) → SKIP")
            return False

    def _save_odds_fetch_time(self):
        """Record when we last fetched odds."""
        now = datetime.now(timezone.utc).isoformat()
        try:
            conn = sqlite3.connect(self.db_path, timeout=30)
            conn.execute(
                "INSERT OR REPLACE INTO worker_settings (key, value) VALUES (?, ?)",
                (self._ODDS_FETCH_CACHE_KEY, now)
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.warning(f"Error saving odds fetch time: {e}")

    def _load_cached_odds(self):
        """Load odds from cache files (already saved by OddsAPIClient)."""
        try:
            from scraper.odds_api import OddsAPIClient, LEAGUES, CACHE_DIR
            active = self._get_active_leagues()
            all_matches = []
            for key in active:
                if key not in LEAGUES:
                    continue
                sport_key, league_name = LEAGUES[key]
                cache_file = CACHE_DIR / f"{sport_key}.json"
                if cache_file.exists():
                    import json as _json
                    with open(cache_file) as f:
                        payload = _json.load(f)
                    # OddsAPIClient cache format: {"ts": ..., "data": [...]}
                    dummy = OddsAPIClient("dummy")
                    matches = dummy._parse_response(payload.get("data", []), league_name)
                    all_matches.extend(matches)
            return all_matches if all_matches else None
        except Exception as e:
            logger.debug(f"Cached odds load error: {e}")
            return None

    # ------------------------------------------------------------------ #
    #  Recupero match imminenti (prossime 24h, solo campionati attivi)
    # ------------------------------------------------------------------ #

    def get_upcoming_matches(self):
        """Recupera match nelle prossime 24h per i campionati attivi.
        Prova prima Sportmonks, poi Odds API come fallback."""
        active_leagues = self._get_active_leagues()
        if not active_leagues:
            logger.info("⏸️  Nessun campionato attivo configurato")
            return []

        raw_matches = []

        # 1. Prova Sportmonks (sorgente primaria)
        sm_key = os.getenv("SPORTMONKS_API_KEY")
        if sm_key:
            try:
                from scraper.sportmonks import SportmonksClient
                sm_client = SportmonksClient(sm_key)
                raw_matches = sm_client.get_all_matches(league_keys=active_leagues)
                if raw_matches:
                    logger.info(f"✅ Sportmonks: {len(raw_matches)} match recuperati")
            except Exception as e:
                logger.warning(f"⚠️ Sportmonks fallito: {e}")

        # 2. Fallback/arricchimento con Odds API (rotazione chiavi)
        #    SMART RATE LIMIT: per risparmiare quota (500/mese), fetch solo ogni N ore
        #    - Giorno match: ogni 2 ore (12 fetch/giorno)
        #    - Giorno prima: ogni 4 ore (6 fetch/giorno)
        #    - Oltre: ogni 6 ore (4 fetch/giorno)
        odds_keys = [v for k, v in sorted(os.environ.items())
                     if k.startswith("ODDS_API_KEY") and v]
        if odds_keys:
            from scraper.odds_api import OddsAPIClient
            odds_matches = None

            # Check if we should fetch odds this cycle
            should_fetch_odds = self._should_fetch_odds()

            if not should_fetch_odds:
                # Use cached odds from last fetch
                odds_matches = self._load_cached_odds()
                if odds_matches:
                    logger.debug(f"📦 Odds API: usando cache ({len(odds_matches)} match)")
            else:
                for idx, okey in enumerate(odds_keys):
                    try:
                        odds_client = OddsAPIClient(okey)
                        odds_matches = odds_client.get_all_matches(active_leagues)
                        quota = odds_client.get_quota_usage()
                        if odds_matches:
                            logger.info(f"✅ Odds API (key #{idx+1}): {len(odds_matches)} match "
                                        f"(remaining: {quota['remaining']})")
                            self._save_odds_fetch_time()
                            break
                        else:
                            logger.warning(f"⚠️ Odds API key #{idx+1}: nessun match "
                                           f"(remaining: {quota['remaining']})")
                    except Exception as e:
                        logger.warning(f"⚠️ Odds API key #{idx+1} fallita: {e}")

            if odds_matches:
                if raw_matches:
                    from scraper.hybrid import merge_odds_into_matches
                    merge_odds_into_matches(raw_matches, odds_matches)
                    logger.info(f"✅ Odds API: {len(odds_matches)} match merged")
                else:
                    raw_matches = odds_matches
                    logger.info(f"✅ Odds API (primaria): {len(odds_matches)} match")

        # 2b. Save odds snapshots for Line Movement tracking
        if raw_matches:
            try:
                from db.database import save_odds_snapshots_batch
                snapshots = []
                for m in raw_matches:
                    if not m.odds:
                        continue
                    match_key = f"{m.home_team}_vs_{m.away_team}_{m.commence_time[:10] if m.commence_time else 'unknown'}"
                    for bk in m.odds:
                        if bk.home and bk.draw and bk.away:
                            snapshots.append({
                                "match_key": match_key,
                                "home_team": m.home_team,
                                "away_team": m.away_team,
                                "league": m.league,
                                "match_date": m.commence_time[:10] if m.commence_time else "",
                                "bookmaker": bk.bookmaker,
                                "home_odds": bk.home,
                                "draw_odds": bk.draw,
                                "away_odds": bk.away,
                                "over25": bk.over25,
                                "under25": bk.under25,
                                "gg": bk.gg,
                                "ng": bk.ng,
                            })
                if snapshots:
                    save_odds_snapshots_batch(snapshots)
            except Exception as e:
                logger.warning(f"Odds snapshot save error: {e}")

        # 3. Fallback API-Football per match senza odds
        #    CACHE: scarica quote UNA VOLTA per data, non ad ogni ciclo (5 min)
        apifb_key = os.getenv("API_FOOTBALL_KEY", "")
        matches_no_odds = [m for m in raw_matches if not m.odds] if raw_matches else []
        if apifb_key and matches_no_odds:
            try:
                from scraper.api_football import merge_apifootball_odds
                today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

                # Solo date di OGGI (evita sprecare chiamate su date future fuori piano free)
                dates_seen = set()
                for m in matches_no_odds:
                    if m.commence_time:
                        try:
                            ct = m.commence_time.replace("Z", "+00:00")
                            md = datetime.fromisoformat(ct).strftime("%Y-%m-%d")
                            if md == today_str:  # Solo oggi!
                                dates_seen.add(md)
                        except Exception:
                            pass
                if not dates_seen:
                    dates_seen.add(today_str)

                # Cache: non richiamare se già scaricate in questo ciclo di vita
                if not hasattr(self, '_apifb_odds_cache'):
                    self._apifb_odds_cache = {}  # {date_str: timestamp}

                for d in sorted(dates_seen):
                    # Skip se già scaricate meno di 30 minuti fa
                    last_fetch = self._apifb_odds_cache.get(d, 0)
                    now_ts = datetime.now(timezone.utc).timestamp()
                    if now_ts - last_fetch < 1800:  # 30 min cache
                        logger.info(f"📦 API-Football odds per {d} in cache (skip)")
                        continue

                    enriched = merge_apifootball_odds(raw_matches, d, apifb_key)
                    self._apifb_odds_cache[d] = now_ts
                    if enriched:
                        logger.info(f"🏈 API-Football fallback: {enriched} match arricchiti con quote")
            except Exception as e:
                logger.warning(f"⚠️ API-Football fallback error: {e}")

        if not raw_matches:
            logger.warning("❌ Nessuna sorgente ha restituito match")
            return []

        now = datetime.now(timezone.utc)
        upcoming = []

        for m in raw_matches:
            try:
                ct = m.commence_time.replace("Z", "+00:00")
                m_date = datetime.fromisoformat(ct)
                if m_date.tzinfo is None:
                    m_date = m_date.replace(tzinfo=timezone.utc)
                # Solo partite nelle prossime 24h (e non già iniziate da più di 2h)
                if (now - timedelta(hours=2)) < m_date < (now + timedelta(hours=24)):
                    upcoming.append({
                        "match_id": m.id,
                        "home": m.home_team,
                        "away": m.away_team,
                        "league": m.league,
                        "match_date": m.commence_time,
                        "commence_time": m.commence_time,
                        "league_key": self._get_league_key(m.league),
                        "kickoff_utc": m_date,
                        "odds": m.odds,  # BookmakerOdds objects for Value Bet calc
                    })
            except Exception:
                continue

        logger.info(f"📋 {len(upcoming)} match nelle prossime 24h per {active_leagues}")
        return upcoming

    def _get_league_key(self, league_name):
        mapping = {
            "Serie A": "italy_serie_a", "Premier League": "england_premier_league",
            "La Liga": "spain_la_liga", "Bundesliga": "germany_bundesliga",
            "Ligue 1": "france_ligue_1", "Eredivisie": "netherlands_eredivisie",
            "Champions League": "champions_league", "Championship": "england_championship",
            "Primeira Liga": "portugal_primeira_liga", "Superliga": "denmark_superliga",
            "Premiership": "scotland_premiership",
        }
        return mapping.get(league_name, "italy_serie_a")

    def _is_in_lineup_window(self, kickoff_utc):
        """Controlla se siamo nella finestra 70→0 minuti prima del kickoff."""
        now = datetime.now(timezone.utc)
        minutes_to_kickoff = (kickoff_utc - now).total_seconds() / 60
        return self.LINEUP_WINDOW_END <= minutes_to_kickoff <= self.LINEUP_WINDOW_START

    def _save_lineup_cache(self, m_dict, lineups):
        """Salva le formazioni ufficiali in un file cache per cartellini/marcatori."""
        cache_path = os.path.join(os.path.dirname(self.db_path), "lineups_cache.json")
        try:
            # Leggi cache esistente
            cache = {}
            if os.path.exists(cache_path):
                with open(cache_path, "r") as f:
                    cache = json.load(f)

            # Chiave: "HomeTeam vs AwayTeam" normalizzata
            key = f"{m_dict['home']} vs {m_dict['away']}"

            # Nomi titolari e panchinari
            starters_home = [p["name"] for p in lineups.get("home", [])]
            starters_away = [p["name"] for p in lineups.get("away", [])]
            bench_home = [p["name"] for p in lineups.get("home_bench", [])]
            bench_away = [p["name"] for p in lineups.get("away_bench", [])]

            cache[key] = {
                "home_team": m_dict["home"],
                "away_team": m_dict["away"],
                "league_key": m_dict.get("league_key", ""),
                "starters_home": starters_home,
                "starters_away": starters_away,
                "bench_home": bench_home,
                "bench_away": bench_away,
                "formation_home": lineups.get("formation", {}).get("home"),
                "formation_away": lineups.get("formation", {}).get("away"),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "kickoff": m_dict.get("kickoff_utc", "").isoformat() if hasattr(m_dict.get("kickoff_utc", ""), "isoformat") else str(m_dict.get("kickoff_utc", ""))
            }

            # Pulisci entries più vecchie di 24h
            cutoff = datetime.now(timezone.utc).isoformat()
            for k in list(cache.keys()):
                ts = cache[k].get("timestamp", "")
                if ts and ts < (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat():
                    del cache[k]

            with open(cache_path, "w") as f:
                json.dump(cache, f, indent=2, ensure_ascii=False)

            logger.info(f"💾 Lineup salvate in cache: {key} (T:{len(starters_home)}+{len(starters_away)}, P:{len(bench_home)}+{len(bench_away)})")
        except Exception as e:
            logger.error(f"Errore salvataggio lineup cache: {e}")

    def is_alert_sent(self, alert_id):
        conn = sqlite3.connect(self.db_path, timeout=30)
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM alerts_log WHERE match_id = ? AND status = ?", (alert_id, "SENT"))
        exists = cursor.fetchone()
        conn.close()
        return exists is not None

    def log_alert_sent(self, alert_id, home, away, league, date, rec, ai_prompt_json=None):
        conn = sqlite3.connect(self.db_path, timeout=30)
        cursor = conn.cursor()
        # Save in Italian timezone (CET/CEST) for display
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("Europe/Rome")).strftime("%Y-%m-%dT%H:%M:%S")
        # Migrate: add ai_prompt_json column if missing
        try:
            cursor.execute("ALTER TABLE alerts_log ADD COLUMN ai_prompt_json TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists
        cursor.execute("""
            INSERT INTO alerts_log (match_id, home_team, away_team, league, match_date, recommendation, sent_at, status, ai_prompt_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (alert_id, home, away, league, date, rec, now, "SENT", ai_prompt_json))
        conn.commit()
        conn.close()

    @staticmethod
    def _normalize_name(name):
        """Normalizza nome: rimuove accenti, trattini, spazi extra."""
        import unicodedata
        # Decomponi accenti (é → e + combining accent) e rimuovi combining chars
        nfkd = unicodedata.normalize('NFKD', name)
        ascii_name = ''.join(c for c in nfkd if not unicodedata.combining(c))
        # Minuscolo, rimuovi trattini/apostrofi, normalizza spazi
        return ascii_name.lower().replace("-", " ").replace("'", "").replace("'", "").strip()

    def _format_scorer_pick(self, p, detail=True):
        """Format a scorer pick with stats aligned to the web page."""
        name = p['player']
        team = p['team']
        prob = p['probability']
        goals = p.get('goals', 0)
        matches = p.get('matches', 0)
        gpm = p.get('goals_per_match', 0)

        # Flags
        flags = []
        if p.get("is_penalty_taker"):
            flags.append("RIGORISTA")
        elif p.get("penalty_goals", 0) >= 1:
            flags.append("rig.riserva")
        if p.get("position", "") in ("Centre-Back", "Defence", "Left-Back", "Right-Back"):
            flags.append("DIF")
        flag_str = f" [{', '.join(flags)}]" if flags else ""

        # Base: nome, squadra, probabilità, gol
        line = f"{name} ({team}): {prob}% | {goals}g in {matches}pg ({gpm}/g)"

        if detail:
            # Stats avanzate (come la pagina web)
            stat_parts = []
            spg = p.get("shots_per_game")
            if spg: stat_parts.append(f"Tiri:{spg}/g")
            sot = p.get("shots_on_target_pct")
            if sot: stat_parts.append(f"Prec:{sot}%")
            ast = p.get("assists_total")
            if ast: stat_parts.append(f"{ast}ass")
            fd = p.get("fouls_drawn_per_game")
            if fd: stat_parts.append(f"FalliSub:{fd}/g")
            kp = p.get("key_passes_per_game")
            if kp: stat_parts.append(f"KeyPass:{kp}/g")
            dr = p.get("dribbles_per_game")
            if dr: stat_parts.append(f"Dribbling:{dr}/g")
            drought = p.get("drought", 0)
            if drought is not None:
                stat_parts.append(f"Astinenza:{drought}gg")
            if stat_parts:
                line += f" | {', '.join(stat_parts)}"

        line += flag_str
        return line

    def _format_card_pick(self, p, detail=True):
        """Format a card pick with stats aligned to the web page."""
        name = p['player']
        team = p['team']
        prob = p['probability']
        yellows = p.get('yellows', 0)
        matches = p.get('matches', 0)
        ypm = p.get('yellows_per_match', 0)

        # Flags
        flags = []
        if p.get("diffidato"):
            flags.append("DIFFIDATO")
        pos = p.get("position", "")
        if pos:
            flags.append(pos)
        flag_str = f" [{', '.join(flags)}]" if flags else ""

        # Base
        line = f"{name} ({team}): {prob}% | {yellows}amm in {matches}pg ({ypm}/g)"

        if detail:
            stat_parts = []
            fpg = p.get("fouls_per_game")
            if fpg: stat_parts.append(f"FalliComm:{fpg}/g")
            tpg = p.get("tackles_per_game")
            if tpg: stat_parts.append(f"Contrasti:{tpg}/g")
            ipg = p.get("interceptions_per_game")
            if ipg: stat_parts.append(f"Intercetti:{ipg}/g")
            fdpg = p.get("fouls_drawn_per_game")
            if fdpg: stat_parts.append(f"FalliSub:{fdpg}/g")
            dapg = p.get("dribbles_att_per_game")
            if dapg: stat_parts.append(f"Dribbling:{dapg}/g")
            apg = p.get("aerials_per_game")
            if apg: stat_parts.append(f"Aerei:{apg}/g")
            # NEW: avg card minute, H2H, recent form, reds
            acm = p.get("avg_card_minute")
            if acm: stat_parts.append(f"MinMedio:{acm}'")
            h2h_y = p.get("h2h_yellows", 0)
            h2h_m = p.get("h2h_matches", 0)
            if h2h_m >= 2: stat_parts.append(f"H2H:{h2h_y}amm/{h2h_m}pg")
            ref_y = p.get("ref_yellows", 0)
            ref_m = p.get("ref_matches", 0)
            if ref_m >= 2: stat_parts.append(f"Arb:{ref_y}amm/{ref_m}pg")
            rfr = p.get("recent_form_ratio", 1.0)
            if rfr > 1.3: stat_parts.append("🔥trend")
            elif rfr < 0.5 and p.get("yellows", 0) > 0: stat_parts.append("❄️trend")
            reds = p.get("reds_season", 0)
            if reds > 0: stat_parts.append(f"{reds}🟥")
            if stat_parts:
                line += f" | {', '.join(stat_parts)}"

        line += flag_str
        return line

    def _fuzzy_match(self, name1, name2):
        """Fuzzy match two player names (containment or last-name match).
        Gestisce diacritici (Smolčić/Smolcic), trattini (Marc-Oliver/Marc Oliver),
        spazi (Delprato/Del Prato) e accenti (Máximo/Maximo)."""
        n1 = self._normalize_name(name1)
        n2 = self._normalize_name(name2)
        # Exact dopo normalizzazione
        if n1 == n2:
            return True
        # Containment
        if n1 in n2 or n2 in n1:
            return True
        # Senza spazi (Delprato == Del Prato)
        if n1.replace(" ", "") == n2.replace(" ", ""):
            return True
        # Last name match (cognome > 3 chars)
        parts1, parts2 = n1.split(), n2.split()
        if parts1 and parts2 and parts1[-1] == parts2[-1] and len(parts1[-1]) > 3:
            return True
        return False

    @staticmethod
    def _position_sort_key(position):
        """Sort key: Goalkeeper=0, Defence=1, Midfield=2, Offence=3."""
        pos = (position or "").lower()
        if pos in ["goalkeeper"]:
            return 0
        elif pos in ["defence", "defender", "centre-back", "left-back", "right-back"]:
            return 1
        elif pos in ["midfield", "midfielder", "central midfield", "defensive midfield",
                      "attacking midfield", "left midfield", "right midfield"]:
            return 2
        elif pos in ["offence", "attacker", "centre-forward", "second striker",
                      "left winger", "right winger"]:
            return 3
        return 2  # default: midfield

    def _load_sportmonks_stats(self, team_name):
        """Load all player stats+ratings from Sportmonks DB for a team."""
        try:
            conn = sqlite3.connect(self.db_path, timeout=30)
            conn.row_factory = sqlite3.Row
            # Prova match diretto, poi parole chiave progressivamente
            rows = []
            # 1. Match diretto
            rows = conn.execute("""
                SELECT pi.name, psc.stats_json, psc.rating, pi.position_id
                FROM player_info pi
                JOIN player_stats_cache psc ON pi.player_id = psc.player_id AND pi.team_id = psc.team_id
                WHERE pi.team_name LIKE ?
                AND psc.season_id = (SELECT MAX(season_id) FROM player_stats_cache WHERE player_id = psc.player_id AND team_id = psc.team_id)
            """, (f"%{team_name}%",)).fetchall()
            # 2. Se nessun risultato, prova con ogni parola del nome (min 4 chars)
            if not rows:
                for word in team_name.split():
                    if len(word) >= 4 and word.lower() not in ["club", "real", "city", "united"]:
                        rows = conn.execute("""
                            SELECT pi.name, psc.stats_json, psc.rating, pi.position_id
                            FROM player_info pi
                            JOIN player_stats_cache psc ON pi.player_id = psc.player_id AND pi.team_id = psc.team_id
                            WHERE pi.team_name LIKE ?
                            AND psc.season_id = (SELECT MAX(season_id) FROM player_stats_cache WHERE player_id = psc.player_id AND team_id = psc.team_id)
                        """, (f"%{word}%",)).fetchall()
                        if rows:
                            break
            conn.close()
            result = {}
            for r in rows:
                result[r["name"].lower()] = {
                    "stats": json.loads(r["stats_json"]),
                    "rating": r["rating"] or 0,
                    "position_id": r["position_id"]
                }
            return result
        except Exception as e:
            logger.warning(f"Errore caricamento stats Sportmonks per {team_name}: {e}")
            return {}

    def _find_sm_data(self, player_name, sm_data):
        """Find Sportmonks data for a player by fuzzy name match."""
        name_lower = player_name.lower()
        # Exact match first
        if name_lower in sm_data:
            return sm_data[name_lower]
        # Fuzzy match
        for sm_name, data in sm_data.items():
            if self._fuzzy_match(player_name, sm_name):
                return data
        return None

    def _build_player_profile(self, player_data, scorer_picks, card_picks, sm_data=None, team_goals=0):
        """Build a compact profile string for a single lineup player."""
        name = player_data.get("name", "?")
        pos = player_data.get("position", "?")
        goals = player_data.get("goals", 0)
        assists = player_data.get("assists", 0)
        yellows = player_data.get("yellow_cards", 0)
        reds = player_data.get("red_cards", 0)
        apps = player_data.get("appearances", 0)
        impact = player_data.get("impact_drop", 0)
        wr_with = player_data.get("win_rate_with", 0)
        wr_without = player_data.get("win_rate_without", 0)
        status = player_data.get("status")
        status_detail = player_data.get("status_detail", "")

        # Sportmonks advanced stats + rating
        sm = self._find_sm_data(name, sm_data) if sm_data else None
        rating = sm["rating"] if sm else 0
        player_data["_rating"] = rating  # Store for avg calculation

        goal_prob, card_prob, diffidato = None, None, False
        for sp in scorer_picks:
            if self._fuzzy_match(sp.get("player", ""), name):
                goal_prob = sp.get("probability")
                break
        for cp in card_picks:
            if self._fuzzy_match(cp.get("player", ""), name):
                card_prob = cp.get("probability")
                diffidato = cp.get("diffidato", False)
                break

        # Rating badge
        parts = [f"  - {name} ({pos})"]
        if rating > 0:
            parts.append(f" [VOTO: {rating}]")

        # Season stats (FD.org)
        stats_parts = []
        if apps > 0: stats_parts.append(f"{apps} pres")
        if goals > 0: stats_parts.append(f"{goals} gol")
        if assists > 0: stats_parts.append(f"{assists} ass")
        if yellows > 0: stats_parts.append(f"{yellows} amm")
        if reds > 0: stats_parts.append(f"{reds} esp")
        if stats_parts:
            parts.append(f" | Stagione: {', '.join(stats_parts)}")

        # Partecipazione gol: (gol + assist) / gol_squadra × 100
        if team_goals > 0 and (goals + assists) > 0:
            goal_inv = round((goals + assists) / team_goals * 100)
            parts.append(f" | PartGol:{goal_inv}%")

        # Advanced stats (Sportmonks DB)
        if sm:
            s = sm["stats"]
            adv_parts = []
            a = s.get("appearances", 0) or 1
            shots = s.get("shots_on_target", 0)
            if shots > 0: adv_parts.append(f"Tiri:{shots}({round(shots/a,1)}/g)")
            kp = s.get("key_passes", 0)
            if kp > 0: adv_parts.append(f"KeyPass:{kp}")
            dr = s.get("dribbles_success", 0)
            if dr > 0: adv_parts.append(f"Dribbling:{dr}")
            fc = s.get("fouls_committed", 0)
            if fc > 0: adv_parts.append(f"FalliComm:{fc}({round(fc/a,1)}/g)")
            fd = s.get("fouls_drawn", 0)
            if fd > 0: adv_parts.append(f"FalliSub:{fd}({round(fd/a,1)}/g)")
            tk = s.get("tackles", 0)
            inter = s.get("interceptions", 0)
            if tk + inter > 0: adv_parts.append(f"Recuperi:{tk+inter}")
            ae = s.get("aerials_won", 0)
            if ae > 0: adv_parts.append(f"Aerei:{ae}")
            bc = s.get("big_chances_created", 0)
            if bc > 0: adv_parts.append(f"BigChance:{bc}")
            pa_pct = s.get("accurate_passes_pct", 0)
            if pa_pct > 0: adv_parts.append(f"PassAcc:{pa_pct}%")
            if adv_parts:
                parts.append(f" | Stats: {', '.join(adv_parts)}")

        if impact != 0:
            parts.append(f" | Impatto: {'+' if impact > 0 else ''}{impact}% (WR con:{wr_with}% senza:{wr_without}%)")
        if goal_prob:
            parts.append(f" | Prob.GOL: {goal_prob}%")
        if card_prob:
            parts.append(f" | Prob.AMMON: {card_prob}%")
        if diffidato:
            parts.append(" | DIFFIDATO")
        if status == "Injured":
            parts.append(f" | INFORTUNATO: {status_detail}")
        elif status == "Suspended":
            parts.append(f" | SQUALIFICATO: {status_detail}")
        return "".join(parts)

    def trigger_ai_analysis(self, m_dict):
        home, away, league_name, league_key = m_dict["home"], m_dict["away"], m_dict["league"], m_dict["league_key"]
        rome_tz = pytz.timezone('Europe/Rome')
        context_parts = []
        official_lineups = None

        try:
            # 1. Recupero Lineups ufficiali da Sportmonks
            sm_matches = []
            try:
                sm_matches = self.sm.get_all_matches(league_keys=[league_key])
            except: pass
            target_m = next((m for m in sm_matches if (home.lower() in m.home_team.lower() or m.home_team.lower() in home.lower()) and (away.lower() in m.away_team.lower() or m.away_team.lower() in away.lower())), None)
            if target_m:
                try:
                    lineups_data = self.sm.get_official_lineups(target_m.id)
                    if lineups_data.get("home") and lineups_data.get("away"):
                        official_lineups = lineups_data
                except: pass

            # 2. Dati completi: Roster, Stats squadra, Marcatori, Cartellini, Assenze
            codes = {"italy_serie_a": "SA", "england_premier_league": "PL", "spain_la_liga": "PD", "germany_bundesliga": "BL1", "france_ligue_1": "FL1", "netherlands_eredivisie": "DED", "champions_league": "CL", "england_championship": "ELC", "portugal_primeira_liga": "PPL", "denmark_superliga": "DSU", "scotland_premiership": "SPL"}
            l_code = codes.get(league_key, "SA")
            standings = self.pa._get_standings(l_code)
            def clean_n(n):
                return n.lower().replace("club ", "").replace("ca ", "").replace("cf ", "").replace("de ", "").replace("fc ", "").strip()
            h_id = next((s["team_id"] for s in standings if clean_n(home) in clean_n(s["name"]) or clean_n(s["name"]) in clean_n(home)), None)
            a_id = next((s["team_id"] for s in standings if clean_n(away) in clean_n(s["name"]) or clean_n(s["name"]) in clean_n(away)), None)

            if h_id and a_id:
                logger.info(f"Team IDs: {home}={h_id}, {away}={a_id}")
                from logic.roster import RosterManager
                rm = RosterManager(os.getenv("FOOTBALL_DATA_API_KEY"))
                h_players, h_team_stats, h_leaders = rm.get_roster_with_stats(h_id, l_code, league_key)
                a_players, a_team_stats, a_leaders = rm.get_roster_with_stats(a_id, l_code, league_key)

                # Betting stats per squadra (Over/Under, GG, Ribaltoni)
                h_betting = rm.get_team_betting_stats(h_id, l_code)
                a_betting = rm.get_team_betting_stats(a_id, l_code)

                # Marcatori & Cartellini probabilita'
                from scraper.scorers import ScorerAnalyzer
                from scraper.cards import CardAnalyzer
                sa_inst = ScorerAnalyzer(os.getenv("FOOTBALL_DATA_API_KEY"))
                ca_inst = CardAnalyzer(os.getenv("FOOTBALL_DATA_API_KEY"))
                s_data = sa_inst.analyze_league(league_key)
                c_data = ca_inst.analyze_league(league_key)
                m_s = next((m for m in s_data.get("matches", []) if m["home_id"] == h_id and m["away_id"] == a_id), None)
                m_c = next((m for m in c_data.get("matches", []) if m["home_id"] == h_id and m["away_id"] == a_id), None)
                scorer_picks = [p for p in (m_s or {}).get("top_picks", []) if p.get("position", "").lower() not in ["goalkeeper", "portiere", "gk"]]
                card_picks = [p for p in (m_c or {}).get("top_picks", []) if p.get("position", "").lower() not in ["goalkeeper", "portiere", "gk"]]

                # === ARBITRO ===
                if m_c:
                    ref_name = m_c.get("referee")
                    ref_cpm = m_c.get("referee_cards_pm")
                    ref_mult = m_c.get("referee_multiplier", 1.0)
                    ref_matches = m_c.get("referee_matches", 0)
                    avg_cpm = m_c.get("league_avg_cards_pm")
                    if ref_name:
                        ref_tendency = "SEVERO" if ref_mult > 1.15 else ("PERMISSIVO" if ref_mult < 0.85 else "NELLA MEDIA")
                        ref_line = f"ARBITRO: {ref_name} | {ref_matches} partite"
                        ref_line += f" | Cartellini: {ref_cpm}/gara (media lega: {avg_cpm}) → {ref_tendency} (x{ref_mult})"
                        # Aggiungi dati rigori dall'analisi marcatori (se disponibili)
                        if m_s and m_s.get("has_referee") and m_s.get("referee_penalties_pm") is not None:
                            ref_ppm = m_s["referee_penalties_pm"]
                            ref_pen_tend = "PROPENSO" if ref_ppm >= 0.3 else ("NELLA MEDIA" if ref_ppm >= 0.15 else "RESTRITTIVO")
                            ref_line += f" | Rigori: {ref_ppm}/gara → {ref_pen_tend}"
                        # VAR stats from var_stats.json
                        try:
                            import json as _json
                            var_path = os.path.join(os.path.dirname(__file__), "data", "var_stats.json")
                            if os.path.exists(var_path):
                                with open(var_path) as _vf:
                                    var_data = _json.load(_vf)
                                var_ref = var_data.get("referees", {}).get(ref_name)
                                avg_vpm = var_data.get("avg_var_per_match", 0.5)
                                if var_ref and var_ref.get("matches", 0) >= 3:
                                    vpm = var_ref["var_per_match"]
                                    vpct = var_ref["var_match_pct"]
                                    overturn = var_ref["var_overturn_pct"]
                                    var_tend = "FREQUENTE" if vpm >= avg_vpm * 1.3 else ("RARO" if vpm <= avg_vpm * 0.6 else "NELLA MEDIA")
                                    ref_line += f" | VAR: {vpm}/gara ({vpct}% partite con VAR) → {var_tend}"
                                    ref_line += f" | Ribalta {overturn}% delle decisioni"
                                    ref_line += f" (Rig:{var_ref['var_penalty']} Gol:{var_ref['var_goal']} Red:{var_ref['var_red']})"
                        except Exception as e:
                            logger.debug(f"VAR stats load: {e}")
                        context_parts.append(ref_line)

                # === STATS SQUADRA ===
                if h_team_stats and h_team_stats.get("matches_played", 0) > 0:
                    context_parts.append(f"STATS {home}: {h_team_stats['wins']}V-{h_team_stats['draws']}P-{h_team_stats['losses']}S | GolFatti:{h_team_stats['goals_for']} Subiti:{h_team_stats['goals_against']} | Modulo:{h_team_stats.get('most_used_formation','?')}")
                if a_team_stats and a_team_stats.get("matches_played", 0) > 0:
                    context_parts.append(f"STATS {away}: {a_team_stats['wins']}V-{a_team_stats['draws']}P-{a_team_stats['losses']}S | GolFatti:{a_team_stats['goals_for']} Subiti:{a_team_stats['goals_against']} | Modulo:{a_team_stats.get('most_used_formation','?')}")

                # === MATCH IMPORTANCE ===
                if standings:
                    from logic.match_importance import calculate_match_importance
                    # Adatta formato standings (name -> team_name)
                    st_adapted = [{"team_name": s.get("name", ""), "position": s.get("position", 0), "points": s.get("points", 0), "played": s.get("played", 0), "goals_diff": s.get("goals_diff", 0)} for s in standings]
                    importance = calculate_match_importance(home, away, league_name, st_adapted)
                    if importance.get("narrative"):
                        context_parts.append("\n" + importance["narrative"])

                # === BETTING STATS (totale + split casa/trasferta) ===
                def _fmt_betting(label, b, split_key=None):
                    line = f"BETTING {label}: O2.5={round(b['over25_pct'])}% GG={round(b['btts_pct'])}% CS={round(b.get('clean_sheet_pct',0))}% Ribaltone={round(b['ribaltone_si_pct'])}% AvgFatti={round(b['avg_scored'],1)} AvgSubiti={round(b['avg_conceded'],1)}"
                    if split_key and b.get(split_key):
                        s = b[split_key]
                        loc = "🏠 CASA" if split_key == "home" else "✈️ TRASFERTA"
                        line += f"\n  {loc}: O2.5={round(s['over25_pct'])}% GG={round(s['btts_pct'])}% CS={round(s.get('clean_sheet_pct',0))}% AvgFatti={round(s['avg_scored'],1)} AvgSubiti={round(s['avg_conceded'],1)} ({s['matches']}g: {round(s['win_pct'])}%V)"
                    return line

                if h_betting:
                    context_parts.append(_fmt_betting(home, h_betting, "home"))
                if a_betting:
                    context_parts.append(_fmt_betting(away, a_betting, "away"))

                # === RISULTATO ESATTO + 1X2 (from Poisson/Dixon-Coles model) ===
                if h_betting and a_betting:
                    top_scores = rm.predict_correct_score(h_betting, a_betting)
                    if top_scores:
                        context_parts.append(f"RISULTATI ESATTI PROBABILI: {' | '.join([s['score'] for s in top_scores])}")

                # 1X2 probabilities from full model (with draw correction)
                cs_match = None
                try:
                    from scraper.correct_score import CorrectScoreAnalyzer
                    cs_inst = CorrectScoreAnalyzer(os.getenv("FOOTBALL_DATA_API_KEY"))
                    cs_data = cs_inst.analyze_league(league_key)
                    cs_match = next((m for m in cs_data.get("matches", [])
                                     if home.lower() in m.get("home_team", "").lower()
                                     or m.get("home_team", "").lower() in home.lower()), None)
                    if cs_match:
                        agg = cs_match.get("aggregates", {})
                        draw_boost = cs_match.get("draw_boost", 0)
                        boost_note = f" (corr.+{draw_boost}%)" if draw_boost > 0 else ""
                        context_parts.append(
                            f"MODELLO 1X2: 1={agg.get('home_win',0)}% X={agg.get('draw',0)}%{boost_note} 2={agg.get('away_win',0)}% | "
                            f"O2.5={agg.get('over_25',0)}% U2.5={agg.get('under_25',0)}% | GG={agg.get('gg',0)}% NG={agg.get('ng',0)}% | "
                            f"xG: {cs_match.get('lambda_home',0)}-{cs_match.get('lambda_away',0)}"
                        )
                except Exception as e:
                    logger.warning(f"1X2 model context error: {e}")

                # === VALUE BET DETECTOR ===
                try:
                    match_odds = m_dict.get("odds", [])
                    if cs_match and match_odds:
                        agg = cs_match.get("aggregates", {})
                        # Model probabilities (%)
                        model_probs = {
                            "1": agg.get("home_win", 0),
                            "X": agg.get("draw", 0),
                            "2": agg.get("away_win", 0),
                            "Over 2.5": agg.get("over_25", 0),
                            "Under 2.5": agg.get("under_25", 0),
                            "GG": agg.get("gg", 0),
                            "NG": agg.get("ng", 0),
                        }
                        # Find best odds across bookmakers for each market
                        best_odds = {}
                        for bk in match_odds:
                            odds_map = {
                                "1": bk.home, "X": bk.draw, "2": bk.away,
                                "Over 2.5": bk.over25, "Under 2.5": bk.under25,
                                "GG": bk.gg, "NG": bk.ng,
                            }
                            for mkt, odd in odds_map.items():
                                if odd and odd > 1.0:
                                    if mkt not in best_odds or odd > best_odds[mkt][0]:
                                        best_odds[mkt] = (odd, bk.bookmaker)

                        # Calculate EV for each market: EV = (model_prob/100) * odds - 1
                        value_bets = []
                        for mkt, prob in model_probs.items():
                            if prob > 0 and mkt in best_odds:
                                odd_val, bk_name = best_odds[mkt]
                                ev = (prob / 100.0) * odd_val - 1.0
                                ev_pct = round(ev * 100, 1)
                                if ev_pct > 5.0:  # Threshold: EV > 5%
                                    value_bets.append({
                                        "market": mkt,
                                        "model_prob": prob,
                                        "odds": odd_val,
                                        "bookmaker": bk_name,
                                        "ev_pct": ev_pct,
                                    })

                        if value_bets:
                            value_bets.sort(key=lambda x: x["ev_pct"], reverse=True)
                            context_parts.append("\n💎 VALUE BETS RILEVATE (EV > 5%):")
                            for vb in value_bets:
                                heat = "🔥🔥" if vb["ev_pct"] > 15 else ("🔥" if vb["ev_pct"] > 10 else "")
                                context_parts.append(
                                    f"  {heat} {vb['market']}: Modello={vb['model_prob']}% | Quota={vb['odds']} ({vb['bookmaker']}) | EV=+{vb['ev_pct']}%"
                                )
                            logger.info(f"💎 Value Bets trovate: {len(value_bets)} mercati con EV > 5%")
                        else:
                            context_parts.append("\n💎 VALUE BETS: Nessun mercato con EV > 5% rilevato.")
                except Exception as e:
                    logger.warning(f"Value Bet calc error: {e}")

                # === LEADER SQUADRA ===
                context_parts.append(f"LEADER {home}: Capocannoniere={h_leaders['top_scorer']['name']}({h_leaders['top_scorer']['val']}g) Assist={h_leaders['top_assistman']['name']}({h_leaders['top_assistman']['val']})")
                context_parts.append(f"LEADER {away}: Capocannoniere={a_leaders['top_scorer']['name']}({a_leaders['top_scorer']['val']}g) Assist={a_leaders['top_assistman']['name']}({a_leaders['top_assistman']['val']})")

                # === HEAD-TO-HEAD ===
                if target_m and target_m.home_id and target_m.away_id:
                    h2h = self.sm.get_head_to_head(target_m.home_id, target_m.away_id, limit=10)
                    if h2h:
                        context_parts.append(f"\nSCONTRI DIRETTI ({len(h2h)} partite):")
                        h_wins, a_wins, draws, total_goals = 0, 0, 0, 0
                        for hm in h2h:
                            hg, ag = hm["home_goals"], hm["away_goals"]
                            total_goals += hg + ag
                            context_parts.append(f"  {hm['date']} | {hm['home']} {hm['score']} {hm['away']}")
                            if hg > ag:
                                if home.lower() in hm["home"].lower() or hm["home"].lower() in home.lower():
                                    h_wins += 1
                                else:
                                    a_wins += 1
                            elif ag > hg:
                                if away.lower() in hm["away"].lower() or hm["away"].lower() in away.lower():
                                    a_wins += 1
                                else:
                                    h_wins += 1
                            else:
                                draws += 1
                        avg_goals = round(total_goals / len(h2h), 1)
                        context_parts.append(f"  BILANCIO: {home} {h_wins}V-{draws}P-{a_wins}S | Media gol H2H: {avg_goals}/partita")

                # === WEATHER IMPACT ===
                weather_data = None  # Salvo per uso in marcatori/cartellini
                try:
                    from logic.weather import get_match_weather_context, get_venue_coords, get_weather_forecast, analyze_weather_impact
                    from datetime import datetime as dt_cls
                    # Parse match datetime per le previsioni
                    match_dt = None
                    try:
                        ct = m_dict.get("commence_time", "")
                        if ct:
                            ct_clean = ct.replace("Z", "+00:00")
                            match_dt = dt_cls.fromisoformat(ct_clean)
                    except Exception:
                        pass
                    # Recupera dati grezzi per riuso
                    venue = get_venue_coords(self.sm, target_m.home_id if target_m else None)
                    if venue:
                        weather_raw = get_weather_forecast(venue["lat"], venue["lon"], match_dt)
                        if weather_raw:
                            weather_data = analyze_weather_impact(weather_raw, venue)
                            if weather_data.get("context"):
                                context_parts.append("\n" + weather_data["context"])
                except Exception as e:
                    logger.warning(f"Weather impact error: {e}")

                # === PLAYER NEWS RADAR + COACH ANALYSIS ===
                try:
                    from logic.player_news import build_news_context
                    # Recupera info coach da Sportmonks
                    h_coach = None
                    a_coach = None
                    if target_m:
                        try:
                            h_coach = self.sm.get_coach_info(target_m.home_id)
                            a_coach = self.sm.get_coach_info(target_m.away_id)
                            if h_coach:
                                logger.info(f"  Coach {home}: {h_coach['coach_name']} ({h_coach['days_in_charge']}gg)")
                            if a_coach:
                                logger.info(f"  Coach {away}: {a_coach['coach_name']} ({a_coach['days_in_charge']}gg)")
                        except Exception as e:
                            logger.warning(f"Coach info error: {e}")

                    news_ctx = build_news_context(
                        home_team=home,
                        away_team=away,
                        home_squad=h_players,
                        away_squad=a_players,
                        home_leaders=h_leaders,
                        away_leaders=a_leaders,
                        home_coach=h_coach,
                        away_coach=a_coach,
                        days=7,
                        max_news=12,
                    )
                    if news_ctx:
                        context_parts.append("\n" + news_ctx)
                except Exception as e:
                    logger.warning(f"Player News Radar error: {e}")

                # === PROFILI COMPLETI 22 TITOLARI ===
                lineup_names_home = {p["name"].lower() for p in official_lineups["home"]} if official_lineups else set()
                lineup_names_away = {p["name"].lower() for p in official_lineups["away"]} if official_lineups else set()

                def get_lineup_players(all_players, lineup_names):
                    if lineup_names:
                        selected = []
                        used_names = set()
                        for ln in lineup_names:
                            best = None
                            best_score = 0
                            for p in all_players:
                                if id(p) in used_names:
                                    continue
                                pn = p["name"].lower()
                                # Exact match = best
                                if pn == ln:
                                    best = p
                                    best_score = 100
                                    break
                                # Containment match, prefer longer overlap
                                elif self._fuzzy_match(p["name"], ln) and best_score < 50:
                                    overlap = min(len(pn), len(ln))
                                    if overlap > best_score:
                                        best = p
                                        best_score = overlap
                            if best:
                                selected.append(best)
                                used_names.add(id(best))
                        return selected
                    else:
                        # Escludi infortunati, ceduti, e squalificati (dal campo status API)
                        active = [p for p in all_players if p.get("status") not in ["Gone", "Injured", "Suspended", "Banned"]]
                        return sorted(active, key=lambda x: x.get("appearances", 0), reverse=True)[:11]

                h_lineup = get_lineup_players(h_players, lineup_names_home)
                a_lineup = get_lineup_players(a_players, lineup_names_away)

                # Log giocatori non trovati nel DB
                if lineup_names_home:
                    matched_h = {p["name"].lower() for p in h_lineup}
                    for ln in lineup_names_home:
                        if not any(self._fuzzy_match(ln, m) for m in matched_h):
                            logger.warning(f"⚠️ {home}: '{ln}' in formazione ma NON trovato nel DB")
                if lineup_names_away:
                    matched_a = {p["name"].lower() for p in a_lineup}
                    for ln in lineup_names_away:
                        if not any(self._fuzzy_match(ln, m) for m in matched_a):
                            logger.warning(f"⚠️ {away}: '{ln}' in formazione ma NON trovato nel DB")

                # Ordinamento per ruolo: Portiere → Difensori → Centrocampisti → Attaccanti
                h_lineup.sort(key=lambda p: self._position_sort_key(p.get("position", "")))
                a_lineup.sort(key=lambda p: self._position_sort_key(p.get("position", "")))

                # Filtra marcatori e cartellini: SOLO giocatori in formazione
                lineup_all_names = [p.get("name", "") for p in h_lineup + a_lineup]
                scorer_picks = [p for p in scorer_picks if any(self._fuzzy_match(p.get("player", ""), ln) for ln in lineup_all_names)]
                card_picks = [p for p in card_picks if any(self._fuzzy_match(p.get("player", ""), ln) for ln in lineup_all_names)]
                logger.info(f"Filtro lineup: {len(scorer_picks)} marcatori, {len(card_picks)} cartellini (solo titolari)")

                # Carica stats avanzate + rating da Sportmonks DB
                h_sm = self._load_sportmonks_stats(home)
                a_sm = self._load_sportmonks_stats(away)
                logger.info(f"Sportmonks DB: {home}={len(h_sm)} giocatori, {away}={len(a_sm)} giocatori")

                h_team_goals = h_team_stats.get("goals_for", 0) if h_team_stats else 0
                a_team_goals = a_team_stats.get("goals_for", 0) if a_team_stats else 0

                # Formazioni ufficiali (modulo tattico) + consiglio cartellini difensori
                if official_lineups:
                    form_home = official_lineups.get("formation", {}).get("home")
                    form_away = official_lineups.get("formation", {}).get("away")
                    if form_home or form_away:
                        # Dati storici: CB vs Terzini cards/partita per lega
                        # (cb_rate, fb_rate, gap_label)
                        _cb_vs_fb = {
                            "italy_serie_a":          (0.79, 0.61, "+30%"),
                            "england_premier_league":  (0.73, 0.70, "+5%"),
                            "spain_la_liga":           (0.82, 0.71, "+15%"),
                            "germany_bundesliga":      (0.75, 0.54, "+40%"),
                            "france_ligue_1":          (0.77, 0.66, "+16%"),
                            "netherlands_eredivisie":  (0.70, 0.60, "+17%"),
                            "champions_league":        (0.75, 0.65, "+15%"),
                            "england_championship":    (0.72, 0.65, "+11%"),
                            "portugal_primeira_liga":   (0.74, 0.64, "+16%"),
                            "brazil_serie_a":          (0.70, 0.62, "+13%"),
                        }
                        def _parse_formation(formation):
                            if not formation: return 0
                            try: return int(formation.split("-")[0])
                            except: return 0
                        h_ndef = _parse_formation(form_home)
                        a_ndef = _parse_formation(form_away)
                        cb_rate, fb_rate, gap_label = _cb_vs_fb.get(league_key, (0.73, 0.65, "+12%"))
                        stat_line = f"In questo campionato: CB {cb_rate}/app vs Terzini {fb_rate}/app ({gap_label} centrali)."

                        if h_ndef == 4 and a_ndef == 4:
                            consiglio = f"🎯 ENTRAMBE A 4 (2 centrali + 2 terzini): investi sui CENTRALI, terzini meno esposti. {stat_line}"
                        elif h_ndef == 3 and a_ndef == 3:
                            consiglio = f"🎯 ENTRAMBE A 3 (3 centrali, 0 terzini): tutti e 3 i CB sono candidati cartellino, specialmente i braccetti larghi che coprono più campo. {stat_line}"
                        elif h_ndef == 5 and a_ndef == 5:
                            consiglio = f"🎯 ENTRAMBE A 5 (3 centrali + 2 wing-back): CB + wing-back tutti candidati. I wing-back fanno falli tattici in transizione. {stat_line}"
                        elif h_ndef == 3 or a_ndef == 3:
                            team3 = home if h_ndef == 3 else away
                            team4 = away if h_ndef == 3 else home
                            consiglio = f"🎯 {team3} a 3 (tutti CB, braccetti larghi esposti) + {team4} a 4 (punta sui centrali, non sui terzini). {stat_line}"
                        else:
                            consiglio = f"🎯 Punta sui centrali. {stat_line}"

                        context_parts.append(f"\n⚙️ MODULO UFFICIALE: {home} [{form_home or '?'}] vs {away} [{form_away or '?'}]")
                        context_parts.append(f"  {consiglio}")

                        # Add historical formation performance from DB
                        from db.database import get_team_formations
                        for tid, tname, tform in [(h_id, home, form_home), (a_id, away, form_away)]:
                            if not tform:
                                continue
                            fstats = get_team_formations(tid)
                            if fstats:
                                match_form = next((f for f in fstats if f["formation"] == tform), None)
                                if match_form:
                                    fm = match_form
                                    total = fm["matches"]
                                    wpct = round(fm["wins"] / total * 100, 1) if total > 0 else 0
                                    context_parts.append(f"  📊 {tname} con {tform}: {fm['wins']}V-{fm['draws']}P-{fm['losses']}S ({wpct}% vittorie) | {fm['goals_for']}GF-{fm['goals_against']}GA in {total} partite")
                                # Show if team has a clearly better formation
                                if len(fstats) >= 2 and fstats[0]["matches"] >= 3:
                                    best = fstats[0]
                                    bpct = round(best["wins"] / best["matches"] * 100, 1)
                                    if best["formation"] != tform and bpct > wpct + 10:
                                        context_parts.append(f"  ⚠️ NOTA: {tname} rende meglio con {best['formation']} ({bpct}% vs {wpct}%)")

                context_parts.append(f"\nROSA TITOLARE {home} ({len(h_lineup)} giocatori):")
                for p in h_lineup:
                    context_parts.append(self._build_player_profile(p, scorer_picks, card_picks, h_sm, h_team_goals))

                # Media voto Home (solo titolari con rating > 0)
                h_ratings = [p.get("_rating", 0) for p in h_lineup if p.get("_rating", 0) > 0]
                if h_ratings:
                    h_avg = round(sum(h_ratings) / len(h_ratings), 1)
                    context_parts.append(f"  MEDIA VOTO {home}: {h_avg}/100 ({len(h_ratings)} giocatori valutati)")

                context_parts.append(f"\nROSA TITOLARE {away} ({len(a_lineup)} giocatori):")
                for p in a_lineup:
                    context_parts.append(self._build_player_profile(p, scorer_picks, card_picks, a_sm, a_team_goals))

                # Media voto Away
                a_ratings = [p.get("_rating", 0) for p in a_lineup if p.get("_rating", 0) > 0]
                if a_ratings:
                    a_avg = round(sum(a_ratings) / len(a_ratings), 1)
                    context_parts.append(f"  MEDIA VOTO {away}: {a_avg}/100 ({len(a_ratings)} giocatori valutati)")

                # === ASSENZE PESANTI (tutte, non solo quelle con gol > 2) ===
                h_absent = [p for p in h_players if p.get("status") in ["Injured", "Suspended"]]
                a_absent = [p for p in a_players if p.get("status") in ["Injured", "Suspended"]]
                if h_absent:
                    context_parts.append(f"\nASSENZE {home}:")
                    for p in h_absent:
                        impact = p.get('impact_drop', 0)
                        context_parts.append(f"  - {p['name']} ({p['status']}: {p.get('status_detail','')}) | {p.get('goals',0)}g {p.get('assists',0)}a | Impatto:{'+' if impact > 0 else ''}{impact}%")
                if a_absent:
                    context_parts.append(f"\nASSENZE {away}:")
                    for p in a_absent:
                        impact = p.get('impact_drop', 0)
                        context_parts.append(f"  - {p['name']} ({p['status']}: {p.get('status_detail','')}) | {p.get('goals',0)}g {p.get('assists',0)}a | Impatto:{'+' if impact > 0 else ''}{impact}%")

                # === TOP 10 MARCATORI & CARTELLINI (con % DB + note meteo) ===
                if scorer_picks:
                    # === MARCATORI: TOP PICKS (2 per squadra) + BACKUP ===
                    # Seleziona i migliori 2 per squadra con prob >= 12%
                    home_scorers = [p for p in scorer_picks if p.get("is_home")]
                    away_scorers = [p for p in scorer_picks if not p.get("is_home")]
                    top_home_sc = [p for p in home_scorers if p["probability"] >= 12][:2]
                    top_away_sc = [p for p in away_scorers if p["probability"] >= 12][:2]
                    # Fallback: se una squadra non ha 2 pick sopra 12%, prendi i top 2 comunque
                    if len(top_home_sc) < 2:
                        top_home_sc = home_scorers[:2]
                    if len(top_away_sc) < 2:
                        top_away_sc = away_scorers[:2]
                    top_scorer_picks = top_home_sc + top_away_sc
                    backup_scorers = [p for p in scorer_picks[:10] if p not in top_scorer_picks]

                    context_parts.append("\n⚽ MARCATORI — 🔥 TOP PICKS (giocata consigliata, 2+2):")
                    for p in top_scorer_picks:
                        context_parts.append(f"  🔥 {self._format_scorer_pick(p, detail=True)}")

                    if backup_scorers:
                        context_parts.append("  📋 BACKUP (giocata separata):")
                        for p in backup_scorers:
                            context_parts.append(f"     {self._format_scorer_pick(p, detail=False)}")

                    # Note meteo specifiche per marcatori
                    if weather_data and weather_data.get("severity") not in ("none", None):
                        w_notes = weather_data.get("betting_impact", {})
                        if w_notes.get("marcatori"):
                            context_parts.append(f"  ⛅ NOTA METEO MARCATORI: {w_notes['marcatori']}")
                        if w_notes.get("over_under"):
                            context_parts.append(f"  ⛅ NOTA METEO GOL: {w_notes['over_under']}")

                if card_picks:
                    # === CARTELLINI: TOP PICKS (2 per squadra) + BACKUP ===
                    home_cards = [p for p in card_picks if p.get("team_id") == h_id]
                    away_cards = [p for p in card_picks if p.get("team_id") == a_id]
                    top_home_cd = [p for p in home_cards if p["probability"] >= 15][:2]
                    top_away_cd = [p for p in away_cards if p["probability"] >= 15][:2]
                    if len(top_home_cd) < 2:
                        top_home_cd = home_cards[:2]
                    if len(top_away_cd) < 2:
                        top_away_cd = away_cards[:2]
                    top_card_picks = top_home_cd + top_away_cd
                    backup_cards = [p for p in card_picks[:14] if p not in top_card_picks]

                    context_parts.append("\n🟨 CARTELLINI — 🔥 TOP PICKS (giocata consigliata, 2+2):")
                    for p in top_card_picks:
                        context_parts.append(f"  🔥 {self._format_card_pick(p, detail=True)}")

                    if backup_cards:
                        context_parts.append("  📋 BACKUP (giocata separata):")
                        for p in backup_cards:
                            context_parts.append(f"     {self._format_card_pick(p, detail=False)}")

                    # Note meteo specifiche per cartellini
                    if weather_data and weather_data.get("severity") not in ("none", None):
                        w_notes = weather_data.get("betting_impact", {})
                        if w_notes.get("cartellini"):
                            context_parts.append(f"  ⛅ NOTA METEO CARTELLINI: {w_notes['cartellini']}")
                # === FATIGUE & CALENDARIO ===
                try:
                    from scraper.fatigue import analyze_fatigue
                    fatigue = analyze_fatigue(
                        home_team_id=h_id, away_team_id=a_id,
                        match_date=m_dict.get("commence_time", ""),
                        api_key=os.getenv("FOOTBALL_DATA_API_KEY"),
                        home_name=home, away_name=away,
                    )
                    if fatigue.get("insight"):
                        context_parts.append(f"\n⚡ FATIGUE & CALENDARIO: {fatigue['insight']}")
                        h_fat = fatigue.get("home", {})
                        a_fat = fatigue.get("away", {})
                        context_parts.append(f"  {home}: fatigue={h_fat.get('fatigue_score',0)}/100 riposo={h_fat.get('rest_days','?')}gg partite14gg={h_fat.get('matches_14d',0)}")
                        context_parts.append(f"  {away}: fatigue={a_fat.get('fatigue_score',0)}/100 riposo={a_fat.get('rest_days','?')}gg partite14gg={a_fat.get('matches_14d',0)}")
                        if fatigue.get("advantage") != "neutral":
                            adv = home if fatigue["advantage"] == "home" else away
                            context_parts.append(f"  ⚡ VANTAGGIO FATICA: {adv} (differenza: {abs(fatigue.get('fatigue_diff',0))} punti)")
                        logger.info(f"  Fatigue: {home}={h_fat.get('fatigue_score',0)} {away}={a_fat.get('fatigue_score',0)} adv={fatigue.get('advantage')}")
                except Exception as e:
                    logger.warning(f"Fatigue analysis error: {e}")

                # === LINE MOVEMENT ===
                try:
                    from db.database import get_line_movement
                    match_date_str = m_dict.get("commence_time", m_dict.get("match_date", ""))[:10]
                    lm_key = f"{home}_vs_{away}_{match_date_str}"
                    lm = get_line_movement(lm_key)
                    if lm and lm.get("snapshots", 0) >= 2:
                        op = lm["opening"]
                        cur = lm["current"]
                        mv = lm["movement"]
                        context_parts.append(f"\n📈 LINE MOVEMENT ({lm['bookmaker']}, {lm['snapshots']} rilevazioni):")
                        context_parts.append(f"  Apertura: 1={op['home']:.2f}  X={op['draw']:.2f}  2={op['away']:.2f}")
                        context_parts.append(f"  Attuale:  1={cur['home']:.2f}  X={cur['draw']:.2f}  2={cur['away']:.2f}")
                        context_parts.append(f"  Movimento: 1={mv['home']:+.3f}  X={mv['draw']:+.3f}  2={mv['away']:+.3f}")
                        if lm.get("signals"):
                            context_parts.append(f"  💰 Soldi su: {', '.join(lm['signals'])}")
                        if lm.get("steam_move"):
                            sm_info = lm["steam_move"]
                            context_parts.append(f"  🚨 STEAM MOVE: quota {sm_info['side']} {sm_info['direction']}{sm_info['delta']:.3f} in un singolo aggiornamento")
                        logger.info(f"  Line Movement: {lm['snapshots']} snapshots, signals={lm.get('signals')}")
                except Exception as e:
                    logger.warning(f"Line movement error: {e}")

            else:
                logger.warning(f"Team IDs NON trovati per {home} o {away}")

        except Exception as ex:
            logger.error(f"Context error: {ex}")
            import traceback
            traceback.print_exc()

        status = "UFFICIALI" if official_lineups else "PROBABILI"
        if official_lineups:
            context_parts.append(f"\nFORMAZIONE UFFICIALE {home}: " + ", ".join([f"{p['name']} (#{p.get('number', '?')})" for p in official_lineups["home"]]))
            context_parts.append(f"FORMAZIONE UFFICIALE {away}: " + ", ".join([f"{p['name']} (#{p.get('number', '?')})" for p in official_lineups["away"]]))

        prompt = f"""Analizza professionale {home} vs {away} ({league_name}). STATO FORMAZIONI: {status}.

DATI CONTESTO COMPLETI (basati su dati reali del database):
{chr(10).join(context_parts)}

REGOLE DI FORMATTAZIONE TASSATIVE (NON DEROGARE MAI):

1. ## 📖 1. ANALISI E MOTIVAZIONI
   Spiegazione verbosa dell'importanza del match, usando le STATS SQUADRA e BETTING dal contesto.
   Cita i numeri reali (V/P/S, gol fatti/subiti, Over 2.5%, GG%).

2. ## 📋 2. FORMAZIONI {status}
   Per OGNI giocatore della rosa titolare, COPIA INTEGRALMENTE la riga dal contesto.
   Ogni giocatore DEVE avere: Nome (Ruolo) [VOTO: XX] | Stagione: ... | Stats: ... | Impatto: ...
   NON semplificare, NON rimuovere dati. COPIA TUTTO cosi com'e nel contesto.
   Alla fine di ogni squadra scrivi la MEDIA VOTO dal contesto.
   - **NON METTERE MAI IL DIVISORE '---' TRA LE DUE SQUADRE.**
   - **METTI IL DIVISORE '---' SOLO ALLA FINE DI ENTRAMBE LE FORMAZIONI.**

3. ## 🎯 3. FOCUS TECNICO
   Per OGNI giocatore chiave (almeno 4 per squadra, NO PORTIERI), analizza:
   - Il suo VOTO e le sue stats avanzate (tiri/gara, passaggi chiave, dribbling, falli, recuperi)
   - Il suo Impatto sulla vittoria (WR con/senza)
   - La sua Prob.GOL e Prob.AMMON dal contesto
   - Se e DIFFIDATO, evidenzialo

4. ## ⚠️ 4. ASSENZE PESANTI
   Elenca le assenze con il loro impatto numerico (gol persi, calo win rate).
   **INSERISCI UN DIVISORE '---' ALLA FINE DI QUESTA SEZIONE.**

5. ## ⚽ 5. PROBABILI MARCATORI
   STRUTTURA OBBLIGATORIA:
   🔥 TOP PICKS (2+2): elenca i 4 giocatori marcati 🔥 nel contesto. Sono la giocata principale.
   Per ognuno: gol stagionali, tiri/gara, astinenza, e perché è un pick forte.
   📋 BACKUP: elenca gli altri 4-6 dal contesto. Sono per giocata separata a quota più alta.
   Se un giocatore è 🎯RIGORISTA, segnalalo. Includi difensori specialisti calci piazzati.

6. ## 🟨 6. PROBABILI CARTELLINI
   STRUTTURA OBBLIGATORIA:
   🔥 TOP PICKS (2+2): elenca i 4 giocatori marcati 🔥 nel contesto. Sono la giocata principale.
   Per ognuno: ammonizioni stagionali, falli/gara, e perché è un pick forte.
   Se ci sono dati H2H (cartellini nei precedenti) o Arb (storico con l'arbitro), citali.
   📋 BACKUP: elenca gli altri 6-8 dal contesto. Sono per giocata separata.
   Segnala i DIFFIDATI con ⚠️. In derby/scontri diretti ci si aspetta più ammonizioni.

6b. ## 📺 6b. ANALISI VAR
   Se nel contesto ci sono dati VAR sull'arbitro, analizza:
   - Frequenza VAR dell'arbitro (quante volte va al monitor per partita)
   - % di decisioni ribaltate dopo il VAR
   - Tipo di interventi VAR più frequenti (rigori, gol, rossi)
   - CONSIGLIO per la giocata "Arbitro va al VAR: Sì/No" con motivazione
   Se non ci sono dati VAR, scrivi "Dati VAR non disponibili per questo arbitro."

7. ## 💹 7. CONSIGLI BETTING
   Almeno 5 opzioni basate sui dati reali:
   - 1X2 con motivazione (cita MEDIA VOTO delle due squadre + stats)
   - Over/Under 2.5 con % reali di entrambe le squadre
   - GG/NG con % reali
   - Combo (es. 1+Over 2.5, con ragionamento)
   - Risultato esatto piu probabile (dal contesto)
   Per ogni consiglio, cita i numeri specifici che lo supportano.

8. ## 💎 8. VALUE BETS
   Se nel contesto ci sono VALUE BETS RILEVATE con EV > 5%, dedicagli una sezione separata:
   - Per ogni value bet: mostra mercato, probabilità modello, quota, bookmaker, e EV%
   - Spiega PERCHE il modello vede valore (es. "il modello assegna 42% all'Over 2.5 ma il book prezza 2.10 = implied 47%")
   - Classifica: EV 5-10% = 💎 Valore Moderato, EV 10-15% = 💎💎 Valore Alto, EV >15% = 💎💎💎 Valore Estremo
   - Se non ci sono value bets nel contesto, scrivi "Nessuna value bet rilevata per questa partita."

!!! REGOLA SUPREMA - LEGGERE BENE !!!:
- NELLA SEZIONE FORMAZIONI, COPIA INTEGRALMENTE OGNI RIGA DEL CONTESTO. NON SEMPLIFICARE.
- OGNI GIOCATORE DEVE AVERE: [VOTO], Stats avanzate, Impatto, Prob.GOL/AMMON se presenti.
- IL DIVISORE '---' VA SOLO DOPO LA FORMAZIONE AWAY E DOPO LE ASSENZE.
- COPIA IDENTICHE LE % DEI MARCATORI E CARTELLINI DAL CONTESTO. NON MODIFICARE MAI I NUMERI.
- NON ANALIZZARE MAI I PORTIERI NEL FOCUS TECNICO.
- USA SOLO I DATI REALI DEL CONTESTO, NON INVENTARE NULLA.
- ATTENZIONE: I gol, assist, ammonizioni nel contesto sono dati STAGIONALI REALI dal database.
  Se il contesto dice "Lautaro: 17 gol", scrivi ESATTAMENTE "17 gol". Non arrotondare, non modificare.
- Le probabilità marcatore e cartellino (es. "Prob.GOL: 28%") sono calcolate dal nostro modello.
  Riportale IDENTICHE. Non inventare probabilità per giocatori non presenti nel contesto.
- I RISULTATI ESATTI PROBABILI vengono dal modello Poisson/Dixon-Coles. Copiali come sono.
"""
        
        # Debug: salva contesto grezzo
        try:
            with open("last_context.txt", "w") as f:
                f.write("\n".join(context_parts))
            logger.info(f"💾 Contesto salvato in last_context.txt ({len(context_parts)} righe)")
        except: pass

        logger.info(f"🧠 Richiesta AI per {home}-{away}...")
        client = OpenAI(api_key=os.getenv("OPENROUTER_API_KEY"), base_url="https://openrouter.ai/api/v1")
        try:
            resp = client.chat.completions.create(model=os.getenv("AI_MODEL", "anthropic/claude-3.5-sonnet"), messages=[{"role": "system", "content": "Analista Senior."}, {"role": "user", "content": prompt}])
            ai_suggestion = resp.choices[0].message.content
            logger.info(f"✅ AI ha risposto per {home}-{away}")
        except Exception as e:
            logger.error(f"❌ AI Fallita: {e}")
            ai_suggestion = "Analisi non disponibile."

        alert_id = f"{m_dict['match_id']}_{'OFF' if official_lineups else 'PROB'}"
        if self.is_alert_sent(alert_id):
            logger.info(f"⏭️ Alert già inviato per {home}-{away} ({status})")
            return
        
        utc_d = datetime.fromisoformat(m_dict["match_date"].replace("Z", "+00:00"))
        rome_d = utc_d.astimezone(rome_tz).strftime("%d/%m %H:%M")
        
        try:
            with open("last_report.txt", "w") as f: f.write(ai_suggestion)
            logger.info(f"💾 Report scritto su last_report.txt")
        except Exception as e:
            logger.error(f"❌ Errore scrittura file: {e}")
        
        # Supporto multi-destinatario (separati da virgola)
        recipients = [e.strip() for e in self.alert_email.split(",") if e.strip()]
        if not recipients:
            logger.error("❌ Nessun destinatario email configurato")
            return

        # Recupera dati Doppio Tempo (HT/FT) per la email
        htft_data = None
        try:
            from scraper.halftime import HalfTimeAnalyzer
            fd_key = os.getenv("FOOTBALL_DATA_API_KEY", "")
            if fd_key and h_id and a_id:
                ha = HalfTimeAnalyzer(fd_key)
                ht_result = ha.analyze_league(league_key)
                for htm in ht_result.get("matches", []):
                    hn = clean_n(htm.get("home_team", ""))
                    an = clean_n(htm.get("away_team", ""))
                    if clean_n(home) in hn or hn in clean_n(home):
                        if clean_n(away) in an or an in clean_n(away):
                            htft_data = htm
                            break
                if htft_data:
                    logger.info(f"✅ Doppio Tempo data trovata per {home}-{away}")
        except Exception as e:
            logger.warning(f"⚠️ HT/FT data error: {e}")

        # Line Movement data for email
        line_movement_data = None
        try:
            from db.database import get_line_movement
            match_date_str = m_dict.get("match_date", m_dict.get("commence_time", ""))[:10]
            lm_key = f"{home}_vs_{away}_{match_date_str}"
            lm = get_line_movement(lm_key)
            if lm and lm.get("snapshots", 0) >= 2:
                line_movement_data = lm
        except Exception as e:
            logger.debug(f"Line movement for email: {e}")

        # ── Log predictions for backtesting ──
        try:
            from db.database import save_prediction_log
            match_date_str = m_dict.get("match_date", m_dict.get("commence_time", ""))[:10]
            pred_key = f"{home}_vs_{away}_{match_date_str}"
            save_prediction_log(
                match_key=pred_key,
                home_team=home,
                away_team=away,
                league=league_name,
                match_date=m_dict.get("match_date", ""),
                cs_data=cs_match,
                scorer_picks=scorer_picks,
                card_picks=card_picks,
            )
        except Exception as e:
            logger.warning(f"Prediction log error: {e}")

        match_info = {"home": home, "away": away, "league": league_name, "date": rome_d}
        logger.info(f"📧 Invio email a {len(recipients)} destinatari: {', '.join(recipients)}")
        all_sent = True
        for recipient in recipients:
            if self.mail.send_bet_alert(recipient, match_info, ai_suggestion, htft_data=htft_data, line_movement=line_movement_data):
                logger.info(f"  ✅ Inviato a {recipient}")
            else:
                logger.error(f"  ❌ Fallito per {recipient}")
                all_sent = False

        # Costruisci JSON del prompt AI per logging
        ai_prompt_json_str = None
        try:
            import json as _json
            ai_prompt_obj = {
                "model": os.getenv("AI_MODEL", "anthropic/claude-3.5-sonnet"),
                "system_message": "Analista Senior.",
                "prompt": prompt,
                "context_parts": context_parts,
                "match": {"home": home, "away": away, "league": league_name, "date": m_dict.get("match_date", "")},
                "lineups_status": status,
            }
            ai_prompt_json_str = _json.dumps(ai_prompt_obj, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"⚠️ Errore costruzione ai_prompt_json: {e}")

        if all_sent:
            self.log_alert_sent(alert_id, home, away, league_name, m_dict["match_date"], ai_suggestion, ai_prompt_json=ai_prompt_json_str)
            logger.info(f"✅ Alert inviato con successo per {home}-{away}")
        else:
            logger.warning(f"⚠️ Alert parzialmente inviato per {home}-{away}")

    def run(self):
        logger.info("🚀 Worker Avviato — Monitoraggio formazioni ufficiali")
        logger.info(f"   Finestra: {self.LINEUP_WINDOW_START}→{self.LINEUP_WINDOW_END} min prima del kickoff")
        logger.info(f"   Intervallo check: ogni {self.CHECK_INTERVAL}s")

        while True:
            try:
                # 1. Controlla se il worker è abilitato
                if not self._is_enabled():
                    logger.info("⏸️  Worker DISABILITATO — skip ciclo")
                    time.sleep(self.CHECK_INTERVAL)
                    continue

                # 2. Rileggi email destinatario (può cambiare dalla UI)
                alert_email = self._get_alert_email()

                # 3. Recupera match prossime 24h
                matches = self.get_upcoming_matches()

                if not matches:
                    logger.info("💤 Nessun match imminente — attendo...")
                    time.sleep(self.CHECK_INTERVAL)
                    continue

                rome_tz = pytz.timezone('Europe/Rome')
                now = datetime.now(timezone.utc)

                for m in matches:
                    kickoff = m["kickoff_utc"]
                    minutes_to = (kickoff - now).total_seconds() / 60
                    kickoff_rome = kickoff.astimezone(rome_tz).strftime("%H:%M")
                    label = f"{m['home']} vs {m['away']} ({kickoff_rome})"

                    # 4. Controlla se siamo nella finestra di monitoraggio
                    if not self._is_in_lineup_window(kickoff):
                        if minutes_to > self.LINEUP_WINDOW_START:
                            logger.info(f"⏳ {label} — {minutes_to:.0f} min al kickoff, fuori finestra")
                        elif minutes_to < self.LINEUP_WINDOW_END:
                            logger.info(f"🏁 {label} — partita già iniziata, skip")
                        continue

                    # 5. Controlla se alert già inviato (con formazioni UFFICIALI)
                    alert_id_off = f"{m['match_id']}_OFF"
                    if self.is_alert_sent(alert_id_off):
                        logger.info(f"✅ {label} — alert UFFICIALE già inviato, skip")
                        continue

                    # 6. Siamo in finestra! Cerca formazioni ufficiali
                    logger.info(f"🔍 {label} — {minutes_to:.0f} min al kickoff, cerco formazioni...")

                    # Prova a recuperare le formazioni ufficiali da Sportmonks
                    official_lineups = None
                    try:
                        sm_matches = self.sm.get_all_matches(league_keys=[m["league_key"]])
                        target_sm = next(
                            (sm for sm in sm_matches
                             if (m["home"].lower() in sm.home_team.lower() or sm.home_team.lower() in m["home"].lower())
                             and (m["away"].lower() in sm.away_team.lower() or sm.away_team.lower() in m["away"].lower())),
                            None
                        )
                        if target_sm:
                            lineups_data = self.sm.get_official_lineups(target_sm.id)
                            if lineups_data.get("home") and lineups_data.get("away"):
                                official_lineups = lineups_data
                                logger.info(f"📋 {label} — FORMAZIONI UFFICIALI TROVATE! Home={len(lineups_data['home'])}, Away={len(lineups_data['away'])}")
                    except Exception as e:
                        logger.warning(f"⚠️  Errore recupero formazioni per {label}: {e}")

                    if not official_lineups:
                        logger.info(f"⏳ {label} — formazioni non ancora disponibili, riprovo tra {self.CHECK_INTERVAL}s")
                        continue

                    # 6b. Salva lineup in cache per cartellini/marcatori
                    self._save_lineup_cache(m, official_lineups)

                    # 7. Formazioni trovate! Lancia analisi AI + invio email
                    logger.info(f"🚀 {label} — Avvio analisi completa + invio email a {alert_email}")
                    self.alert_email = alert_email  # aggiorna per trigger_ai_analysis
                    self.trigger_ai_analysis(m)

            except Exception as e:
                logger.error(f"❌ Errore nel ciclo worker: {e}")
                import traceback
                traceback.print_exc()

            time.sleep(self.CHECK_INTERVAL)

if __name__ == "__main__":
    BetAnalyzerWorker().run()
