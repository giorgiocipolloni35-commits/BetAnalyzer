"""
BetAnalyzer — Flask app principale
"""
import os
import re
import json
import logging
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path
from dotenv import load_dotenv
from flask import Flask, render_template, redirect, url_for, jsonify, request

load_dotenv()

# Configura logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler("betanalyzer.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

from db.database import init_db
init_db()

# ------------------------------------------------------------------ #
#  Config                                                              #
# ------------------------------------------------------------------ #
ODDS_API_KEY      = os.getenv("ODDS_API_KEY", "")
OPENROUTER_KEY    = os.getenv("OPENROUTER_API_KEY", "")
AI_MODEL          = os.getenv("AI_MODEL", "openai/gpt-4o-mini")
CACHE_MINUTES     = int(os.getenv("CACHE_MINUTES", 30))
SPORTMONKS_KEY    = os.getenv("SPORTMONKS_API_KEY", "")
FOOTBALL_DATA_KEY = os.getenv("FOOTBALL_DATA_API_KEY", "")


# ------------------------------------------------------------------ #
#  Data freshness helper                                               #
# ------------------------------------------------------------------ #
def get_data_freshness(*sources):
    """Return freshness info for template badge.

    Each source is a tuple: (label, path_or_type, detail)
    - label: display name (e.g. "Partite", "Giocatori")
    - path_or_type: file path or "db:table_name"
    - detail: extra info (e.g. "Football-Data API", "Sportmonks DB")
    """
    items = []
    for label, path_or_type, detail in sources:
        ts = None
        if path_or_type.startswith("db:"):
            # Get latest updated_at from a DB table
            table = path_or_type.split(":", 1)[1]
            try:
                import sqlite3
                db_path = os.path.join("data", "betanalyzer.db")
                if os.path.exists(db_path):
                    conn = sqlite3.connect(db_path)
                    row = conn.execute(f"SELECT MAX(updated_at) FROM {table}").fetchone()
                    conn.close()
                    if row and row[0]:
                        ts = row[0][:16].replace("T", " ")
            except Exception:
                pass
        elif os.path.exists(path_or_type):
            mtime = os.path.getmtime(path_or_type)
            ts = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")

        items.append({"label": label, "time": ts or "N/A", "source": detail})
    return items

# Stato globale (in-memory, per semplicità)
_state = {
    "matches": [],
    "loading": False,
    "error": None,
    "source": "sportmonks", # Sorgente predefinita
    "selected_leagues": [], # Tutti disabilitati all'avvio
    "last_update": None,
    "quota_usage":        {"remaining": "?", "used": "?"},
    "openrouter_balance": {"usage": None, "limit": None, "remaining": None},
    "progress":           {"current": 0, "total": 0, "match": ""},
}

_lock = threading.Lock()


# ------------------------------------------------------------------ #
#  Data loading                                                        #
# ------------------------------------------------------------------ #

def load_data(force: bool = False, source: str = "odds_api"):
    """Carica le quote e lancia l'analisi AI in background."""
    from ai.analyzer import analyze_all, get_openrouter_balance

    with _lock:
        if _state["loading"]:
            return
        _state["loading"] = True
        _state["error"]   = None
        
        # Cooldown per evitare spam di crediti
        now = datetime.now()
        last_upd = _state.get("last_update_dt")
        if not force and last_upd and (now - last_upd) < timedelta(minutes=15):
            logger.info("Uso dati in cache (cooldown 15 min attivo)")
            _state["loading"] = False
            return

        _state["matches"] = [] # Clear old matches to avoid confusion
        _state["source"]  = source
        _state["last_update_dt"] = now

    def _worker():
        try:
            if not OPENROUTER_KEY:
                raise ValueError("OPENROUTER_API_KEY non configurata nel file .env")

            logger.info(f"Fonte dati selezionata: {source}")

            # Recupera subito il saldo OpenRouter
            balance = get_openrouter_balance(OPENROUTER_KEY)
            with _lock:
                _state["openrouter_balance"] = balance
            logger.info(f"Saldo OpenRouter: usato=${balance['usage']} residuo=${balance['remaining']}")

            # ---- Recupero quote in base alla fonte scelta ----
            selected_leagues = _state.get("selected_leagues", [])
            if not selected_leagues:
                # Default per evitare dashboard vuota
                selected_leagues = ["italy_serie_a", "england_premier_league", "spain_la_liga", "germany_bundesliga", "france_ligue_1"]
                with _lock:
                    _state["selected_leagues"] = selected_leagues
            
            if force:
                cache_dir = Path(__file__).parent / "cache"
                for f in cache_dir.glob("*.json"):
                    f.unlink(missing_ok=True)
                logger.info("Cache svuotata")

            if source == "sportmonks":
                if not SPORTMONKS_KEY:
                    raise ValueError("SPORTMONKS_API_KEY non configurata nel file .env")
                from scraper.sportmonks import SportmonksClient
                client = SportmonksClient(SPORTMONKS_KEY, cache_minutes=CACHE_MINUTES)
                matches = client.get_all_matches(league_keys=selected_leagues)

                # Modalità ibrida: arricchisci con quote multi-bookmaker da Odds API
                if ODDS_API_KEY:
                    try:
                        from scraper.odds_api import OddsAPIClient
                        from scraper.hybrid import merge_odds_into_matches
                        odds_client = OddsAPIClient(ODDS_API_KEY, cache_minutes=CACHE_MINUTES)
                        odds_matches = odds_client.get_all_matches(league_keys=selected_leagues)
                        if odds_matches:
                            merge_odds_into_matches(matches, odds_matches)
                            logger.info(f"Modalità ibrida: {len(odds_matches)} partite Odds API merged")
                        else:
                            logger.warning("Odds API non ha restituito partite (quota esaurita?) — uso solo Sportmonks")
                        _state["quota_usage"] = odds_client.get_quota_usage()
                    except Exception as e:
                        logger.warning(f"Odds API fallita ({e}) — uso solo dati Sportmonks")
                else:
                    logger.info("Odds API key non configurata — uso solo quote Sportmonks")

            else:  # odds_api (default)
                from scraper.odds_api import OddsAPIClient
                matches = []
                if ODDS_API_KEY:
                    try:
                        client = OddsAPIClient(ODDS_API_KEY, cache_minutes=CACHE_MINUTES)
                        matches = client.get_all_matches(league_keys=selected_leagues)
                        _state["quota_usage"] = client.get_quota_usage()
                    except Exception as e:
                        logger.warning(f"Odds API fallita ({e}) — provo API-Football")

                # Se Odds API non ha dato nulla, usa API-Football come source primaria
                if not matches:
                    apifb_key_primary = os.getenv("API_FOOTBALL_KEY", "")
                    if apifb_key_primary:
                        try:
                            from scraper.api_football import APIFootballClient
                            from datetime import datetime as dt_p
                            afb = APIFootballClient(apifb_key_primary)
                            today_str = dt_p.now().strftime("%Y-%m-%d")

                            # 1) Get odds by fixture_id
                            fixtures_odds = afb.get_fixtures_with_odds(today_str)
                            # 2) Get fixture details (team names, times)
                            fixture_mapping = afb.get_fixture_mapping(today_str)
                            # Invert mapping: fixture_id → (home, away)
                            fid_to_teams = {}
                            for (h, a), fid in fixture_mapping.items():
                                fid_to_teams[fid] = (h, a)

                            if fixtures_odds:
                                from models.match import Match
                                for fx in fixtures_odds:
                                    fid = fx["fixture_id"]
                                    teams = fid_to_teams.get(fid)
                                    if not teams:
                                        continue
                                    m = Match(
                                        id=str(fid),
                                        home_team=teams[0].title(),
                                        away_team=teams[1].title(),
                                        league=fx.get("league_name", "?"),
                                        commence_time=today_str + "T15:00:00Z",
                                        odds=fx.get("odds", []),
                                    )
                                    matches.append(m)
                                logger.info(f"🏈 API-Football come source primaria: {len(matches)} partite con quote")
                        except Exception as e:
                            logger.warning(f"API-Football primary fallback error: {e}")
                    if not matches and not ODDS_API_KEY:
                        raise ValueError("Nessuna API quote configurata (ODDS_API_KEY o API_FOOTBALL_KEY)")

            if hasattr(client, 'get_quota_usage'):
                sm_quota = client.get_quota_usage()
                if "quota_usage" not in _state or _state["quota_usage"].get("remaining") == "?":
                    _state["quota_usage"] = sm_quota

            # === FALLBACK API-FOOTBALL: arricchisci match senza odds ===
            apifb_key = os.getenv("API_FOOTBALL_KEY", "")
            matches_without_odds = [m for m in matches if not m.odds]
            if apifb_key and matches_without_odds:
                try:
                    from scraper.api_football import merge_apifootball_odds
                    from datetime import datetime as dt_util
                    # Determina le date delle partite
                    dates_seen = set()
                    for m in matches_without_odds:
                        if m.commence_time:
                            try:
                                ct = m.commence_time.replace("Z", "+00:00")
                                match_date = dt_util.fromisoformat(ct).strftime("%Y-%m-%d")
                                dates_seen.add(match_date)
                            except Exception:
                                pass
                    if not dates_seen:
                        dates_seen.add(dt_util.now().strftime("%Y-%m-%d"))

                    total_enriched = 0
                    for d in sorted(dates_seen):
                        enriched = merge_apifootball_odds(matches, d, apifb_key)
                        total_enriched += enriched

                    if total_enriched:
                        logger.info(f"🏈 API-Football fallback: {total_enriched} match arricchiti con quote")
                    else:
                        logger.info("API-Football: nessuna quote aggiuntiva trovata")
                except Exception as e:
                    logger.warning(f"API-Football fallback error: {e}")

            logger.info(f"Totale partite recuperate: {len(matches)}")

            if not matches:
                msg = "Nessuna partita trovata per i criteri selezionati."
                if source == "sportmonks":
                    msg += " Verifica che i campionati siano inclusi nel tuo piano Sportmonks (es. Serie A non è nel Free Plan)."
                else:
                    msg += " Prova a selezionare altri campionati o a svuotare la cache."
                with _lock:
                    _state["error"] = msg
                return

            logger.info("Inizio analisi AI...")
            _state["progress"] = {"current": 0, "total": len(matches), "match": ""}

            def _on_progress(current, total, match_name):
                _state["progress"] = {"current": current, "total": total, "match": match_name}

            # Passa client per classifica e forma recente
            sm_client = None
            fd_client = None
            if SPORTMONKS_KEY:
                from scraper.sportmonks import SportmonksClient
                sm_client = SportmonksClient(SPORTMONKS_KEY, cache_minutes=CACHE_MINUTES)
            if FOOTBALL_DATA_KEY:
                from scraper.football_data import FootballDataClient
                fd_client = FootballDataClient(FOOTBALL_DATA_KEY)

            matches = analyze_all(matches, OPENROUTER_KEY, AI_MODEL,
                                  on_progress=_on_progress,
                                  sportmonks_client=sm_client,
                                  football_data_client=fd_client)

            # Recupera saldo OpenRouter
            balance = get_openrouter_balance(OPENROUTER_KEY)
            logger.info(f"Saldo OpenRouter: usato=${balance['usage']} residuo=${balance['remaining']}")

            # Salva scansione nel DB
            from db.database import save_scan
            scan_id = save_scan(source, selected_leagues, matches)
            logger.info(f"Scansione salvata nel DB con ID #{scan_id}")

            with _lock:
                _state["matches"]            = matches
                _state["last_update"]        = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
                _state["error"]              = None
                _state["openrouter_balance"] = balance
                _state["progress"]           = {"current": 0, "total": 0, "match": ""}

            logger.info("Aggiornamento completato.")

        except Exception as e:
            logger.error(f"Errore load_data: {e}")
            with _lock:
                _state["error"] = str(e)
        finally:
            with _lock:
                _state["loading"] = False

    t = threading.Thread(target=_worker, daemon=True)
    t.start()


# ------------------------------------------------------------------ #
#  Jinja2 filters                                                      #
# ------------------------------------------------------------------ #

@app.template_filter("format_date")
def format_date(iso_str: str) -> str:
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.strftime("%d/%m %H:%M")
    except Exception:
        return iso_str


@app.template_filter("stars")
def stars_filter(rating: float) -> str:
    # Trasformiamo un rating 0-10 in una percentuale 0-100% per 5 stelle
    percentage = (rating / 10) * 100
    return f"""
    <div class="star-rating-wrapper" title="Rating: {rating}/10">
        <div class="star-rating-outer">
            <div class="star-rating-inner" style="width: {percentage}%"></div>
        </div>
    </div>
    """


# ------------------------------------------------------------------ #
#  Routes                                                              #
# ------------------------------------------------------------------ #

@app.route("/")
def index():
    league_filter = request.args.get("league", "all")
    only_play     = request.args.get("play", "0") == "1"

    matches = _state["matches"]

    if league_filter != "all":
        matches = [m for m in matches if m.league == league_filter]

    if only_play:
        matches = [m for m in matches if any(r.play for r in m.recommendations)]

    # Ordina: prima per data (più vicine in alto), poi per value_rating
    matches.sort(
        key=lambda m: (
            m.commence_time or "9999",
            -(1 if any(r.play for r in m.recommendations) else 0),
            -(m.recommendations[0].value_rating if m.recommendations else 0),
        )
    )

    leagues = ["Serie A", "Premier League", "La Liga", "Bundesliga", "Ligue 1", "Superliga", "Premiership"]

    # Conta partite per campionato (tutte, non filtrate)
    from collections import Counter
    all_matches = _state["matches"]
    league_counts = Counter(m.league for m in all_matches)

    # Se nessuna partita in memoria, leggi conteggi dalla cache su disco
    if not league_counts:
        try:
            import json
            cache_file = Path(__file__).parent / "cache" / "sportmonks_matches.json"
            if cache_file.exists():
                with open(cache_file) as f:
                    cached = json.load(f)
                league_counts = Counter(m.get("league", "") for m in cached)
        except Exception:
            pass

    from db.database import get_worker_setting
    worker_enabled = get_worker_setting("worker_enabled", "on")

    # Ultimo aggiornamento dati (dai log launchd)
    last_sync = _get_last_sync_info()

    freshness = get_data_freshness(
        ("Quote", "cache/sportmonks_matches.json", "Sportmonks API / Worker"),
        ("Partite", "data/penalties/SA_matches.json", "Football-Data API / Precache"),
        ("Giocatori", "db:player_stats_cache", "Sportmonks DB / Nightly Sync"),
    )
    return render_template(
        "index.html",
        matches=matches,
        state=_state,
        league_filter=league_filter,
        only_play=only_play,
        leagues=leagues,
        league_counts=league_counts,
        last_sync=last_sync,
        worker_enabled=worker_enabled,
        freshness=freshness
    )


