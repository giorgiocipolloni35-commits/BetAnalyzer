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

        CREATE TABLE IF NOT EXISTS odds_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_key TEXT NOT NULL,
            home_team TEXT NOT NULL,
            away_team TEXT NOT NULL,
            league TEXT NOT NULL,
            match_date TEXT NOT NULL,
            snapshot_at TEXT NOT NULL,
            bookmaker TEXT NOT NULL,
            home_odds REAL,
            draw_odds REAL,
            away_odds REAL,
            over25 REAL,
            under25 REAL,
            gg REAL,
            ng REAL
        );

        CREATE INDEX IF NOT EXISTS idx_odds_snapshots_match
            ON odds_snapshots(match_key, bookmaker, snapshot_at);

        CREATE TABLE IF NOT EXISTS prediction_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_key TEXT NOT NULL,
            home_team TEXT NOT NULL,
            away_team TEXT NOT NULL,
            league TEXT NOT NULL,
            match_date TEXT NOT NULL,
            created_at TEXT NOT NULL,
            -- 1X2 model probabilities
            model_home_win REAL,
            model_draw REAL,
            model_away_win REAL,
            model_over25 REAL,
            model_gg REAL,
            lambda_home REAL,
            lambda_away REAL,
            draw_boost REAL,
            home_boost REAL,
            motivation_home TEXT,
            motivation_away TEXT,
            -- Top scorer picks (JSON array: [{player, team, prob}, ...])
            scorer_picks_json TEXT,
            -- Top card picks (JSON array)
            card_picks_json TEXT,
            -- Actual results (filled post-match)
            actual_home_goals INTEGER,
            actual_away_goals INTEGER,
            actual_scorers TEXT,
            actual_cards TEXT,
            settled_at TEXT
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_prediction_log_match
            ON prediction_log(match_key);

        CREATE TABLE IF NOT EXISTS wc_squads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            country TEXT NOT NULL,
            wc_group TEXT,
            coach TEXT,
            player_name TEXT NOT NULL,
            position TEXT NOT NULL,
            club TEXT,
            -- Link to our DB (filled by matching)
            player_id INTEGER,
            matched_rating REAL,
            matched_stats_json TEXT,
            UNIQUE(country, player_name)
        );

        CREATE INDEX IF NOT EXISTS idx_wc_squads_country
            ON wc_squads(country);

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


# ── Prediction Log (Backtesting) ──

