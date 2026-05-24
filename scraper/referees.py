"""
Referee statistics analyzer.

Builds comprehensive referee profiles from cached match data:
cards, penalties, goals, results, timing patterns.
No additional API calls needed — reads from existing match cache.
"""

import logging
import json
import glob
import os

logger = logging.getLogger(__name__)

LEAGUE_CODES = {
    "italy_serie_a": "SA",
    "england_premier_league": "PL",
    "spain_la_liga": "PD",
    "germany_bundesliga": "BL1",
    "france_ligue_1": "FL1",
    "netherlands_eredivisie": "DED",
    "champions_league": "CL",
    "england_championship": "ELC",
    "portugal_primeira_liga": "PPL",
    "brazil_serie_a": "BSA",
    "world_cup": "WC",
}

LEAGUE_NAMES = {
    "SA": "Serie A", "PL": "Premier League", "PD": "La Liga",
    "BL1": "Bundesliga", "FL1": "Ligue 1", "DED": "Eredivisie",
    "CL": "Champions League", "ELC": "Championship",
    "PPL": "Primeira Liga", "BSA": "Brasileirão", "WC": "Mondiali",
}


def _load_cache(league_code: str) -> dict:
    path = f"data/penalties/{league_code}_matches.json"
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f)


def _load_standings(league_code: str) -> dict:
    """Load team names from cache for ID->name mapping."""
    cache = _load_cache(league_code)
    teams = {}
    for d in cache.values():
        hid = d.get("home_id")
        aid = d.get("away_id")
        # Try to get names from players info
        for pid, pinfo in d.get("players", {}).items():
            tid = pinfo.get("team_id")
            if tid and tid not in teams:
                teams[tid] = None  # placeholder
    return teams


def analyze_referees(league_key: str) -> dict:
    """Build full referee stats for a league."""
    if league_key == "top10":
        return _analyze_all_leagues()

    code = LEAGUE_CODES.get(league_key)
    if not code:
        return {"success": False, "error": f"Campionato sconosciuto: {league_key}"}

    cache = _load_cache(code)
    if not cache:
        return {"success": False, "error": "Cache non disponibile. Lancia il precache."}

    league_name = LEAGUE_NAMES.get(code, league_key)
    refs = _build_referee_stats(cache, code)

    if not refs:
        return {"success": False, "error": "Nessun dato arbitro disponibile."}

    # Compute league averages for comparison
    league_avg = _compute_league_averages(refs)

    # Sort by matches descending
    refs.sort(key=lambda x: x["matches"], reverse=True)

    return {
        "success": True,
        "league": league_name,
        "league_avg": league_avg,
        "data": refs,
    }


def _analyze_all_leagues() -> dict:
    """Merge referee stats across all leagues."""
    all_cache = {}
    leagues_loaded = []
    for key, code in LEAGUE_CODES.items():
        c = _load_cache(code)
        if c:
            # Prefix match IDs with league code to avoid collisions
            for mid, detail in c.items():
                detail["_league"] = code
                all_cache[f"{code}_{mid}"] = detail
            leagues_loaded.append(LEAGUE_NAMES.get(code, key))

    if not all_cache:
        return {"success": False, "error": "Nessuna cache disponibile."}

    refs = _build_referee_stats(all_cache, "ALL")
    league_avg = _compute_league_averages(refs)
    refs.sort(key=lambda x: x["matches"], reverse=True)

    return {
        "success": True,
        "league": "Tutti i campionati",
        "league_avg": league_avg,
        "data": refs,
    }


