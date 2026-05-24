#!/usr/bin/env python3
"""
Fetch referee statistics from Transfermarkt for current season.

Searches for referees by name, scrapes their season summary stats
(appearances, yellows, second yellows, reds, penalties) across all
competitions (Serie A, Serie B, Coppa Italia, etc.).

Saves to data/referee_tm_stats.json for use as fallback when
Football-Data cache has insufficient data for a referee.
"""

import json
import os
import re
import time
import logging
import requests
from pathlib import Path
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)

BASE_URL = "https://www.transfermarkt.com"
DATA_DIR = Path(os.path.dirname(__file__)) / "data"
CACHE_PATH = DATA_DIR / "referee_tm_stats.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}

# Season ID for TM (2025 = 2025/26 season)
CURRENT_SEASON = "2025"


def _search_referee(name: str) -> dict | None:
    """Search TM for a referee by name. Returns {tm_id, slug, full_name} or None."""
    query = name.strip()
    url = f"{BASE_URL}/schnellsuche/ergebnis/schnellsuche"
    params = {"query": query, "Schiedsrichter": "Schiedsrichter"}

    try:
        r = requests.get(url, headers=HEADERS, params=params, timeout=15)
        if r.status_code != 200:
            return None

        soup = BeautifulSoup(r.text, "html.parser")
        refs = soup.find_all("a", href=lambda h: h and "/schiedsrichter/" in h if h else False)

        # Find best match by name
        query_parts = query.lower().split()
        best = None
        best_score = 0

        for ref_link in refs:
            href = ref_link.get("href", "")
            ref_name = ref_link.get_text(strip=True)
            if not ref_name or len(ref_name) < 3:
                continue

            # Extract TM ID from href: /name-slug/profil/schiedsrichter/12345
            match = re.search(r"/schiedsrichter/(\d+)", href)
            if not match:
                continue

            tm_id = int(match.group(1))
            slug = href.split("/")[1] if "/" in href else ""

            # Score: how well does the name match?
            ref_parts = ref_name.lower().split()
            score = sum(1 for p in query_parts if any(p in rp or rp in p for rp in ref_parts))
            # Bonus for exact surname match
            if query_parts[-1] == ref_parts[-1]:
                score += 2

            if score > best_score:
                best_score = score
                best = {"tm_id": tm_id, "slug": slug, "full_name": ref_name}

        return best if best_score >= 2 else None

    except Exception as e:
        logger.warning(f"Search error for '{name}': {e}")
        return None


def _fetch_referee_stats(tm_id: int, slug: str, season: str = CURRENT_SEASON) -> dict | None:
    """Fetch season summary stats from TM referee profile page.

    Returns {
        appearances: int, yellows: int, second_yellows: int,
        reds: int, penalties: int,
        competitions: [{name, appearances, yellows, ...}],
        yellows_per_match: float, penalties_per_match: float,
    }
    """
    url = f"{BASE_URL}/{slug}/profil/schiedsrichter/{tm_id}/saison/{season}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code != 200:
            return None

        soup = BeautifulSoup(r.text, "html.parser")
        resp_tables = soup.find_all("div", class_="responsive-table")
        if not resp_tables:
            return None

        # First table = competition summary (Competition, Appearances, Y, Y2, R, Pen)
        table = resp_tables[0].find("table")
        if not table:
            return None

        competitions = []
        total = None

        # Parse tbody rows (per-competition)
        tbody = table.find("tbody")
        if tbody:
            for tr in tbody.find_all("tr"):
                tds = tr.find_all("td")
                if len(tds) < 5:
                    continue
                cells = [td.get_text(strip=True) for td in tds]
                # cells: ['', 'Serie B', '15', '56', '1', '1', '4']
                # Find the competition name and numeric values
                comp_name = ""
                nums = []
                for c in cells:
                    if c.isdigit():
                        nums.append(int(c))
                    elif c and not c.startswith("€") and len(c) > 1:
                        comp_name = c

                if comp_name and len(nums) >= 4:
                    competitions.append({
                        "competition": comp_name,
                        "appearances": nums[0],
                        "yellows": nums[1],
                        "second_yellows": nums[2] if len(nums) > 2 else 0,
                        "reds": nums[3] if len(nums) > 3 else 0,
                        "penalties": nums[4] if len(nums) > 4 else 0,
                    })

        # Parse tfoot (total row)
        tfoot = table.find("tfoot")
        if tfoot:
            for tr in tfoot.find_all("tr"):
                tds = tr.find_all("td")
                nums = []
                for td in tds:
                    text = td.get_text(strip=True)
                    if text.isdigit():
                        nums.append(int(text))
                if len(nums) >= 4:
                    total = {
                        "appearances": nums[0],
                        "yellows": nums[1],
                        "second_yellows": nums[2] if len(nums) > 2 else 0,
                        "reds": nums[3] if len(nums) > 3 else 0,
                        "penalties": nums[4] if len(nums) > 4 else 0,
                    }

        # Fallback: sum from competitions
        if not total and competitions:
            total = {
                "appearances": sum(c["appearances"] for c in competitions),
                "yellows": sum(c["yellows"] for c in competitions),
                "second_yellows": sum(c["second_yellows"] for c in competitions),
                "reds": sum(c["reds"] for c in competitions),
                "penalties": sum(c["penalties"] for c in competitions),
            }

        if not total:
            return None

        apps = total["appearances"] or 1
        total["yellows_per_match"] = round(total["yellows"] / apps, 2)
        total["penalties_per_match"] = round(total["penalties"] / apps, 2)
        total["cards_per_match"] = round((total["yellows"] + total["second_yellows"] + total["reds"]) / apps, 2)
        total["competitions"] = competitions

        return total

    except Exception as e:
        logger.warning(f"Fetch error for {slug} (ID {tm_id}): {e}")
        return None


