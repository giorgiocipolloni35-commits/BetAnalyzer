#!/usr/bin/env python3
"""
Fetch detailed player positions from Transfermarkt for all tracked leagues.

Scrapes the squad page (/kader/) for each team in each league to get
granular positions (Centre-Back, Left-Back, Defensive Midfield, etc.)
instead of the 4 generic ones from Sofascore (G, D, M, F).

Output: data/tm_positions.json
    {
        "player_name_normalized": {
            "name": "Original Name",
            "team": "Team Name",
            "position": "Centre-Back",
            "position_short": "CB",
            "tm_id": "123456"
        },
        ...
    }

Usage:
    python3 fetch_tm_positions.py                # all leagues
    python3 fetch_tm_positions.py serie_a         # single league
    python3 fetch_tm_positions.py --delay 3       # custom delay (default 2s)

Estimated runtime: ~5 minutes for all 9 leagues (~160 teams, 2s delay).
"""

import json
import logging
import os
import re
import sys
import time
import unicodedata
from pathlib import Path

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── Config ──────────────────────────────────────────────────────────

DATA_DIR = Path(__file__).parent / "data"
OUTPUT_FILE = DATA_DIR / "tm_positions.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

# Proxy support (DataImpulse) — optional, uses direct if not set
_PROXY_LOGIN = os.environ.get("DATAIMPULSE_LOGIN", "")
_PROXY_PASS = os.environ.get("DATAIMPULSE_PASSWORD", "")
_PROXY_HOST = os.environ.get("DATAIMPULSE_HOST", "gw.dataimpulse.com")
_PROXY_PORT = os.environ.get("DATAIMPULSE_PORT", "823")

PROXIES = {}
if _PROXY_LOGIN and _PROXY_PASS:
    _proxy_url = f"http://{_PROXY_LOGIN}:{_PROXY_PASS}@{_PROXY_HOST}:{_PROXY_PORT}"
    PROXIES = {"http": _proxy_url, "https": _proxy_url}
    logger.info("Proxy residenziale attivo: %s:%s", _PROXY_HOST, _PROXY_PORT)

# ── League → TM competition code ───────────────────────────────────

TM_LEAGUES = {
    "serie_a": {
        "name": "Serie A",
        "code": "IT1",
        "slug": "serie-a",
    },
    "premier_league": {
        "name": "Premier League",
        "code": "GB1",
        "slug": "premier-league",
    },
    "la_liga": {
        "name": "La Liga",
        "code": "ES1",
        "slug": "laliga",
    },
    "bundesliga": {
        "name": "Bundesliga",
        "code": "L1",
        "slug": "1-bundesliga",
    },
    "ligue_1": {
        "name": "Ligue 1",
        "code": "FR1",
        "slug": "ligue-1",
    },
    "brazil_serie_a": {
        "name": "Brasileirao",
        "code": "BRA1",
        "slug": "campeonato-brasileiro-serie-a",
    },
    "denmark_superliga": {
        "name": "Superliga",
        "code": "DK1",
        "slug": "superligaen",
    },
    "scotland_premiership": {
        "name": "Premiership",
        "code": "SC1",
        "slug": "scottish-premiership",
    },
    "champions_league": {
        "name": "Champions League",
        "code": "CL",
        "slug": "uefa-champions-league",
    },
}

# ── Position mapping (TM English → short code) ─────────────────────

POSITION_SHORT = {
    # Goalkeepers
    "Goalkeeper": "GK",
    # Defenders
    "Centre-Back": "CB",
    "Left-Back": "LB",
    "Right-Back": "RB",
    "Left Wing-Back": "LWB",
    "Right Wing-Back": "RWB",
    # Midfielders
    "Defensive Midfield": "DM",
    "Central Midfield": "CM",
    "Attacking Midfield": "AM",
    "Left Midfield": "LM",
    "Right Midfield": "RM",
    # Forwards
    "Left Winger": "LW",
    "Right Winger": "RW",
    "Second Striker": "SS",
    "Centre-Forward": "CF",
}


# ── Helpers ─────────────────────────────────────────────────────────

def normalize_name(name: str) -> str:
    """Normalize player name for fuzzy matching across sources."""
    # Remove accents
    nfkd = unicodedata.normalize("NFKD", name)
    ascii_name = "".join(c for c in nfkd if not unicodedata.combining(c))
    # Lowercase, strip extra spaces
    return re.sub(r"\s+", " ", ascii_name.lower().strip())


def _get(url: str, retries: int = 3, delay: float = 2.0) -> str | None:
    """GET with retries. Returns HTML text or None."""
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, proxies=PROXIES, timeout=20)
            if r.status_code == 200:
                return r.text
            if r.status_code == 429:
                wait = 30 * (attempt + 1)
                logger.warning("Rate-limited (429), waiting %ds...", wait)
                time.sleep(wait)
                continue
            if r.status_code == 404:
                logger.warning("404 for %s", url)
                return None
            logger.warning("HTTP %d for %s (attempt %d)", r.status_code, url, attempt + 1)
        except Exception as e:
            logger.warning("Request error: %s (attempt %d)", e, attempt + 1)
        time.sleep(delay)
    return None


