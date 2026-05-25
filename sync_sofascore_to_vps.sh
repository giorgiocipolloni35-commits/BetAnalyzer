#!/bin/bash
# ─────────────────────────────────────────────────────────
# Sofascore Stats: scrape → export → upload to VPS
# Runs from Mac (residential IP) to avoid datacenter blocks
# ─────────────────────────────────────────────────────────

set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

VPS_USER="ubuntu"
VPS_HOST="91.134.141.134"
VPS_DB_PATH="/data/coolify/betanalyzer"
LOG="$DIR/data/sofascore_sync.log"

echo "$(date '+%Y-%m-%d %H:%M:%S') ── Sofascore sync START" >> "$LOG"

# 1. Scrape (3 leagues per run, rotates through all 9)
echo "$(date '+%H:%M:%S') [1/3] Scraping 3 leagues (rotation)..." >> "$LOG"
python3 "$DIR/fetch_sofascore_stats.py" --rotate 3 >> "$LOG" 2>&1

# 2. Export Sofascore data to SQL
echo "$(date '+%H:%M:%S') [2/3] Exporting to SQL..." >> "$LOG"
python3 "$DIR/export_sofascore_data.py" >> "$LOG" 2>&1

# 3. Upload and import on VPS
echo "$(date '+%H:%M:%S') [3/3] Uploading to VPS..." >> "$LOG"
scp -q "$DIR/data/sofascore_export.sql" "$VPS_USER@$VPS_HOST:/tmp/sofascore_export.sql"

# Find nightly container and import
ssh "$VPS_USER@$VPS_HOST" "
  CONTAINER=\$(sudo docker ps --format '{{.Names}}' | grep nightly)
  sudo docker exec -i \$CONTAINER python3 -c \"
import sqlite3, sys
conn = sqlite3.connect('/app/data/betanalyzer.db')
sql = sys.stdin.read()
conn.executescript(sql)
conn.close()
print('Imported OK')
\" < /tmp/sofascore_export.sql
  rm /tmp/sofascore_export.sql
"

echo "$(date '+%Y-%m-%d %H:%M:%S') ── Sofascore sync DONE" >> "$LOG"
echo "" >> "$LOG"
