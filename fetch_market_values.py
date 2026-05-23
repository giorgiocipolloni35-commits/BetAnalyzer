#!/usr/bin/env python3
"""
Recupera il valore di mercato da Transfermarkt per TUTTI i giocatori nel DB.

Uso:
    python3 fetch_market_values.py          # tutti i giocatori senza valore
    python3 fetch_market_values.py --all    # ricalcola anche quelli già valorizzati
    python3 fetch_market_values.py --limit 100   # solo i primi N

Lo script è "resumable": salva ad ogni batch di 20, quindi puoi
interromperlo e rilanciarlo senza perdere progresso.

Tempo stimato: ~3.5 sec/giocatore → ~3 ore per 3000 giocatori.
"""

import os
import sys
import re
import time
import json
import sqlite3
import logging
import argparse
import requests
import urllib3

urllib3.disable_warnings()

sys.path.insert(0, os.path.dirname(__file__))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
)
logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "betanalyzer.db")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Connection": "keep-alive",
}


def ensure_table(conn):
    """Create market_values table if it doesn't exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS player_market_values (
            player_id INTEGER PRIMARY KEY,
            name TEXT,
            team TEXT,
            tm_id INTEGER,
            market_value_eur INTEGER,
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()


def search_tm_player(name, team_name):
    """Search Transfermarkt for a player and return their TM ID."""
    # Clean name for search
    search_name = name.replace("'", "").replace(".", " ").strip()
    search_url = (
        f"https://www.transfermarkt.com/schnellsuche/ergebnis/"
        f"schnellsuche?query={search_name.replace(' ', '+')}"
    )
    r = requests.get(search_url, headers=HEADERS, timeout=10, verify=False)
    if r.status_code != 200:
        return None

    # Find player profile links
    links = re.findall(r'(/[a-z\-]+/profil/spieler/(\d+))', r.text)
    if not links:
        return None

    # If multiple results, try to match by team
    if len(links) > 1 and team_name:
        team_low = team_name.lower()
        # Check around each link for team name
        for link_path, tm_id in links:
            idx = r.text.find(link_path)
            if idx > 0:
                context = r.text[max(0, idx - 500):idx + 500].lower()
                # Match team name words
                team_words = [w for w in team_low.split() if len(w) > 3]
                if any(w in context for w in team_words):
                    return int(tm_id)

    # Return first result
    return int(links[0][1])


def get_market_value(tm_id):
    """Get market value from TM profile page meta tag."""
    url = f"https://www.transfermarkt.com/x/profil/spieler/{tm_id}"
    r = requests.get(url, headers=HEADERS, timeout=10, verify=False)
    if r.status_code != 200:
        return None

    match = re.search(r'Market value: €([\d,.]+)(k|m|bn)', r.text, re.IGNORECASE)
    if not match:
        return None

    val = float(match.group(1).replace(',', ''))
    unit = match.group(2).lower()
    if unit == 'bn':
        val *= 1_000_000_000
    elif unit == 'm':
        val *= 1_000_000
    elif unit == 'k':
        val *= 1_000
    return int(val)


def main():
    parser = argparse.ArgumentParser(description="Fetch TM market values for all players")
    parser.add_argument("--all", action="store_true", help="Re-fetch even if already valued")
    parser.add_argument("--limit", type=int, default=0, help="Max players to process")
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    ensure_table(conn)

    # Get players to process
    if args.all:
        players = conn.execute("""
            SELECT player_id, name, team_name
            FROM player_info
            ORDER BY name
        """).fetchall()
    else:
        # Only players without a market value yet
        players = conn.execute("""
            SELECT pi.player_id, pi.name, pi.team_name
            FROM player_info pi
            LEFT JOIN player_market_values pmv ON pi.player_id = pmv.player_id
            WHERE pmv.player_id IS NULL
            ORDER BY pi.name
        """).fetchall()

    if args.limit > 0:
        players = players[:args.limit]

    total = len(players)
    if total == 0:
        print("✅ Tutti i giocatori hanno già un valore di mercato!")
        conn.close()
        return

    print("=" * 60)
    print(f"  Market Values — {total} giocatori da processare")
    print(f"  Tempo stimato: ~{total * 3.5 / 60:.0f} minuti")
    print("=" * 60)

    found = 0
    not_found = 0
    errors = 0

    for i, player in enumerate(players):
        pid = player["player_id"]
        name = player["name"]
        team = player["team_name"] or ""

        if i > 0 and i % 20 == 0:
            # Save progress
            conn.commit()
            pct = round(found / max(i, 1) * 100)
            logger.info(
                f"   ... {i}/{total} | trovati: {found} ({pct}%) | "
                f"non trovati: {not_found} | errori: {errors}"
            )

        try:
            # Step 1: search TM for player
            tm_id = search_tm_player(name, team)
            if not tm_id:
                not_found += 1
                time.sleep(1)
                continue

            time.sleep(1)

            # Step 2: get market value from profile
            value = get_market_value(tm_id)
            time.sleep(1)

            if value:
                conn.execute("""
                    INSERT OR REPLACE INTO player_market_values
                    (player_id, name, team, tm_id, market_value_eur, updated_at)
                    VALUES (?, ?, ?, ?, ?, datetime('now'))
                """, (pid, name, team, tm_id, value))
                found += 1
            else:
                # Player found on TM but no value listed
                conn.execute("""
                    INSERT OR REPLACE INTO player_market_values
                    (player_id, name, team, tm_id, market_value_eur, updated_at)
                    VALUES (?, ?, ?, ?, 0, datetime('now'))
                """, (pid, name, team, tm_id))
                not_found += 1

        except Exception as e:
            errors += 1
            if errors <= 10:
                logger.warning(f"   ⚠ Errore per {name}: {e}")
            time.sleep(2)

    conn.commit()
    conn.close()

    print("\n" + "=" * 60)
    print(f"  RISULTATO FINALE")
    print(f"  Processati: {found + not_found + errors}/{total}")
    print(f"  Con valore:  {found}")
    print(f"  Senza valore: {not_found}")
    print(f"  Errori: {errors}")
    print("=" * 60)


if __name__ == "__main__":
    main()