def fetch_all_referee_stats(referee_names: list[str] | None = None) -> dict:
    """Fetch TM stats for a list of referees (or all assigned to upcoming matches).

    If referee_names is None, reads assigned referees from penalty caches.
    Returns {referee_name: {appearances, yellows, penalties, ...}}
    """
    if referee_names is None:
        # Auto-discover from penalty caches
        referee_names = set()
        penalties_dir = DATA_DIR / "penalties"
        if penalties_dir.exists():
            for fname in penalties_dir.iterdir():
                if not fname.name.endswith("_matches.json"):
                    continue
                try:
                    with open(fname) as f:
                        cache = json.load(f)
                    for mid, d in cache.items():
                        ref = d.get("referee")
                        if ref:
                            referee_names.add(ref)
                except Exception:
                    pass
        referee_names = sorted(referee_names)

    # Load existing cache to avoid re-fetching
    existing = {}
    if CACHE_PATH.exists():
        try:
            with open(CACHE_PATH) as f:
                existing = json.load(f)
        except Exception:
            pass

    result = existing.get("referees", {})
    fetched = 0
    skipped = 0

    for name in referee_names:
        # Skip if already cached (refresh weekly via scheduler)
        if name in result and result[name].get("appearances", 0) > 0:
            skipped += 1
            continue

        logger.info(f"🔍 Searching TM for: {name}")
        ref_info = _search_referee(name)
        if not ref_info:
            logger.info(f"  ❌ Not found on TM")
            result[name] = {"appearances": 0, "not_found": True}
            fetched += 1
            time.sleep(2)
            continue

        logger.info(f"  ✅ Found: {ref_info['full_name']} (ID: {ref_info['tm_id']})")
        time.sleep(1.5)

        stats = _fetch_referee_stats(ref_info["tm_id"], ref_info["slug"])
        if stats:
            stats["tm_id"] = ref_info["tm_id"]
            stats["tm_name"] = ref_info["full_name"]
            result[name] = stats
            logger.info(f"  📊 {stats['appearances']} app, {stats['yellows']} Y, "
                        f"{stats['penalties']} Pen ({stats['penalties_per_match']}/g)")
        else:
            logger.info(f"  ⚠️ No stats found for current season")
            result[name] = {"appearances": 0, "tm_id": ref_info["tm_id"]}

        fetched += 1
        time.sleep(2)

    from datetime import datetime
    output = {
        "updated_at": datetime.now().isoformat(),
        "season": CURRENT_SEASON,
        "total_referees": len(result),
        "referees": result,
    }

    # Save
    DATA_DIR.mkdir(exist_ok=True)
    with open(CACHE_PATH, "w") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    logger.info(f"\n💾 Saved to {CACHE_PATH}")
    logger.info(f"   Total: {len(result)} referees ({fetched} fetched, {skipped} cached)")

    return output


def main():
    """Run standalone: fetch stats for all referees found in penalty caches."""
    logger.info("🚀 Fetching referee stats from Transfermarkt...")
    stats = fetch_all_referee_stats()

    # Print summary
    refs = stats.get("referees", {})
    logger.info(f"\n{'=' * 80}")
    logger.info(f"📊 REFEREE TM STATS SUMMARY")
    logger.info(f"{'=' * 80}")
    logger.info(f"{'Arbitro':<30} {'PG':>4} {'Gialli':>6} {'G/pg':>5} {'Rossi':>5} {'Rig':>4} {'R/pg':>5}")
    logger.info("-" * 70)

    for name, r in sorted(refs.items(), key=lambda x: x[1].get("appearances", 0), reverse=True):
        if r.get("not_found") or r.get("appearances", 0) == 0:
            continue
        logger.info(
            f"{name:<30} {r['appearances']:>4} {r['yellows']:>6} "
            f"{r['yellows_per_match']:>5} {r['reds']:>5} "
            f"{r['penalties']:>4} {r['penalties_per_match']:>5}"
        )


if __name__ == "__main__":
    main()
