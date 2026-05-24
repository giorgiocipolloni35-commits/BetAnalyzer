#!/usr/bin/env python3
"""
Fetch Brasileirão Série A player statistics from Transfermarkt.

Scrapes two pages per team:
  1. leistungsdaten (player performance) — appearances, goals, assists,
     yellows, second yellows, reds, subs on/off, PPG, minutes
  2. kader (squad) — market values, height, foot, contract expiry

Saves everything to data/brazil_tm_stats.json.
Designed to run every 3 days via crontab.
"""

import json
import os
import re
import time
import logging
import requests
from pathlib import Path
from datetime import datetime
from bs4 import BeautifulSoup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [BRA] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

BASE_URL = "https://www.transfermarkt.com"
DATA_DIR = Path(os.path.dirname(os.path.abspath(__file__))) / "data"
CACHE_PATH = DATA_DIR / "brazil_tm_stats.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}

CURRENT_SEASON = "2025"

# ── All 20 Brasileirão Série A 2025 teams ──────────────────────────
TEAMS = [
    {"tm_id": 614,   "slug": "cr-flamengo",                         "name": "Flamengo"},
    {"tm_id": 1023,  "slug": "sociedade-esportiva-palmeiras",       "name": "Palmeiras"},
    {"tm_id": 585,   "slug": "fc-sao-paulo",                        "name": "São Paulo"},
    {"tm_id": 210,   "slug": "gremio-porto-alegre",                 "name": "Grêmio"},
    {"tm_id": 6600,  "slug": "sc-internacional-porto-alegre",       "name": "Internacional"},
    {"tm_id": 330,   "slug": "clube-atletico-mineiro",              "name": "Atlético Mineiro"},
    {"tm_id": 537,   "slug": "botafogo-rio-de-janeiro",             "name": "Botafogo"},
    {"tm_id": 2462,  "slug": "fluminense-rio-de-janeiro",           "name": "Fluminense"},
    {"tm_id": 199,   "slug": "corinthians-sao-paulo",               "name": "Corinthians"},
    {"tm_id": 978,   "slug": "vasco-da-gama-rio-de-janeiro",        "name": "Vasco da Gama"},
    {"tm_id": 221,   "slug": "fc-santos",                           "name": "Santos"},
    {"tm_id": 609,   "slug": "ec-cruzeiro-belo-horizonte",          "name": "Cruzeiro"},
    {"tm_id": 10010, "slug": "esporte-clube-bahia",                 "name": "Bahia"},
    {"tm_id": 679,   "slug": "club-athletico-paranaense",           "name": "Athletico Paranaense"},
    {"tm_id": 8793,  "slug": "red-bull-bragantino",                 "name": "Red Bull Bragantino"},
    {"tm_id": 2125,  "slug": "esporte-clube-vitoria",               "name": "Vitória"},
    {"tm_id": 776,   "slug": "coritiba-fc",                         "name": "Coritiba"},
    {"tm_id": 17776, "slug": "chapecoense",                         "name": "Chapecoense"},
    {"tm_id": 3876,  "slug": "mirassol-futebol-clube-sp-",          "name": "Mirassol"},
    {"tm_id": 10997, "slug": "clube-do-remo-pa-",                   "name": "Remo"},
]


def _get(url: str, retries: int = 3) -> requests.Response | None:
    """GET with retries and polite delay."""
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            if r.status_code == 200:
                return r
            if r.status_code == 429:
                wait = 30 * (attempt + 1)
                logger.warning("Rate-limited, waiting %ds...", wait)
                time.sleep(wait)
                continue
            logger.warning("HTTP %d for %s", r.status_code, url)
            return None
        except requests.RequestException as e:
            logger.warning("Request error: %s (attempt %d)", e, attempt + 1)
            time.sleep(5)
    return None


def _parse_int(s: str) -> int:
    """Parse a stat value, returning 0 for '-' or empty."""
    s = s.strip().replace("'", "").replace(".", "").replace(",", "")
    if not s or s == "-":
        return 0
    m = re.search(r"\d+", s)
    return int(m.group()) if m else 0


def _parse_market_value(s: str) -> dict:
    """Parse TM market value string like '€10.00m' or '€500k'."""
    s = s.strip()
    if not s or s == "-":
        return {"raw": s, "eur": 0}
    # Remove € sign
    s_clean = s.replace("€", "").strip()
    multiplier = 1
    if s_clean.endswith("m"):
        multiplier = 1_000_000
        s_clean = s_clean[:-1]
    elif s_clean.endswith("k"):
        multiplier = 1_000
        s_clean = s_clean[:-1]
    elif s_clean.endswith("bn"):
        multiplier = 1_000_000_000
        s_clean = s_clean[:-2]
    try:
        value = float(s_clean.replace(",", ".")) * multiplier
        return {"raw": s, "eur": int(value)}
    except ValueError:
        return {"raw": s, "eur": 0}