def _build_referee_stats(cache: dict, league_code: str) -> list:
    """Core stats builder from match cache."""
    ref_map = {}

    for mid, d in cache.items():
        ref = d.get("referee")
        if not ref:
            continue

        if ref not in ref_map:
            ref_map[ref] = {
                "name": ref,
                "nationality": None,
                "leagues": set(),
                "matches": 0,
                "yellows": 0,
                "reds": 0,
                "yellow_reds": 0,
                "total_cards": 0,
                "penalties_awarded": 0,
                "penalty_minutes": [],
                "goals_total": 0,
                "goals_ht": 0,
                "goals_ft": 0,
                "home_wins": 0,
                "away_wins": 0,
                "draws": 0,
                "clean_sheets": 0,
                "high_scoring": 0,
                "cards_first_half": 0,
                "cards_second_half": 0,
                "cards_early": 0,      # ≤ 15'
                "cards_late": 0,       # ≥ 80'
                "cards_by_minute": [],
                "goals_by_minute": [],
                "subs_total": 0,
                "var_referees": set(),
                "match_details": [],   # for last matches display
                # --- NEW: score-state & home bias ---
                "cards_when_draw": 0,       # cards when match is level
                "cards_when_home_lead": 0,  # cards when home team leads
                "cards_when_away_lead": 0,  # cards when away team leads
                "cards_to_home": 0,         # cards given to home team
                "cards_to_away": 0,         # cards given to away team
                "minutes_draw": 0,          # total match-minutes in draw state
                "minutes_home_lead": 0,
                "minutes_away_lead": 0,
            }

        rs = ref_map[ref]
        rs["matches"] += 1

        # Nationality (take first non-null)
        nat = d.get("referee_nationality")
        if nat and not rs["nationality"]:
            rs["nationality"] = nat

        # League tracking
        lg = d.get("_league", league_code)
        if lg != "ALL":
            rs["leagues"].add(LEAGUE_NAMES.get(lg, lg))

        # VAR
        var = d.get("var_referee")
        if var:
            rs["var_referees"].add(var)

        # Cards
        for c in d.get("cards", []):
            minute = c.get("minute") or 0
            card_type = c.get("card", "")
            rs["cards_by_minute"].append(minute)

            if card_type == "YELLOW":
                rs["yellows"] += 1
                rs["total_cards"] += 1
            elif card_type == "RED":
                rs["reds"] += 1
                rs["total_cards"] += 1
            elif card_type == "YELLOW_RED":
                rs["yellow_reds"] += 1
                rs["total_cards"] += 1

            if minute <= 45:
                rs["cards_first_half"] += 1
            else:
                rs["cards_second_half"] += 1
            if minute <= 15:
                rs["cards_early"] += 1
            if minute >= 80:
                rs["cards_late"] += 1

        # --- NEW: Cards by score state & home/away bias ---
        home_id = d.get("home_id")
        away_id = d.get("away_id")
        match_goals = sorted(d.get("goals", []), key=lambda g: g.get("minute") or 0)
        match_cards = d.get("cards", [])

        # Reconstruct score at each card minute
        for c in match_cards:
            c_min = c.get("minute") or 0
            c_team = c.get("team_id")
            # Compute score at this minute
            h_score, a_score = 0, 0
            for g in match_goals:
                g_min = g.get("minute") or 0
                if g_min < c_min:
                    if g.get("team_id") == home_id:
                        h_score += 1
                    else:
                        a_score += 1
                else:
                    break
            if h_score == a_score:
                rs["cards_when_draw"] += 1
            elif h_score > a_score:
                rs["cards_when_home_lead"] += 1
            else:
                rs["cards_when_away_lead"] += 1

            # Home/Away card bias
            if c_team == home_id:
                rs["cards_to_home"] += 1
            elif c_team == away_id:
                rs["cards_to_away"] += 1

        # Goals & Penalties
        for g in d.get("goals", []):
            minute = g.get("minute") or 0
            rs["goals_by_minute"].append(minute)
            if g.get("type") == "PENALTY":
                rs["penalties_awarded"] += 1
                rs["penalty_minutes"].append(minute)

        # Score
        score = d.get("score") or {}
        winner = score.get("winner")
        if winner == "HOME_TEAM":
            rs["home_wins"] += 1
        elif winner == "AWAY_TEAM":
            rs["away_wins"] += 1
        elif winner == "DRAW":
            rs["draws"] += 1

        ft = score.get("fullTime") or {}
        ht = score.get("halfTime") or {}
        ft_goals = (ft.get("home") or 0) + (ft.get("away") or 0)
        ht_goals = (ht.get("home") or 0) + (ht.get("away") or 0)
        rs["goals_total"] += ft_goals
        rs["goals_ht"] += ht_goals
        rs["goals_ft"] += ft_goals

        if ft_goals == 0:
            rs["clean_sheets"] += 1
        if ft_goals >= 4:
            rs["high_scoring"] += 1

        # Substitutions
        rs["subs_total"] += len(d.get("substitutions", []))

        # Match detail for recent history
        home_id = d.get("home_id")
        away_id = d.get("away_id")
        rs["match_details"].append({
            "matchday": d.get("matchday", 0),
            "home_id": home_id,
            "away_id": away_id,
            "ft_home": ft.get("home", 0),
            "ft_away": ft.get("away", 0),
            "yellows": sum(1 for c in d.get("cards", []) if c.get("card") == "YELLOW"),
            "reds": sum(1 for c in d.get("cards", []) if c.get("card") in ("RED", "YELLOW_RED")),
            "penalties": sum(1 for g in d.get("goals", []) if g.get("type") == "PENALTY"),
            "league": LEAGUE_NAMES.get(d.get("_league", league_code), ""),
        })

    # ── Enrich with Transfermarkt stats for refs with few FD matches ──
    tm_ref_stats = _load_tm_referee_stats()
    if tm_ref_stats:
        for name, rs in ref_map.items():
            if rs["matches"] < 3:
                tm = tm_ref_stats.get(name)
                if tm and tm.get("appearances", 0) >= 3:
                    _merge_tm_into_ref(rs, tm)

        # Also inject TM-only referees not in FD cache at all
        # (e.g., Zanotti with 0 SA matches but assigned to upcoming games)
        for tm_name, tm in tm_ref_stats.items():
            if tm_name not in ref_map and tm.get("appearances", 0) >= 3 and not tm.get("not_found"):
                rs = {
                    "name": tm_name, "nationality": None, "leagues": set(),
                    "matches": 0, "yellows": 0, "reds": 0, "yellow_reds": 0,
                    "total_cards": 0, "penalties_awarded": 0, "penalty_minutes": [],
                    "goals_total": 0, "goals_ht": 0, "goals_ft": 0,
                    "home_wins": 0, "away_wins": 0, "draws": 0,
                    "clean_sheets": 0, "high_scoring": 0,
                    "cards_first_half": 0, "cards_second_half": 0,
                    "cards_early": 0, "cards_late": 0,
                    "cards_by_minute": [], "goals_by_minute": [],
                    "subs_total": 0, "var_referees": set(), "match_details": [],
                    "cards_when_draw": 0, "cards_when_home_lead": 0,
                    "cards_when_away_lead": 0, "cards_to_home": 0,
                    "cards_to_away": 0, "minutes_draw": 0,
                    "minutes_home_lead": 0, "minutes_away_lead": 0,
                }
                _merge_tm_into_ref(rs, tm)
                ref_map[tm_name] = rs

    # Convert to list with computed metrics
    result = []
    for name, rs in ref_map.items():
        m = rs["matches"]
        if m < 3:
            continue  # Skip referees with too few matches

        # Cards distribution by time zones
        cards_minutes = rs["cards_by_minute"]
        card_zones = {"0-15": 0, "16-30": 0, "31-45": 0, "46-60": 0, "61-75": 0, "76-90": 0}
        for cm in cards_minutes:
            if cm <= 15: card_zones["0-15"] += 1
            elif cm <= 30: card_zones["16-30"] += 1
            elif cm <= 45: card_zones["31-45"] += 1
            elif cm <= 60: card_zones["46-60"] += 1
            elif cm <= 75: card_zones["61-75"] += 1
            else: card_zones["76-90"] += 1

        # Goals distribution by time zones
        goals_minutes = rs["goals_by_minute"]
        goal_zones = {"0-15": 0, "16-30": 0, "31-45": 0, "46-60": 0, "61-75": 0, "76-90": 0}
        for gm in goals_minutes:
            if gm <= 15: goal_zones["0-15"] += 1
            elif gm <= 30: goal_zones["16-30"] += 1
            elif gm <= 45: goal_zones["31-45"] += 1
            elif gm <= 60: goal_zones["46-60"] += 1
            elif gm <= 75: goal_zones["61-75"] += 1
            else: goal_zones["76-90"] += 1

        # Sort match details by matchday desc, keep last 5
        match_history = sorted(rs["match_details"], key=lambda x: x["matchday"], reverse=True)[:5]

        entry = {
            "name": name,
            "nationality": rs["nationality"],
            "leagues": sorted(rs["leagues"]),
            "matches": m,
            # Cards
            "yellows": rs["yellows"],
            "reds": rs["reds"],
            "yellow_reds": rs["yellow_reds"],
            "cards_pg": round(rs["total_cards"] / m, 1),
            "yellows_pg": round(rs["yellows"] / m, 1),
            "reds_pg": round((rs["reds"] + rs["yellow_reds"]) / m, 2),
            "cards_first_half_pg": round(rs["cards_first_half"] / m, 1),
            "cards_second_half_pg": round(rs["cards_second_half"] / m, 1),
            "cards_early": rs["cards_early"],
            "cards_early_pg": round(rs["cards_early"] / m, 2),
            "cards_late": rs["cards_late"],
            "cards_late_pg": round(rs["cards_late"] / m, 2),
            "card_zones": card_zones,
            # Penalties
            "penalties": rs["penalties_awarded"],
            "penalties_pg": round(rs["penalties_awarded"] / m, 2),
            "penalty_minutes": sorted(rs["penalty_minutes"]),
            # Goals
            "goals_pg": round(rs["goals_total"] / m, 2),
            "goals_ht_pg": round(rs["goals_ht"] / m, 2),
            "goals_2nd_half_pg": round((rs["goals_ft"] - rs["goals_ht"]) / m, 2),
            "goal_zones": goal_zones,
            # Results
            "home_win_pct": round(rs["home_wins"] / m * 100),
            "away_win_pct": round(rs["away_wins"] / m * 100),
            "draw_pct": round(rs["draws"] / m * 100),
            "clean_sheets": rs["clean_sheets"],
            "clean_sheet_pct": round(rs["clean_sheets"] / m * 100),
            "high_scoring": rs["high_scoring"],
            "high_scoring_pct": round(rs["high_scoring"] / m * 100),
            # VAR
            "var_referees": sorted(rs["var_referees"])[:3],
            # Match history
            "last_matches": match_history,
            # --- NEW: Score-state card distribution ---
            "cards_when_draw": rs["cards_when_draw"],
            "cards_when_home_lead": rs["cards_when_home_lead"],
            "cards_when_away_lead": rs["cards_when_away_lead"],
            "cards_draw_pct": round(rs["cards_when_draw"] / max(rs["total_cards"], 1) * 100),
            "cards_home_lead_pct": round(rs["cards_when_home_lead"] / max(rs["total_cards"], 1) * 100),
            "cards_away_lead_pct": round(rs["cards_when_away_lead"] / max(rs["total_cards"], 1) * 100),
            # --- NEW: Home/Away card bias ---
            "cards_to_home": rs["cards_to_home"],
            "cards_to_away": rs["cards_to_away"],
            "cards_home_pct": round(rs["cards_to_home"] / max(rs["total_cards"], 1) * 100),
            "cards_away_pct": round(rs["cards_to_away"] / max(rs["total_cards"], 1) * 100),
            # --- TM enrichment flag ---
            "tm_enriched": rs.get("_tm_enriched", False),
        }
        result.append(entry)

    return result


