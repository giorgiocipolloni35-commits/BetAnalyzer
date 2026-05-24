"""
Martingala Pareggi — Draw streak analyzer.

For each team in a league, computes:
- Draw rate (overall, home, away)
- Current draw drought (consecutive matchdays without a draw)
- Average matchdays between draws
- Max drought this season
- Recent form (last 10 results)
- Opportunity score (draw% + drought pressure + matchup context)
- Table position & zone (TOP/MID/LOW)
- Draw% by matchup type (TOP vs TOP, MID vs MID, etc.)
"""

import json
import os
import logging
from collections import defaultdict

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
}

LEAGUE_NAMES = {
    "SA": "Serie A", "PL": "Premier League", "PD": "La Liga",
    "BL1": "Bundesliga", "FL1": "Ligue 1", "DED": "Eredivisie",
    "CL": "Champions League", "ELC": "Championship",
    "PPL": "Primeira Liga", "BSA": "Brasileirao",
}


def _load_cache(league_code: str) -> dict:
    path = f"data/penalties/{league_code}_matches.json"
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f)


def _load_standings(league_code: str) -> dict:
    """Load team ID -> {name, position} from competitions_stats_cache."""
    mapping = {}
    try:
        path = "data/competitions_stats_cache.json"
        if not os.path.exists(path):
            return mapping
        with open(path) as f:
            data = json.load(f)
        code_to_key = {}
        for key, val in data.items():
            if isinstance(val, dict):
                code_to_key[val.get("league_code", "")] = key
        league_key = code_to_key.get(league_code, "")
        if league_key and league_key in data:
            teams = data[league_key].get("teams", [])
            for t in teams:
                mapping[t["id"]] = {
                    "name": t["name"],
                    "position": t.get("position", 99),
                }
    except Exception as e:
        logger.warning(f"Standings load error: {e}")
    return mapping


def _zone(position: int, total_teams: int) -> str:
    """Classify position into zone: TOP / MID / LOW."""
    # Top ~30%, Bottom ~30%, Middle rest
    top_cutoff = max(3, round(total_teams * 0.3))
    low_cutoff = total_teams - max(3, round(total_teams * 0.3))
    if position <= top_cutoff:
        return "TOP"
    elif position > low_cutoff:
        return "LOW"
    return "MID"


def _zone_label(zone: str) -> str:
    return {"TOP": "Alta", "MID": "Media", "LOW": "Bassa"}.get(zone, zone)


