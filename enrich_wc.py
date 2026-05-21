#!/usr/bin/env python3
"""
Enrichment giocatori Mondiali — lancia dentro il container nightly:

    docker exec -it <nightly-container> python3 enrich_wc.py

Cerca su Sportmonks i giocatori WC non matchati e scarica le loro stats.
"""

import os
import sys
import logging

sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
)

from scraper.worldcup import import_squads, enrich_unmatched_players

print("=" * 60)
print("  WC 2026 — Import + Enrichment giocatori")
print("=" * 60)

# Step 1: import/refresh squads
print("\n📥 Step 1: Import convocazioni...")
res = import_squads()
print(f"   Importati: {res['imported']} giocatori da {res['countries']} nazionali")
print(f"   Matchati con DB locale: {res['matched']}")

# Step 2: enrich unmatched via Sportmonks
print("\n🔍 Step 2: Enrichment via Sportmonks API...")
print("   (ogni giocatore = ~2-3 secondi, stampo progresso ogni 20)\n")
enrich = enrich_unmatched_players()

print("\n" + "=" * 60)
print(f"  RISULTATO FINALE")
print(f"  Cercati:  {enrich.get('searched', 0)}")
print(f"  Trovati:  {enrich.get('found', 0)}")
print(f"  Errori:   {enrich.get('errors', 0)}")
print("=" * 60)

# Show final coverage
import sqlite3
conn = sqlite3.connect("data/betanalyzer.db", timeout=30)
total = conn.execute("SELECT COUNT(*) FROM wc_squads").fetchone()[0]
matched = conn.execute("SELECT COUNT(*) FROM wc_squads WHERE player_id IS NOT NULL").fetchone()[0]
conn.close()
pct = round(matched / total * 100) if total > 0 else 0
print(f"\n📊 Copertura finale: {matched}/{total} giocatori ({pct}%)")