@app.route("/match/<match_id>")
def match_detail(match_id: str):
    history_home = []
    history_away = []
    standings = []

    match = next((m for m in _state["matches"] if m.id == match_id), None)
    if not match:
        return redirect(url_for("index"))

    # Recupera dati extra da Sportmonks (se ha IDs)
    h2h_results = []
    h2h_stats = {}
    if SPORTMONKS_KEY and (match.home_id or match.away_id):
        from scraper.sportmonks import SportmonksClient
        client = SportmonksClient(SPORTMONKS_KEY)
        if match.home_id:
            history_home = client.get_last_results(match.home_id)
        if match.away_id:
            history_away = client.get_last_results(match.away_id)
        if match.league_id:
            standings = client.get_standings(match.league_id, match.season_id)
        # Head-to-Head
        if match.home_id and match.away_id:
            h2h_results = client.get_head_to_head(match.home_id, match.away_id, limit=10)
            if h2h_results:
                h_wins, a_wins, draws, total_goals = 0, 0, 0, 0
                for h in h2h_results:
                    hg, ag = h["home_goals"], h["away_goals"]
                    total_goals += hg + ag
                    if hg > ag:
                        if match.home_team.lower() in h["home"].lower() or h["home"].lower() in match.home_team.lower():
                            h_wins += 1
                        else:
                            a_wins += 1
                    elif ag > hg:
                        if match.away_team.lower() in h["away"].lower() or h["away"].lower() in match.away_team.lower():
                            a_wins += 1
                        else:
                            h_wins += 1
                    else:
                        draws += 1
                h2h_stats = {
                    "home_wins": h_wins,
                    "away_wins": a_wins,
                    "draws": draws,
                    "avg_goals": round(total_goals / len(h2h_results), 1),
                }

    # Fallback: Football-Data.org (per Odds API source senza Sportmonks IDs)
    if FOOTBALL_DATA_KEY and not history_home and not history_away:
        from scraper.football_data import FootballDataClient
        fd = FootballDataClient(FOOTBALL_DATA_KEY)
        league_key = _find_league_key(match.league)
        if league_key:
            standings = fd.get_standings(league_key)
            history_home = fd.get_team_form(match.home_team, league_key)
            history_away = fd.get_team_form(match.away_team, league_key)

    # Tag le squadre del match nella classifica con fuzzy matching
    for s in standings:
        s["is_match_team"] = (
            _fuzzy_team_match(s.get("team_name", ""), match.home_team) or
            _fuzzy_team_match(s.get("team_name", ""), match.away_team) or
            (s.get("team_id") and (s["team_id"] == match.home_id or s["team_id"] == match.away_id))
        )

    return render_template(
        "match_detail.html",
        match=match,
        state=_state,
        history_home=history_home,
        history_away=history_away,
        standings=standings,
        h2h_results=h2h_results,
        h2h_stats=h2h_stats,
    )


def _get_last_sync_info() -> dict:
    """Legge i log launchd per determinare quando i dati sono stati aggiornati l'ultima volta."""
    import re
    from datetime import datetime

    base = Path(__file__).parent
    result = {"precache": None, "playersync": None}

    # Precache: cerca "Pre-cache completato: YYYY-MM-DD HH:MM"
    precache_err = base / "launchd_nightly_err.log"
    if precache_err.exists():
        try:
            with open(precache_err, "r") as f:
                for line in f:
                    m = re.search(r"Pre-cache completato:\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})", line)
                    if m:
                        result["precache"] = m.group(1)
        except Exception:
            pass

    # Player sync: cerca "SINCRONIZZAZIONE TOTALE COMPLETATA"
    sync_err = base / "launchd_playersync_err.log"
    if sync_err.exists():
        try:
            lines = []
            with open(sync_err, "r") as f:
                for line in f:
                    lines.append(line)
            # Trova l'ultima riga con timestamp prima del marker di completamento
            for i, line in enumerate(lines):
                if "SINCRONIZZAZIONE TOTALE COMPLETATA" in line:
                    ts_match = re.search(r"(\d{2}:\d{2}:\d{2})", line)
                    if ts_match:
                        # Prendi la data dal file modification time
                        from os.path import getmtime
                        mod_date = datetime.fromtimestamp(getmtime(sync_err)).strftime("%Y-%m-%d")
                        result["playersync"] = f"{mod_date} {ts_match.group(1)[:5]}"
        except Exception:
            pass

    return result


def _normalize_name(name: str) -> str:
    import re
    n = name.lower().strip()
    n = re.sub(r'\b(fc|afc|sc|ssc|ac|as|ss|us|acf|cfc|bc|calcio|1907|1909|1913)\b', '', n)
    n = re.sub(r'\s+', ' ', n).strip()
    return n


def _fuzzy_team_match(name_a: str, name_b: str) -> bool:
    na = _normalize_name(name_a)
    nb = _normalize_name(name_b)
    if not na or not nb:
        return False
    if na in nb or nb in na:
        return True
    from difflib import SequenceMatcher
    return SequenceMatcher(None, na, nb).ratio() > 0.6


def _find_league_key(league_name: str) -> str | None:
    mapping = {
        "Serie A": "italy_serie_a",
        "Premier League": "england_premier_league",
        "La Liga": "spain_la_liga",
        "Bundesliga": "germany_bundesliga",
        "Ligue 1": "france_ligue_1",
        "Superliga": "denmark_superliga",
        "Premiership": "scotland_premiership",
    }
    return mapping.get(league_name)


@app.route("/refresh", methods=["POST"])
def refresh():
    source = request.form.get("source", "odds_api")
    leagues = request.form.getlist("leagues") # Prende tutti i checkbox selezionati
    force = request.form.get("force", "0") == "1"
    
    with _lock:
        _state["source"] = source
        if leagues:
            _state["selected_leagues"] = leagues
            
    load_data(force=force, source=source)
    return redirect(url_for("index"))


@app.route("/api/status")
def api_status():
    prog = _state["progress"]
    pct  = int(prog["current"] / prog["total"] * 100) if prog["total"] > 0 else 0
    return jsonify({
        "loading":     _state["loading"],
        "last_update": _state["last_update"],
        "error":       _state["error"],
        "matches":     len(_state["matches"]),
        "quota_usage": _state["quota_usage"],
        "progress": {
            "current":    prog["current"],
            "total":      prog["total"],
            "match":      prog["match"],
            "percent":    pct,
        },
    })


@app.route("/api/odds")
def api_odds():
    """Ritorna tutte le quote in JSON (utile per debug)."""
    result = []
    for m in _state["matches"]:
        result.append({
            "id":         m.id,
            "home":       m.home_team,
            "away":       m.away_team,
            "league":     m.league,
            "date":       m.commence_time,
            "bookmakers": len(m.odds),
            "best_home":  m.best_home[0],
            "best_draw":  m.best_draw[0],
            "best_away":  m.best_away[0],
            "recommendation": {
                "play":         m.recommendation.play,
                "market":       m.recommendation.market,
                "confidence":   m.recommendation.confidence,
                "value_rating": m.recommendation.value_rating,
            } if m.recommendation else None,
        })
    return jsonify(result)


# ------------------------------------------------------------------ #
#  Avvio                                                               #
# ------------------------------------------------------------------ #