def save_prediction_log(match_key: str, home_team: str, away_team: str,
                         league: str, match_date: str,
                         model_1x2: dict = None, cs_data: dict = None,
                         scorer_picks: list = None, card_picks: list = None):
    """Log model predictions pre-match for backtesting.

    Called from worker.py after computing all predictions for a match.
    Uses INSERT OR REPLACE to update if predictions are regenerated.
    """
    conn = _get_conn()
    now = datetime.now(timezone.utc).isoformat()

    # Extract 1X2 model data
    hw = dd = aw = o25 = gg_pct = lh = la = db = hb = None
    mot_h = mot_a = None
    if cs_data:
        agg = cs_data.get("aggregates", {})
        hw = agg.get("home_win")
        dd = agg.get("draw")
        aw = agg.get("away_win")
        o25 = agg.get("over_25")
        gg_pct = agg.get("gg")
        lh = cs_data.get("lambda_home")
        la = cs_data.get("lambda_away")
        db = cs_data.get("draw_boost")
        hb = cs_data.get("home_boost")
        mot_h = cs_data.get("motivation_home")
        mot_a = cs_data.get("motivation_away")

    # Trim scorer/card picks to top 10 with essential fields
    def _slim_picks(picks, fields):
        if not picks:
            return None
        slim = []
        for p in picks[:10]:
            slim.append({f: p.get(f) for f in fields if p.get(f) is not None})
        return json.dumps(slim, ensure_ascii=False)

    sc_json = _slim_picks(scorer_picks, ["player", "team", "probability", "goals", "avg_minutes"])
    cd_json = _slim_picks(card_picks, ["player", "team", "probability", "yellows", "avg_minutes"])

    try:
        conn.execute("""
            INSERT OR REPLACE INTO prediction_log
            (match_key, home_team, away_team, league, match_date, created_at,
             model_home_win, model_draw, model_away_win, model_over25, model_gg,
             lambda_home, lambda_away, draw_boost, home_boost,
             motivation_home, motivation_away,
             scorer_picks_json, card_picks_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (match_key, home_team, away_team, league, match_date, now,
              hw, dd, aw, o25, gg_pct, lh, la, db, hb,
              mot_h, mot_a, sc_json, cd_json))
        conn.commit()
        logger.info(f"📊 Prediction logged: {match_key}")
    except Exception as e:
        logger.error(f"Prediction log error: {e}")
    finally:
        conn.close()


def settle_prediction_log(match_key: str, home_goals: int, away_goals: int,
                           scorers: list[str] = None, cards: list[str] = None):
    """Fill in actual results for a logged prediction (post-match)."""
    conn = _get_conn()
    now = datetime.now(timezone.utc).isoformat()
    try:
        conn.execute("""
            UPDATE prediction_log
            SET actual_home_goals = ?, actual_away_goals = ?,
                actual_scorers = ?, actual_cards = ?, settled_at = ?
            WHERE match_key = ? AND settled_at IS NULL
        """, (home_goals, away_goals,
              json.dumps(scorers or [], ensure_ascii=False),
              json.dumps(cards or [], ensure_ascii=False),
              now, match_key))
        conn.commit()
    except Exception as e:
        logger.error(f"Settle prediction error: {e}")
    finally:
        conn.close()


def get_backtest_stats() -> dict:
    """Compute backtesting accuracy stats from settled predictions.

    Returns accuracy for each model component:
    - 1X2: % of matches where highest-prob outcome was correct
    - Over/Under 2.5: calibration (predicted 60% over → actual ~60%?)
    - Scorers: % of top picks who actually scored
    - Cards: % of top picks who got carded
    """
    conn = _get_conn()
    try:
        rows = conn.execute("""
            SELECT * FROM prediction_log
            WHERE settled_at IS NOT NULL
            AND actual_home_goals IS NOT NULL
        """).fetchall()

        if not rows:
            return {"total_matches": 0, "message": "Nessun dato di backtesting disponibile."}

        total = len(rows)
        correct_1x2 = 0
        over25_predicted = []  # (model_prob, actual_bool)
        scorer_hits = 0
        scorer_total = 0
        card_hits = 0
        card_total = 0

        for r in rows:
            hg = r["actual_home_goals"]
            ag = r["actual_away_goals"]

            # 1X2 accuracy
            hw = r["model_home_win"] or 0
            dd = r["model_draw"] or 0
            aw = r["model_away_win"] or 0
            predicted = max([(hw, "1"), (dd, "X"), (aw, "2")], key=lambda x: x[0])
            actual = "1" if hg > ag else ("X" if hg == ag else "2")
            if predicted[1] == actual:
                correct_1x2 += 1

            # Over 2.5 calibration
            o25 = r["model_over25"]
            if o25 is not None:
                over25_predicted.append((o25, 1 if (hg + ag) > 2 else 0))

            # Scorer accuracy
            if r["scorer_picks_json"] and r["actual_scorers"]:
                picks = json.loads(r["scorer_picks_json"])
                actual_sc = json.loads(r["actual_scorers"])
                actual_sc_lower = [s.lower() for s in actual_sc]
                for p in picks[:4]:  # top 4
                    scorer_total += 1
                    pname = p.get("player", "").lower()
                    if any(pname in s or s in pname for s in actual_sc_lower):
                        scorer_hits += 1

            # Card accuracy
            if r["card_picks_json"] and r["actual_cards"]:
                picks = json.loads(r["card_picks_json"])
                actual_cd = json.loads(r["actual_cards"])
                actual_cd_lower = [s.lower() for s in actual_cd]
                for p in picks[:4]:  # top 4
                    card_total += 1
                    pname = p.get("player", "").lower()
                    if any(pname in s or s in pname for s in actual_cd_lower):
                        card_hits += 1

        # Over 2.5 calibration by bucket
        o25_buckets = {}
        for prob, actual in over25_predicted:
            bucket = round(prob / 10) * 10  # bucket by 10%
            if bucket not in o25_buckets:
                o25_buckets[bucket] = {"count": 0, "actual": 0}
            o25_buckets[bucket]["count"] += 1
            o25_buckets[bucket]["actual"] += actual

        o25_calibration = {}
        for b, v in sorted(o25_buckets.items()):
            o25_calibration[f"{b}%"] = {
                "predicted": b,
                "actual": round(v["actual"] / v["count"] * 100, 1),
                "count": v["count"],
            }

        return {
            "total_matches": total,
            "1x2_accuracy": round(correct_1x2 / total * 100, 1) if total > 0 else 0,
            "1x2_correct": correct_1x2,
            "over25_calibration": o25_calibration,
            "scorer_hit_rate": round(scorer_hits / scorer_total * 100, 1) if scorer_total > 0 else 0,
            "scorer_hits": scorer_hits,
            "scorer_total": scorer_total,
            "card_hit_rate": round(card_hits / card_total * 100, 1) if card_total > 0 else 0,
            "card_hits": card_hits,
            "card_total": card_total,
        }
    except Exception as e:
        logger.error(f"Backtest stats error: {e}")
        return {"error": str(e)}
    finally:
        conn.close()


# ── Odds Snapshots ──

def save_odds_snapshot(match_key: str, home_team: str, away_team: str,
                       league: str, match_date: str, bookmaker: str,
                       home_odds: float, draw_odds: float, away_odds: float,
                       over25: float = None, under25: float = None,
                       gg: float = None, ng: float = None):
    """Save a single odds snapshot. Skips if identical to last snapshot for same match+bookmaker."""
    conn = _get_conn()
    try:
        # Check if we already have an identical snapshot (avoid duplicates)
        last = conn.execute(
            """SELECT home_odds, draw_odds, away_odds FROM odds_snapshots
               WHERE match_key = ? AND bookmaker = ?
               ORDER BY snapshot_at DESC LIMIT 1""",
            (match_key, bookmaker)
        ).fetchone()

        if last and last["home_odds"] == home_odds and last["draw_odds"] == draw_odds and last["away_odds"] == away_odds:
            return  # No change, skip

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute(
            """INSERT INTO odds_snapshots
               (match_key, home_team, away_team, league, match_date, snapshot_at,
                bookmaker, home_odds, draw_odds, away_odds, over25, under25, gg, ng)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (match_key, home_team, away_team, league, match_date, now,
             bookmaker, home_odds, draw_odds, away_odds, over25, under25, gg, ng)
        )
        conn.commit()
    finally:
        conn.close()


