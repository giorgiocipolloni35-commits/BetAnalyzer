#!/usr/bin/env python3
"""
Fetch VAR review statistics per referee from Sportmonks match comments.

Scans finished matches for all managed leagues, parses "VAR" mentions in
comments, and builds per-referee stats:
  - var_reviews: total VAR checks
  - var_per_match: average reviews per match
  - var_overturned: decisions changed after review
  - var_confirmed: decisions confirmed
  - var_penalty, var_goal, var_red: breakdown by type

Saves to data/var_stats.json for use by the card model and email pipeline.
"""

import json
import os
import re
import sys
import time
import logging
import requests
from datetime import datetime, timedelta
from collections import defaultdict
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)

# Sportmonks league IDs (same as sportmonks.py)
SM_LEAGUES = {
    "italy_serie_a": 384,
    "england_premier_league": 8,
    "spain_la_liga": 564,
    "germany_bundesliga": 82,
    "france_ligue_1": 301,
    "netherlands_eredivisie": 72,
    "england_championship": 9,
    "portugal_primeira_liga": 462,
}

BASE_URL = "https://api.sportmonks.com/v3/football"
DATA_DIR = Path(os.path.dirname(__file__)) / "data"


def _get(endpoint: str, params: dict, api_key: str) -> dict | None:
    """Make a Sportmonks API request with rate limiting."""
    params["api_token"] = api_key
    try:
        r = requests.get(f"{BASE_URL}/{endpoint}", params=params, timeout=20)
        if r.status_code == 429:
            logger.warning("Rate limited, sleeping 60s...")
            time.sleep(60)
            r = requests.get(f"{BASE_URL}/{endpoint}", params=params, timeout=20)
        if r.status_code != 200:
            logger.warning(f"API {r.status_code}: {r.text[:200]}")
            return None
        return r.json()
    except Exception as e:
        logger.error(f"Request error: {e}")
        return None


def _parse_var_comment(text: str) -> dict | None:
    """Parse a VAR-related comment and classify it.

    Returns dict with type and outcome, or None if not VAR-related.
    """
    if "VAR" not in text:
        return None

    t = text.lower()

    result = {"raw": text[:200]}

    # Classify type
    if "penalty" in t:
        result["type"] = "penalty"
    elif "goal" in t and ("disallow" in t or "confirmed" in t or "annull" in t):
        result["type"] = "goal"
    elif "red card" in t or "red" in t:
        result["type"] = "red_card"
    elif "card" in t:
        result["type"] = "card"
    else:
        result["type"] = "other"

    # Classify outcome
    if any(w in t for w in ["disallow", "annull", "overturned", "no penalty", "denied", "no goal"]):
        result["outcome"] = "overturned"
    elif any(w in t for w in ["confirmed", "granted", "awarded", "receives"]):
        result["outcome"] = "confirmed"
    elif "review" in t and "no " in t:
        result["outcome"] = "overturned"
    else:
        result["outcome"] = "review"  # just a review, unclear outcome

    return result