def _merge_tm_into_ref(rs: dict, tm: dict):
    """Merge Transfermarkt season stats into a ref_map entry."""
    rs["matches"] += tm["appearances"]
    rs["yellows"] += tm.get("yellows", 0)
    rs["reds"] += tm.get("reds", 0) + tm.get("second_yellows", 0)
    rs["total_cards"] += tm.get("yellows", 0) + tm.get("reds", 0) + tm.get("second_yellows", 0)
    rs["penalties_awarded"] += tm.get("penalties", 0)
    tm_comps = [c["competition"] for c in tm.get("competitions", [])]
    for comp in tm_comps:
        rs["leagues"].add(comp)
    rs["_tm_enriched"] = True


def _load_tm_referee_stats() -> dict:
    """Load Transfermarkt referee stats fallback from referee_tm_stats.json."""
    try:
        tm_path = os.path.join("data", "referee_tm_stats.json")
        if os.path.exists(tm_path):
            with open(tm_path) as f:
                data = json.load(f)
            return data.get("referees", {})
    except Exception as e:
        logger.warning("TM referee stats load error: %s", e)
    return {}


def _compute_league_averages(refs: list) -> dict:
    """Compute weighted league averages for comparison."""
    if not refs:
        return {}

    total_m = sum(r["matches"] for r in refs)
    if total_m == 0:
        return {}

    total_cards = sum(r["cards_pg"] * r["matches"] for r in refs)
    total_pens = sum(r["penalties_pg"] * r["matches"] for r in refs)
    total_goals = sum(r["goals_pg"] * r["matches"] for r in refs)
    total_yellows = sum(r["yellows_pg"] * r["matches"] for r in refs)

    total_home = sum(r["home_win_pct"] * r["matches"] for r in refs)
    total_draw = sum(r["draw_pct"] * r["matches"] for r in refs)
    total_away = sum(r["away_win_pct"] * r["matches"] for r in refs)

    return {
        "cards_pg": round(total_cards / total_m, 1),
        "penalties_pg": round(total_pens / total_m, 2),
        "goals_pg": round(total_goals / total_m, 2),
        "yellows_pg": round(total_yellows / total_m, 1),
        "matches_avg": round(total_m / len(refs), 1),
        "home_win_pct": round(total_home / total_m),
        "draw_pct": round(total_draw / total_m),
        "away_win_pct": round(total_away / total_m),
    }