def save_odds_snapshots_batch(snapshots: list[dict]):
    """Save multiple odds snapshots efficiently in a single transaction."""
    if not snapshots:
        return
    conn = _get_conn()
    try:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        inserted = 0
        for s in snapshots:
            # Check for duplicate
            last = conn.execute(
                """SELECT home_odds, draw_odds, away_odds FROM odds_snapshots
                   WHERE match_key = ? AND bookmaker = ?
                   ORDER BY snapshot_at DESC LIMIT 1""",
                (s["match_key"], s["bookmaker"])
            ).fetchone()

            if last and last["home_odds"] == s.get("home_odds") and last["draw_odds"] == s.get("draw_odds") and last["away_odds"] == s.get("away_odds"):
                continue

            conn.execute(
                """INSERT INTO odds_snapshots
                   (match_key, home_team, away_team, league, match_date, snapshot_at,
                    bookmaker, home_odds, draw_odds, away_odds, over25, under25, gg, ng)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (s["match_key"], s["home_team"], s["away_team"], s["league"],
                 s["match_date"], now, s["bookmaker"],
                 s.get("home_odds"), s.get("draw_odds"), s.get("away_odds"),
                 s.get("over25"), s.get("under25"), s.get("gg"), s.get("ng"))
            )
            inserted += 1
        conn.commit()
        if inserted:
            logger.info(f"Odds snapshots: {inserted} nuovi salvati")
    finally:
        conn.close()


def get_odds_history(match_key: str, bookmaker: str = None) -> list[dict]:
    """Get all odds snapshots for a match, optionally filtered by bookmaker."""
    conn = _get_conn()
    try:
        if bookmaker:
            rows = conn.execute(
                """SELECT * FROM odds_snapshots
                   WHERE match_key = ? AND bookmaker = ?
                   ORDER BY snapshot_at ASC""",
                (match_key, bookmaker)
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM odds_snapshots
                   WHERE match_key = ?
                   ORDER BY snapshot_at ASC""",
                (match_key,)
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_line_movement(match_key: str) -> dict:
    """
    Calculate line movement for a match using Pinnacle as reference bookmaker.
    Returns opening odds, current odds, and movement analysis.
    """
    conn = _get_conn()
    try:
        # Prefer Pinnacle (sharpest), fallback to any bookmaker with most snapshots
        for bk in ["Pinnacle", "Betfair Exchange", None]:
            if bk:
                rows = conn.execute(
                    """SELECT * FROM odds_snapshots
                       WHERE match_key = ? AND bookmaker = ?
                       ORDER BY snapshot_at ASC""",
                    (match_key, bk)
                ).fetchall()
            else:
                # Fallback: bookmaker with most snapshots
                top_bk = conn.execute(
                    """SELECT bookmaker, COUNT(*) as cnt FROM odds_snapshots
                       WHERE match_key = ?
                       GROUP BY bookmaker ORDER BY cnt DESC LIMIT 1""",
                    (match_key,)
                ).fetchone()
                if not top_bk:
                    return {}
                rows = conn.execute(
                    """SELECT * FROM odds_snapshots
                       WHERE match_key = ? AND bookmaker = ?
                       ORDER BY snapshot_at ASC""",
                    (match_key, top_bk["bookmaker"])
                ).fetchall()

            if rows and len(rows) >= 2:
                break

        if not rows or len(rows) < 2:
            return {}

        opening = rows[0]
        current = rows[-1]
        bk_name = opening["bookmaker"]

        # Calculate movements
        h_move = round(current["home_odds"] - opening["home_odds"], 3) if opening["home_odds"] and current["home_odds"] else 0
        d_move = round(current["draw_odds"] - opening["draw_odds"], 3) if opening["draw_odds"] and current["draw_odds"] else 0
        a_move = round(current["away_odds"] - opening["away_odds"], 3) if opening["away_odds"] and current["away_odds"] else 0

        # Detect steam move (sharp movement > 0.15 in single snapshot)
        steam_move = None
        for i in range(1, len(rows)):
            prev, curr = rows[i-1], rows[i]
            for side, key in [("home", "home_odds"), ("draw", "draw_odds"), ("away", "away_odds")]:
                if prev[key] and curr[key]:
                    delta = abs(curr[key] - prev[key])
                    if delta >= 0.15:
                        direction = "↓" if curr[key] < prev[key] else "↑"
                        steam_move = {"side": side, "delta": round(delta, 3), "direction": direction}

        # Determine which side money is going to
        # Odds dropping = money coming in on that side
        signals = []
        if h_move <= -0.10:
            signals.append(f"1 ({h_move:+.2f})")
        if d_move <= -0.10:
            signals.append(f"X ({d_move:+.2f})")
        if a_move <= -0.10:
            signals.append(f"2 ({a_move:+.2f})")

        return {
            "bookmaker": bk_name,
            "snapshots": len(rows),
            "opening": {"home": opening["home_odds"], "draw": opening["draw_odds"], "away": opening["away_odds"]},
            "current": {"home": current["home_odds"], "draw": current["draw_odds"], "away": current["away_odds"]},
            "movement": {"home": h_move, "draw": d_move, "away": a_move},
            "steam_move": steam_move,
            "signals": signals,
            "first_seen": opening["snapshot_at"],
            "last_seen": current["snapshot_at"],
        }
    finally:
        conn.close()


# ── CLV (Closing Line Value) ──

def get_closing_line(home_team: str, away_team: str, match_date: str) -> dict:
    """Get the last odds snapshot before match start (closing line)."""
    conn = _get_conn()
    try:
        # Try exact match_key format
        match_key = f"{home_team}_vs_{away_team}_{match_date[:10]}"
        row = conn.execute(
            """SELECT * FROM odds_snapshots
               WHERE match_key = ? AND bookmaker = 'Pinnacle'
               ORDER BY snapshot_at DESC LIMIT 1""",
            (match_key,)
        ).fetchone()

        if not row:
            # Fallback: any bookmaker
            row = conn.execute(
                """SELECT * FROM odds_snapshots
                   WHERE match_key = ?
                   ORDER BY snapshot_at DESC LIMIT 1""",
                (match_key,)
            ).fetchone()

        if not row:
            # Fuzzy match: search by teams and date
            row = conn.execute(
                """SELECT * FROM odds_snapshots
                   WHERE home_team = ? AND away_team = ? AND match_date = ?
                   ORDER BY snapshot_at DESC LIMIT 1""",
                (home_team, away_team, match_date[:10])
            ).fetchone()

        return dict(row) if row else {}
    finally:
        conn.close()


def compute_clv(bet_odds: float, closing_odds: float) -> dict:
    """
    Calculate CLV (Closing Line Value).

    If you bet at 2.10 and the line closed at 1.90:
    - You got +10.5% CLV (you beat the market)

    If you bet at 1.80 and the line closed at 1.95:
    - You got -7.7% CLV (market moved against you)
    """
    if not bet_odds or not closing_odds or closing_odds <= 1:
        return {"clv_pct": None, "edge": None}

    # CLV = (bet_odds / closing_odds - 1) * 100
    clv_pct = round((bet_odds / closing_odds - 1) * 100, 2)

    return {
        "clv_pct": clv_pct,
        "bet_odds": bet_odds,
        "closing_odds": closing_odds,
        "edge": "positive" if clv_pct > 0 else "negative",
    }


def get_bets_with_clv() -> list[dict]:
    """Get all bets with CLV calculated from closing line snapshots."""
    bets = get_bets()
    for bet in bets:
        closing = get_closing_line(bet["home_team"], bet["away_team"], bet["match_date"])
        if closing:
            # Map bet_type to the right odds field
            odds_map = {
                "1": "home_odds", "X": "draw_odds", "2": "away_odds",
                "over25": "over25", "under25": "under25",
                "gg": "gg", "ng": "ng",
            }
            closing_field = odds_map.get(bet["bet_type"])
            if closing_field and closing.get(closing_field):
                clv = compute_clv(bet["odds"], closing[closing_field])
                bet["clv"] = clv["clv_pct"]
                bet["closing_odds"] = closing[closing_field]
            else:
                bet["clv"] = None
                bet["closing_odds"] = None
        else:
            bet["clv"] = None
            bet["closing_odds"] = None
    return bets


def get_clv_stats() -> dict:
    """Aggregate CLV statistics across all settled bets."""
    bets = get_bets_with_clv()
    settled_with_clv = [b for b in bets if b.get("clv") is not None and b["result"] != "pending"]

    if not settled_with_clv:
        return {"avg_clv": None, "beats_closing": None, "total_tracked": 0, "verdict": None}

    clvs = [b["clv"] for b in settled_with_clv]
    avg_clv = round(sum(clvs) / len(clvs), 2)
    beats_closing = sum(1 for c in clvs if c > 0)
    beats_pct = round(beats_closing / len(clvs) * 100, 1)

    # Verdict
    if len(settled_with_clv) < 20:
        verdict = "Dati insufficienti (servono 20+ giocate con CLV)"
    elif avg_clv >= 3:
        verdict = "🟢 Eccellente — Stai battendo il mercato costantemente"
    elif avg_clv >= 1:
        verdict = "🟢 Buono — Hai un edge reale sui bookmaker"
    elif avg_clv >= 0:
        verdict = "🟡 Neutro — Sei in linea col mercato"
    elif avg_clv >= -2:
        verdict = "🟠 Attenzione — Stai prendendo quote leggermente peggiori del mercato"
    else:
        verdict = "🔴 Problematico — Stai giocando sistematicamente a quote peggiori della chiusura"

    return {
        "avg_clv": avg_clv,
        "beats_closing": beats_closing,
        "beats_pct": beats_pct,
        "total_tracked": len(settled_with_clv),
        "verdict": verdict,
    }


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
