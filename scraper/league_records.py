"""
League Records — Top/Bottom stats per campionato.

Calculates league-wide records from match cache:
team fouls/cards/penalties, referee stats, scoring patterns.
"""

import json
import os
import logging

logger = logging.getLogger(__name__)

LEAGUE_CODES = {
    "italy_serie_a": "SA", "england_premier_league": "PL",
    "spain_la_liga": "PD", "germany_bundesliga": "BL1",
    "france_ligue_1": "FL1", "netherlands_eredivisie": "DED",
    "champions_league": "CL", "england_championship": "ELC",
    "portugal_primeira_liga": "PPL", "brazil_serie_a": "BSA",
}


def _load_cache(code: str) -> dict:
    path = f"data/penalties/{code}_matches.json"
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f)


def _load_standings(code: str) -> dict:
    """Load team names from standings cache."""
    path = "data/competitions_stats_cache.json"
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        data = json.load(f)
    # data is dict: {"Italy Serie A": {"league_code": "SA", "teams": [...]}, ...}
    for league_name, entry in data.items():
        if isinstance(entry, dict) and entry.get("league_code") == code:
            return {t["id"]: t["name"] for t in entry.get("teams", [])}
    return {}


def get_league_records(league_key: str) -> dict:
    """Calculate all league records for a given league."""
    code = LEAGUE_CODES.get(league_key)
    # Also accept direct codes (SA, PL, etc.)
    if not code and league_key in LEAGUE_CODES.values():
        code = league_key
    if not code:
        return {"success": False, "error": "Campionato sconosciuto"}

    cache = _load_cache(code)
    if not cache:
        return {"success": False, "error": "Cache non disponibile"}

    # Load team names
    team_names = _load_standings(code)

    # --- Accumulators ---
    teams = {}  # team_id -> stats
    refs = {}   # ref_name -> stats

    for mid, d in cache.items():
        home_id = d.get("home_id")
        away_id = d.get("away_id")
        referee = d.get("referee")
        score = d.get("score") or {}
        ft = score.get("fullTime") or {}
        ht = score.get("halfTime") or {}
        winner = score.get("winner")

        ft_home = ft.get("home") or 0
        ft_away = ft.get("away") or 0
        ht_home = ht.get("home") or 0
        ht_away = ht.get("away") or 0

        # Init teams
        for tid in [home_id, away_id]:
            if tid and tid not in teams:
                teams[tid] = {
                    "id": tid,
                    "name": team_names.get(tid, f"Team {tid}"),
                    "matches": 0,
                    "yellows": 0, "reds": 0, "total_cards": 0,
                    "fouls_cards": 0,  # proxy: total cards = proxy for fouls
                    "penalties_for": 0, "penalties_against": 0,
                    "goals_1st_half": 0, "goals_2nd_half": 0,
                    "goals_total": 0,
                    "draws": 0,
                }

        # Team matches
        if home_id in teams:
            teams[home_id]["matches"] += 1
            teams[home_id]["goals_1st_half"] += ht_home
            teams[home_id]["goals_2nd_half"] += (ft_home - ht_home)
            teams[home_id]["goals_total"] += ft_home
            if winner == "DRAW":
                teams[home_id]["draws"] += 1
        if away_id in teams:
            teams[away_id]["matches"] += 1
            teams[away_id]["goals_1st_half"] += ht_away
            teams[away_id]["goals_2nd_half"] += (ft_away - ht_away)
            teams[away_id]["goals_total"] += ft_away
            if winner == "DRAW":
                teams[away_id]["draws"] += 1

        # Cards per team
        for c in d.get("cards", []):
            tid = c.get("team_id")
            card_type = c.get("card", "")
            if tid in teams:
                teams[tid]["total_cards"] += 1
                if card_type == "YELLOW":
                    teams[tid]["yellows"] += 1
                elif card_type in ("RED", "YELLOW_RED"):
                    teams[tid]["reds"] += 1

        # Penalties per team
        for g in d.get("goals", []):
            if g.get("type") == "PENALTY":
                scorer_tid = g.get("team_id")
                if scorer_tid == home_id:
                    if home_id in teams:
                        teams[home_id]["penalties_for"] += 1
                    if away_id in teams:
                        teams[away_id]["penalties_against"] += 1
                elif scorer_tid == away_id:
                    if away_id in teams:
                        teams[away_id]["penalties_for"] += 1
                    if home_id in teams:
                        teams[home_id]["penalties_against"] += 1

        # Referee stats
        if referee:
            if referee not in refs:
                refs[referee] = {
                    "name": referee,
                    "matches": 0, "yellows": 0, "reds": 0,
                    "penalties": 0, "total_cards": 0,
                }
            refs[referee]["matches"] += 1
            for c in d.get("cards", []):
                card_type = c.get("card", "")
                refs[referee]["total_cards"] += 1
                if card_type == "YELLOW":
                    refs[referee]["yellows"] += 1
                elif card_type in ("RED", "YELLOW_RED"):
                    refs[referee]["reds"] += 1
            for g in d.get("goals", []):
                if g.get("type") == "PENALTY":
                    refs[referee]["penalties"] += 1

    # --- Build records ---
    team_list = [t for t in teams.values() if t["matches"] >= 5]
    ref_list = [r for r in refs.values() if r["matches"] >= 5]

    def _top(lst, key, n=3, reverse=True):
        s = sorted(lst, key=lambda x: x[key], reverse=reverse)
        return [{"name": x["name"], "value": x[key], "matches": x["matches"],
                 "per_game": round(x[key] / max(x["matches"], 1), 2)} for x in s[:n]]

    records = {
        "team_most_cards": _top(team_list, "total_cards"),
        "team_most_yellows": _top(team_list, "yellows"),
        "team_most_reds": _top(team_list, "reds"),
        "team_most_penalties_for": _top(team_list, "penalties_for"),
        "team_least_penalties_for": _top(team_list, "penalties_for", reverse=False),
        "team_most_goals_1st": _top(team_list, "goals_1st_half"),
        "team_most_goals_2nd": _top(team_list, "goals_2nd_half"),
        "team_most_draws": _top(team_list, "draws"),
        "ref_most_yellows": _top(ref_list, "yellows"),
        "ref_most_reds": _top(ref_list, "reds"),
        "ref_most_penalties": _top(ref_list, "penalties"),
        "ref_least_penalties": _top(ref_list, "penalties", reverse=False),
    }

    return {"success": True, "records": records}
