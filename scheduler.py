#!/usr/bin/env python3
"""
BetAnalyzer — Nightly Scheduler (Docker version)

Replaces cron-based nightly_docker.sh with a reliable Python scheduler.
Uses APScheduler to run precache + nightly_sync at fixed times (UTC).

Runs as a long-lived process inside the 'nightly' Docker container.
"""

import os
import sys
import logging
import subprocess
from datetime import datetime
from pathlib import Path

from apscheduler.schedulers.blocking import BlockingScheduler
from dotenv import load_dotenv

# Setup
sys.path.insert(0, os.path.dirname(__file__))
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler("data/scheduler.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("scheduler")


def run_nightly():
    """Full nightly update: precache + invalidate teams cache + nightly_sync."""
    logger.info("=" * 50)
    logger.info("🌙 Nightly update START")
    logger.info("=" * 50)

    # 1. Precache partite (Football-Data.org)
    logger.info("[STEP 1] Precache partite...")
    try:
        result = subprocess.run(
            [sys.executable, "precache_all.py"],
            capture_output=True, text=True, timeout=1800,
        )
        if result.stdout:
            logger.info(result.stdout.strip())
        if result.returncode != 0:
            logger.error(f"Precache failed (exit {result.returncode}): {result.stderr}")
    except subprocess.TimeoutExpired:
        logger.error("Precache timeout (30 min)")
    except Exception as e:
        logger.error(f"Precache error: {e}")

    # 2. Invalida cache statistiche Teams
    cache_file = Path("data/competitions_stats_cache.json")
    if cache_file.exists():
        cache_file.unlink()
        logger.info("[STEP 2] Cache Teams invalidata")
    else:
        logger.info("[STEP 2] Nessuna cache Teams da invalidare")

    # 3. Sync giocatori Sportmonks
    logger.info("[STEP 3] Sync giocatori (Sportmonks)...")
    try:
        from scraper.nightly_sync import run_nightly_sync
        run_nightly_sync()
    except ImportError:
        # Fallback: run as subprocess
        try:
            result = subprocess.run(
                [sys.executable, "-m", "scraper.nightly_sync"],
                capture_output=True, text=True, timeout=3600,
            )
            if result.stdout:
                logger.info(result.stdout.strip())
            if result.returncode != 0:
                logger.error(f"Nightly sync failed (exit {result.returncode}): {result.stderr}")
        except subprocess.TimeoutExpired:
            logger.error("Nightly sync timeout (60 min)")
        except Exception as e:
            logger.error(f"Nightly sync error: {e}")
    except Exception as e:
        logger.error(f"Nightly sync error: {e}")

    logger.info("✅ Nightly update DONE")
    logger.info("=" * 50)


if __name__ == "__main__":
    scheduler = BlockingScheduler(timezone="UTC")

    # Nightly job: 04:15 UTC (= 06:15 CEST) — same as old cron
    scheduler.add_job(run_nightly, "cron", hour=4, minute=15, id="nightly")

    # Run immediately on startup if we missed today's window
    now = datetime.utcnow()
    if now.hour >= 5:
        logger.info("⏰ Startup after scheduled time — running nightly now...")
        run_nightly()

    logger.info(f"📅 Scheduler avviato — prossimo run: 04:15 UTC ogni giorno")
    logger.info(f"   Prossima esecuzione: {scheduler.get_job('nightly').next_run_time}")

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler fermato")