# ── Scrape player performance stats ────────────────────────────────
def _fetch_player_stats(team: dict) -> list[dict]:
    """
    Scrape leistungsdaten (player performance) page for a team.

    TM table columns (td indices for 18-cell rows):
      0: shirt number
      1: player cell (posrela)
      2: (empty image cell)
      3: name (hauptlink)
      4: position
      5: age
      6: nationality flag
      7: in squad (total squad entries)
      8: appearances
      9: goals
     10: assists
     11: yellow cards
     12: second yellow cards
     13: red cards
     14: substitutions on
     15: substitutions off
     16: PPG (points per game)
     17: minutes played
    """
    url = (
        f"{BASE_URL}/{team['slug']}/leistungsdaten/verein/{team['tm_id']}"
        f"/plus/1?reldata=%26{CURRENT_SEASON}"
    )
    r = _get(url)
    if not r:
        logger.error("Failed to fetch stats for %s", team["name"])
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    table = soup.find("table", class_="items")
    if not table:
        logger.warning("No stats table found for %s", team["name"])
        return []

    tbody = table.find("tbody")
    if not tbody:
        return []

    players = []
    for row in tbody.find_all("tr"):
        tds = row.find_all("td")
        if len(tds) < 18:
            continue  # skip sub-rows (2 or 1 cell rows)

        vals = [td.get_text(strip=True) for td in tds]

        # Extract name from hauptlink cell
        name_td = tds[3]
        name_link = name_td.find("a")
        player_name = name_link.get_text(strip=True) if name_link else vals[3]
        player_url = name_link["href"] if name_link and name_link.has_attr("href") else ""

        # Extract player TM ID from URL
        player_tm_id = None
        if player_url:
            m = re.search(r"/(\d+)$", player_url)
            if m:
                player_tm_id = int(m.group(1))

        position = vals[4]
        age = _parse_int(vals[5])
        shirt = vals[0] if vals[0] != "-" else None

        appearances = _parse_int(vals[8])
        goals = _parse_int(vals[9])
        assists = _parse_int(vals[10])
        yellows = _parse_int(vals[11])
        second_yellows = _parse_int(vals[12])
        reds = _parse_int(vals[13])
        subs_on = _parse_int(vals[14])
        subs_off = _parse_int(vals[15])

        # PPG
        ppg_str = vals[16].replace(",", ".")
        try:
            ppg = float(ppg_str)
        except ValueError:
            ppg = 0.0

        # Minutes — remove apostrophe and dots
        minutes = _parse_int(vals[17])

        players.append({
            "name": player_name,
            "tm_id": player_tm_id,
            "tm_url": player_url,
            "shirt": shirt,
            "position": position,
            "age": age,
            "appearances": appearances,
            "goals": goals,
            "assists": assists,
            "yellow_cards": yellows,
            "second_yellows": second_yellows,
            "red_cards": reds,
            "subs_on": subs_on,
            "subs_off": subs_off,
            "ppg": ppg,
            "minutes": minutes,
        })

    return players


# ── Scrape market values from kader page ───────────────────────────
def _fetch_market_values(team: dict) -> dict[str, dict]:
    """
    Scrape kader page for market values and bio data.
    Returns dict keyed by player name -> {market_value, height, foot, contract_until}.
    """
    url = (
        f"{BASE_URL}/{team['slug']}/kader/verein/{team['tm_id']}"
        f"/saison_id/{CURRENT_SEASON}/plus/1"
    )
    r = _get(url)
    if not r:
        logger.warning("Failed to fetch kader for %s", team["name"])
        return {}

    soup = BeautifulSoup(r.text, "html.parser")
    table = soup.find("table", class_="items")
    if not table:
        logger.warning("No kader table found for %s", team["name"])
        return {}

    tbody = table.find("tbody")
    if not tbody:
        return {}

    mv_data = {}
    for row in tbody.find_all("tr"):
        tds = row.find_all("td")
        if len(tds) < 10:
            continue

        # Name from hauptlink
        name_td = row.find("td", class_="hauptlink")
        if not name_td:
            continue
        name_link = name_td.find("a")
        player_name = name_link.get_text(strip=True) if name_link else name_td.get_text(strip=True)

        vals = [td.get_text(strip=True) for td in tds]

        # Market value is the last cell
        mv_str = vals[-1]
        mv = _parse_market_value(mv_str)

        # Height (index 7 in 13-cell rows)
        height = ""
        contract = ""
        foot = ""
        if len(tds) >= 13:
            height = vals[7]
            foot = vals[8]
            contract = vals[11]

        mv_data[player_name] = {
            "market_value": mv,
            "height": height,
            "foot": foot,
            "contract_until": contract,
        }

    return mv_data


