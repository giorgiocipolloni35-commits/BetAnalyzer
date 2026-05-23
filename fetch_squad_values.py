#!/usr/bin/env python3
"""
Recupera il valore rosa di ogni squadra da Transfermarkt per tutti i campionati.

Uso:
    python3 fetch_squad_values.py

Salva i dati nella tabella team_squad_values del DB.
Tempo stimato: ~30 secondi (una richiesta per campionato).
"""

import os
import re
import time
import sqlite3
import logging
import requests
import urllib3

urllib3.disable_warnings()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "betanalyzer.db")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Connection": "keep-alive",
}

# league_key (nostro) -> TM wettbewerb code
LEAGUES = {
    "italy_serie_a":              "IT1",
    "england_premier_league":     "GB1",
    "spain_la_liga":              "ES1",
    "germany_bundesliga":         "L1",
    "france_ligue_1":             "FR1",
    "netherlands_eredivisie":     "NL1",
    "england_championship":       "GB2",
    "portugal_primeira_liga":     "PO1",
    "brazil_serie_a":             "BRA1",
}

# Slug per la URL di TM
LEAGUE_SLUGS = {
    "IT1":  "serie-a",
    "GB1":  "premier-league",
    "ES1":  "la-liga",
    "L1":   "bundesliga",
    "FR1":  "ligue-1",
    "NL1":  "eredivisie",
    "GB2":  "championship",
    "PO1":  "primeira-liga",
    "BRA1": "serie-a-brazil",  # TM slug for Brazilian Serie A
}


def ensure_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS team_squad_values (
            team_name TEXT,
            league_key TEXT,
            squad_value_eur INTEGER,
            updated_at TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (team_name, league_key)
        )
    """)
    conn.commit()


def fetch_league(league_key, tm_code):
    """Fetch squad values for one league from TM."""
    slug = LEAGUE_SLUGS.get(tm_code, league_key)
    url = f"https://www.transfermarkt.com/{slug}/startseite/wettbewerb/{tm_code}"

    r = requests.get(url, headers=HEADERS, timeout=15, verify=False)
    if r.status_code != 200:
        logger.error(f"HTTP {r.status_code} per {league_key}")
        return []

    # Pattern: <a title="Team" href="/team/kader/verein/ID/saison_id/YEAR">€VALUE</a>
    rows = re.findall(
        r'<a\s+title="([^"]+)"\s+href="/[^"]+/kader/verein/\d+/saison_id/\d+">'
        r'\s*€([\d,.]+)(bn|m|k)\s*</a>',
        r.text, re.IGNORECASE
    )

    results = []
    for name, val_str, unit in rows:
        name = name.replace("&amp;", "&")
        val = float(val_str.replace(",", ""))
        if unit.lower() == "bn":
            val *= 1_000_000_000
        elif unit.lower() == "m":
            val *= 1_000_000
        elif unit.lower() == "k":
            val *= 1_000
        results.append({"team": name, "value": int(val)})

    return results


def main():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    ensure_table(conn)

    total_teams = 0
    total_leagues = 0

    print("=" * 60)
    print("  Squad Values — Aggiornamento da Transfermarkt")
    print("=" * 60)

    for league_key, tm_code in LEAGUES.items():
        try:
            teams = fetch_league(league_key, tm_code)
            if not teams:
                print(f"  ⚠ {league_key}: nessun dato")
                continue

            for t in teams:
                conn.execute("""
                    INSERT OR REPLACE INTO team_squad_values
                    (team_name, league_key, squad_value_eur, updated_at)
                    VALUES (?, ?, ?, datetime('now'))
                """, (t["team"], league_key, t["value"]))

            conn.commit()
            total_teams += len(teams)
            total_leagues += 1
            print(f"  ✅ {league_key}: {len(teams)} squadre")
            time.sleep(2)

        except Exception as e:
            logger.error(f"Errore {league_key}: {e}")

    conn.close()

    print("\n" + "=" * 60)
    print(f"  RISULTATO: {total_teams} squadre in {total_leagues} campionati")
    print("=" * 60)


if __name__ == "__main__":
    main()
