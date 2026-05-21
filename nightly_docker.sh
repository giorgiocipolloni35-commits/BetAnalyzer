#!/bin/bash
# ─────────────────────────────────────────────────────────
# BetAnalyzer — Nightly update (Docker version)
# Runs: precache + invalidate teams cache + nightly_sync
# ─────────────────────────────────────────────────────────

LOG="/var/log/nightly.log"

echo "" >> "$LOG"
echo "========================================" >> "$LOG"
echo "$(date '+%Y-%m-%d %H:%M:%S') — Nightly update START" >> "$LOG"
echo "========================================" >> "$LOG"

cd /app

# 1. Pre-cache partite (Football-Data.org)
echo "$(date '+%H:%M:%S') [STEP 1] Precache partite..." >> "$LOG"
python3 precache_all.py >> "$LOG" 2>&1

# 2. Invalida cache statistiche Teams
if [ -f "/app/data/competitions_stats_cache.json" ]; then
    rm "/app/data/competitions_stats_cache.json"
    echo "$(date '+%H:%M:%S') [STEP 2] Cache Teams invalidata" >> "$LOG"
fi

# 3. Sync giocatori Sportmonks
echo "$(date '+%H:%M:%S') [STEP 3] Sync giocatori (Sportmonks)..." >> "$LOG"
python3 -m scraper.nightly_sync >> "$LOG" 2>&1

echo "$(date '+%H:%M:%S') — Nightly update DONE" >> "$LOG"
echo "========================================" >> "$LOG"