def fetch_var_stats(api_key: str, days_back: int = 180) -> dict:
    """Fetch VAR stats for all leagues over the last N days.

    Returns {referee_name: {matches, var_reviews, var_per_match, ...}}
    """
    # Collect all referee→match→VAR data
    referee_data = defaultdict(lambda: {
        "matches": set(),
        "var_reviews": 0,
        "var_overturned": 0,
        "var_confirmed": 0,
        "var_penalty": 0,
        "var_goal": 0,
        "var_red": 0,
        "var_card": 0,
        "var_other": 0,
        "var_details": [],
        "leagues": set(),
    })

    # Also track matches per referee (even without VAR) for rate calculation
    referee_all_matches = defaultdict(set)

    total_matches = 0
    total_var = 0

    for league_name, league_id in SM_LEAGUES.items():
        logger.info(f"📡 Fetching {league_name} (ID: {league_id})...")

        # Get current season
        season_resp = _get(f"leagues/{league_id}", {"include": "currentSeason"}, api_key)
        if not season_resp or "data" not in season_resp:
            logger.warning(f"  Cannot get season for {league_name}")
            continue

        league_data = season_resp["data"]
        # Sportmonks uses lowercase "currentseason" (no underscore/camelCase)
        current_season = (league_data.get("currentseason")
                          or league_data.get("currentSeason")
                          or league_data.get("current_season"))

        season_id = None
        if isinstance(current_season, dict):
            season_id = current_season.get("id")

        # Fallback: get most recent season from seasons list
        if not season_id:
            seasons_resp = _get(f"leagues/{league_id}", {"include": "seasons"}, api_key)
            if seasons_resp and "data" in seasons_resp:
                seasons = seasons_resp["data"].get("seasons", [])
                if seasons:
                    # Sort by ID desc (most recent first)
                    recent = sorted(seasons, key=lambda s: s.get("id", 0), reverse=True)
                    season_id = recent[0].get("id")

        if not season_id:
            logger.warning(f"  No season found for {league_name}")
            continue

        logger.info(f"  Season ID: {season_id}")

        # Fetch finished matches with comments + referees
        page = 1
        league_matches = 0
        league_var = 0

        while True:
            resp = _get("fixtures", {
                "filters": f"fixtureLeagues:{league_id};seasons:{season_id}",
                "include": "comments;referees.referee",
                "per_page": 25,
                "page": page,
            }, api_key)

            if not resp or "data" not in resp:
                break

            fixtures = resp["data"]
            if not fixtures:
                break

            for fix in fixtures:
                # state_id=5 means FT (finished)
                state_id = fix.get("state_id")
                if state_id != 5:
                    continue

                fix_id = fix["id"]

                # Get main referee (type_id=6 in Sportmonks)
                referees = fix.get("referees", [])
                main_ref = None
                for ref in referees:
                    if ref.get("type_id") == 6:
                        inner = ref.get("referee", {})
                        main_ref = inner.get("common_name") or inner.get("name")
                        break

                if not main_ref:
                    continue

                referee_all_matches[main_ref].add(fix_id)
                referee_data[main_ref]["leagues"].add(league_name)
                total_matches += 1
                league_matches += 1

                # Parse comments for VAR
                comments = fix.get("comments", [])
                match_var_count = 0

                for c in comments:
                    text = c.get("comment", "")
                    var_info = _parse_var_comment(text)
                    if not var_info:
                        continue

                    match_var_count += 1
                    rd = referee_data[main_ref]
                    rd["var_reviews"] += 1

                    # Type breakdown
                    vtype = var_info["type"]
                    if vtype == "penalty":
                        rd["var_penalty"] += 1
                    elif vtype == "goal":
                        rd["var_goal"] += 1
                    elif vtype == "red_card":
                        rd["var_red"] += 1
                    elif vtype == "card":
                        rd["var_card"] += 1
                    else:
                        rd["var_other"] += 1

                    # Outcome
                    if var_info["outcome"] == "overturned":
                        rd["var_overturned"] += 1
                    elif var_info["outcome"] == "confirmed":
                        rd["var_confirmed"] += 1

                if match_var_count > 0:
                    referee_data[main_ref]["matches"].add(fix_id)
                    total_var += match_var_count
                    league_var += match_var_count

            # Check pagination
            pagination = resp.get("pagination", {})
            has_more = pagination.get("has_more", False)
            if not has_more:
                break
            page += 1
            time.sleep(0.5)  # Rate limiting

        logger.info(f"  ✅ {league_name}: {league_matches} matches, {league_var} VAR reviews")

    # Build final output
    result = {}
    for ref_name, rd in referee_data.items():
        total_ref_matches = len(referee_all_matches.get(ref_name, set()))
        matches_with_var = len(rd["matches"])

        if total_ref_matches < 3:
            continue  # Not enough data

        result[ref_name] = {
            "matches": total_ref_matches,
            "matches_with_var": matches_with_var,
            "var_reviews": rd["var_reviews"],
            "var_per_match": round(rd["var_reviews"] / total_ref_matches, 2) if total_ref_matches > 0 else 0,
            "var_match_pct": round(matches_with_var / total_ref_matches * 100, 1) if total_ref_matches > 0 else 0,
            "var_overturned": rd["var_overturned"],
            "var_confirmed": rd["var_confirmed"],
            "var_overturn_pct": round(rd["var_overturned"] / rd["var_reviews"] * 100, 1) if rd["var_reviews"] > 0 else 0,
            "var_penalty": rd["var_penalty"],
            "var_goal": rd["var_goal"],
            "var_red": rd["var_red"],
            "var_card": rd["var_card"],
            "var_other": rd["var_other"],
            "leagues": sorted(rd["leagues"]),
        }

    # Sort by var_per_match
    result = dict(sorted(result.items(), key=lambda x: x[1]["var_per_match"], reverse=True))

    # League averages
    all_ref_vpm = [v["var_per_match"] for v in result.values() if v["matches"] >= 5]
    avg_vpm = round(sum(all_ref_vpm) / len(all_ref_vpm), 2) if all_ref_vpm else 0

    output = {
        "updated_at": datetime.now().isoformat(),
        "total_matches": total_matches,
        "total_var_reviews": total_var,
        "avg_var_per_match": avg_vpm,
        "referees": result,
    }

    return output


def main():
    api_key = os.getenv("SPORTMONKS_API_KEY", "")
    if not api_key:
        try:
            env_path = Path(__file__).parent / ".env"
            with open(env_path) as f:
                for line in f:
                    if line.startswith("SPORTMONKS_API_KEY"):
                        api_key = line.split("=", 1)[1].strip().strip('"')
        except:
            pass

    if not api_key:
        print("❌ SPORTMONKS_API_KEY not found")
        sys.exit(1)

    logger.info("🚀 Fetching VAR statistics from Sportmonks...")
    stats = fetch_var_stats(api_key)

    # Save
    DATA_DIR.mkdir(exist_ok=True)
    out_path = DATA_DIR / "var_stats.json"
    with open(out_path, "w") as f:
        json.dump(stats, f, indent=2, ensure_ascii=False)

    # Print summary
    refs = stats.get("referees", {})
    logger.info(f"\n{'='*80}")
    logger.info(f"📊 VAR STATISTICS SUMMARY")
    logger.info(f"{'='*80}")
    logger.info(f"Total matches scanned: {stats['total_matches']}")
    logger.info(f"Total VAR reviews found: {stats['total_var_reviews']}")
    logger.info(f"Average VAR/match (league): {stats['avg_var_per_match']}")
    logger.info(f"Referees with data: {len(refs)}")
    logger.info(f"\n{'Arbitro':<30} {'PG':>4} {'VAR':>4} {'V/g':>5} {'%PG':>5} {'Rib%':>5} {'Rig':>3} {'Gol':>3} {'Red':>3}")
    logger.info("-" * 90)

    for name, r in list(refs.items())[:25]:
        logger.info(
            f"{name:<30} {r['matches']:>4} {r['var_reviews']:>4} "
            f"{r['var_per_match']:>5} {r['var_match_pct']:>4.0f}% "
            f"{r['var_overturn_pct']:>4.0f}% {r['var_penalty']:>3} "
            f"{r['var_goal']:>3} {r['var_red']:>3}"
        )

    logger.info(f"\n💾 Saved to {out_path}")


if __name__ == "__main__":
    main()
