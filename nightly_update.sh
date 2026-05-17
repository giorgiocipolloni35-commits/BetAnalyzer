#!/bin/bash
# ─────────────────────────────────────────────────────────
# BetAnalyzer — Aggiornamento notturno automatico
# Lanciato via cron alle 04:00 ogni giorno
#
# Step 1: Precache partite (Football-Data.org) ~2-5 min
#         → aggiorna: Teams, Rigori, Cartellini, Marcatori,
#           Doppio Tempo, Risultato Esatto
#
# Step 2: Invalida cache statistiche Teams
#
# Step 3: Sync giocatori (Sportmonks) ~2-4 ore
#         → aggiorna: Players Explorer (Scouting)
# ─────────────────────────────────────────────────────────

DIR="/Users/g5/Documents/BetAnalyzer_Pro_Saved"
VENV="$DIR/venv/bin/python"
LOG="$DIR/nightly_sync.log"

export PYTHONPATH="$DIR"

echo "" >> "$LOG"
echo "========================================" >> "$LOG"
echo "$(date '+%Y-%m-%d %H:%M:%S') — Nightly update START" >> "$LOG"
echo "========================================" >> "$LOG"

# 1. Pre-cache: scarica risultati e dettagli partite per tutti i campionati
echo "$(date '+%H:%M:%S') [STEP 1] Precache partite (Football-Data.org)..." >> "$LOG"
"$VENV" "$DIR/precache_all.py" >> "$LOG" 2>&1

# 2. Invalida cache statistiche Teams (si rigenera al primo accesso)
if [ -f "$DIR/data/competitions_stats_cache.json" ]; then
    rm "$DIR/data/competitions_stats_cache.json"
    echo "$(date '+%H:%M:%S') [STEP 2] Cache Teams invalidata" >> "$LOG"
fi

# 3. Sync giocatori Sportmonks → Players Explorer
echo "$(date '+%H:%M:%S') [STEP 3] Sync giocatori (Sportmonks)..." >> "$LOG"
cd "$DIR" && "$VENV" "$DIR/scraper/nightly_sync.py" >> "$LOG" 2>&1

echo "$(date '+%H:%M:%S') — Nightly update DONE" >> "$LOG"
echo "========================================" >> "$LOG"
