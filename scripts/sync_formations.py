"""
Quick script to populate team_formations table for all leagues.
Takes ~2 minutes total. Safe to run multiple times (overwrites).

Usage: python3 -m scripts.sync_formations
"""
import os
import sys
import time
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from scraper.sportmonks import SportmonksClient
from db.database import init_db, save_team_formations

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

SM_KEY = os.getenv("SPORTMONKS_API_KEY")
FD_KEY = os.getenv("FOOTBALL_DATA_API_KEY")

if not SM_KEY or not FD_KEY:
    print("ERROR: SPORTMONKS_API_KEY and FOOTBALL_DATA_API_KEY required in .env")
    sys.exit(1)

# Football-Data league ID → Sportmonks season ID
LEAGUES = [
    {"fd_comp": 2019, "name": "Serie A",        "sm_season": 25533},
    {"fd_comp": 2021, "name": "Premier League",  "sm_season": 25583},
    {"fd_comp": 2014, "name": "La Liga",         "sm_season": 25659},
    {"fd_comp": 2002, "name": "Bundesliga",      "sm_season": 25646},
    {"fd_comp": 2015, "name": "Ligue 1",         "sm_season": 25651},
]

import requests

def main():
    init_db()
    sm = SportmonksClient(SM_KEY)
    headers = {"X-Auth-Token": FD_KEY}

    total_teams = 0
    total_ok = 0
    t0 = time.time()

    for league in LEAGUES:
        logger.info(f"\n{'='*50}")
        logger.info(f" {league['name']}")
        logger.info(f"{'='*50}")

        # Get teams from Football-Data
        url = f"https://api.football-data.org/v4/competitions/{league['fd_comp']}/teams"
        res = requests.get(url, headers=headers, timeout=10).json()
        teams = res.get("teams", [])
        logger.info(f"  {len(teams)} squadre")

        for i, team in enumerate(teams):
            fd_id = team["id"]
            name = team["name"]
            # Clean name for Sportmonks search
            clean = name.replace("AC ", "").replace("AS ", "").replace("FC ", "").replace("SS ", "").replace("SSD ", "").strip()

            total_teams += 1
            try:
                # Search team on Sportmonks
                sm_results = sm.search_team(clean)
                if not sm_results:
                    logger.warning(f"  [{i+1}/{len(teams)}] {name}: NOT FOUND on SM")
                    continue

                sm_id = sm_results[0]["id"]
                stats = sm.get_team_formation_stats(sm_id)

                if stats:
                    save_team_formations(fd_id, league["sm_season"], stats)
                    top = stats[0]
                    wpct = round(top["wins"] / top["matches"] * 100) if top["matches"] else 0
                    logger.info(f"  [{i+1}/{len(teams)}] {name}: {len(stats)} moduli | {top['formation']} ({top['wins']}V-{top['draws']}P-{top['losses']}S = {wpct}%)")
                    total_ok += 1
                else:
                    logger.warning(f"  [{i+1}/{len(teams)}] {name}: no formation data")

                time.sleep(0.3)  # gentle rate limit

            except Exception as e:
                logger.error(f"  [{i+1}/{len(teams)}] {name}: ERROR {e}")

    elapsed = time.time() - t0
    logger.info(f"\n{'#'*50}")
    logger.info(f"  DONE: {total_ok}/{total_teams} teams in {elapsed:.0f}s")
    logger.info(f"{'#'*50}")


if __name__ == "__main__":
    main()
