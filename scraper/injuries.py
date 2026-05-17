"""
Injury scraper using Transfermarkt.
Fetches currently injured players per league from the public injury page.
Caches results for 6 hours to avoid excessive requests.
"""

import json
import logging
import os
import re
import time

import requests

logger = logging.getLogger(__name__)

CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "injuries")

TRANSFERMARKT_LEAGUES = {
    "italy_serie_a": "IT1",
    "england_premier_league": "GB1",
    "spain_la_liga": "ES1",
    "germany_bundesliga": "L1",
    "france_ligue_1": "FR1",
    "netherlands_eredivisie": "NL1",
    "england_championship": "GB2",
    "portugal_primeira_liga": "PO1",
    "brazil_serie_a": "BRA1",
    "champions_league": None,   # No injury page for CL
    "world_cup": None,
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}

CACHE_TTL = 6 * 3600  # 6 hours


def get_injured_players(league_key: str) -> list[dict]:
    """Return list of injured players for a league.

    Each entry: {"player": str, "team": str, "injury": str, "return_date": str}
    Uses disk cache (6h TTL) to avoid hammering Transfermarkt.
    """
    tm_code = TRANSFERMARKT_LEAGUES.get(league_key)
    if not tm_code:
        return []

    os.makedirs(CACHE_DIR, exist_ok=True)

    # Check cache
    cache_path = os.path.join(CACHE_DIR, f"{league_key}.json")
    if os.path.exists(cache_path):
        age = time.time() - os.path.getmtime(cache_path)
        if age < CACHE_TTL:
            try:
                with open(cache_path, "r") as f:
                    data = json.load(f)
                logger.debug("Injuries cache hit for %s (%d players)", league_key, len(data))
                return data
            except Exception:
                pass

    # Fetch from Transfermarkt
    url = f"https://www.transfermarkt.com/wettbewerb/verletztespieler/wettbewerb/{tm_code}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code != 200:
            logger.warning("Transfermarkt %s returned HTTP %d", league_key, r.status_code)
            return _load_stale_cache(cache_path)
    except Exception as e:
        logger.warning("Transfermarkt fetch error for %s: %s", league_key, e)
        return _load_stale_cache(cache_path)

    results = _parse_injury_page(r.text)
    logger.info("Scraped %d injured players for %s", len(results), league_key)

    # Save cache
    try:
        with open(cache_path, "w") as f:
            json.dump(results, f, ensure_ascii=False)
    except Exception as e:
        logger.warning("Failed to cache injuries: %s", e)

    return results


def get_injured_by_team(league_key: str) -> dict[str, list[dict]]:
    """Return injured players grouped by team name (lowercase).

    {team_name_lower: [{"player": str, "injury": str}, ...]}
    """
    players = get_injured_players(league_key)
    by_team: dict[str, list[dict]] = {}
    for p in players:
        team_low = p["team"].lower()
        by_team.setdefault(team_low, []).append({
            "player": p["player"],
            "injury": p["injury"],
        })
    return by_team


def _load_stale_cache(cache_path: str) -> list[dict]:
    """Load stale cache as fallback when fetch fails."""
    if os.path.exists(cache_path):
        try:
            with open(cache_path, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return []


def _parse_injury_page(html: str) -> list[dict]:
    """Parse Transfermarkt injury page HTML into structured data."""
    results = []

    # Find the <tbody> of the injury table
    tbody_match = re.search(r"<tbody>(.*?)</tbody>", html, re.DOTALL)
    if not tbody_match:
        return results

    tbody = tbody_match.group(1)

    # Split on top-level TR tags (odd/even class)
    entries = re.split(r'<tr\s+class="(?:odd|even)">', tbody)

    for entry in entries[1:]:  # skip empty first split
        # Player name
        player_m = re.search(r'class="hauptlink">\s*<a\s+title="([^"]+)"', entry)
        if not player_m:
            continue
        player = player_m.group(1)

        # Team (tiny_wappen title attribute)
        team_m = re.search(r'title="([^"]+)"[^>]*class="tiny_wappen"', entry)
        if not team_m:
            team_m = re.search(r'class="tiny_wappen"[^>]*alt="([^"]+)"', entry)
        team = team_m.group(1) if team_m else "?"

        # Injury reason — td with class="links"
        injury_m = re.search(r'class="links">([^<]+)<', entry)
        injury = injury_m.group(1).strip() if injury_m else "Unknown"

        # Return date — td class="zentriert" after the injury td
        return_date = ""
        after_injury = entry[entry.find('class="links"'):] if 'class="links"' in entry else ""
        return_m = re.search(r'class="zentriert"[^>]*>([^<]*)<', after_injury)
        if return_m:
            return_date = return_m.group(1).strip()

        results.append({
            "player": player,
            "team": team,
            "injury": injury,
            "return_date": return_date,
        })

    return results
