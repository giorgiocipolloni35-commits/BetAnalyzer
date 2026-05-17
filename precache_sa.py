#!/usr/bin/env python3
import logging
import os
import sys
from dotenv import load_dotenv

# Add project root to path
sys.path.insert(0, os.path.dirname(__file__))
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

from scraper.penalties import PenaltyAnalyzer

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

def main():
    api_key = os.getenv("FOOTBALL_DATA_API_KEY")
    if not api_key:
        logger.error("FOOTBALL_DATA_API_KEY not found in .env")
        return

    pa = PenaltyAnalyzer(api_key)
    league_code = "SA"
    
    logger.info(f"Avvio precache for Serie A ({league_code})...")
    
    data = pa._get(f"/competitions/{league_code}/matches", params={"status": "FINISHED"})
    if not data:
        logger.error("Nessun dato ricevuto dall'API Football-Data.")
        return

    finished = data.get("matches", [])
    logger.info(f"Partite finite trovate: {len(finished)}")

    # Fetch last 30 matches only to be faster and stay within limits
    to_fetch = finished[-30:] if len(finished) > 30 else finished
    logger.info(f"Processo le ultime {len(to_fetch)} partite...")

    def progress(current, total, mid):
        logger.info(f"  [{current}/{total}] Scaricando match {mid}...")

    cache = pa._fetch_match_details(league_code, to_fetch, progress_cb=progress)
    pa._save_cache(league_code, cache)
    
    logger.info(f"✅ Completato! {len(cache)} partite salvate in SA_matches.json")

if __name__ == '__main__':
    main()