def get_league_teams(league: dict) -> list[dict]:
    """Get list of teams (verein_id, slug, name) for a league."""
    url = (
        f"https://www.transfermarkt.com/{league['slug']}"
        f"/startseite/wettbewerb/{league['code']}/plus/?saison_id=2025"
    )
    html = _get(url)
    if not html:
        return []

    # Extract team verein IDs and slugs
    teams = re.findall(
        r'href="/([^/]+)/startseite/verein/(\d+)',
        html,
    )
    seen = {}
    result = []
    for slug, verein_id in teams:
        if verein_id not in seen:
            seen[verein_id] = True
            # Get readable name from slug
            name = slug.replace("-", " ").title()
            result.append({
                "verein_id": verein_id,
                "slug": slug,
                "name": name,
            })
    return result


def get_squad_positions(verein_id: str, slug: str) -> list[dict]:
    """Get all players + detailed positions for a team."""
    url = (
        f"https://www.transfermarkt.com/{slug}"
        f"/kader/verein/{verein_id}/saison_id/2025"
    )
    html = _get(url)
    if not html:
        return []

    # Get the real team name from the page header
    team_match = re.search(
        r'<h1[^>]*class="data-header__headline-wrapper[^"]*"[^>]*>\s*(.*?)\s*</h1>',
        html, re.DOTALL,
    )
    team_name = ""
    if team_match:
        team_name = re.sub(r"<[^>]+>", "", team_match.group(1)).strip()

    # Extract player TM IDs
    player_ids = re.findall(r'/profil/spieler/(\d+)', html)

    # Extract inline-table blocks (name + position)
    tables = re.findall(
        r'<table class="inline-table">(.*?)</table>',
        html, re.DOTALL,
    )

    players = []
    id_idx = 0
    for t in tables:
        rows = re.findall(r"<tr>(.*?)</tr>", t, re.DOTALL)
        if len(rows) < 2:
            continue
        name = re.sub(r"<[^>]+>", "", rows[0]).strip().replace("\xa0", "").replace("&nbsp;", "")
        pos = re.sub(r"<[^>]+>", "", rows[1]).strip()
        if not name or not pos:
            continue

        # Try to match a TM player ID
        tm_id = ""
        if id_idx < len(player_ids):
            tm_id = player_ids[id_idx]
            id_idx += 1

        players.append({
            "name": name,
            "team": team_name or slug.replace("-", " ").title(),
            "position": pos,
            "position_short": POSITION_SHORT.get(pos, pos[:3].upper()),
            "tm_id": tm_id,
        })

    return players


# ── Main ────────────────────────────────────────────────────────────

def main():
    delay = 2.0
    target_leagues = None

    # Parse args
    args = sys.argv[1:]
    for i, arg in enumerate(args):
        if arg == "--delay" and i + 1 < len(args):
            delay = float(args[i + 1])
        elif arg in TM_LEAGUES:
            if target_leagues is None:
                target_leagues = []
            target_leagues.append(arg)

    if target_leagues is None:
        target_leagues = list(TM_LEAGUES.keys())

    # Load existing data (merge mode)
    existing = {}
    if OUTPUT_FILE.exists():
        with open(OUTPUT_FILE) as f:
            existing = json.load(f)
        logger.info("Caricati %d giocatori esistenti da cache", len(existing))

    total_new = 0
    total_updated = 0
    total_players = 0

    for league_key in target_leagues:
        league = TM_LEAGUES[league_key]
        logger.info("=== %s (%s) ===", league["name"], league["code"])

        teams = get_league_teams(league)
        if not teams:
            logger.warning("Nessuna squadra trovata per %s", league["name"])
            continue
        logger.info("Trovate %d squadre", len(teams))

        time.sleep(delay)

        for i, team in enumerate(teams):
            players = get_squad_positions(team["verein_id"], team["slug"])
            if players:
                for p in players:
                    key = normalize_name(p["name"])
                    if key not in existing:
                        total_new += 1
                    else:
                        total_updated += 1
                    existing[key] = p
                    total_players += 1
                logger.info(
                    "  [%d/%d] %s: %d giocatori",
                    i + 1, len(teams), players[0]["team"] if players else team["slug"],
                    len(players),
                )
            else:
                logger.warning("  [%d/%d] %s: nessun dato", i + 1, len(teams), team["slug"])

            time.sleep(delay)

        # Save after each league (resume-safe)
        with open(OUTPUT_FILE, "w") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
        logger.info("Salvati %d giocatori totali dopo %s", len(existing), league["name"])

    logger.info(
        "=== COMPLETO === %d giocatori processati (%d nuovi, %d aggiornati). "
        "Totale in cache: %d",
        total_players, total_new, total_updated, len(existing),
    )


if __name__ == "__main__":
    main()
