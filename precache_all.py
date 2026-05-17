#!/usr/bin/env python3
"""
Pre-cache match details for ALL leagues.

Run overnight to populate/update the disk cache with match details
(penalties, cards with player names, referee data) for every league.

Usage:
    ./venv/bin/python precache_all.py

Each league takes ~12 minutes on first run (rate limit: 2.1s per API call).
Subsequent runs only fetch new/updated matches.
"""

import logging
import os
import sys
import time
from datetime import datetime

from dotenv import load_dotenv

# Add project root to path
sys.path.insert(0, os.path.dirname(__file__))

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

from scraper.penalties import PenaltyAnalyzer, LEAGUE_CODES

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def main():
    api_key = os.getenv("FOOTBALL_DATA_API_KEY")
    if not api_key:
        logger.error("FOOTBALL_DATA_API_KEY not found in .env")
        sys.exit(1)

    pa = PenaltyAnalyzer(api_key)

    leagues = list(LEAGUE_CODES.items())
    total_leagues = len(leagues)

    logger.info("=" * 60)
    logger.info("BetAnalyzer — Pre-cache avviato: %s", datetime.now().strftime("%Y-%m-%d %H:%M"))
    logger.info("Campionati da aggiornare: %d", total_leagues)
    logger.info("=" * 60)

    results = {}

    for idx, (league_key, league_code) in enumerate(leagues, 1):
        logger.info("")
        logger.info("─" * 50)
        logger.info("[%d/%d] %s (%s)", idx, total_leagues, league_key, league_code)
        logger.info("─" * 50)

        t0 = time.time()
        try:
            # Get finished matches for this league
            data = pa._get(f"/competitions/{league_code}/matches",
                           params={"status": "FINISHED"})
            if not data:
                logger.warning("  Nessun dato per %s — skip", league_key)
                results[league_key] = "SKIP (no data)"
                continue

            finished = data.get("matches", [])
            logger.info("  Partite finite trovate: %d", len(finished))

            # Fetch/update match details (uses disk cache, only fetches new ones)
            def progress(current, total, mid):
                logger.info("  Fetching %d/%d (match %s)", current, total, mid)

            cache = pa._fetch_match_details(league_code, finished, progress_cb=progress)

            elapsed = time.time() - t0
            logger.info("  ✅ Completato in %.0fs — %d partite in cache", elapsed, len(cache))
            results[league_key] = f"OK ({len(cache)} matches, {elapsed:.0f}s)"

        except Exception as e:
            elapsed = time.time() - t0
            logger.error("  ❌ Errore per %s: %s", league_key, e)
            results[league_key] = f"ERROR: {e}"

        # Small pause between leagues to be safe with rate limits
        if idx < total_leagues:
            logger.info("  Pausa 5s prima del prossimo campionato...")
            time.sleep(5)

    # Summary
    logger.info("")
    logger.info("=" * 60)
    logger.info("RIEPILOGO")
    logger.info("=" * 60)
    for league, status in results.items():
        logger.info("  %-30s %s", league, status)
    logger.info("=" * 60)
    logger.info("Pre-cache completato: %s", datetime.now().strftime("%Y-%m-%d %H:%M"))


if __name__ == "__main__":
    main()
