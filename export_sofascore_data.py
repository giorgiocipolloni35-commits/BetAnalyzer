#!/usr/bin/env python3
"""
Export Sofascore-only data from local DB to a SQL file
that can be imported on the VPS without overwriting existing data.

Exports only rows with player_id >= 90_000_000 (Sofascore offset).
"""

import sqlite3
import json
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "betanalyzer.db"
OUTPUT = Path(__file__).parent / "data" / "sofascore_export.sql"

SS_PLAYER_OFFSET = 90_000_000

conn = sqlite3.connect(str(DB_PATH))
cursor = conn.cursor()

lines = []
lines.append("BEGIN TRANSACTION;")

# 1. Export player_info
cursor.execute("""
    SELECT player_id, name, team_id, team_name, league_id, position_id, updated_at
    FROM player_info
    WHERE player_id >= ?
""", (SS_PLAYER_OFFSET,))

pi_count = 0
for row in cursor.fetchall():
    pid, name, tid, tname, lid, pos, updated = row
    name_esc = name.replace("'", "''") if name else ""
    tname_esc = tname.replace("'", "''") if tname else ""
    lid_esc = lid.replace("'", "''") if lid else ""
    updated_esc = updated.replace("'", "''") if updated else ""
    pos_val = str(pos) if pos is not None else "NULL"
    lines.append(
        f"INSERT OR REPLACE INTO player_info "
        f"(player_id, name, team_id, team_name, league_id, position_id, updated_at) "
        f"VALUES ({pid}, '{name_esc}', {tid}, '{tname_esc}', '{lid_esc}', {pos_val}, '{updated_esc}');"
    )
    pi_count += 1

# 2. Export player_stats_cache
cursor.execute("""
    SELECT player_id, season_id, team_id, stats_json, rating, updated_at
    FROM player_stats_cache
    WHERE player_id >= ?
""", (SS_PLAYER_OFFSET,))

psc_count = 0
for row in cursor.fetchall():
    pid, sid, tid, sj, rating, updated = row
    sj_esc = sj.replace("'", "''") if sj else ""
    updated_esc = updated.replace("'", "''") if updated else ""
    rating_val = round(rating, 2) if rating else 0.0
    lines.append(
        f"INSERT OR REPLACE INTO player_stats_cache "
        f"(player_id, season_id, team_id, stats_json, rating, updated_at) "
        f"VALUES ({pid}, {sid}, {tid}, '{sj_esc}', {rating_val}, '{updated_esc}');"
    )
    psc_count += 1

lines.append("COMMIT;")

conn.close()

with open(OUTPUT, "w", encoding="utf-8") as f:
    f.write("\n".join(lines))

size_mb = OUTPUT.stat().st_size / 1024 / 1024
print(f"Exported {pi_count} player_info + {psc_count} player_stats_cache rows")
print(f"File: {OUTPUT} ({size_mb:.1f} MB)")