@app.route("/api/match/<match_id>/deep-analysis")
def api_deep_analysis(match_id):
    """Endpoint per l'analisi profonda con dati reali."""
    m = next((m for m in _state["matches"] if m.id == match_id), None)

    if not m:
        return jsonify({"success": False, "error": "Partita non trovata o cache scaduta"})

    from openai import OpenAI
    from ai.analyzer import deep_analyze_match, _compute_market_stats
    client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=OPENROUTER_KEY)

    # Raccogli tutti i dati reali disponibili
    context_parts = []

    # Quote e analisi quantitativa
    market_stats = _compute_market_stats(m)
    if market_stats:
        context_parts.append("ANALISI QUOTE:")
        for mg in market_stats:
            context_parts.append(f"  {mg['group']} (margine book: {mg['margin_pct']}%)")
            for opt in mg["options"]:
                context_parts.append(f"    {opt['market']}: {opt['odds']:.2f} (prob.implicita {opt['implied_prob']}%)")

    # Assenti
    if m.absentees:
        context_parts.append("\nGIOCATORI ASSENTI:")
        for a in m.absentees:
            team_name = m.home_team if a['team'] == 'home' else m.away_team
            context_parts.append(f"  - {team_name}: {a['player']} ({a['reason']})")

    # Classifica e forma
    history_home = []
    history_away = []
    standings = []
    h2h_results = []
    if SPORTMONKS_KEY and (m.home_id or m.away_id):
        from scraper.sportmonks import SportmonksClient
        sm = SportmonksClient(SPORTMONKS_KEY)
        if m.home_id:
            history_home = sm.get_last_results(m.home_id, limit=5)
        if m.away_id:
            history_away = sm.get_last_results(m.away_id, limit=5)
        if m.league_id:
            standings = sm.get_standings(m.league_id, m.season_id)
        # Head-to-Head
        if m.home_id and m.away_id:
            h2h_results = sm.get_head_to_head(m.home_id, m.away_id, limit=10)

    if FOOTBALL_DATA_KEY and not history_home and not history_away:
        from scraper.football_data import FootballDataClient
        fd = FootballDataClient(FOOTBALL_DATA_KEY)
        league_key = _find_league_key(m.league)
        if league_key:
            standings = fd.get_standings(league_key)
            history_home = fd.get_team_form(m.home_team, league_key, limit=5)
            history_away = fd.get_team_form(m.away_team, league_key, limit=5)

    if standings:
        context_parts.append("\nCLASSIFICA:")
        for s in standings:
            name = s.get("team_name", "?")
            if name and (name.lower() in m.home_team.lower() or m.home_team.lower() in name.lower()
                         or name.lower() in m.away_team.lower() or m.away_team.lower() in name.lower()):
                context_parts.append(f"  {name}: {s['position']}° | {s['points']}pt | {s['played']}g | diff.reti {s['goals_diff']:+d}")

        # Match Importance Engine
        from logic.match_importance import calculate_match_importance
        importance = calculate_match_importance(m.home_team, m.away_team, m.league, standings)
        if importance.get("narrative"):
            context_parts.append("\n" + importance["narrative"])

    if history_home:
        results = ", ".join([f"{r['text']} ({r['outcome']})" for r in history_home])
        context_parts.append(f"\nFORMA {m.home_team} (ultime 5): {results}")
    if history_away:
        results = ", ".join([f"{r['text']} ({r['outcome']})" for r in history_away])
        context_parts.append(f"FORMA {m.away_team} (ultime 5): {results}")

    # Head-to-Head
    if h2h_results:
        context_parts.append(f"\nSCONTRI DIRETTI ({len(h2h_results)} partite):")
        h_wins, a_wins, draws = 0, 0, 0
        total_goals = 0
        for h in h2h_results:
            hg, ag = h["home_goals"], h["away_goals"]
            total_goals += hg + ag
            context_parts.append(f"  {h['date']} | {h['home']} {h['score']} {h['away']}")
            # Determina chi ha vinto in base ai nomi
            if hg > ag:
                if m.home_team.lower() in h["home"].lower() or h["home"].lower() in m.home_team.lower():
                    h_wins += 1
                else:
                    a_wins += 1
            elif ag > hg:
                if m.away_team.lower() in h["away"].lower() or h["away"].lower() in m.away_team.lower():
                    a_wins += 1
                else:
                    h_wins += 1
            else:
                draws += 1
        avg_goals = round(total_goals / len(h2h_results), 1) if h2h_results else 0
        context_parts.append(f"  BILANCIO: {m.home_team} {h_wins}V-{draws}P-{a_wins}S | Media gol: {avg_goals}/partita")

    # Raccomandazioni già calcolate
    if m.recommendations:
        context_parts.append("\nRACCOMANDAZIONI GIÀ CALCOLATE:")
        for r in m.recommendations:
            label = "GIOCA" if r.play else "SKIP"
            context_parts.append(f"  {r.market}: {label} @ {r.odds_value:.2f} ({r.confidence})")

    # ------------------------------------------------------------------ #
    #  Sportmonks + Football-Data context (team stats, leaders, etc.)     #
    # ------------------------------------------------------------------ #
    league_key = _find_league_key(m.league)
    if league_key and FOOTBALL_DATA_KEY:
        try:
            codes = {
                "italy_serie_a": "SA", "england_premier_league": "PL",
                "spain_la_liga": "PD", "germany_bundesliga": "BL1",
                "france_ligue_1": "FL1", "netherlands_eredivisie": "DED",
                "champions_league": "CL", "england_championship": "ELC",
                "portugal_primeira_liga": "PPL", "denmark_superliga": "DSU",
                "scotland_premiership": "SPL",
            }
            l_code = codes.get(league_key, "SA")

            from scraper.penalties import PenaltyAnalyzer
            pa = PenaltyAnalyzer(FOOTBALL_DATA_KEY)
            fd_standings = pa._get_standings(l_code)

            def clean_n(n):
                return n.lower().replace("club ", "").replace("ca ", "").replace("cf ", "").replace("de ", "").replace("fc ", "").strip()

            h_id = next((s["team_id"] for s in fd_standings if clean_n(m.home_team) in clean_n(s["name"]) or clean_n(s["name"]) in clean_n(m.home_team)), None)
            a_id = next((s["team_id"] for s in fd_standings if clean_n(m.away_team) in clean_n(s["name"]) or clean_n(s["name"]) in clean_n(m.away_team)), None)

            if h_id and a_id:
                from logic.roster import RosterManager
                rm = RosterManager(FOOTBALL_DATA_KEY)

                h_players, h_team_stats, h_leaders = rm.get_roster_with_stats(h_id, l_code, league_key)
                a_players, a_team_stats, a_leaders = rm.get_roster_with_stats(a_id, l_code, league_key)

                # Betting stats
                h_betting = rm.get_team_betting_stats(h_id, l_code)
                a_betting = rm.get_team_betting_stats(a_id, l_code)

                # Marcatori & Cartellini
                from scraper.scorers import ScorerAnalyzer
                from scraper.cards import CardAnalyzer
                sa_inst = ScorerAnalyzer(FOOTBALL_DATA_KEY)
                ca_inst = CardAnalyzer(FOOTBALL_DATA_KEY)
                s_data = sa_inst.analyze_league(league_key)
                c_data = ca_inst.analyze_league(league_key)
                m_s = next((x for x in s_data.get("matches", []) if x["home_id"] == h_id and x["away_id"] == a_id), None)
                m_c = next((x for x in c_data.get("matches", []) if x["home_id"] == h_id and x["away_id"] == a_id), None)
                scorer_picks = [p for p in (m_s or {}).get("top_picks", []) if p.get("position", "").lower() not in ["goalkeeper", "portiere", "gk"]]
                card_picks = [p for p in (m_c or {}).get("top_picks", []) if p.get("position", "").lower() not in ["goalkeeper", "portiere", "gk"]]

                # Arbitro
                if m_c:
                    ref_name = m_c.get("referee")
                    ref_cpm = m_c.get("referee_cards_pm")
                    ref_mult = m_c.get("referee_multiplier", 1.0)
                    ref_matches = m_c.get("referee_matches", 0)
                    avg_cpm = m_c.get("league_avg_cards_pm")
                    if ref_name:
                        ref_tendency = "SEVERO" if ref_mult > 1.15 else ("PERMISSIVO" if ref_mult < 0.85 else "NELLA MEDIA")
                        context_parts.append(f"\nARBITRO: {ref_name} | {ref_matches} partite | {ref_cpm} cartellini/gara (media lega: {avg_cpm}) | Tendenza: {ref_tendency} (x{ref_mult})")

                # Stats squadra
                if h_team_stats and h_team_stats.get("matches_played", 0) > 0:
                    context_parts.append(f"\nSTATS {m.home_team}: {h_team_stats['wins']}V-{h_team_stats['draws']}P-{h_team_stats['losses']}S | GolFatti:{h_team_stats['goals_for']} Subiti:{h_team_stats['goals_against']} | Modulo:{h_team_stats.get('most_used_formation','?')}")
                if a_team_stats and a_team_stats.get("matches_played", 0) > 0:
                    context_parts.append(f"STATS {m.away_team}: {a_team_stats['wins']}V-{a_team_stats['draws']}P-{a_team_stats['losses']}S | GolFatti:{a_team_stats['goals_for']} Subiti:{a_team_stats['goals_against']} | Modulo:{a_team_stats.get('most_used_formation','?')}")

                # Betting stats (totale + split casa/trasferta)
                def _fmt_betting(label, b, split_key=None):
                    line = f"BETTING {label}: O2.5={round(b['over25_pct'])}% GG={round(b['btts_pct'])}% CS={round(b.get('clean_sheet_pct',0))}% Ribaltone={round(b['ribaltone_si_pct'])}% AvgFatti={round(b['avg_scored'],1)} AvgSubiti={round(b['avg_conceded'],1)}"
                    if split_key and b.get(split_key):
                        s = b[split_key]
                        loc = "🏠 CASA" if split_key == "home" else "✈️ TRASFERTA"
                        line += f"\n  {loc}: O2.5={round(s['over25_pct'])}% GG={round(s['btts_pct'])}% CS={round(s.get('clean_sheet_pct',0))}% AvgFatti={round(s['avg_scored'],1)} AvgSubiti={round(s['avg_conceded'],1)} ({s['matches']}g: {round(s['win_pct'])}%V)"
                    return line

                if h_betting:
                    context_parts.append("\n" + _fmt_betting(m.home_team, h_betting, "home"))
                if a_betting:
                    context_parts.append(_fmt_betting(m.away_team, a_betting, "away"))

                # Risultato esatto
                if h_betting and a_betting:
                    top_scores = rm.predict_correct_score(h_betting, a_betting)
                    if top_scores:
                        context_parts.append(f"RISULTATI ESATTI PROBABILI: {' | '.join([s['score'] for s in top_scores])}")

                # Leader squadra
                if h_leaders and a_leaders:
                    context_parts.append(f"\nLEADER {m.home_team}: Capocannoniere={h_leaders['top_scorer']['name']}({h_leaders['top_scorer']['val']}g) Assist={h_leaders['top_assistman']['name']}({h_leaders['top_assistman']['val']})")
                    context_parts.append(f"LEADER {m.away_team}: Capocannoniere={a_leaders['top_scorer']['name']}({a_leaders['top_scorer']['val']}g) Assist={a_leaders['top_assistman']['name']}({a_leaders['top_assistman']['val']})")

                # Top 11 probabili titolari (senza formazioni ufficiali)
                active_h = [p for p in h_players if p.get("status") not in ["Gone", "Injured", "Suspended"]]
                active_a = [p for p in a_players if p.get("status") not in ["Gone", "Injured", "Suspended"]]
                h_lineup = sorted(active_h, key=lambda x: x.get("appearances", 0), reverse=True)[:11]
                a_lineup = sorted(active_a, key=lambda x: x.get("appearances", 0), reverse=True)[:11]

                context_parts.append(f"\nPROBABILI TITOLARI {m.home_team}:")
                for p in h_lineup:
                    context_parts.append(f"  {p['name']} ({p.get('position','?')}) | {p.get('appearances',0)} presenze | {p.get('goals',0)}g {p.get('assists',0)}a")

                context_parts.append(f"\nPROBABILI TITOLARI {m.away_team}:")
                for p in a_lineup:
                    context_parts.append(f"  {p['name']} ({p.get('position','?')}) | {p.get('appearances',0)} presenze | {p.get('goals',0)}g {p.get('assists',0)}a")

                # Assenze pesanti
                h_absent = [p for p in h_players if p.get("status") in ["Injured", "Suspended"]]
                a_absent = [p for p in a_players if p.get("status") in ["Injured", "Suspended"]]
                if h_absent:
                    context_parts.append(f"\nASSENZE {m.home_team}:")
                    for p in h_absent:
                        context_parts.append(f"  - {p['name']} ({p['status']}: {p.get('status_detail','')}) | {p.get('goals',0)}g {p.get('assists',0)}a")
                if a_absent:
                    context_parts.append(f"\nASSENZE {m.away_team}:")
                    for p in a_absent:
                        context_parts.append(f"  - {p['name']} ({p['status']}: {p.get('status_detail','')}) | {p.get('goals',0)}g {p.get('assists',0)}a")

                # === WEATHER IMPACT ===
                weather_data = None
                try:
                    from logic.weather import get_venue_coords, get_weather_forecast, analyze_weather_impact
                    match_dt = None
                    try:
                        ct = m.commence_time or ""
                        if ct:
                            ct_clean = ct.replace("Z", "+00:00")
                            match_dt = datetime.fromisoformat(ct_clean)
                    except Exception:
                        pass
                    if SPORTMONKS_KEY and m.home_id:
                        from scraper.sportmonks import SportmonksClient
                        sm_weather = SportmonksClient(SPORTMONKS_KEY)
                        venue = get_venue_coords(sm_weather, str(m.home_id).replace("sm_", ""))
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
                    if SPORTMONKS_KEY:
                        try:
                            from scraper.sportmonks import SportmonksClient
                            sm_coach = SportmonksClient(SPORTMONKS_KEY)
                            if m.home_id:
                                h_coach = sm_coach.get_coach_info(str(m.home_id).replace("sm_", ""))
                            if m.away_id:
                                a_coach = sm_coach.get_coach_info(str(m.away_id).replace("sm_", ""))
                        except Exception as e:
                            logger.warning(f"Coach info error: {e}")

                    news_ctx = build_news_context(
                        home_team=m.home_team,
                        away_team=m.away_team,
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

                # Top marcatori e cartellini probabili (con note meteo)
                if scorer_picks:
                    context_parts.append("\nTOP 10 PROBABILI MARCATORI:")
                    for i, p in enumerate(scorer_picks[:10], 1):
                        context_parts.append(f"  {i}. {p['player']} ({p['team']}): {p['probability']}% | {p.get('goals',0)}g in {p.get('matches',0)} partite | Astinenza:{p.get('drought',0)} giornate")
                    if weather_data and weather_data.get("severity") not in ("none", None):
                        w_notes = weather_data.get("betting_impact", {})
                        if w_notes.get("marcatori"):
                            context_parts.append(f"  ⛅ NOTA METEO MARCATORI: {w_notes['marcatori']}")
                        if w_notes.get("over_under"):
                            context_parts.append(f"  ⛅ NOTA METEO GOL: {w_notes['over_under']}")

                if card_picks:
                    context_parts.append("\nTOP 10 PROBABILI AMMONITI:")
                    for i, p in enumerate(card_picks[:10], 1):
                        diff_str = " DIFFIDATO" if p.get("diffidato") else ""
                        context_parts.append(f"  {i}. {p['player']} ({p['team']}): {p['probability']}% | {p.get('yellows',0)} amm in {p.get('matches',0)} partite{diff_str}")
                    if weather_data and weather_data.get("severity") not in ("none", None):
                        w_notes = weather_data.get("betting_impact", {})
                        if w_notes.get("cartellini"):
                            context_parts.append(f"  ⛅ NOTA METEO CARTELLINI: {w_notes['cartellini']}")

                # Sportmonks DB advanced stats (tiri, dribbling, passaggi, etc.)
                try:
                    import sqlite3
                    db_path = Path(__file__).parent / "data" / "betanalyzer.db"
                    if db_path.exists():
                        conn = sqlite3.connect(str(db_path))
                        conn.row_factory = sqlite3.Row
                        cur = conn.cursor()
                        cur.execute("""
                            SELECT pi.team_name,
                                   SUM(JSON_EXTRACT(ps.stats_json, '$.shots.shots_total')) as shots_total,
                                   SUM(JSON_EXTRACT(ps.stats_json, '$.goals.scored')) as goals,
                                   SUM(JSON_EXTRACT(ps.stats_json, '$.dribbles.success')) as dribbles_ok,
                                   SUM(JSON_EXTRACT(ps.stats_json, '$.passing.accuracy')) as pass_acc_sum,
                                   SUM(JSON_EXTRACT(ps.stats_json, '$.other.assists')) as assists,
                                   SUM(JSON_EXTRACT(ps.stats_json, '$.fouls.drawn')) as fouls_drawn,
                                   SUM(JSON_EXTRACT(ps.stats_json, '$.cards.yellowcards')) as yellows,
                                   COUNT(DISTINCT pi.player_id) as player_count
                            FROM player_info pi
                            JOIN player_stats_cache ps ON pi.player_id = ps.player_id
                            GROUP BY pi.team_name
                        """)
                        team_stats_db = {}
                        for row in cur.fetchall():
                            team_stats_db[row["team_name"].lower()] = dict(row)
                        conn.close()

                        for team_name in [m.home_team, m.away_team]:
                            tn_lower = team_name.lower()
                            ts = team_stats_db.get(tn_lower)
                            if not ts:
                                ts = next((v for k, v in team_stats_db.items() if k in tn_lower or tn_lower in k), None)
                            if ts and ts.get("player_count", 0) > 0:
                                pc = ts["player_count"]
                                context_parts.append(f"\nSPORTMONKS DB {team_name}: Tiri={ts.get('shots_total',0)} Gol={ts.get('goals',0)} Dribbling={ts.get('dribbles_ok',0)} Assist={ts.get('assists',0)} FoulSubiti={ts.get('fouls_drawn',0)} Ammonizioni={ts.get('yellows',0)} ({pc} giocatori)")
                except Exception as e:
                    logger.warning(f"Sportmonks DB stats error: {e}")

                # === FATIGUE & CALENDAR ===
                try:
                    from scraper.fatigue import analyze_fatigue
                    fatigue = analyze_fatigue(
                        home_team_id=h_id, away_team_id=a_id,
                        match_date=m.commence_time or "",
                        api_key=FOOTBALL_DATA_KEY,
                        home_name=m.home_team, away_name=m.away_team,
                    )
                    if fatigue.get("insight"):
                        context_parts.append(f"\nFATIGUE & CALENDARIO: {fatigue['insight']}")
                        h_fat = fatigue.get("home", {})
                        a_fat = fatigue.get("away", {})
                        context_parts.append(f"  {m.home_team}: fatigue={h_fat.get('fatigue_score',0)}/100 riposo={h_fat.get('rest_days','?')}gg partite14gg={h_fat.get('matches_14d',0)}")
                        context_parts.append(f"  {m.away_team}: fatigue={a_fat.get('fatigue_score',0)}/100 riposo={a_fat.get('rest_days','?')}gg partite14gg={a_fat.get('matches_14d',0)}")
                        if fatigue.get("advantage") != "neutral":
                            adv = m.home_team if fatigue["advantage"] == "home" else m.away_team
                            context_parts.append(f"  ⚡ VANTAGGIO FATICA: {adv} (differenza: {abs(fatigue.get('fatigue_diff',0))} punti)")
                except Exception as e:
                    logger.warning(f"Fatigue analysis error: {e}")

                # === LINE MOVEMENT ===
                try:
                    from db.database import get_line_movement
                    match_date_str = (m.commence_time or "")[:10]
                    lm_key = f"{m.home_team}_vs_{m.away_team}_{match_date_str}"
                    lm = get_line_movement(lm_key)
                    if lm and lm.get("snapshots", 0) >= 2:
                        op = lm["opening"]
                        cur = lm["current"]
                        mv = lm["movement"]
                        context_parts.append(f"\nLINE MOVEMENT ({lm['bookmaker']}, {lm['snapshots']} rilevazioni):")
                        context_parts.append(f"  Apertura: 1={op['home']:.2f}  X={op['draw']:.2f}  2={op['away']:.2f}")
                        context_parts.append(f"  Attuale:  1={cur['home']:.2f}  X={cur['draw']:.2f}  2={cur['away']:.2f}")
                        context_parts.append(f"  Movimento: 1={mv['home']:+.3f}  X={mv['draw']:+.3f}  2={mv['away']:+.3f}")
                        if lm.get("signals"):
                            context_parts.append(f"  Soldi su: {', '.join(lm['signals'])}")
                        if lm.get("steam_move"):
                            sm_info = lm["steam_move"]
                            context_parts.append(f"  STEAM MOVE: quota {sm_info['side']} {sm_info['direction']}{sm_info['delta']:.3f}")
                except Exception as e:
                    logger.warning(f"Line movement error: {e}")

            else:
                logger.warning(f"Deep analysis: Team IDs non trovati per {m.home_team}/{m.away_team}")
        except Exception as ex:
            logger.error(f"Deep analysis context error: {ex}")
            import traceback
            traceback.print_exc()

    match_context = "\n".join(context_parts)
    analysis = deep_analyze_match(m, match_context, client, AI_MODEL)

    return jsonify({
        "success": True,
        "analysis": analysis
    })


@app.route("/players_explorer")
def players_explorer():
    from db.database import list_players_with_stats
    from scraper.penalties import LEAGUE_CODES
    
    page = request.args.get("page", 1, type=int)
    search = request.args.get("search", "")
    league = request.args.get("league", "")
    role = request.args.get("role", "")
    sort = request.args.get("sort", "rating")
    age_group = request.args.get("age_group", "")
    min_assists = request.args.get("min_assists", "")
    min_dribbles = request.args.get("min_dribbles", "")
    min_interceptions = request.args.get("min_interceptions", "")
    min_recoveries = request.args.get("min_recoveries", "")
    
    # Normalizzazione Lega per match con Database
    league_map = {
        "england_premier_league": "premier_league",
        "italy_serie_a": "serie_a",
        "spain_la_liga": "la_liga",
        "germany_bundesliga": "bundesliga",
        "france_ligue_1": "ligue_1",
        "netherlands_eredivisie": "eredivisie"
    }
    db_league = league_map.get(league, league)
    
    filters = {
        "search": search,
        "league": db_league,
        "role": role,
        "sort": sort,
        "age_group": age_group,
        "min_assists": min_assists,
        "min_dribbles": min_dribbles,
        "min_interceptions": min_interceptions,
        "min_recoveries": min_recoveries
    }
    
    per_page = 40
    players, total = list_players_with_stats(filters, page=page, per_page=per_page)
    
    total_pages = (total + per_page - 1) // per_page
    
    # Costruiamo i query params per la paginazione
    q_params = f"search={search}&league={league}&role={role}&sort={sort}&age_group={age_group}&min_assists={min_assists}&min_dribbles={min_dribbles}&min_interceptions={min_interceptions}&min_recoveries={min_recoveries}"
    
    leagues_list = [{"id": k, "name": k.replace("_", " ").title()} for k in LEAGUE_CODES.keys()]
    
    freshness = get_data_freshness(
        ("Giocatori", "db:player_info", "Sportmonks DB / Nightly Sync"),
        ("Statistiche", "db:player_stats_cache", "Sportmonks DB / Nightly Sync"),
    )
    return render_template("players_explorer.html",
                           players=players,
                           leagues=leagues_list,
                           filters=filters,
                           pagination={
                               "page": page,
                               "total_pages": total_pages,
                               "total_count": total,
                               "query_params": q_params
                           },
                           state=_state,
                           freshness=freshness)


@app.route("/api/player_compare")
def api_player_compare():
    """Return stats for two players for radar chart comparison."""
    import sqlite3 as _sq
    p1 = request.args.get("p1", type=int)
    p2 = request.args.get("p2", type=int)
    if not p1 or not p2:
        return jsonify({"error": "Servono p1 e p2 (player_id)"}), 400

    conn = _sq.connect("data/betanalyzer.db")
    conn.row_factory = _sq.Row

    def _load(pid):
        row = conn.execute("""
            SELECT pi.player_id, pi.name, pi.team_name, pi.position_id, psc.stats_json, psc.rating
            FROM player_info pi
            JOIN player_stats_cache psc ON pi.player_id = psc.player_id AND pi.team_id = psc.team_id
            WHERE pi.player_id = ?
            ORDER BY psc.season_id DESC LIMIT 1
        """, (pid,)).fetchone()
        if not row:
            return None
        s = json.loads(row["stats_json"]) if row["stats_json"] else {}
        apps = s.get("appearances", 1) or 1
        pos_map = {24: "GK", 25: "DEF", 26: "MID", 27: "ATT"}
        return {
            "player_id": row["player_id"],
            "name": row["name"],
            "team": row["team_name"],
            "position": pos_map.get(row["position_id"], "?"),
            "rating": row["rating"] or 0,
            "stats": {
                "Gol/G": round(s.get("goals", 0) / apps, 2),
                "Assist/G": round(s.get("assists", 0) / apps, 2),
                "Tiri/G": round(s.get("shots_on_target", 0) / apps, 2),
                "KeyPass/G": round(s.get("key_passes", 0) / apps, 2),
                "Dribbling/G": round(s.get("dribbles_success", 0) / apps, 2),
                "Recuperi/G": round((s.get("interceptions", 0) + s.get("tackles", 0)) / apps, 2),
                "Aerei/G": round(s.get("aerials_won", 0) / apps, 2),
                "Pass%": round(s.get("accurate_passes_pct", 0), 1),
                "BigChance/G": round(s.get("big_chances_created", 0) / apps, 2),
                "FalliSub/G": round(s.get("fouls_drawn", 0) / apps, 2),
            },
            "raw": {
                "appearances": s.get("appearances", 0),
                "goals": s.get("goals", 0),
                "assists": s.get("assists", 0),
                "shots_on_target": s.get("shots_on_target", 0),
                "key_passes": s.get("key_passes", 0),
                "dribbles_success": s.get("dribbles_success", 0),
                "interceptions": s.get("interceptions", 0),
                "tackles": s.get("tackles", 0),
                "aerials_won": s.get("aerials_won", 0),
                "accurate_passes_pct": s.get("accurate_passes_pct", 0),
                "big_chances_created": s.get("big_chances_created", 0),
                "fouls_drawn": s.get("fouls_drawn", 0),
                "fouls_committed": s.get("fouls_committed", 0),
            }
        }

    d1 = _load(p1)
    d2 = _load(p2)
    conn.close()

    if not d1 or not d2:
        return jsonify({"error": "Giocatore non trovato"}), 404

    return jsonify({"player1": d1, "player2": d2})


@app.route("/teams_explorer")
def teams_explorer():
    from db.database import list_team_stats
    from scraper.penalties import LEAGUE_CODES

    league = request.args.get("league", "")
    sort = request.args.get("sort", "avg_rating")

    league_map = {
        "england_premier_league": "premier_league",
        "italy_serie_a": "serie_a",
        "spain_la_liga": "la_liga",
        "germany_bundesliga": "bundesliga",
        "france_ligue_1": "ligue_1",
        "netherlands_eredivisie": "eredivisie"
    }
    db_league = league_map.get(league, league)

    filters = {"league": db_league} if db_league else {}
    teams = list_team_stats(filters=filters, sort=sort)

    leagues_list = [{"id": k, "name": k.replace("_", " ").title()} for k in LEAGUE_CODES.keys()]

    return render_template("teams_explorer.html",
                           teams=teams,
                           leagues=leagues_list,
                           filters={"league": league, "sort": sort},
                           state=_state)


@app.route("/scans")
def scans_page():
    from db.database import list_scans
    scans = list_scans()
    return render_template("scans.html", scans=scans, state=_state)


@app.route("/scans/<int:scan_id>")
def load_scan_route(scan_id):
    from db.database import load_scan, get_scan_info
    info = get_scan_info(scan_id)
    if not info:
        return redirect(url_for("scans_page"))

    matches = load_scan(scan_id)
    with _lock:
        _state["matches"] = matches
        _state["last_update"] = f"Scansione #{scan_id} del {info['timestamp'][:16].replace('T', ' ')}"
        _state["error"] = None
        _state["source"] = info["source"]

    return redirect(url_for("index"))


@app.route("/stats")
def stats_page():
    from db.database import get_stats
    stats = get_stats()
    return render_template("stats.html", stats=stats, state=_state)


@app.route("/api/settle", methods=["POST", "GET"])
def api_settle():
    from db.database import get_unsettled_predictions, settle_prediction, determine_result
    from scraper.sportmonks import SportmonksClient
    import json as _json

    if not SPORTMONKS_KEY:
        return jsonify({"success": False, "error": "SPORTMONKS_API_KEY necessaria per il settling"})

    sm = SportmonksClient(SPORTMONKS_KEY)
    unsettled = get_unsettled_predictions()
    settled_count = 0

    for pred in unsettled:
        try:
            match_data = _json.loads(pred["match_data"])
            home_id = match_data.get("home_id")
            if not home_id:
                continue

            results = sm.get_last_results(home_id, limit=5)
            for r in results:
                if pred["away_team"].lower() in r["text"].lower():
                    score_match = re.search(r'(\d+)-(\d+)', r["text"])
                    if score_match:
                        h_goals = int(score_match.group(1))
                        a_goals = int(score_match.group(2))
                        result = determine_result(pred["market"], h_goals, a_goals)
                        settle_prediction(pred["id"], result)
                        settled_count += 1
                        break
        except Exception as e:
            logger.error(f"Errore settling prediction #{pred['id']}: {e}")

    logger.info(f"Settling completato: {settled_count} predictions aggiornate")
    return redirect(url_for("stats_page"))


@app.route("/api/top-picks")
def api_top_picks():
    """Restituisce le 8 migliori scommesse dalla scansione corrente."""
    all_recs = []
    for m in _state["matches"]:
        for r in m.recommendations:
            if r.play and r.odds_value > 0:
                all_recs.append({
                    "match_id": m.id,
                    "home_team": m.home_team,
                    "away_team": m.away_team,
                    "league": m.league,
                    "market": r.market,
                    "odds_value": r.odds_value,
                    "bookmaker": r.bookmaker,
                    "confidence": r.confidence,
                    "value_rating": r.value_rating,
                    "reasoning": r.reasoning,
                    "ev_pct": _extract_ev(r.reasoning),
                })
    all_recs.sort(key=lambda x: x["value_rating"], reverse=True)
    return jsonify({"picks": all_recs[:8]})


def _extract_ev(reasoning: str) -> float:
    import re as _re
    m = _re.search(r'EV:\s*([+-]?\d+\.?\d*)%', reasoning)
    return float(m.group(1)) if m else 0.0


@app.route("/api/team-stats-bulk")
def api_team_stats_bulk():
    """Restituisce stats aggregate Sportmonks per tutte le squadre nel DB."""
    import sqlite3
    try:
        conn = sqlite3.connect('data/betanalyzer.db')
        cursor = conn.cursor()
        cursor.execute("""
            SELECT pi.team_name,
                   COUNT(*) as num_players,
                   SUM(JSON_EXTRACT(psc.stats_json, '$.appearances')) as total_apps,
                   SUM(JSON_EXTRACT(psc.stats_json, '$.goals')) as goals,
                   SUM(JSON_EXTRACT(psc.stats_json, '$.assists')) as assists,
                   SUM(JSON_EXTRACT(psc.stats_json, '$.shots_total')) as shots,
                   SUM(JSON_EXTRACT(psc.stats_json, '$.shots_on_target')) as sot,
                   SUM(JSON_EXTRACT(psc.stats_json, '$.big_chances_created')) as big_ch,
                   SUM(JSON_EXTRACT(psc.stats_json, '$.dribbles_success')) as dribbles,
                   SUM(JSON_EXTRACT(psc.stats_json, '$.fouls_drawn')) as fouls_d,
                   SUM(JSON_EXTRACT(psc.stats_json, '$.fouls_committed')) as fouls_c,
                   SUM(JSON_EXTRACT(psc.stats_json, '$.tackles')) as tackles,
                   SUM(JSON_EXTRACT(psc.stats_json, '$.interceptions')) as interceptions,
                   SUM(JSON_EXTRACT(psc.stats_json, '$.key_passes')) as key_passes,
                   AVG(CASE WHEN JSON_EXTRACT(psc.stats_json, '$.appearances') >= 5
                       THEN JSON_EXTRACT(psc.stats_json, '$.accurate_passes_pct') END) as pass_pct
            FROM player_stats_cache psc
            JOIN player_info pi ON psc.player_id = pi.player_id
            WHERE JSON_EXTRACT(psc.stats_json, '$.appearances') > 0
            GROUP BY pi.team_name
        """)
        result = {}
        for row in cursor.fetchall():
            team, n, apps, g, a, sh, sot_v, bc, dr, fd, fc, tk, intr, kp, pp = row
            if not team or not apps:
                continue
            apps = int(apps)
            # Normalize per 11 starters per game
            pg = apps / max(n, 1)  # average appearances per player
            result[team.lower()] = {
                "goals_pg": round(int(g or 0) / pg, 1) if pg > 0 else 0,
                "assists_pg": round(int(a or 0) / pg, 1) if pg > 0 else 0,
                "shots_pg": round(int(sh or 0) / pg, 1) if pg > 0 else 0,
                "sot_pct": round(int(sot_v or 0) / int(sh) * 100) if sh and int(sh) > 0 else 0,
                "big_chances": int(bc or 0),
                "dribbles_pg": round(int(dr or 0) / pg, 1) if pg > 0 else 0,
                "fouls_drawn_pg": round(int(fd or 0) / pg, 1) if pg > 0 else 0,
                "fouls_pg": round(int(fc or 0) / pg, 1) if pg > 0 else 0,
                "tackles_pg": round(int(tk or 0) / pg, 1) if pg > 0 else 0,
                "key_passes_pg": round(int(kp or 0) / pg, 1) if pg > 0 else 0,
                "pass_pct": round(float(pp), 1) if pp else 0,
            }
        conn.close()
        return jsonify({"success": True, "data": result})
    except Exception as e:
        logger.error(f"Team stats bulk error: {e}")
        return jsonify({"success": False, "error": str(e)})


@app.route("/rigori")
def rigori_page():
    """Pagina analisi rigori."""
    freshness = get_data_freshness(
        ("Partite", "data/penalties/SA_matches.json", "Football-Data API / Precache"),
    )
    return render_template("rigori.html", state=_state, freshness=freshness)


LEAGUE_KEYS_ALL = [
    "italy_serie_a", "england_premier_league", "spain_la_liga",
    "germany_bundesliga", "france_ligue_1", "netherlands_eredivisie",
    "champions_league", "england_championship", "portugal_primeira_liga",
    "brazil_serie_a", "world_cup",
]

LEAGUE_LABELS = {
    "italy_serie_a": "🇮🇹",
    "england_premier_league": "🏴󠁧󠁢󠁥󠁮󠁧󠁿",
    "spain_la_liga": "🇪🇸",
    "germany_bundesliga": "🇩🇪",
    "france_ligue_1": "🇫🇷",
    "denmark_superliga": "🇩🇰",
    "scotland_premiership": "🏴󠁧󠁢󠁳󠁣󠁴󠁿",
    "netherlands_eredivisie": "🇳🇱",
    "champions_league": "🏆",
    "england_championship": "🏴󠁧󠁢󠁥󠁮󠁧󠁿",
    "portugal_primeira_liga": "🇵🇹",
    "brazil_serie_a": "🇧🇷",
}
LEAGUE_DISPLAY_NAMES = {
    "italy_serie_a": "Serie A",
    "england_premier_league": "Premier League",
    "spain_la_liga": "La Liga",
    "germany_bundesliga": "Bundesliga",
    "france_ligue_1": "Ligue 1",
    "denmark_superliga": "Superliga",
    "scotland_premiership": "Premiership",
    "netherlands_eredivisie": "Eredivisie",
    "champions_league": "Champions League",
    "england_championship": "Championship",
    "portugal_primeira_liga": "Primeira Liga",
    "brazil_serie_a": "Brasileirao",
}


@app.route("/api/xg-ev")
def api_xg_ev():
    """Calcola xG (Poisson) e EV per ogni partita caricata nella Dashboard."""
    if not FOOTBALL_DATA_KEY:
        return jsonify({"success": False, "error": "FOOTBALL_DATA_API_KEY mancante"})

    from scraper.correct_score import CorrectScoreAnalyzer
    csa = CorrectScoreAnalyzer(FOOTBALL_DATA_KEY)

    matches = _state["matches"]
    if not matches:
        return jsonify({"success": True, "data": {}})

    # Raggruppa partite per lega
    from collections import defaultdict
    by_league = defaultdict(list)
    for m in matches:
        lk = _find_league_key(m.league)
        if lk:
            by_league[lk].append(m)

    result = {}  # match_id → { xg_home, xg_away, ev_markets: [...] }

    for league_key, league_matches in by_league.items():
        try:
            cs_data = csa.analyze_league(league_key)
            cs_matches = cs_data.get("matches", [])
            if not cs_matches:
                continue

            # Mappa nome squadra casa → dati correct score
            cs_lookup = {}
            for cm in cs_matches:
                # Normalizza nome per matching
                key = cm["home_team"].strip().lower() + "_" + cm["away_team"].strip().lower()
                cs_lookup[key] = cm

            for m in league_matches:
                key = m.home_team.strip().lower() + "_" + m.away_team.strip().lower()
                cm = cs_lookup.get(key)
                # Fuzzy fallback: containment match
                if not cm:
                    mh = m.home_team.strip().lower()
                    ma = m.away_team.strip().lower()
                    for cs_key, cs_val in cs_lookup.items():
                        cs_h, cs_a = cs_key.split("_", 1)
                        if (mh in cs_h or cs_h in mh) and (ma in cs_a or cs_a in ma):
                            cm = cs_val
                            break
                if not cm:
                    continue

                xg_home = cm["lambda_home"]
                xg_away = cm["lambda_away"]
                agg = cm.get("aggregates", {})

                # Probabilità dal modello Poisson
                model_probs = {
                    "1":        agg.get("home_win", 0),
                    "X":        agg.get("draw", 0),
                    "2":        agg.get("away_win", 0),
                    "OVER 2.5": agg.get("over_25", 0),
                    "UNDER 2.5": agg.get("under_25", 0),
                    "GG":       agg.get("gg", 0),
                    "NG":       agg.get("ng", 0),
                }

                # Calcola EV per ogni mercato confrontando con le quote reali
                ev_markets = []
                odds_map = {
                    "1": m.best_home[0], "X": m.best_draw[0], "2": m.best_away[0],
                    "OVER 2.5": m.best_over25[0], "UNDER 2.5": m.best_under25[0],
                    "GG": m.best_gg[0], "NG": m.best_ng[0],
                }

                best_ev = None
                for market, model_prob in model_probs.items():
                    odds = odds_map.get(market, 0)
                    if model_prob <= 0:
                        continue
                    if not odds or odds <= 1.0:
                        # No odds available — include model prob only (for manual input)
                        ev_markets.append({
                            "market": market,
                            "model_prob": round(model_prob, 1),
                            "implied_prob": 0,
                            "odds": 0,
                            "ev_pct": 0,
                        })
                        continue
                    implied_prob = (1.0 / odds) * 100 * 0.92  # rimuovi margine ~8%
                    ev_pct = round((model_prob / 100.0) * odds - 1, 4) * 100
                    ev_entry = {
                        "market": market,
                        "model_prob": round(model_prob, 1),
                        "implied_prob": round(implied_prob, 1),
                        "odds": round(odds, 2),
                        "ev_pct": round(ev_pct, 1),
                    }
                    ev_markets.append(ev_entry)
                    if best_ev is None or ev_pct > best_ev["ev_pct"]:
                        best_ev = ev_entry

                # Sort: items with real EV first, then by model_prob
                ev_markets.sort(key=lambda x: (x["ev_pct"], x["model_prob"]), reverse=True)

                result[m.id] = {
                    "xg_home": xg_home,
                    "xg_away": xg_away,
                    "ev_markets": ev_markets,
                    "best_ev": best_ev,
                }

        except Exception as e:
            logger.warning(f"xG/EV error for {league_key}: {e}")
            continue

    return jsonify({"success": True, "data": result})


@app.route("/value-bets")
def value_bets_page():
    return render_template("value_bets.html", state=_state)


@app.route("/api/matches-list")
def api_matches_list():
    """Lightweight endpoint: returns match id, teams, league for current state."""
    matches = _state.get("matches", [])
    return jsonify({"success": True, "matches": [
        {"id": m.id, "home_team": m.home_team, "away_team": m.away_team, "league": m.league}
        for m in matches
    ]})


@app.route("/api/rigori/<league_key>")
def api_rigori(league_key):
    """API per i dati rigori di una lega (o top10 cross-lega)."""
    if not FOOTBALL_DATA_KEY:
        return jsonify({"success": False, "error": "FOOTBALL_DATA_API_KEY non configurata"})
    from scraper.penalties import PenaltyAnalyzer

    try:
        pa = PenaltyAnalyzer(FOOTBALL_DATA_KEY)

        if league_key == "top10":
            all_teams = []
            for lk in LEAGUE_KEYS_ALL:
                try:
                    teams = pa.analyze_league(lk, skip_fetch=True)
                    flag = LEAGUE_LABELS.get(lk, "")
                    for t in teams:
                        t["team"] = f"{flag} {t['team']}"
                        t["_league"] = lk
                    all_teams.extend(teams)
                except Exception as e:
                    logger.warning("Top10 rigori skip %s: %s", lk, e)
            all_teams.sort(key=lambda x: x["probability_score"], reverse=True)
            data = all_teams[:10]
        else:
            data = pa.analyze_league(league_key)

        # Cross-check rigoristi con squalificati (FD.org) + assenti (Dashboard)
        # Source 1: FD.org suspensions (by team_id)
        fd_suspended: dict[int, list[tuple[str, str]]] = {}
        keys_to_check = LEAGUE_KEYS_ALL if league_key == "top10" else [league_key]
        for lk in keys_to_check:
            try:
                suspended_by_tid = pa.get_suspended_for_league(lk)
                for tid, players in suspended_by_tid.items():
                    for p in players:
                        fd_suspended.setdefault(tid, []).append(
                            (p["player"].lower(), p["reason"])
                        )
            except Exception:
                pass

        # Source 2: Transfermarkt injuries (by team name)
        tm_injured: dict[str, list[tuple[str, str]]] = {}
        try:
            from scraper.injuries import get_injured_by_team
            for lk in keys_to_check:
                try:
                    injured = get_injured_by_team(lk)
                    for team_low, players in injured.items():
                        for p in players:
                            tm_injured.setdefault(team_low, []).append(
                                (p["player"].lower(), f"Infortunio ({p['injury']})")
                            )
                except Exception:
                    pass
        except Exception:
            pass

        # Source 3: Dashboard absentees (by team name)
        db_absent: dict[str, list[tuple[str, str]]] = {}
        for m in _state.get("matches", []):
            md = m if isinstance(m, dict) else m.to_dict()
            for a in md.get("absentees", []):
                team_name = md["home_team"] if a["team"] == "home" else md["away_team"]
                db_absent.setdefault(team_name.lower(), []).append(
                    (a["player"].lower(), a.get("reason", "Assente"))
                )

        for team_data in data:
            if not team_data.get("penalty_takers"):
                continue

            team_id = team_data.get("team_id")
            team_low = team_data["team"].lower()

            # Combine all sources for this team
            check_list: list[tuple[str, str]] = []
            if team_id and team_id in fd_suspended:
                check_list.extend(fd_suspended[team_id])
            for source in (tm_injured, db_absent):
                for akey, alist in source.items():
                    if akey in team_low or team_low in akey:
                        check_list.extend(alist)
                        break

            if not check_list:
                continue

            for taker in team_data["penalty_takers"]:
                taker_low = taker["player"].lower()
                for absent_name, reason in check_list:
                    if absent_name in taker_low or taker_low in absent_name:
                        taker["absent"] = True
                        taker["absent_reason"] = reason
                        break
                    parts_a = absent_name.split()
                    parts_t = taker_low.split()
                    if len(parts_a) >= 2 and len(parts_t) >= 2 and parts_a[-1] == parts_t[-1]:
                        taker["absent"] = True
                        taker["absent_reason"] = reason
                        break

        return jsonify({"success": True, "data": data})
    except Exception as e:
        logger.error(f"Errore analisi rigori: {e}")
        return jsonify({"success": False, "error": str(e)})


@app.route("/multipla")
def multipla_page():
    return render_template("multipla.html", state=_state)


@app.route("/api/multipla/<league_key>")
def api_multipla(league_key):
    """Build combo picks from Dashboard, Rigori, Cartellini, Marcatori,
    Risultato Esatto and Doppio Tempo — 6 sources."""
    try:
        all_picks = []

        # ── 1. Dashboard picks (AI recommendations with play=True) ──
        league_name_map = {
            "italy_serie_a": "Serie A",
            "england_premier_league": "Premier League",
            "spain_la_liga": "La Liga",
            "germany_bundesliga": "Bundesliga",
            "france_ligue_1": "Ligue 1",
            "netherlands_eredivisie": "Eredivisie",
            "champions_league": "Champions League",
            "england_championship": "Championship",
            "portugal_primeira_liga": "Primeira Liga",
            "brazil_serie_a": "Brasileirão",
        }
        league_display = league_name_map.get(league_key, "")
        for m in _state.get("matches", []):
            md = m if isinstance(m, dict) else m
            home = md.home_team if hasattr(md, 'home_team') else md.get("home_team", "")
            away = md.away_team if hasattr(md, 'away_team') else md.get("away_team", "")
            league = md.league if hasattr(md, 'league') else md.get("league", "")

            if league_display and league_display.lower() not in league.lower():
                continue

            recs = md.recommendations if hasattr(md, 'recommendations') else md.get("recommendations", [])
            for r in recs:
                rec = r if isinstance(r, dict) else r
                play = rec.play if hasattr(rec, 'play') else rec.get("play", False)
                if not play:
                    continue
                odds_val = rec.odds_value if hasattr(rec, 'odds_value') else rec.get("odds_value", 0)
                market = rec.market if hasattr(rec, 'market') else rec.get("market", "?")
                vr = rec.value_rating if hasattr(rec, 'value_rating') else rec.get("value_rating", 0)
                confidence = rec.confidence if hasattr(rec, 'confidence') else rec.get("confidence", "")

                # Estimate probability from odds (remove ~8% bookmaker margin)
                raw_prob = (100 / odds_val) if odds_val > 1 else 50
                prob = round(raw_prob * 0.92)  # margin-adjusted

                all_picks.append({
                    "source": "dashboard",
                    "match": f"{home} vs {away}",
                    "date": md.commence_time if hasattr(md, 'commence_time') else md.get("commence_time"),
                    "bet": market,
                    "odds": round(odds_val, 2) if odds_val else None,
                    "prob": prob,
                    "value_rating": vr,
                    "confidence": confidence,
                })

        # ── 2. Rigori picks (top teams with high probability) ──
        if FOOTBALL_DATA_KEY:
            try:
                from scraper.penalties import PenaltyAnalyzer
                pa = PenaltyAnalyzer(FOOTBALL_DATA_KEY)
                rigori_data = pa.analyze_league(league_key, skip_fetch=True)
                for team in rigori_data[:10]:
                    if team["probability_score"] >= 55:
                        nxt = team.get("next_match", "")
                        all_picks.append({
                            "source": "rigori",
                            "match": f"{team['team']} {nxt}",
                            "date": team.get("utcDate"),
                            "bet": f"Rigore in {team['team']}",
                            "odds": None,
                            "prob": team["probability_score"],
                            "value_rating": 0,
                            "confidence": "",
                        })
            except Exception as e:
                logger.warning("Multipla: rigori error: %s", e)

        # ── 3. Cartellini picks (top players with high card probability) ──
        if FOOTBALL_DATA_KEY:
            try:
                from scraper.cards import CardAnalyzer
                ca = CardAnalyzer(FOOTBALL_DATA_KEY)
                cards_data = ca.analyze_league(league_key)
                for match in cards_data.get("matches", [])[:15]:
                    for p in match.get("top_picks", [])[:3]:
                        if p["probability"] >= 30:
                            diff_flag = " ⚠DIFF" if p.get("diffidato") else ""
                            all_picks.append({
                                "source": "cartellini",
                                "match": f"{match['home_team']} vs {match['away_team']}",
                                "date": match.get("utcDate"),
                                "bet": f"🟨 {p['player']}{diff_flag}",
                                "odds": None,
                                "prob": p["probability"],
                                "value_rating": 0,
                                "confidence": "",
                            })
            except Exception as e:
                logger.warning("Multipla: cartellini error: %s", e)

        # ── 4. Marcatori picks (top scorers) ──
        if FOOTBALL_DATA_KEY:
            try:
                from scraper.scorers import ScorerAnalyzer
                sa = ScorerAnalyzer(FOOTBALL_DATA_KEY)
                scorers_data = sa.analyze_league(league_key)
                for match in scorers_data.get("matches", [])[:15]:
                    for p in match.get("top_picks", [])[:2]:
                        if p["probability"] >= 25:
                            all_picks.append({
                                "source": "marcatori",
                                "match": f"{match['home_team']} vs {match['away_team']}",
                                "date": match.get("utcDate"),
                                "bet": f"⚽ {p['player']} segna",
                                "odds": None,
                                "prob": p["probability"],
                                "value_rating": 0,
                                "confidence": "",
                            })
            except Exception as e:
                logger.warning("Multipla: marcatori error: %s", e)

        # ── 5. Risultato Esatto picks (top correct score predictions) ──
        if FOOTBALL_DATA_KEY:
            try:
                from scraper.correct_score import CorrectScoreAnalyzer
                csa = CorrectScoreAnalyzer(FOOTBALL_DATA_KEY)
                cs_data = csa.analyze_league(league_key)
                for match in cs_data.get("matches", [])[:15]:
                    agg = match.get("aggregates", {})
                    h = match["home_team"]
                    a = match["away_team"]
                    match_label = f"{h} vs {a}"
                    utc = match.get("utcDate")

                    # Over/Under 2.5
                    o25 = agg.get("over_25", 0)
                    u25 = agg.get("under_25", 0)
                    if o25 >= 60:
                        all_picks.append({
                            "source": "correct_score", "match": match_label, "date": utc,
                            "bet": "Over 2.5", "odds": None, "prob": round(o25),
                            "value_rating": 0, "confidence": "",
                        })
                    elif u25 >= 65:
                        all_picks.append({
                            "source": "correct_score", "match": match_label, "date": utc,
                            "bet": "Under 2.5", "odds": None, "prob": round(u25),
                            "value_rating": 0, "confidence": "",
                        })

                    # GG/NG
                    gg = agg.get("gg", 0)
                    ng = agg.get("ng", 0)
                    if gg >= 60:
                        all_picks.append({
                            "source": "correct_score", "match": match_label, "date": utc,
                            "bet": "Goal (GG)", "odds": None, "prob": round(gg),
                            "value_rating": 0, "confidence": "",
                        })
                    elif ng >= 65:
                        all_picks.append({
                            "source": "correct_score", "match": match_label, "date": utc,
                            "bet": "No Goal (NG)", "odds": None, "prob": round(ng),
                            "value_rating": 0, "confidence": "",
                        })

                    # 1X2 forte (>= 55%)
                    hw = agg.get("home_win", 0)
                    dr = agg.get("draw", 0)
                    aw = agg.get("away_win", 0)
                    if hw >= 55:
                        all_picks.append({
                            "source": "correct_score", "match": match_label, "date": utc,
                            "bet": f"1 ({h})", "odds": None, "prob": round(hw),
                            "value_rating": 0, "confidence": "",
                        })
                    elif aw >= 55:
                        all_picks.append({
                            "source": "correct_score", "match": match_label, "date": utc,
                            "bet": f"2 ({a})", "odds": None, "prob": round(aw),
                            "value_rating": 0, "confidence": "",
                        })
            except Exception as e:
                logger.warning("Multipla: correct_score error: %s", e)

        # ── 6. Doppio Tempo picks (strong HT/FT predictions) ──
        if FOOTBALL_DATA_KEY:
            try:
                from scraper.halftime import HalfTimeAnalyzer
                hta = HalfTimeAnalyzer(FOOTBALL_DATA_KEY)
                ht_data = hta.analyze_league(league_key)
                for match in ht_data.get("matches", [])[:15]:
                    analysis = match.get("analysis", {})
                    preds = analysis.get("predictions", [])
                    if preds:
                        best = preds[0]  # already sorted by probability desc
                        prob_val = best.get("probability", 0)
                        if prob_val >= 30:
                            # Translate cryptic market labels to human-readable
                            raw_market = best.get('market', '?')
                            MARKET_LABELS = {
                                "GG/NG NG/NG": "No Goal PT + No Goal FT",
                                "GG/NG GG/GG": "Goal PT + Goal FT",
                                "GG/NG NG/GG": "No Goal PT + Goal FT",
                                "GG/NG GG/NG": "Goal PT + No Goal FT",
                                "1X2 1/1": "Casa vince PT + Casa vince FT",
                                "1X2 2/2": "Fuori vince PT + Fuori vince FT",
                                "1X2 X/1": "Pari PT + Casa vince FT",
                                "1X2 X/2": "Pari PT + Fuori vince FT",
                                "1X2 1/X": "Casa vince PT + Pari FT",
                                "1X2 2/X": "Fuori vince PT + Pari FT",
                                "1X2 X/X": "Pari PT + Pari FT",
                                "1X2 1/2": "Casa vince PT + Fuori vince FT",
                                "1X2 2/1": "Fuori vince PT + Casa vince FT",
                            }
                            friendly_label = MARKET_LABELS.get(raw_market, raw_market)
                            all_picks.append({
                                "source": "doppio_tempo",
                                "match": f"{match['home_team']} vs {match['away_team']}",
                                "date": match.get("utcDate"),
                                "bet": f"⏱️ {friendly_label}",
                                "odds": None,
                                "prob": prob_val,
                                "value_rating": 0,
                                "confidence": "",
                            })
            except Exception as e:
                logger.warning("Multipla: doppio_tempo error: %s", e)

        if not all_picks:
            return jsonify({"success": False, "error": "Nessuna pick disponibile. Lancia prima le analisi."})

        # Sort all picks by date (closest first)
        all_picks.sort(key=lambda x: (x.get("date") or "9999-12-31"))

        # ── Assign estimated odds where missing ──
        for p in all_picks:
            if p["odds"] is None and p["prob"] > 0:
                # Convert probability to fair odds, then add ~8% bookmaker margin
                fair_odds = 100 / p["prob"]
                p["odds"] = round(fair_odds * 0.92, 2)  # what a bookmaker would pay

        # ── Build 3 combo types ──

        def _build_combo(pool, n, avoid_same_match=True, avoid_same_source_match=True):
            """Select n picks, diversifying by match and source."""
            selected = []
            seen_matches = set()
            seen_source_match = set()
            for p in pool:
                key = f"{p['source']}_{p['match']}"
                if avoid_same_match and p["match"] in seen_matches:
                    continue
                if avoid_same_source_match and key in seen_source_match:
                    continue
                selected.append(p)
                seen_matches.add(p["match"])
                seen_source_match.add(key)
                if len(selected) >= n:
                    break
            if len(selected) < n:
                for p in pool:
                    if p not in selected:
                        selected.append(p)
                        if len(selected) >= n:
                            break
            return selected

        # SICURA: top 4 by probability (high prob = safe, >50% each)
        sicura_pool = sorted(all_picks, key=lambda x: x["prob"], reverse=True)
        sicura_picks = _build_combo(sicura_pool, 4)
        sicura_picks.sort(key=lambda x: x.get("date") or "9999-12-31")

        # VALUE: maximize source diversity — one pick per source, highest prob each
        # This gives a balanced "best of each model" combo
        value_picks = []
        seen_sources = set()
        by_source = {}
        for p in all_picks:
            src = p["source"]
            if src not in by_source:
                by_source[src] = []
            by_source[src].append(p)
        # Sort each source's picks by prob desc
        for src in by_source:
            by_source[src].sort(key=lambda x: x["prob"], reverse=True)
        # Round-robin: best from each source
        source_order = sorted(by_source.keys(), key=lambda s: by_source[s][0]["prob"] if by_source[s] else 0, reverse=True)
        seen_matches_v = set()
        for src in source_order:
            for p in by_source[src]:
                if p["match"] not in seen_matches_v:
                    value_picks.append(p)
                    seen_matches_v.add(p["match"])
                    break
            if len(value_picks) >= 5:
                break
        # If we still need more, fill from top picks
        if len(value_picks) < 4:
            for p in sorted(all_picks, key=lambda x: x["prob"], reverse=True):
                if p not in value_picks and p["match"] not in seen_matches_v:
                    value_picks.append(p)
                    seen_matches_v.add(p["match"])
                    if len(value_picks) >= 4:
                        break
        value_picks.sort(key=lambda x: x.get("date") or "9999-12-31")

        # RISCHIO: 6 picks in the 20-55% range from different matches
        rischio_pool = [p for p in all_picks if 20 <= p["prob"] <= 55]
        rischio_pool.sort(key=lambda x: x["prob"], reverse=True)
        rischio_picks = _build_combo(rischio_pool, 6)
        rischio_picks.sort(key=lambda x: x.get("date") or "9999-12-31")

        def _calc_combo_stats(picks):
            combined_odds = 1.0
            combined_prob = 1.0
            for p in picks:
                odds = p.get("odds") or 2.0
                combined_odds *= odds
                combined_prob *= p["prob"] / 100
            return {
                "picks": picks,
                "combined_odds": round(combined_odds, 2),
                "combined_prob": round(combined_prob * 100, 2),
            }

        # Source count for stats
        sources_used = list(set(p["source"] for p in all_picks))

        result = {
            "sicura": _calc_combo_stats(sicura_picks) if sicura_picks else None,
            "value": _calc_combo_stats(value_picks) if value_picks else None,
            "rischio": _calc_combo_stats(rischio_picks) if len(rischio_picks) >= 3 else None,
            "all_picks": all_picks,
            "sources_used": sources_used,
            "total_picks": len(all_picks),
        }

        return jsonify({"success": True, "data": result})

    except Exception as e:
        logger.error(f"Errore multipla builder: {e}")
        return jsonify({"success": False, "error": str(e)})


def _mark_absent_players(matches_data, league_key: str = ""):
    """Cross-check player names in top_picks against:
    1. Football-Data.org: yellow-card suspensions and red-card bans (all 11 leagues)
       — uses disk cache only, ZERO API calls
    2. Transfermarkt: injury data scraped from public page (cached 6h)
    3. Dashboard (Sportmonks): injury/absence data (when Dashboard has been loaded)

    Adds 'absent' and 'absent_reason' keys to matching picks.
    """

    # ── Source 1: Football-Data.org suspensions (by team_id → player name) ──
    fd_suspended: dict[int, list[tuple[str, str]]] = {}

    if FOOTBALL_DATA_KEY and league_key:
        try:
            from scraper.penalties import PenaltyAnalyzer
            pa = PenaltyAnalyzer(FOOTBALL_DATA_KEY)

            keys_to_check = LEAGUE_KEYS_ALL if league_key == "top10" else [league_key]

            for lk in keys_to_check:
                try:
                    suspended_by_tid = pa.get_suspended_for_league(lk)
                    for tid, players in suspended_by_tid.items():
                        for p in players:
                            fd_suspended.setdefault(tid, []).append(
                                (p["player"].lower(), p["reason"])
                            )
                except Exception as e:
                    logger.debug("Suspended check skip %s: %s", lk, e)
        except Exception as e:
            logger.warning("Suspended players check error: %s", e)

    # ── Source 2: Transfermarkt injuries (by team name) ──
    tm_injured_by_team: dict[str, list[tuple[str, str]]] = {}
    if league_key:
        try:
            from scraper.injuries import get_injured_by_team
            keys_to_check = LEAGUE_KEYS_ALL if league_key == "top10" else [league_key]
            for lk in keys_to_check:
                try:
                    injured = get_injured_by_team(lk)
                    for team_low, players in injured.items():
                        for p in players:
                            tm_injured_by_team.setdefault(team_low, []).append(
                                (p["player"].lower(), f"Infortunio ({p['injury']})")
                            )
                except Exception as e:
                    logger.debug("Injury check skip %s: %s", lk, e)
        except Exception as e:
            logger.warning("Transfermarkt injury check error: %s", e)

    # ── Source 3: Dashboard (Sportmonks) absentees (by team name) ──
    db_absent_by_name: dict[str, list[tuple[str, str]]] = {}
    dashboard_matches = _state.get("matches", [])
    for m in dashboard_matches:
        md = m if isinstance(m, dict) else m.to_dict()
        for a in md.get("absentees", []):
            team_name = md["home_team"] if a["team"] == "home" else md["away_team"]
            db_absent_by_name.setdefault(team_name.lower(), []).append(
                (a["player"].lower(), a.get("reason", "Assente"))
            )

    if not fd_suspended and not tm_injured_by_team and not db_absent_by_name:
        return

    # ── Apply to picks ──
    def _fuzzy_player_match(player_low: str, absent_name: str) -> bool:
        if absent_name in player_low or player_low in absent_name:
            return True
        parts_a = absent_name.split()
        parts_p = player_low.split()
        if len(parts_a) >= 2 and len(parts_p) >= 2 and parts_a[-1] == parts_p[-1]:
            return True
        return False

    for match_data in matches_data:
        home_low = match_data.get("home_team", "").lower()
        away_low = match_data.get("away_team", "").lower()

        # Transfermarkt injuries for this match (fuzzy team name match)
        home_tm_injured: list[tuple[str, str]] = []
        away_tm_injured: list[tuple[str, str]] = []
        for tkey, tlist in tm_injured_by_team.items():
            if tkey in home_low or home_low in tkey:
                home_tm_injured = tlist
            elif tkey in away_low or away_low in tkey:
                away_tm_injured = tlist

        # Dashboard absentees for this match (fuzzy team name match)
        home_db_absent: list[tuple[str, str]] = []
        away_db_absent: list[tuple[str, str]] = []
        for akey, alist in db_absent_by_name.items():
            if akey in home_low or home_low in akey:
                home_db_absent = alist
            elif akey in away_low or away_low in akey:
                away_db_absent = alist

        for pick in match_data.get("top_picks", []):
            player_low = pick.get("player", "").lower()
            team_id = pick.get("team_id")
            is_home = pick.get("is_home", False)

            # Check FD.org suspensions (by team_id — exact match)
            if team_id and team_id in fd_suspended:
                for absent_name, reason in fd_suspended[team_id]:
                    if _fuzzy_player_match(player_low, absent_name):
                        pick["absent"] = True
                        pick["absent_reason"] = reason
                        break

            # Check Transfermarkt injuries (by team name — fuzzy)
            if not pick.get("absent"):
                check_list = home_tm_injured if is_home else away_tm_injured
                for absent_name, reason in check_list:
                    if _fuzzy_player_match(player_low, absent_name):
                        pick["absent"] = True
                        pick["absent_reason"] = reason
                        break

            # Check Dashboard absentees (by team name — fuzzy)
            if not pick.get("absent"):
                check_list = home_db_absent if is_home else away_db_absent
                for absent_name, reason in check_list:
                    if _fuzzy_player_match(player_low, absent_name):
                        pick["absent"] = True
                        pick["absent_reason"] = reason
                        break

    # ── Source 4: Worker lineup cache (starters / bench / not in squad) ──
    lineup_cache_path = os.path.join("data", "lineups_cache.json")
    if os.path.exists(lineup_cache_path):
        try:
            with open(lineup_cache_path, "r") as f:
                lineup_cache = json.load(f)
        except Exception:
            lineup_cache = {}

        if lineup_cache:
            for match_data in matches_data:
                home_team = match_data.get("home_team", "")
                away_team = match_data.get("away_team", "")

                # Find matching lineup entry (fuzzy)
                lineup_entry = None
                home_low = home_team.lower()
                away_low = away_team.lower()
                for lk, lv in lineup_cache.items():
                    lh = lv.get("home_team", "").lower()
                    la = lv.get("away_team", "").lower()
                    if (home_low in lh or lh in home_low) and (away_low in la or la in away_low):
                        lineup_entry = lv
                        break

                if not lineup_entry:
                    continue

                # Build name sets (lowercased)
                starters_home = {n.lower() for n in lineup_entry.get("starters_home", [])}
                starters_away = {n.lower() for n in lineup_entry.get("starters_away", [])}
                bench_home = {n.lower() for n in lineup_entry.get("bench_home", [])}
                bench_away = {n.lower() for n in lineup_entry.get("bench_away", [])}

                has_lineups = bool(starters_home or starters_away)
                if not has_lineups:
                    continue

                # Mark lineup_status on "has_lineup" match
                match_data["has_lineup"] = True
                match_data["formation_home"] = lineup_entry.get("formation_home")
                match_data["formation_away"] = lineup_entry.get("formation_away")

                for pick in match_data.get("top_picks", []):
                    if pick.get("absent"):
                        continue  # already marked absent, skip

                    player_low = pick.get("player", "").lower()
                    is_home = pick.get("is_home", False)

                    starters = starters_home if is_home else starters_away
                    bench = bench_home if is_home else bench_away

                    # Fuzzy match against lineup names
                    def _in_set(name_low, name_set):
                        for n in name_set:
                            if n in name_low or name_low in n:
                                return True
                            # Cognome match
                            parts_n = n.split()
                            parts_p = name_low.split()
                            if len(parts_n) >= 2 and len(parts_p) >= 2 and parts_n[-1] == parts_p[-1]:
                                return True
                        return False

                    if _in_set(player_low, starters):
                        pick["lineup_status"] = "starter"
                    elif _in_set(player_low, bench):
                        pick["lineup_status"] = "bench"
                    else:
                        pick["lineup_status"] = "out"


@app.route("/marcatori")
def marcatori_page():
    freshness = get_data_freshness(
        ("Partite", "data/penalties/SA_matches.json", "Football-Data API / Precache"),
        ("Giocatori", "db:player_stats_cache", "Sportmonks DB / Nightly Sync"),
    )
    return render_template("marcatori.html", state=_state, freshness=freshness)


@app.route("/api/marcatori/<league_key>")
def api_marcatori(league_key):
    if not FOOTBALL_DATA_KEY:
        return jsonify({"success": False, "error": "FOOTBALL_DATA_API_KEY non configurata"})
    from scraper.scorers import ScorerAnalyzer
    try:
        if league_key == "top10":
            all_matches = []
            for lk in LEAGUE_KEYS_ALL:
                try:
                    sa = ScorerAnalyzer(FOOTBALL_DATA_KEY)
                    result = sa.analyze_league(lk)
                    flag = LEAGUE_LABELS.get(lk, "")
                    for m in result.get("matches", []):
                        m["home_team"] = f"{flag} {m['home_team']}"
                        m["away_team"] = f"{flag} {m['away_team']}"
                        all_matches.append(m)
                except Exception as e:
                    logger.warning("Top10 marcatori skip %s: %s", lk, e)
            # Sort by top pick probability and take best 10 matches
            all_matches.sort(key=lambda x: max((p["probability"] for p in x.get("top_picks", [])), default=0), reverse=True)
            matches_out = all_matches[:10]
            # Then sort these 10 chronologically
            matches_out.sort(key=lambda x: x.get("utcDate") or "")
        else:
            sa = ScorerAnalyzer(FOOTBALL_DATA_KEY)
            data = sa.analyze_league(league_key)
            if data.get("error"):
                return jsonify({"success": False, "error": data["error"]})
            matches_out = data["matches"]

        # Cross-check marcatori con squalificati (FD.org) + assenti (Dashboard)
        _mark_absent_players(matches_out, league_key)

        return jsonify({"success": True, "data": matches_out})
    except Exception as e:
        logger.error(f"Errore analisi marcatori: {e}")
        return jsonify({"success": False, "error": str(e)})


@app.route("/cartellini")
def cartellini_page():
    freshness = get_data_freshness(
        ("Partite", "data/penalties/SA_matches.json", "Football-Data API / Precache"),
        ("Giocatori", "db:player_stats_cache", "Sportmonks DB / Nightly Sync"),
    )
    return render_template("cartellini.html", state=_state, freshness=freshness)


@app.route("/api/cartellini/<league_key>")
def api_cartellini(league_key):
    if not FOOTBALL_DATA_KEY:
        return jsonify({"success": False, "error": "FOOTBALL_DATA_API_KEY non configurata"})
    from scraper.cards import CardAnalyzer
    try:
        if league_key == "top10":
            all_matches = []
            for lk in LEAGUE_KEYS_ALL:
                try:
                    ca = CardAnalyzer(FOOTBALL_DATA_KEY)
                    result = ca.analyze_league(lk)
                    flag = LEAGUE_LABELS.get(lk, "")
                    for m in result.get("matches", []):
                        m["home_team"] = f"{flag} {m['home_team']}"
                        m["away_team"] = f"{flag} {m['away_team']}"
                        all_matches.append(m)
                except Exception as e:
                    logger.warning("Top10 cartellini skip %s: %s", lk, e)
            # Sort by top pick probability and take best 10 matches
            all_matches.sort(key=lambda x: max((p["probability"] for p in x.get("top_picks", [])), default=0), reverse=True)
            matches_out = all_matches[:10]
            # Then sort these 10 chronologically
            matches_out.sort(key=lambda x: x.get("utcDate") or "")
        else:
            ca = CardAnalyzer(FOOTBALL_DATA_KEY)
            data = ca.analyze_league(league_key)
            if data.get("error"):
                return jsonify({"success": False, "error": data["error"]})
            matches_out = data["matches"]

        # Cross-check cartellini con squalificati (FD.org) + assenti (Dashboard)
        _mark_absent_players(matches_out, league_key)

        return jsonify({"success": True, "data": matches_out})
    except Exception as e:
        logger.error(f"Errore analisi cartellini: {e}")
        return jsonify({"success": False, "error": str(e)})



# ------------------------------------------------------------------ #
#  Arbitri — Referee Statistics                                         #
# ------------------------------------------------------------------ #

@app.route("/arbitri")
def arbitri_page():
    freshness = get_data_freshness(
        ("Partite", "data/penalties/SA_matches.json", "Football-Data API / Precache"),
    )
    return render_template("arbitri.html", state=_state, freshness=freshness)


@app.route("/api/arbitri/<league_key>")
def api_arbitri(league_key):
    from scraper.referees import analyze_referees
    try:
        result = analyze_referees(league_key)
        return jsonify(result)
    except Exception as e:
        logger.error(f"Errore analisi arbitri: {e}")
        return jsonify({"success": False, "error": str(e)})


# ------------------------------------------------------------------ #
#  Doppio Tempo — HT/FT Analysis                                       #
# ------------------------------------------------------------------ #

@app.route("/doppiotempo")
def doppiotempo_page():
    freshness = get_data_freshness(
        ("Partite", "data/penalties/SA_matches.json", "Football-Data API / Precache"),
    )
    return render_template("doppiotempo.html", state=_state, freshness=freshness)


@app.route("/api/doppiotempo/<league_key>")
def api_doppiotempo(league_key):
    if not FOOTBALL_DATA_KEY:
        return jsonify({"success": False, "error": "FOOTBALL_DATA_API_KEY non configurata"})
    from scraper.halftime import HalfTimeAnalyzer
    try:
        if league_key == "top10":
            all_matches = []
            for lk in LEAGUE_KEYS_ALL:
                try:
                    ha = HalfTimeAnalyzer(FOOTBALL_DATA_KEY)
                    result = ha.analyze_league(lk)
                    flag = LEAGUE_LABELS.get(lk, "")
                    for m in result.get("matches", []):
                        m["home_team"] = f"{flag} {m['home_team']}"
                        m["away_team"] = f"{flag} {m['away_team']}"
                        all_matches.append(m)
                except Exception as e:
                    logger.warning("Top10 doppiotempo skip %s: %s", lk, e)
            all_matches.sort(
                key=lambda x: max((p.get("probability", 0) for p in x.get("analysis", {}).get("predictions", [])), default=0),
                reverse=True
            )
            matches_out = all_matches[:10]
            matches_out.sort(key=lambda x: x.get("utcDate") or "")
        else:
            ha = HalfTimeAnalyzer(FOOTBALL_DATA_KEY)
            data = ha.analyze_league(league_key)
            if data.get("error"):
                return jsonify({"success": False, "error": data["error"]})
            matches_out = data["matches"]

        return jsonify({"success": True, "data": matches_out})
    except Exception as e:
        logger.error(f"Errore analisi doppiotempo: {e}")
        return jsonify({"success": False, "error": str(e)})


# ------------------------------------------------------------------ #
#  Risultato Esatto — Correct Score (Poisson + Dixon-Coles)            #
# ------------------------------------------------------------------ #

@app.route("/risultato-esatto")
def risultato_esatto_page():
    freshness = get_data_freshness(
        ("Partite", "data/penalties/SA_matches.json", "Football-Data API / Precache"),
    )
    return render_template("risultato_esatto.html", state=_state, freshness=freshness)


@app.route("/api/correct_score/<league_key>")
def api_correct_score(league_key):
    try:
        from scraper.correct_score import CorrectScoreAnalyzer
        fd_key = os.getenv("FOOTBALL_DATA_API_KEY", "")
        if not fd_key:
            return jsonify({"success": False, "error": "FOOTBALL_DATA_API_KEY non configurata"})

        cs = CorrectScoreAnalyzer(fd_key)
        result = cs.analyze_league(league_key)

        if result.get("error"):
            return jsonify({"success": False, "error": result["error"]})

        return jsonify({"success": True, "matches": result["matches"]})

    except Exception as e:
        logger.error(f"Errore analisi correct_score: {e}")
        return jsonify({"success": False, "error": str(e)})


# ------------------------------------------------------------------ #
#  Teams & Rosters                                                     #
# ------------------------------------------------------------------ #

# ── World Cup 2026 ──

@app.route("/worldcup")
def worldcup_page():
    from scraper.worldcup import get_all_squads_summary, get_top_scorers, get_top_card_candidates, WC_SQUADS

    # Import squads if not yet in DB (first visit)
    import sqlite3
    conn = sqlite3.connect("data/betanalyzer.db")
    count = conn.execute("SELECT COUNT(*) FROM wc_squads").fetchone()[0]
    conn.close()
    if count == 0:
        from scraper.worldcup import import_squads
        import_squads()

    rankings = get_all_squads_summary()
    top_scorers = get_top_scorers(20)
    top_cards = get_top_card_candidates(20)

    # Build groups dict for template
    groups = {}
    # All WC groups
    ALL_GROUPS = {
        "A": ["Messico", "Sudafrica", "Corea del Sud", "Repubblica Ceca"],
        "B": ["Canada", "Bosnia", "Qatar", "Svizzera"],
        "C": ["Brasile", "Marocco", "Haiti", "Scozia"],
        "D": ["USA", "Paraguay", "Australia", "Turchia"],
        "E": ["Germania", "Curacao", "Costa d'Avorio", "Ecuador"],
        "F": ["Olanda", "Giappone", "Svezia", "Tunisia"],
        "G": ["Belgio", "Egitto", "Iran", "Nuova Zelanda"],
        "H": ["Spagna", "Capo Verde", "Arabia Saudita", "Uruguay"],
        "I": ["Francia", "Senegal", "Iraq", "Norvegia"],
        "J": ["Argentina", "Algeria", "Austria", "Giordania"],
        "K": ["Portogallo", "Rep. Dem. Congo", "Uzbekistan", "Colombia"],
        "L": ["Inghilterra", "Croazia", "Ghana", "Panama"],
    }

    ranking_map = {r["country"]: r for r in rankings}
    missing_groups = []
    for g, teams in ALL_GROUPS.items():
        group_teams = []
        for t in teams:
            if t in ranking_map:
                group_teams.append(ranking_map[t])
            else:
                group_teams.append({"country": t, "group": g, "avg_rating": None, "matched": 0, "total_players": 0, "total_goals": 0})
        groups[g] = group_teams

    total_players = sum(r["total_players"] for r in rankings)
    matched_players = sum(r["matched"] for r in rankings)
    match_pct = round(matched_players / total_players * 100) if total_players > 0 else 0

    return render_template("worldcup.html",
        rankings=rankings,
        top_scorers=top_scorers,
        top_cards=top_cards,
        groups=groups,
        countries=len(rankings),
        total_players=total_players,
        matched_players=matched_players,
        match_pct=match_pct,
        missing_groups=[],
    )


@app.route("/worker", methods=["GET", "POST"])
def worker_page():
    from db.database import save_worker_setting, get_worker_setting, get_alerts_log
    
    if request.method == "POST":
        # Salviamo i campionati selezionati
        selected_leagues = request.form.getlist("leagues")
        save_worker_setting("active_leagues", json.dumps(selected_leagues))
        # Salviamo la mail destinatario
        email = request.form.get("email", "")
        save_worker_setting("alert_email", email)
        _state["message"] = "Impostazioni Worker salvate!"
        return redirect(url_for("worker_page"))

    active_leagues_raw = get_worker_setting("active_leagues", "[]")
    active_leagues = json.loads(active_leagues_raw)
    alert_email = get_worker_setting("alert_email", "")
    alerts_log = get_alerts_log(30)
    
    # Usiamo i nomi globali sincronizzati con Odds API
    league_display_names = LEAGUE_DISPLAY_NAMES

    # Preview dei match imminenti (prossime 24h) — Sportmonks + Odds API fallback
    upcoming_matches = []

    if active_leagues:
        import pytz
        rome_tz = pytz.timezone("Europe/Rome")
        raw_matches = []

        # 1. Prova Sportmonks (sorgente primaria)
        if SPORTMONKS_KEY:
            try:
                from scraper.sportmonks import SportmonksClient
                sm_client = SportmonksClient(SPORTMONKS_KEY)
                raw_matches = sm_client.get_all_matches(league_keys=active_leagues)
                logger.info(f"Worker preview: Sportmonks {len(raw_matches)} match")
            except Exception as e:
                logger.warning(f"Worker preview: Sportmonks fallito: {e}")

        # 2. Fallback/arricchimento Odds API
        if ODDS_API_KEY:
            try:
                from scraper.odds_api import OddsAPIClient
                odds_client = OddsAPIClient(ODDS_API_KEY)
                odds_matches = odds_client.get_all_matches(active_leagues)
                if odds_matches:
                    if raw_matches:
                        from scraper.hybrid import merge_odds_into_matches
                        merge_odds_into_matches(raw_matches, odds_matches)
                    else:
                        raw_matches = odds_matches
                    logger.info(f"Worker preview: Odds API {len(odds_matches)} match merged")
                else:
                    logger.warning("Worker preview: Odds API quota esaurita")
            except Exception as e:
                logger.warning(f"Worker preview: Odds API fallita: {e}")

        now = datetime.now(timezone.utc)
        for m in raw_matches:
            try:
                ct = m.commence_time.replace("Z", "+00:00")
                m_date_utc = datetime.fromisoformat(ct)
                if m_date_utc.tzinfo is None:
                    m_date_utc = m_date_utc.replace(tzinfo=timezone.utc)
                m_date_rome = m_date_utc.astimezone(rome_tz)

                if now < m_date_utc < (now + timedelta(hours=24)):
                    upcoming_matches.append({
                        "home": m.home_team,
                        "away": m.away_team,
                        "date": m_date_rome.strftime("%d/%m %H:%M"),
                        "league": m.league
                    })
            except Exception:
                continue
        logger.info(f"Worker preview: {len(upcoming_matches)} match imminenti (24h).")
    # Fallback su cache se Odds API non restituisce nulla o fallisce
    if not upcoming_matches and active_leagues:
        from scraper.penalties import PenaltyAnalyzer, LEAGUE_CODES
        pa = PenaltyAnalyzer(FOOTBALL_DATA_KEY)
        for lk in active_leagues:
            try:
                code = LEAGUE_CODES.get(lk)
                if not code: continue
                cache = pa._load_cache(code)
                if not cache: continue
                
                # Carichiamo i nomi delle squadre dalla classifica per il fallback
                standings = pa._get_standings(code)
                team_names = {s["team_id"]: s["name"] for s in standings} if standings else {}
                
                # Identifichiamo i match pendenti (senza score)
                for mid, m in cache.items():
                    score = m.get("score", {})
                    full_time = score.get("fullTime", {})
                    if full_time.get("home") is None:
                        # Fallback basato su matchday se manca la data
                        md = m.get("matchday", "?")
                        upcoming_matches.append({
                            "home": team_names.get(m.get("home_id"), "Home"),
                            "away": team_names.get(m.get("away_id"), "Away"),
                            "date": f"Giornata {md}",
                            "league": league_display_names.get(lk, lk)
                        })
            except Exception: continue

    upcoming_matches.sort(key=lambda x: x["date"])

    return render_template("worker_settings.html", 
                           leagues=LEAGUE_LABELS,
                           league_names=league_display_names,
                           active_leagues=active_leagues,
                           alert_email=alert_email,
                           alerts_log=alerts_log,
                           upcoming_matches=upcoming_matches[:15], # Max 15 per non intasare
                           state=_state)


@app.route("/api/league/<league_id>/top-xi")
def league_top_xi(league_id):
    """Restituisce il Top XI di un campionato. Supporta formazioni: 4-4-2, 3-4-3, 3-5-2."""
    import sqlite3, json
    db_path = Path(__file__).parent / "data" / "betanalyzer.db"
    if not db_path.exists():
        return jsonify({"error": "Database non trovato"}), 404

    # Mappa league display name → league_id nel DB
    league_map = {
        "Serie-A": "serie_a", "Italy-Serie-A": "serie_a", "serie_a": "serie_a", "SA": "serie_a",
        "Premier-League": "premier_league", "England-Premier-League": "premier_league", "premier_league": "premier_league", "PL": "premier_league",
        "La-Liga": "la_liga", "Spain-La-Liga": "la_liga", "la_liga": "la_liga", "PD": "la_liga",
        "Bundesliga": "bundesliga", "Germany-Bundesliga": "bundesliga", "BL1": "bundesliga",
        "Ligue-1": "ligue_1", "France-Ligue-1": "ligue_1", "ligue_1": "ligue_1", "FL1": "ligue_1",
    }
    db_league = league_map.get(league_id, league_id.lower().replace(" ", "_").replace("-", "_"))

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    # Formazioni supportate
    FORMATIONS = {
        "4-4-2": {24: 1, 25: 4, 26: 4, 27: 2},
        "3-4-3": {24: 1, 25: 3, 26: 4, 27: 3},
        "3-5-2": {24: 1, 25: 3, 26: 5, 27: 2},
    }
    req_formation = request.args.get("formation", "4-4-2")
    if req_formation not in FORMATIONS:
        req_formation = "4-4-2"

    formation = {"name": req_formation, "slots": FORMATIONS[req_formation]}
    position_labels = {24: "Portiere", 25: "Difensore", 26: "Centrocampista", 27: "Attaccante"}
    position_short = {24: "GK", 25: "DEF", 26: "MID", 27: "ATT"}

    players = []
    for pos_id, count in formation["slots"].items():
        cur = conn.execute('''
            SELECT pi.player_id, pi.name, pi.team_name, pi.position_id,
                   ps.rating,
                   ps.stats_json
            FROM player_info pi
            JOIN player_stats_cache ps ON pi.player_id = ps.player_id
            WHERE pi.league_id = ? AND pi.position_id = ? AND ps.rating > 0
            ORDER BY ps.rating DESC
            LIMIT ?
        ''', (db_league, pos_id, count))

        for row in cur:
            stats = json.loads(row["stats_json"]) if row["stats_json"] else {}
            players.append({
                "id": row["player_id"],
                "name": row["name"],
                "team": row["team_name"],
                "position": position_labels.get(pos_id, "?"),
                "position_short": position_short.get(pos_id, "?"),
                "position_id": pos_id,
                "rating": round(row["rating"], 1),
                "goals": stats.get("goals", 0) or 0,
                "assists": stats.get("assists", 0) or 0,
                "appearances": stats.get("appearances", 0) or 0,
                "shots_total": stats.get("shots_total", 0) or 0,
                "key_passes": stats.get("key_passes", 0) or 0,
                "tackles": stats.get("tackles", 0) or 0,
                "interceptions": stats.get("interceptions", 0) or 0,
                "clearances": stats.get("clearances", 0) or 0,
                "dribbles": stats.get("dribbles_success", 0) or 0,
                "pass_accuracy": round(stats.get("accurate_passes_pct", 0) or 0, 1),
                "aerials_won": stats.get("aerials_won", 0) or 0,
            })

    conn.close()

    return jsonify({
        "league": league_id,
        "formation": formation["name"],
        "players": players,
        "total": len(players),
    })


@app.route("/teams")
def teams_list():
    from logic.roster import RosterManager
    rm = RosterManager(FOOTBALL_DATA_KEY)
    competitions = rm.get_all_competitions_with_stats()
    freshness = get_data_freshness(
        ("Classifica", "data/competitions_stats_cache.json", "Football-Data API / Precache"),
        ("Giocatori", "db:player_info", "Sportmonks DB / Nightly Sync"),
    )
    return render_template("teams.html", competitions=competitions, state=_state, freshness=freshness)

@app.route("/api/league-records/<league_key>")
def api_league_records(league_key):
    from scraper.league_records import get_league_records
    return jsonify(get_league_records(league_key))

@app.route("/team/<int:team_id>")
def team_detail(team_id):
    lk = request.args.get("league")
    if not lk:
        from logic.roster import RosterManager
        rm = RosterManager(FOOTBALL_DATA_KEY)
        details = rm.get_team_details(team_id)
        if details: lk = details["league_key"]
    
    if not lk: return "Lega non trovata", 404
    
    from logic.roster import RosterManager, LEAGUE_CODES
    from scraper.transfermarkt import TransfermarktScraper
    rm = RosterManager(FOOTBALL_DATA_KEY)
    tm = TransfermarktScraper()
    league_code = LEAGUE_CODES.get(lk)
    
    players, team_stats, leaders = rm.get_roster_with_stats(team_id, league_code, lk)
    team = rm.get_team_details(team_id)
    
    # Timing and grouping
    all_impacts = []
    players_by_role = {
        "Portieri": [],
        "Difensori Centrali": [],
        "Terzini": [],
        "Centrocampisti Difensivi": [],
        "Centrocampisti Centrali": [],
        "Trequartisti": [],
        "Ali": [],
        "Attaccanti": [],
    }
    role_map = {
        "Goalkeeper": "Portieri",
        "Defence": "Difensori Centrali", "Centre-Back": "Difensori Centrali", "DC": "Difensori Centrali",
        "Left-Back": "Terzini", "Right-Back": "Terzini", "TS": "Terzini", "TD": "Terzini", "Terzino sinistro": "Terzini", "Terzino destro": "Terzini",
        "Defensive Midfield": "Centrocampisti Difensivi", "CDM": "Centrocampisti Difensivi",
        "Midfield": "Centrocampisti Centrali", "Central Midfield": "Centrocampisti Centrali", "CEN": "Centrocampisti Centrali", "CC": "Centrocampisti Centrali",
        "Attacking Midfield": "Trequartisti", "Trequartista": "Trequartisti",
        "Left Winger": "Ali", "Right Winger": "Ali",
        "Offence": "Attaccanti", "Attacker": "Attaccanti", "Centre-Forward": "Attaccanti", "ATT": "Attaccanti",
    }
    # Fallback map for Dream Team (old 4-group keys)
    _dream_role_map = {
        "Portieri": "Goalkeeper", "Difensori Centrali": "Defence", "Terzini": "Defence",
        "Centrocampisti Difensivi": "Midfield", "Centrocampisti Centrali": "Midfield",
        "Trequartisti": "Midfield", "Ali": "Offence", "Attaccanti": "Offence",
    }

    # Mapping ruoli italiani Transfermarkt → nostri gruppi granulari
    tm_role_map = {
        "Portiere": "Portieri",
        "Difensore centrale": "Difensori Centrali", "Difesa": "Difensori Centrali",
        "Terzino sinistro": "Terzini", "Terzino destro": "Terzini",
        "Centrocampista difensivo": "Centrocampisti Difensivi", "Mediano": "Centrocampisti Difensivi", "Pivot": "Centrocampisti Difensivi",
        "Centrocampista": "Centrocampisti Centrali", "Centrocampista centrale": "Centrocampisti Centrali", "Mezzala": "Centrocampisti Centrali",
        "Trequartista": "Trequartisti", "Fantasista": "Trequartisti",
        "Ala sinistra": "Ali", "Ala destra": "Ali", "Esterno sinistro": "Ali", "Esterno destro": "Ali",
        "Attaccante": "Attaccanti", "Attaccante centrale": "Attaccanti", "Centravanti": "Attaccanti", "Punta centrale": "Attaccanti", "Seconda punta": "Attaccanti", "Punta": "Attaccanti",
    }

    # Fetch TM positions for all players (cache makes repeated calls instant)
    team_name_for_tm = team.get("name", "")
    tm_position_overrides = {}
    for p in players:
        pname = p.get("name", "")
        tm_info = tm.get_player_info(pname, team_name_for_tm)
        if tm_info and tm_info.get("detailed_role") and tm_info["detailed_role"] != "N/D":
            tm_grp = tm_role_map.get(tm_info["detailed_role"])
            if tm_grp:
                tm_position_overrides[pname] = tm_grp

    real_matches_played = team.get("played", team_stats.get("matches_played", 0))
    min_appearances = max(5, int(real_matches_played * 0.40))

    for p in players:
        pname = p.get("name", "")
        # Use TM override if available, else Football-Data position
        if pname in tm_position_overrides:
            mapped_role = tm_position_overrides[pname]
        else:
            orig_role = p.get("role") or p.get("position") or "Offence"
            mapped_role = role_map.get(orig_role, "Attaccanti")
        if p.get("appearances", 0) >= min_appearances:
            all_impacts.append({
                "id": p["id"],
                "name": p["name"],
                "wr": p.get("win_rate_with", 0),
                "drop": p.get("impact_drop", 0),
                "played": p.get("appearances", 0)
            })
        players_by_role[mapped_role].append(p)

    sorted_by_drop = sorted(all_impacts, key=lambda x: x["drop"], reverse=True)
    top_impact = sorted_by_drop[:3]
    potential_bottom = sorted(all_impacts, key=lambda x: x["drop"])
    bottom_impact = []
    top_names = [p["name"] for p in top_impact]
    for p in potential_bottom:
        if p["name"] not in top_names:
            bottom_impact.append(p)
        if len(bottom_impact) >= 3: break
    if top_impact and bottom_impact and top_impact[0]["wr"] == bottom_impact[0]["wr"]:
        bottom_impact = []

    # Calcolo Dream Team Dinamico (Top Impact Drop)
    # Formazione: Cerchiamo quella dell'allenatore su TM, altrimenti usiamo l'inferenza statistica
    official_formation = tm.get_team_formation(team.get("name"))
    if official_formation:
        formation_str = official_formation
    else:
        formation_str = team_stats.get("most_used_formation", "4-4-2")
    
    try:
        parts = formation_str.split("-")
        if len(parts) == 3:
            f_parts = [int(x) for x in parts]
        elif len(parts) == 4:
            # Caso 3-4-2-1 o 4-2-3-1 -> [Dif, Cent, Att]
            f_parts = [int(parts[0]), int(parts[1]), int(parts[2]) + int(parts[3])]
        else:
            f_parts = [4, 4, 2]
    except:
        f_parts = [4, 4, 2]
    
    # Scala di lateralità per posizionamento corretto sul campo (Incluso sigle TD, TS, DC, CEN, CDM, CC...)
    lateral_map = {
        "Left-Back": 1, "Terzino sinistro": 1, "TS": 1, "Left Midfield": 1, "Left Winger": 1, "Ala sinistra": 1,
        "Centre-Back": 3, "Difensore centrale": 3, "DC": 3, "Central Midfield": 3, "CEN": 3, "CC": 3, "Defensive Midfield": 3, "CDM": 3, "Attacking Midfield": 3, "Centre-Forward": 3, "Second Striker": 3, "Punta centrale": 3, "ATT": 3,
        "Right-Back": 5, "Terzino destro": 5, "TD": 5, "Right Midfield": 5, "Right Winger": 5, "Ala destra": 5
    }

    # Build merged pools for Dream Team (old 4-group logic)
    _dream_pools = {"Goalkeeper": [], "Defence": [], "Midfield": [], "Offence": []}
    for grp_name, grp_players in players_by_role.items():
        dream_key = _dream_role_map.get(grp_name, "Offence")
        _dream_pools[dream_key].extend(grp_players)

    dream_team = {"Goalkeeper": [], "Defence": [], "Midfield": [], "Offence": [], "formation": formation_str}
    for role in ["Goalkeeper", "Defence", "Midfield", "Offence"]:
        if role == "Goalkeeper": count = 1
        elif role == "Defence": count = f_parts[0]
        elif role == "Midfield": count = f_parts[1]
        else: count = f_parts[2]

        # Try with standard min_appearances first, then relax if not enough players
        valid_players = [p for p in _dream_pools.get(role, []) if p.get("appearances", 0) >= min_appearances]
        if len(valid_players) < count:
            # Relax to 20% of matches
            fallback_min = max(3, int(real_matches_played * 0.20))
            valid_players = [p for p in _dream_pools.get(role, []) if p.get("appearances", 0) >= fallback_min]
        if len(valid_players) < count:
            # Last resort: any player with at least 1 appearance
            valid_players = [p for p in _dream_pools.get(role, []) if p.get("appearances", 0) >= 1]
        sorted_players = sorted(valid_players, key=lambda x: x.get("impact_drop", -100), reverse=True)

        selected = sorted_players[:count]
        # Ordiniamo i selezionati per lateralità (da sinistra a destra)
        selected.sort(key=lambda x: lateral_map.get(x.get("role"), 3))
        
        # Arricchimento dati Transfermarkt (Best Effort)
        for p in selected:
            tm_info = tm.get_player_info(p["name"], team.get("name"))
            if tm_info:
                p["market_value"] = tm_info.get("market_value")
                p["foot"] = tm_info.get("foot")
                # Se il ruolo su TM è più specifico, lo usiamo per la lateralità
                tm_role = tm_info.get("detailed_role")
                if tm_role and tm_role != "N/D":
                    p["detailed_role"] = tm_role
                    # Aggiorniamo la lateralità se il ruolo è specifico
                    if "sinistro" in tm_role.lower() or "mancino" in tm_role.lower():
                        p["tm_lateral"] = 1
                    elif "destro" in tm_role.lower():
                        p["tm_lateral"] = 5
                    else:
                        p["tm_lateral"] = 3
        
        # Ri-ordiniamo se abbiamo dati TM sulla lateralità
        selected.sort(key=lambda x: x.get("tm_lateral", lateral_map.get(x.get("role"), 3)))
        dream_team[role] = selected

    # Rank history
    rank_history = rm.get_team_rank_history(team_id, league_code)
    # League teams
    standings = rm.pa._get_standings(league_code)
    league_teams = [{"id": s["team_id"], "name": s["name"]} for s in standings]

    # Betting
    betting_stats = rm.get_team_betting_stats(team_id, league_code)
    league_avg_stats = {"avg_scored": 1.3, "avg_conceded": 1.3}
    top_scores = rm.predict_correct_score(betting_stats, league_avg_stats) if betting_stats else []

    # Formation stats from DB
    from db.database import get_team_formations
    formation_stats = get_team_formations(team_id)

    return render_template("team_detail.html",
                         team=team,
                         players=players,
                         players_by_role=players_by_role,
                         team_stats=team_stats,
                         leaders=leaders,
                         top_impact=top_impact,
                         bottom_impact=bottom_impact,
                         rank_history=rank_history,
                         league_teams=league_teams,
                         betting_stats=betting_stats,
                         top_scores=top_scores,
                         dream_team=dream_team,
                         formation_stats=formation_stats,
                         state=_state)

@app.route("/player/<int:player_id>")
def player_detail(player_id):
    team_id = request.args.get("team_id", type=int)
    lk = request.args.get("league")
    if not team_id or not lk: return "Contesto mancante", 400
    from logic.roster import RosterManager
    rm = RosterManager(FOOTBALL_DATA_KEY)
    data = rm.get_player_full_details(player_id, team_id, lk)
    if not data: return "Giocatore non trovato", 404
    return render_template("player_detail.html", state=_state, **data)

@app.route("/api/rank_history/<int:team_id>/<string:league_code>")
def api_rank_history(team_id, league_code):
    from logic.roster import RosterManager
    rm = RosterManager(FOOTBALL_DATA_KEY)
    history = rm.get_team_rank_history(team_id, league_code)
    return jsonify({"success": True, "history": history})

@app.route("/predictions")
def predictions_page():
    from logic.roster import RosterManager
    rm = RosterManager(FOOTBALL_DATA_KEY)
    preds = rm.get_upcoming_predictions()
    return render_template("predictions.html", predictions=preds, state=_state)

@app.route("/api/worker/toggle", methods=["POST"])
def api_worker_toggle():
    """Attiva o disattiva il Worker."""
    enabled = request.form.get("enabled")
    status = "on" if enabled else "off"
    from db.database import save_worker_setting
    save_worker_setting("worker_enabled", status)
    logger.info(f"Worker interruttore impostato su: {status}")
    return redirect(request.referrer or url_for("index"))

@app.route("/api/worker/test", methods=["POST"])
def api_worker_test():
    """Esegue un test diagnostico completo del Worker."""
    from db.database import get_worker_setting
    from scraper.odds_api import OddsAPIClient
    from logic.notifications import EmailService
    
    try:
        active_leagues_raw = get_worker_setting("active_leagues", "[]")
        active_leagues = json.loads(active_leagues_raw)
        target_email = get_worker_setting("alert_email")
        
        if not target_email:
            return jsonify({"success": False, "error": "Configura prima l'email destinatario!"})
        
        # 1. Test Odds API
        client = OddsAPIClient(ODDS_API_KEY)
        # Proviamo a recuperare un match qualsiasi (es. Serie A) per testare la chiave
        test_matches = client.get_all_matches(["italy_serie_a"])
        if not test_matches:
             logger.warning("Odds API Test: chiave valida ma nessun match trovato (normale se fine stagione)")
        
        # 2. Test Email
        email_service = EmailService()
        match_info = {
            "home": "TEST SYSTEM",
            "away": "DIAGNOSTIC",
            "date": datetime.now().strftime("%d/%m %H:%M"),
            "league": "Test Connection"
        }
        ai_suggestion = "Questo è un alert di test per confermare che il Worker è configurato correttamente. Se leggi questo messaggio, il sistema è pronto per i match reali!"
        
        success = email_service.send_bet_alert(target_email, match_info, ai_suggestion)
        
        if success:
            return jsonify({"success": True})
        else:
            return jsonify({"success": False, "error": "Impossibile inviare email. Controlla le API key di Resend/Brevo nel file .env"})
            
    except Exception as e:
        logger.error(f"Errore durante il test del worker: {e}")
        return jsonify({"success": False, "error": str(e)})

# ── My Bets ──

@app.route("/my-bets")
def my_bets_page():
    return render_template("my_bets.html", state=_state)


@app.route("/api/my-bets", methods=["GET"])
def api_get_bets():
    from db.database import get_bets_with_clv, get_bets_stats, get_clv_stats
    status = request.args.get("status")
    date = request.args.get("date")
    bets = get_bets_with_clv()
    # Apply filters after CLV computation
    if status:
        bets = [b for b in bets if b["result"] == status]
    if date:
        bets = [b for b in bets if b["match_date"].startswith(date)]
    stats = get_bets_stats()
    clv_stats = get_clv_stats()
    stats["clv"] = clv_stats
    return jsonify({"bets": bets, "stats": stats})


@app.route("/api/my-bets", methods=["POST"])
def api_save_bet():
    from db.database import save_bet
    data = request.json
    bet_id = save_bet(
        match_id=data["match_id"],
        home_team=data["home_team"],
        away_team=data["away_team"],
        league=data.get("league", ""),
        match_date=data["match_date"],
        bet_type=data["bet_type"],
        player_name=data.get("player_name"),
        odds=float(data["odds"]),
        stake=float(data.get("stake", 0))
    )
    return jsonify({"success": True, "id": bet_id})


@app.route("/api/my-bets/<int:bet_id>", methods=["DELETE"])
def api_delete_bet(bet_id):
    from db.database import delete_bet
    delete_bet(bet_id)
    return jsonify({"success": True})


@app.route("/api/my-bets/<int:bet_id>/settle", methods=["POST"])
def api_settle_bet(bet_id):
    from db.database import settle_bet
    data = request.json
    result = data["result"]  # "won" or "lost"
    # Recupera la bet per calcolare il profitto
    from db.database import get_bets
    all_bets = get_bets()
    bet = next((b for b in all_bets if b["id"] == bet_id), None)
    if not bet:
        return jsonify({"success": False, "error": "Bet non trovata"})
    if result == "won":
        profit = round(bet["stake"] * (bet["odds"] - 1), 2)
    else:
        profit = -bet["stake"]
    settle_bet(bet_id, result, profit)
    return jsonify({"success": True, "profit": profit})


@app.route("/api/backtest")
def api_backtest():
    """Backtesting stats: model accuracy across settled predictions."""
    from db.database import get_backtest_stats
    return jsonify(get_backtest_stats())


@app.route("/api/worldcup/import", methods=["POST"])
def api_wc_import():
    """Import/refresh WC squads into DB and match with our player data."""
    from scraper.worldcup import import_squads
    result = import_squads()
    return jsonify(result)


@app.route("/api/worldcup/rankings")
def api_wc_rankings():
    """Get all WC squads ranked by average player rating."""
    from scraper.worldcup import get_all_squads_summary
    return jsonify(get_all_squads_summary())


@app.route("/api/worldcup/squad/<country>")
def api_wc_squad(country):
    """Get a specific country's WC squad with matched stats."""
    from scraper.worldcup import get_squad
    return jsonify(get_squad(country))


@app.route("/api/worldcup/top-scorers")
def api_wc_top_scorers():
    """Top WC scorer candidates based on club stats."""
    from scraper.worldcup import get_top_scorers
    limit = request.args.get("limit", 20, type=int)
    return jsonify(get_top_scorers(limit))


@app.route("/api/worldcup/top-cards")
def api_wc_top_cards():
    """Top WC card candidates based on club stats."""
    from scraper.worldcup import get_top_card_candidates
    limit = request.args.get("limit", 20, type=int)
    return jsonify(get_top_card_candidates(limit))


@app.route("/api/today-lineups")
def api_today_lineups():
    """Restituisce le partite di oggi con i giocatori delle formazioni (da alerts_log)."""
    import sqlite3
    conn = sqlite3.connect("data/betanalyzer.db")
    conn.row_factory = sqlite3.Row

    today = datetime.now().strftime("%Y-%m-%d")

    # Prendi gli alert di oggi che contengono le formazioni
    alerts = conn.execute(
        "SELECT match_id, home_team, away_team, league, match_date, recommendation FROM alerts_log WHERE match_date LIKE ?",
        (f"{today}%",)
    ).fetchall()

    matches = []
    for a in alerts:
        # Estrai nomi giocatori dalla recommendation (formato: "Nome Cognome (Ruolo)")
        rec = a["recommendation"]
        home_players = []
        away_players = []

        # Parse giocatori dal formato AI:
        # "**Genoa:**" seguito da "- Nome (Ruolo) [VOTO: XX]"
        # oppure "ROSA TITOLARE Genoa (N giocatori):" seguito da "- Nome (Ruolo) XX.X"
        import re
        lines = rec.split("\n")
        current_team = None
        home_clean = a["home_team"].lower().strip()
        away_clean = a["away_team"].lower().strip()

        for line in lines:
            line_lower = line.lower().strip()

            # Detect team header (multiple formats: **Team:**, ### Team, ROSA TITOLARE Team)
            is_header = ("**" in line or line.strip().startswith("###") or
                         "ROSA" in line.upper() or "FORMAZI" in line.upper())
            if is_header:
                if any(h in line_lower for h in [home_clean, home_clean.split()[-1]]):
                    current_team = "home"
                    continue
                elif any(h in line_lower for h in [away_clean, away_clean.split()[-1]]):
                    current_team = "away"
                    continue
            if "MEDIA VOTO" in line:
                current_team = None
                continue
            elif line.strip().startswith("---"):
                current_team = None
                continue

            if current_team and line.strip().startswith("- "):
                # Estrai nome: "- Nome Cognome (Ruolo) [VOTO: XX]" — il ruolo deve essere un ruolo calcistico
                valid_roles = ["goalkeeper", "centre-back", "left-back", "right-back", "defence",
                               "defender", "midfield", "midfielder", "central midfield",
                               "defensive midfield", "attacking midfield", "left midfield",
                               "right midfield", "offence", "attacker", "centre-forward",
                               "second striker", "left winger", "right winger", "forward"]
                match = re.match(r'^-\s+(.+?)\s+\(([^)]+)\)', line.strip())
                if match:
                    name = match.group(1).strip()
                    role = match.group(2).strip().lower()
                    if any(r in role for r in valid_roles):
                        if current_team == "home":
                            home_players.append(name)
                        else:
                            away_players.append(name)

        matches.append({
            "match_id": a["match_id"].replace("_OFF", "").replace("_PROB", ""),
            "home_team": a["home_team"],
            "away_team": a["away_team"],
            "league": a["league"],
            "match_date": a["match_date"],
            "home_players": home_players,
            "away_players": away_players
        })

    conn.close()
    return jsonify({"matches": matches})


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    logger.info(f"BetAnalyzer avviato su http://localhost:{port}")
    logger.info("BetAnalyzer pronto. In attesa di configurazione dall'utente.")
    app.run(host="0.0.0.0", port=port, debug=True)