# ── Scrape team total market value from startseite ────────────────
def _fetch_team_total_value(team: dict) -> str:
    """Get total squad market value from team main page."""
    url = f"{BASE_URL}/{team['slug']}/startseite/verein/{team['tm_id']}/saison_id/{CURRENT_SEASON}"
    r = _get(url)
    if not r:
        return ""
    soup = BeautifulSoup(r.text, "html.parser")
    # TM shows total market value in data-header
    val_el = soup.find("a", class_="data-header__market-value-wrapper")
    if val_el:
        return val_el.get_text(strip=True)
    return ""


# ── Main scrape function ──────────────────────────────────────────
def fetch_all():
    """Scrape all 20 Brasileirão teams and save to JSON."""
    logger.info("Starting Brasileirão scrape — %d teams", len(TEAMS))
    all_data = {}
    success = 0

    for i, team in enumerate(TEAMS, 1):
        logger.info("[%d/%d] Scraping %s (TM ID: %d)...",
                    i, len(TEAMS), team["name"], team["tm_id"])

        # 1) Player performance stats
        players = _fetch_player_stats(team)
        time.sleep(2)  # polite delay

        # 2) Market values from kader page
        mv_data = _fetch_market_values(team)
        time.sleep(2)

        # 3) Merge market values into player data
        for p in players:
            mv_info = mv_data.get(p["name"])
            if mv_info:
                p["market_value"] = mv_info["market_value"]
                p["height"] = mv_info["height"]
                p["foot"] = mv_info["foot"]
                p["contract_until"] = mv_info["contract_until"]
            else:
                p["market_value"] = {"raw": "", "eur": 0}
                p["height"] = ""
                p["foot"] = ""
                p["contract_until"] = ""

        # Sort by appearances desc, then goals desc
        players.sort(key=lambda x: (-x["appearances"], -x["goals"]))

        # Team summary stats
        total_goals = sum(p["goals"] for p in players)
        total_assists = sum(p["assists"] for p in players)
        total_yellows = sum(p["yellow_cards"] for p in players)
        total_reds = sum(p["red_cards"] + p["second_yellows"] for p in players)
        squad_size = len(players)
        players_used = len([p for p in players if p["appearances"] > 0])
        total_mv = sum(p["market_value"]["eur"] for p in players)

        # Top scorer / top assister
        top_scorer = max(players, key=lambda x: x["goals"]) if players else None
        top_assister = max(players, key=lambda x: x["assists"]) if players else None

        all_data[team["name"]] = {
            "tm_id": team["tm_id"],
            "slug": team["slug"],
            "squad_size": squad_size,
            "players_used": players_used,
            "total_goals": total_goals,
            "total_assists": total_assists,
            "total_yellows": total_yellows,
            "total_reds": total_reds,
            "total_market_value_eur": total_mv,
            "top_scorer": {
                "name": top_scorer["name"],
                "goals": top_scorer["goals"],
            } if top_scorer and top_scorer["goals"] > 0 else None,
            "top_assister": {
                "name": top_assister["name"],
                "assists": top_assister["assists"],
            } if top_assister and top_assister["assists"] > 0 else None,
            "players": players,
        }

        success += 1
        logger.info("  ✓ %s — %d players (%d used), %d goals, %d assists",
                    team["name"], squad_size, players_used, total_goals, total_assists)

        # Extra delay every 5 teams
        if i % 5 == 0 and i < len(TEAMS):
            logger.info("  (pause 5s to be polite to TM)")
            time.sleep(5)

    # Save
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    result = {
        "fetched_at": datetime.now().isoformat(),
        "season": CURRENT_SEASON,
        "league": "Brasileirão Série A",
        "teams_scraped": success,
        "teams_total": len(TEAMS),
        "teams": all_data,
    }

    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    logger.info("Done! Saved %d teams to %s", success, CACHE_PATH)
    return result


if __name__ == "__main__":
    fetch_all()