def analyze_martingala(league_key: str) -> dict:
    """Build martingala draw analysis for a league (or all leagues)."""
    if league_key == "all":
        return _analyze_all_leagues()

    code = LEAGUE_CODES.get(league_key)
    if not code:
        return {"success": False, "error": f"Campionato sconosciuto: {league_key}"}

    cache = _load_cache(code)
    if not cache:
        return {"success": False, "error": "Cache non disponibile. Lancia il precache."}

    league_name = LEAGUE_NAMES.get(code, league_key)
    standings = _load_standings(code)
    team_names = {tid: info["name"] for tid, info in standings.items()}
    team_positions = {tid: info["position"] for tid, info in standings.items()}
    total_teams_in_league = len(standings) or 20

    # ── Build per-team match history ──
    teams = defaultdict(list)

    for mid, d in cache.items():
        matchday = d.get("matchday") or 0
        if matchday == 0:
            continue

        score = d.get("score") or {}
        winner = score.get("winner", "")
        ft = score.get("fullTime") or {}
        ft_home = ft.get("home") or 0
        ft_away = ft.get("away") or 0

        home_id = d.get("home_id")
        away_id = d.get("away_id")

        if home_id:
            teams[home_id].append({
                "matchday": matchday,
                "is_home": True,
                "opponent_id": away_id,
                "result": "D" if winner == "DRAW" else ("W" if winner == "HOME_TEAM" else "L"),
                "ft_home": ft_home,
                "ft_away": ft_away,
                "score_str": f"{ft_home}-{ft_away}",
            })

        if away_id:
            teams[away_id].append({
                "matchday": matchday,
                "is_home": False,
                "opponent_id": home_id,
                "result": "D" if winner == "DRAW" else ("W" if winner == "AWAY_TEAM" else "L"),
                "ft_home": ft_home,
                "ft_away": ft_away,
                "score_str": f"{ft_home}-{ft_away}",
            })

    if not teams:
        return {"success": False, "error": "Nessun dato disponibile."}

    # ── Compute draw% by matchup type ──
    matchup_stats = defaultdict(lambda: {"total": 0, "draws": 0})

    for mid, d in cache.items():
        score = d.get("score") or {}
        winner = score.get("winner", "")
        hid = d.get("home_id")
        aid = d.get("away_id")
        if hid not in team_positions or aid not in team_positions:
            continue

        hz = _zone(team_positions[hid], total_teams_in_league)
        az = _zone(team_positions[aid], total_teams_in_league)
        # Normalize key (sorted so TOP vs LOW = LOW vs TOP)
        key = " vs ".join(sorted([hz, az]))
        matchup_stats[key]["total"] += 1
        if winner == "DRAW":
            matchup_stats[key]["draws"] += 1

    matchup_draw_pct = {}
    for key, ms in matchup_stats.items():
        if ms["total"] >= 5:
            matchup_draw_pct[key] = round(ms["draws"] / ms["total"] * 100, 1)

    # ── Analyze each team ──
    result = []
    for team_id, matches in teams.items():
        matches.sort(key=lambda x: x["matchday"])
        team_name = team_names.get(team_id, f"Team {team_id}")
        position = team_positions.get(team_id, 99)
        zone = _zone(position, total_teams_in_league)

        total = len(matches)
        if total < 5:
            continue

        draws_total = sum(1 for m in matches if m["result"] == "D")
        draws_home = sum(1 for m in matches if m["result"] == "D" and m["is_home"])
        draws_away = sum(1 for m in matches if m["result"] == "D" and not m["is_home"])
        home_matches = sum(1 for m in matches if m["is_home"])
        away_matches = sum(1 for m in matches if not m["is_home"])

        draw_pct = round(draws_total / total * 100, 1)
        draw_pct_home = round(draws_home / max(home_matches, 1) * 100, 1)
        draw_pct_away = round(draws_away / max(away_matches, 1) * 100, 1)

        # ── Droughts ──
        droughts = []
        current_drought = 0
        draw_matchdays = []

        for m in matches:
            if m["result"] == "D":
                if current_drought > 0:
                    droughts.append(current_drought)
                current_drought = 0
                draw_matchdays.append(m["matchday"])
            else:
                current_drought += 1

        max_drought = max(droughts + [current_drought]) if droughts or current_drought > 0 else 0

        # Average matchdays between draws
        if draws_total >= 2:
            intervals = [draw_matchdays[i] - draw_matchdays[i - 1]
                         for i in range(1, len(draw_matchdays))]
            avg_between = round(sum(intervals) / len(intervals), 1) if intervals else round(total / max(draws_total, 1), 1)
        elif draws_total == 1:
            avg_between = float(total)
        else:
            avg_between = float(total)

        # ── Draw% per opponent zone ──
        draw_by_opp_zone = {"TOP": {"total": 0, "draws": 0},
                            "MID": {"total": 0, "draws": 0},
                            "LOW": {"total": 0, "draws": 0}}
        for m in matches:
            opp_pos = team_positions.get(m["opponent_id"], 99)
            opp_zone = _zone(opp_pos, total_teams_in_league)
            draw_by_opp_zone[opp_zone]["total"] += 1
            if m["result"] == "D":
                draw_by_opp_zone[opp_zone]["draws"] += 1

        draw_pct_vs = {}
        for z, zs in draw_by_opp_zone.items():
            if zs["total"] >= 3:
                draw_pct_vs[z] = round(zs["draws"] / zs["total"] * 100, 1)

        # ── Status ──
        if draws_total == 0:
            status = "overdue"
            status_label = "MAI PAREGGIATO"
        elif current_drought >= avg_between * 2:
            status = "overdue"
            status_label = "OVERDUE"
        elif current_drought >= avg_between:
            status = "late"
            status_label = "IN RITARDO"
        else:
            status = "normal"
            status_label = "NELLA MEDIA"

        # ── Recent form (last 10) ──
        recent = matches[-10:]
        form = []
        for m in recent:
            opp_pos = team_positions.get(m["opponent_id"], 99)
            form.append({
                "matchday": m["matchday"],
                "result": m["result"],
                "is_home": m["is_home"],
                "score": m["score_str"],
                "opponent_id": m["opponent_id"],
                "opponent": team_names.get(m["opponent_id"], "?"),
                "opponent_pos": opp_pos,
                "opponent_zone": _zone(opp_pos, total_teams_in_league),
            })

        # ── Opportunity score ──
        # Base: draw_rate contributes 0-50
        # Drought pressure: (current_drought / avg_between) * 25
        # Zone bonus: MID gets +5, LOW gets +3 (they draw more)
        # Malus: TOP gets -5 (they draw less)
        if avg_between > 0 and draws_total > 0:
            drought_pressure = (current_drought / avg_between) * 25
        else:
            drought_pressure = current_drought * 5

        zone_bonus = 0
        if zone == "MID":
            zone_bonus = 5
        elif zone == "LOW":
            zone_bonus = 3
        elif zone == "TOP":
            zone_bonus = -5

        opportunity = min(100, max(0, round(draw_pct + drought_pressure + zone_bonus)))

        # Draw scores distribution
        draw_scores = defaultdict(int)
        for m in matches:
            if m["result"] == "D":
                draw_scores[m["score_str"]] += 1

        entry = {
            "team_id": team_id,
            "team": team_name,
            "position": position,
            "zone": zone,
            "zone_label": _zone_label(zone),
            "matches": total,
            "draws": draws_total,
            "draw_pct": draw_pct,
            "draw_pct_home": draw_pct_home,
            "draw_pct_away": draw_pct_away,
            "home_matches": home_matches,
            "away_matches": away_matches,
            "draws_home": draws_home,
            "draws_away": draws_away,
            "drought": current_drought,
            "avg_between": avg_between,
            "max_drought": max_drought,
            "status": status,
            "status_label": status_label,
            "opportunity": opportunity,
            "form": form,
            "draw_scores": dict(sorted(draw_scores.items(),
                                       key=lambda x: x[1], reverse=True)),
            "draw_matchdays": draw_matchdays,
            "last_draw_md": draw_matchdays[-1] if draw_matchdays else 0,
            "last_matchday": matches[-1]["matchday"] if matches else 0,
            "draw_pct_vs": draw_pct_vs,  # {TOP: 15.0, MID: 35.0, LOW: 42.0}
        }
        result.append(entry)

    # Sort by opportunity score descending
    result.sort(key=lambda x: x["opportunity"], reverse=True)

    # League averages
    total_matches = sum(t["matches"] for t in result)
    total_draws_all = sum(t["draws"] for t in result)
    league_draw_pct = round(total_draws_all / max(total_matches, 1) * 100, 1)
    avg_drought = round(sum(t["drought"] for t in result) / max(len(result), 1), 1)

    return {
        "success": True,
        "league": league_name,
        "league_code": code,
        "teams_count": len(result),
        "league_draw_pct": league_draw_pct,
        "avg_drought": avg_drought,
        "matchup_draw_pct": matchup_draw_pct,  # {"LOW vs LOW": 44.8, ...}
        "data": result,
    }


