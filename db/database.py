import sqlite3
import json
import re
import logging
from pathlib import Path
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent.parent / "data" / "betanalyzer.db"


def _get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    conn = _get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS scans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            source TEXT NOT NULL,
            leagues TEXT NOT NULL,
            match_count INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS scan_matches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_id INTEGER NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
            match_data TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_id INTEGER NOT NULL REFERENCES scans(id),
            match_id TEXT NOT NULL,
            home_team TEXT NOT NULL,
            away_team TEXT NOT NULL,
            league TEXT NOT NULL,
            match_date TEXT NOT NULL,
            market TEXT NOT NULL,
            odds_value REAL NOT NULL,
            bookmaker TEXT NOT NULL,
            ev_pct REAL,
            prob_estimated REAL,
            prob_implied REAL,
            result TEXT,
            settled_at TEXT
        );

        CREATE TABLE IF NOT EXISTS alerts_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id TEXT NOT NULL,
            home_team TEXT NOT NULL,
            away_team TEXT NOT NULL,
            league TEXT NOT NULL,
            match_date TEXT NOT NULL,
            recommendation TEXT NOT NULL,
            sent_at TEXT NOT NULL,
            status TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS worker_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS player_stats_cache (
            player_id INTEGER,
            season_id INTEGER,
            team_id INTEGER,
            stats_json TEXT NOT NULL,
            rating REAL DEFAULT 0,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (player_id, season_id, team_id)
        );

        CREATE TABLE IF NOT EXISTS player_info (
            player_id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            team_id INTEGER,
            team_name TEXT,
            league_id TEXT,
            position_id INTEGER,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS team_formations (
            team_id INTEGER NOT NULL,
            season_id INTEGER NOT NULL,
            formation TEXT NOT NULL,
            matches INTEGER NOT NULL DEFAULT 0,
            wins INTEGER NOT NULL DEFAULT 0,
            draws INTEGER NOT NULL DEFAULT 0,
            losses INTEGER NOT NULL DEFAULT 0,
            goals_for INTEGER NOT NULL DEFAULT 0,
            goals_against INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (team_id, season_id, formation)
        );

        CREATE TABLE IF NOT EXISTS my_bets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id TEXT NOT NULL,
            home_team TEXT NOT NULL,
            away_team TEXT NOT NULL,
            league TEXT,
            match_date TEXT NOT NULL,
            bet_type TEXT NOT NULL,
            player_name TEXT,
            odds REAL NOT NULL,
            stake REAL NOT NULL DEFAULT 0,
            result TEXT DEFAULT 'pending',
            profit REAL DEFAULT 0,
            settled_at TEXT,
            created_at TEXT NOT NULL
        );
    """)
    # Migrations for existing databases
    try:
        conn.execute("ALTER TABLE player_stats_cache ADD COLUMN rating REAL DEFAULT 0")
    except Exception:
        pass  # column already exists
    conn.close()
    logger.info(f"Database inizializzato: {DB_PATH}")


# ── My Bets CRUD ──

def save_bet(match_id, home_team, away_team, league, match_date, bet_type, player_name, odds, stake):
    conn = _get_conn()
    try:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        cursor = conn.execute(
            """INSERT INTO my_bets (match_id, home_team, away_team, league, match_date,
               bet_type, player_name, odds, stake, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (match_id, home_team, away_team, league, match_date, bet_type, player_name, odds, stake, ts)
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def get_bets(status=None, date=None):
    conn = _get_conn()
    try:
        where = ["1=1"]
        params = []
        if status:
            where.append("result = ?")
            params.append(status)
        if date:
            where.append("match_date LIKE ?")
            params.append(f"{date}%")
        rows = conn.execute(
            f"SELECT * FROM my_bets WHERE {' AND '.join(where)} ORDER BY created_at DESC",
            params
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def delete_bet(bet_id):
    conn = _get_conn()
    try:
        conn.execute("DELETE FROM my_bets WHERE id = ?", (bet_id,))
        conn.commit()
    finally:
        conn.close()


def settle_bet(bet_id, result, profit):
    conn = _get_conn()
    try:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute(
            "UPDATE my_bets SET result = ?, profit = ?, settled_at = ? WHERE id = ?",
            (result, profit, ts, bet_id)
        )
        conn.commit()
    finally:
        conn.close()


def get_bets_stats():
    """Statistiche aggregate delle scommesse."""
    conn = _get_conn()
    try:
        total = conn.execute("SELECT COUNT(*) FROM my_bets").fetchone()[0]
        settled = conn.execute("SELECT COUNT(*) FROM my_bets WHERE result != 'pending'").fetchone()[0]
        won = conn.execute("SELECT COUNT(*) FROM my_bets WHERE result = 'won'").fetchone()[0]
        lost = conn.execute("SELECT COUNT(*) FROM my_bets WHERE result = 'lost'").fetchone()[0]
        total_stake = conn.execute("SELECT COALESCE(SUM(stake), 0) FROM my_bets WHERE result != 'pending'").fetchone()[0]
        total_profit = conn.execute("SELECT COALESCE(SUM(profit), 0) FROM my_bets").fetchone()[0]
        return {
            "total": total, "settled": settled, "won": won, "lost": lost,
            "pending": total - settled,
            "win_rate": round(won / settled * 100, 1) if settled else 0,
            "total_stake": round(total_stake, 2),
            "total_profit": round(total_profit, 2),
            "roi": round(total_profit / total_stake * 100, 1) if total_stake else 0
        }
    finally:
        conn.close()


def save_player_info(player_id: int, name: str, team_id: int, team_name: str, league_id: str, position_id: int, date_of_birth: str = None):
    conn = _get_conn()
    try:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute(
            """INSERT OR REPLACE INTO player_info (player_id, name, team_id, team_name, league_id, position_id, updated_at, date_of_birth)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (player_id, name, team_id, team_name, league_id, position_id, ts, date_of_birth)
        )
        conn.commit()
    finally:
        conn.close()


def save_team_formations(team_id: int, season_id: int, formations: list[dict]):
    """Save formation stats for a team. formations is list of dicts with keys:
       formation, matches, wins, draws, losses, goals_for, goals_against"""
    conn = _get_conn()
    try:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        # Clear old data for this team/season
        conn.execute("DELETE FROM team_formations WHERE team_id = ? AND season_id = ?",
                      (team_id, season_id))
        for f in formations:
            conn.execute(
                """INSERT INTO team_formations
                   (team_id, season_id, formation, matches, wins, draws, losses, goals_for, goals_against, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (team_id, season_id, f["formation"], f["matches"], f["wins"], f["draws"],
                 f["losses"], f["goals_for"], f["goals_against"], ts)
            )
        conn.commit()
    finally:
        conn.close()


def get_team_formations(team_id: int, season_id: int = None) -> list[dict]:
    """Get formation stats for a team, sorted by matches played desc."""
    conn = _get_conn()
    try:
        if season_id:
            rows = conn.execute(
                "SELECT * FROM team_formations WHERE team_id = ? AND season_id = ? ORDER BY matches DESC",
                (team_id, season_id)).fetchall()
        else:
            # Get latest season
            rows = conn.execute(
                """SELECT * FROM team_formations WHERE team_id = ? AND season_id = (
                    SELECT MAX(season_id) FROM team_formations WHERE team_id = ?
                ) ORDER BY matches DESC""",
                (team_id, team_id)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def list_players_with_stats(filters: dict, page: int = 1, per_page: int = 40):
    conn = _get_conn()
    try:
        offset = (page - 1) * per_page
        
        # Filtri comuni per query e conteggio
        where_clauses = ["1=1"]
        params = []
        
        if filters.get("search"):
            import unicodedata
            search_term = filters['search']
            # Normalizzazione NFC per risolvere discrepanze Mac/Web
            normalized_search = unicodedata.normalize('NFC', search_term)
            where_clauses.append("pi.name LIKE ?")
            params.append(f"%{normalized_search}%")
        
        if filters.get("league"):
            where_clauses.append("pi.league_id = ?")
            params.append(filters["league"])
            
        if filters.get("role"):
            where_clauses.append("pi.position_id = ?")
            params.append(int(filters["role"]))

        if filters.get("age_group"):
            # Filtro per fascia d'età basato su date_of_birth
            from datetime import date
            today = date.today()
            ag = filters["age_group"]
            if ag == "u21":
                # Nati dopo (today - 21 anni)
                min_dob = today.replace(year=today.year - 21).isoformat()
                where_clauses.append("pi.date_of_birth >= ?")
                params.append(min_dob)
            elif ag == "21-25":
                max_dob = today.replace(year=today.year - 21).isoformat()
                min_dob = today.replace(year=today.year - 25).isoformat()
                where_clauses.append("pi.date_of_birth < ? AND pi.date_of_birth >= ?")
                params.append(max_dob)
                params.append(min_dob)
            elif ag == "26-30":
                max_dob = today.replace(year=today.year - 26).isoformat()
                min_dob = today.replace(year=today.year - 30).isoformat()
                where_clauses.append("pi.date_of_birth < ? AND pi.date_of_birth >= ?")
                params.append(max_dob)
                params.append(min_dob)
            elif ag == "30+":
                max_dob = today.replace(year=today.year - 30).isoformat()
                where_clauses.append("pi.date_of_birth < ?")
                params.append(max_dob)

        if filters.get("min_assists"):
            where_clauses.append("CAST(json_extract(psc.stats_json, '$.assists') AS INTEGER) >= ?")
            params.append(int(filters["min_assists"]))

        if filters.get("min_dribbles"):
            where_clauses.append("CAST(json_extract(psc.stats_json, '$.dribbles_success') AS INTEGER) >= ?")
            params.append(int(filters["min_dribbles"]))

        if filters.get("min_interceptions"):
            where_clauses.append("CAST(json_extract(psc.stats_json, '$.interceptions') AS INTEGER) >= ?")
            params.append(int(filters["min_interceptions"]))

        if filters.get("min_recoveries"):
            # Recuperi = Intercettazioni + Contrasti
            where_clauses.append("(CAST(json_extract(psc.stats_json, '$.interceptions') AS INTEGER) + CAST(json_extract(psc.stats_json, '$.tackles') AS INTEGER)) >= ?")
            params.append(int(filters["min_recoveries"]))
            
        where_sql = " AND ".join(where_clauses)
        
        # Ordinamento SQL
        sort_field = filters.get("sort", "goals")
        valid_sorts = {
            "goals": "COALESCE(CAST(json_extract(psc.stats_json, '$.goals') AS INTEGER), 0)",
            "assists": "COALESCE(CAST(json_extract(psc.stats_json, '$.assists') AS INTEGER), 0)",
            "appearances": "COALESCE(CAST(json_extract(psc.stats_json, '$.appearances') AS INTEGER), 0)",
            "shots_on_target": "COALESCE(CAST(json_extract(psc.stats_json, '$.shots_on_target') AS INTEGER), 0)",
            "key_passes": "COALESCE(CAST(json_extract(psc.stats_json, '$.key_passes') AS INTEGER), 0)",
            "dribbles": "COALESCE(CAST(json_extract(psc.stats_json, '$.dribbles_success') AS INTEGER), 0)",
            "interceptions": "COALESCE(CAST(json_extract(psc.stats_json, '$.interceptions') AS INTEGER), 0)",
            "tackles": "COALESCE(CAST(json_extract(psc.stats_json, '$.tackles') AS INTEGER), 0)",
            "recoveries": "COALESCE((CAST(json_extract(psc.stats_json, '$.interceptions') AS INTEGER) + CAST(json_extract(psc.stats_json, '$.tackles') AS INTEGER)), 0)",
            "fouls_committed": "COALESCE(CAST(json_extract(psc.stats_json, '$.fouls_committed') AS INTEGER), 0)",
            "fouls_drawn": "COALESCE(CAST(json_extract(psc.stats_json, '$.fouls_drawn') AS INTEGER), 0)",
            "aerials": "COALESCE(CAST(json_extract(psc.stats_json, '$.aerials_won') AS INTEGER), 0)",
            "pass_accuracy": "COALESCE(CAST(json_extract(psc.stats_json, '$.accurate_passes_pct') AS FLOAT), 0)",
            "big_chances": "COALESCE(CAST(json_extract(psc.stats_json, '$.big_chances_created') AS INTEGER), 0)",
            "saves": "COALESCE(CAST(json_extract(psc.stats_json, '$.saves') AS INTEGER), 0)",
            "clean_sheets": "COALESCE(CAST(json_extract(psc.stats_json, '$.clean_sheets') AS INTEGER), 0)",
            "goals_conceded": "COALESCE(CAST(json_extract(psc.stats_json, '$.goals_conceded') AS INTEGER), 0)",
            "clearances": "COALESCE(CAST(json_extract(psc.stats_json, '$.clearances') AS INTEGER), 0)",
            "rating": "COALESCE(psc.rating, 0)"
        }
        order_by_sql = valid_sorts.get(sort_field, valid_sorts["goals"])
        
        query = f"""
            SELECT pi.*, psc.stats_json, psc.rating 
            FROM player_info pi
            LEFT JOIN player_stats_cache psc ON pi.player_id = psc.player_id AND pi.team_id = psc.team_id
            AND psc.season_id = (
                SELECT MAX(season_id) FROM player_stats_cache 
                WHERE player_id = psc.player_id AND team_id = psc.team_id
            )
            WHERE {where_sql}
            ORDER BY {order_by_sql} DESC
            LIMIT ? OFFSET ?
        """
        
        # Esegui query principale
        rows = conn.execute(query, params + [per_page, offset]).fetchall()
        
        # Conteggio totale con STESSI filtri
        count_query = f"""
            SELECT COUNT(*) 
            FROM player_info pi 
            LEFT JOIN player_stats_cache psc ON pi.player_id = psc.player_id AND pi.team_id = psc.team_id
            WHERE {where_sql}
        """
        total_players = conn.execute(count_query, params).fetchone()[0]
        
        players = []
        for r in rows:
            p = dict(r)
            p["stats"] = json.loads(r["stats_json"]) if r["stats_json"] else {}
            players.append(p)
            
        return players, total_players
    finally:
        conn.close()


def list_team_stats(filters: dict = None, sort: str = "avg_rating") -> list[dict]:
    """Aggregate player stats per team for the team scouting view.

    Returns a list of dicts, one per team, with overall and per-role averages.
    Filters: league (str).
    Sort options: avg_rating, attack_rating, midfield_rating, defense_rating,
                  total_goals, total_assists, total_dribbles, total_recoveries,
                  total_aerials, avg_pass_acc, total_key_passes, total_big_chances,
                  total_shots.
    """
    filters = filters or {}
    conn = _get_conn()
    try:
        where_clauses = ["psc.rating > 0"]
        params = []

        if filters.get("league"):
            where_clauses.append("pi.league_id = ?")
            params.append(filters["league"])

        where_sql = " AND ".join(where_clauses)

        # Main aggregation: one row per team
        rows = conn.execute(f"""
            SELECT
                pi.team_id,
                pi.team_name,
                pi.league_id,
                COUNT(*) as num_players,
                ROUND(AVG(psc.rating), 1) as avg_rating,
                ROUND(MAX(psc.rating), 1) as best_rating,
                SUM(COALESCE(JSON_EXTRACT(psc.stats_json, '$.goals'), 0)) as total_goals,
                SUM(COALESCE(JSON_EXTRACT(psc.stats_json, '$.assists'), 0)) as total_assists,
                SUM(COALESCE(JSON_EXTRACT(psc.stats_json, '$.dribbles_success'), 0)) as total_dribbles,
                ROUND(AVG(COALESCE(JSON_EXTRACT(psc.stats_json, '$.accurate_passes_pct'), 0)), 1) as avg_pass_acc,
                SUM(COALESCE(JSON_EXTRACT(psc.stats_json, '$.interceptions'), 0)
                  + COALESCE(JSON_EXTRACT(psc.stats_json, '$.tackles'), 0)) as total_recoveries,
                SUM(COALESCE(JSON_EXTRACT(psc.stats_json, '$.aerials_won'), 0)) as total_aerials,
                SUM(COALESCE(JSON_EXTRACT(psc.stats_json, '$.shots_on_target'), 0)) as total_shots,
                SUM(COALESCE(JSON_EXTRACT(psc.stats_json, '$.key_passes'), 0)) as total_key_passes,
                SUM(COALESCE(JSON_EXTRACT(psc.stats_json, '$.big_chances_created'), 0)) as total_big_chances,
                SUM(COALESCE(JSON_EXTRACT(psc.stats_json, '$.fouls_committed'), 0)) as total_fouls
            FROM player_info pi
            JOIN player_stats_cache psc ON pi.player_id = psc.player_id AND pi.team_id = psc.team_id
            WHERE {where_sql}
            GROUP BY pi.team_id
        """, params).fetchall()

        team_map = {}
        for r in rows:
            team_map[r["team_id"]] = dict(r)
            team_map[r["team_id"]].update({
                "attack_rating": 0, "attack_count": 0,
                "midfield_rating": 0, "midfield_count": 0,
                "defense_rating": 0, "defense_count": 0,
                "gk_rating": 0, "gk_count": 0,
                "best_player": "", "best_player_rating": 0,
            })

        # Per-role averages
        role_rows = conn.execute(f"""
            SELECT
                pi.team_id,
                pi.position_id,
                ROUND(AVG(psc.rating), 1) as role_avg,
                COUNT(*) as role_count
            FROM player_info pi
            JOIN player_stats_cache psc ON pi.player_id = psc.player_id AND pi.team_id = psc.team_id
            WHERE {where_sql}
            GROUP BY pi.team_id, pi.position_id
        """, params).fetchall()

        for r in role_rows:
            tid = r["team_id"]
            if tid not in team_map:
                continue
            pos = r["position_id"]
            if pos == 27:
                team_map[tid]["attack_rating"] = r["role_avg"]
                team_map[tid]["attack_count"] = r["role_count"]
            elif pos == 26:
                team_map[tid]["midfield_rating"] = r["role_avg"]
                team_map[tid]["midfield_count"] = r["role_count"]
            elif pos == 25:
                team_map[tid]["defense_rating"] = r["role_avg"]
                team_map[tid]["defense_count"] = r["role_count"]
            elif pos == 24:
                team_map[tid]["gk_rating"] = r["role_avg"]
                team_map[tid]["gk_count"] = r["role_count"]

        # Best player per team
        best_rows = conn.execute(f"""
            SELECT pi.team_id, pi.name, psc.rating
            FROM player_info pi
            JOIN player_stats_cache psc ON pi.player_id = psc.player_id AND pi.team_id = psc.team_id
            WHERE {where_sql}
            AND psc.rating = (
                SELECT MAX(psc2.rating)
                FROM player_info pi2
                JOIN player_stats_cache psc2 ON pi2.player_id = psc2.player_id AND pi2.team_id = psc2.team_id
                WHERE pi2.team_id = pi.team_id AND psc2.rating > 0
            )
        """, params).fetchall()

        for r in best_rows:
            tid = r["team_id"]
            if tid in team_map:
                team_map[tid]["best_player"] = r["name"]
                team_map[tid]["best_player_rating"] = r["rating"]

        teams = list(team_map.values())

        # Sort
        valid_sorts = {
            "avg_rating": "avg_rating",
            "attack_rating": "attack_rating",
            "midfield_rating": "midfield_rating",
            "defense_rating": "defense_rating",
            "total_goals": "total_goals",
            "total_assists": "total_assists",
            "total_dribbles": "total_dribbles",
            "total_recoveries": "total_recoveries",
            "total_aerials": "total_aerials",
            "avg_pass_acc": "avg_pass_acc",
            "total_key_passes": "total_key_passes",
            "total_big_chances": "total_big_chances",
            "total_shots": "total_shots",
            "total_fouls": "total_fouls",
        }
        sort_key = valid_sorts.get(sort, "avg_rating")
        teams.sort(key=lambda t: t.get(sort_key, 0) or 0, reverse=True)

        return teams
    finally:
        conn.close()


def get_cached_player_stats(player_id: int, season_id: int, team_id: int) -> dict | None:
    conn = _get_conn()
    try:
        r = conn.execute(
            "SELECT stats_json, updated_at FROM player_stats_cache WHERE player_id = ? AND season_id = ? AND team_id = ?",
            (player_id, season_id, team_id)
        ).fetchone()
        if r:
            return json.loads(r["stats_json"])
        return None
    finally:
        conn.close()


def save_player_stats_cache(player_id: int, season_id: int, team_id: int, stats: dict, rating: float = 0.0):
    conn = _get_conn()
    try:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute(
            """INSERT OR REPLACE INTO player_stats_cache (player_id, season_id, team_id, stats_json, rating, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (player_id, season_id, team_id, json.dumps(stats), rating, ts)
        )
        conn.commit()
    except Exception as e:
        logger.error(f"Errore salvataggio cache stats per {player_id}: {e}")
    finally:
        conn.close()


def save_scan(source: str, leagues: list[str], matches: list) -> int:
    conn = _get_conn()
    try:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        cur = conn.execute(
            "INSERT INTO scans (timestamp, source, leagues, match_count) VALUES (?, ?, ?, ?)",
            (ts, source, json.dumps(leagues), len(matches))
        )
        scan_id = cur.lastrowid

        for match in matches:
            conn.execute(
                "INSERT INTO scan_matches (scan_id, match_data) VALUES (?, ?)",
                (scan_id, json.dumps(match.to_dict()))
            )

            for rec in match.recommendations:
                if not rec.play:
                    continue
                ev, prob_est, prob_imp = _parse_ev_from_reasoning(rec.reasoning)
                conn.execute(
                    """INSERT INTO predictions
                       (scan_id, match_id, home_team, away_team, league, match_date,
                        market, odds_value, bookmaker, ev_pct, prob_estimated, prob_implied)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (scan_id, match.id, match.home_team, match.away_team,
                     match.league, match.commence_time, rec.market,
                     rec.odds_value, rec.bookmaker, ev, prob_est, prob_imp)
                )

        conn.commit()
        logger.info(f"Scansione #{scan_id} salvata: {len(matches)} partite, source={source}")
        return scan_id
    except Exception as e:
        conn.rollback()
        logger.error(f"Errore salvataggio scansione: {e}")
        raise
    finally:
        conn.close()


def list_scans(limit: int = 50) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute("""
        SELECT s.*,
               COUNT(p.id) as prediction_count,
               SUM(CASE WHEN p.result = 'W' THEN 1 ELSE 0 END) as wins,
               SUM(CASE WHEN p.result IS NOT NULL THEN 1 ELSE 0 END) as settled
        FROM scans s
        LEFT JOIN predictions p ON p.scan_id = s.id
        GROUP BY s.id
        ORDER BY s.timestamp DESC
        LIMIT ?
    """, (limit,)).fetchall()
    conn.close()

    return [{
        "id": r["id"],
        "timestamp": r["timestamp"],
        "source": r["source"],
        "leagues": json.loads(r["leagues"]),
        "match_count": r["match_count"],
        "prediction_count": r["prediction_count"],
        "wins": r["wins"] or 0,
        "settled": r["settled"] or 0,
    } for r in rows]


def load_scan(scan_id: int) -> list:
    from models.match import Match
    conn = _get_conn()
    rows = conn.execute(
        "SELECT match_data FROM scan_matches WHERE scan_id = ?", (scan_id,)
    ).fetchall()
    conn.close()

    return [Match.from_dict(json.loads(r["match_data"])) for r in rows]


def get_scan_info(scan_id: int) -> dict | None:
    conn = _get_conn()
    r = conn.execute("SELECT * FROM scans WHERE id = ?", (scan_id,)).fetchone()
    conn.close()
    if not r:
        return None
    return {
        "id": r["id"],
        "timestamp": r["timestamp"],
        "source": r["source"],
        "leagues": json.loads(r["leagues"]),
        "match_count": r["match_count"],
    }


def get_unsettled_predictions() -> list[dict]:
    conn = _get_conn()
    rows = conn.execute("""
        SELECT p.*, sm.match_data
        FROM predictions p
        JOIN scan_matches sm ON sm.scan_id = p.scan_id
            AND json_extract(sm.match_data, '$.id') = p.match_id
        WHERE p.result IS NULL
        ORDER BY p.match_date ASC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def settle_prediction(prediction_id: int, result: str):
    conn = _get_conn()
    conn.execute(
        "UPDATE predictions SET result = ?, settled_at = ? WHERE id = ?",
        (result, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), prediction_id)
    )
    conn.commit()
    conn.close()


def determine_result(market: str, home_goals: int, away_goals: int) -> str:
    total = home_goals + away_goals
    market = market.upper()
    if market == "1":
        return "W" if home_goals > away_goals else "L"
    elif market == "X":
        return "W" if home_goals == away_goals else "L"
    elif market == "2":
        return "W" if away_goals > home_goals else "L"
    elif market == "OVER 2.5":
        return "W" if total > 2.5 else "L"
    elif market == "UNDER 2.5":
        return "W" if total < 2.5 else "L"
    elif market == "GG":
        return "W" if home_goals > 0 and away_goals > 0 else "L"
    elif market == "NG":
        return "W" if home_goals == 0 or away_goals == 0 else "L"
    return "L"


def get_stats() -> dict:
    conn = _get_conn()

    totals = conn.execute("""
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN result IS NOT NULL THEN 1 ELSE 0 END) as settled,
            SUM(CASE WHEN result = 'W' THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN result = 'L' THEN 1 ELSE 0 END) as losses,
            SUM(CASE WHEN result = 'W' THEN odds_value - 1 WHEN result = 'L' THEN -1 ELSE 0 END) as profit
        FROM predictions
    """).fetchone()

    by_market = conn.execute("""
        SELECT market,
            COUNT(*) as total,
            SUM(CASE WHEN result = 'W' THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN result IS NOT NULL THEN 1 ELSE 0 END) as settled,
            SUM(CASE WHEN result = 'W' THEN odds_value - 1 WHEN result = 'L' THEN -1 ELSE 0 END) as profit
        FROM predictions
        WHERE result IS NOT NULL
        GROUP BY market
        ORDER BY wins DESC
    """).fetchall()

    by_league = conn.execute("""
        SELECT league,
            COUNT(*) as total,
            SUM(CASE WHEN result = 'W' THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN result IS NOT NULL THEN 1 ELSE 0 END) as settled,
            SUM(CASE WHEN result = 'W' THEN odds_value - 1 WHEN result = 'L' THEN -1 ELSE 0 END) as profit
        FROM predictions
        WHERE result IS NOT NULL
        GROUP BY league
    """).fetchall()

    recent = conn.execute("""
        SELECT match_date, home_team, away_team, league, market,
               odds_value, ev_pct, result, bookmaker
        FROM predictions
        ORDER BY match_date DESC
        LIMIT 30
    """).fetchall()

    conn.close()

    settled = totals["settled"] or 0
    wins = totals["wins"] or 0
    profit = totals["profit"] or 0

    return {
        "total": totals["total"],
        "settled": settled,
        "unsettled": totals["total"] - settled,
        "wins": wins,
        "losses": totals["losses"] or 0,
        "hit_rate": round(wins / settled * 100, 1) if settled > 0 else 0,
        "profit": round(profit, 2),
        "roi": round(profit / settled * 100, 1) if settled > 0 else 0,
        "by_market": [dict(r) for r in by_market],
        "by_league": [dict(r) for r in by_league],
        "recent": [dict(r) for r in recent],
    }


def _parse_ev_from_reasoning(reasoning: str) -> tuple:
    m = re.search(
        r'\[EV:\s*([+-]?\d+\.?\d*)%\s*\|\s*Prob\.stimata:\s*(\d+\.?\d*)%\s*\|\s*Prob\.implicita:\s*(\d+\.?\d*)%',
        reasoning
    )
    if m:
        return float(m.group(1)), float(m.group(2)), float(m.group(3))
    return None, None, None


def save_worker_setting(key: str, value: str):
    conn = _get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO worker_settings (key, value) VALUES (?, ?)",
        (key, value)
    )
    conn.commit()
    conn.close()


def get_worker_setting(key: str, default: str = None) -> str:
    conn = _get_conn()
    r = conn.execute("SELECT value FROM worker_settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    return r["value"] if r else default


def log_alert(match_id: str, home: str, away: str, league: str, date: str, rec: str, status: str = "SENT"):
    conn = _get_conn()
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        """INSERT INTO alerts_log 
           (match_id, home_team, away_team, league, match_date, recommendation, sent_at, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (match_id, home, away, league, date, rec, ts, status)
    )
    conn.commit()
    conn.close()


def get_alerts_log(limit: int = 50) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM alerts_log ORDER BY sent_at DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def is_alert_sent(match_id: str) -> bool:
    conn = _get_conn()
    r = conn.execute("SELECT id FROM alerts_log WHERE match_id = ?", (match_id,)).fetchone()
    conn.close()
    return r is not None