def _analyze_all_leagues() -> dict:
    """Merge martingala data across all leagues, return top opportunities."""
    all_data = []
    all_matchups = defaultdict(lambda: {"total": 0, "draws": 0})
    leagues_loaded = []

    for league_key, code in LEAGUE_CODES.items():
        if code == "CL":
            continue  # Skip CL — no stable standings for zone logic
        r = analyze_martingala(league_key)
        if not r.get("success"):
            continue
        league_name = r["league"]
        leagues_loaded.append(league_name)

        # Tag each team with its league
        for t in r["data"]:
            t["league_name"] = league_name
            t["league_flag"] = {
                "SA": "🇮🇹", "PL": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "PD": "🇪🇸", "BL1": "🇩🇪",
                "FL1": "🇫🇷", "DED": "🇳🇱", "ELC": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "PPL": "🇵🇹", "BSA": "🇧🇷",
            }.get(code, "")
            all_data.append(t)

        # Merge matchup stats
        for key, pct in r.get("matchup_draw_pct", {}).items():
            # We can't merge percentages directly, but for the global view
            # we just show per-league matchup data isn't meaningful — skip
            pass

    # Sort all by opportunity
    all_data.sort(key=lambda x: x["opportunity"], reverse=True)

    total_matches = sum(t["matches"] for t in all_data)
    total_draws = sum(t["draws"] for t in all_data)
    league_draw_pct = round(total_draws / max(total_matches, 1) * 100, 1)
    avg_drought = round(sum(t["drought"] for t in all_data) / max(len(all_data), 1), 1)

    return {
        "success": True,
        "league": "Tutti i campionati",
        "league_code": "ALL",
        "teams_count": len(all_data),
        "league_draw_pct": league_draw_pct,
        "avg_drought": avg_drought,
        "matchup_draw_pct": {},
        "leagues_loaded": leagues_loaded,
        "data": all_data,
    }
