"""
Yellow card probability analyzer.

Uses cached match details from PenaltyAnalyzer to build player card stats.
Crosses player card rate with referee card tendency to find value bets.
"""

import logging
import os
import json

from scraper.penalties import PenaltyAnalyzer, LEAGUE_CODES, CACHE_DIR
from logic.match_importance import detect_derby

logger = logging.getLogger(__name__)


from typing import Optional

def _fuzzy_name_match(fd_name: str, sm_names: dict) -> Optional[str]:
    """Match football-data.org name to Sportmonks name."""
    fd_low = fd_name.lower().strip()
    if fd_low in sm_names:
        return fd_low

    fd_parts = fd_low.replace(".", "").split()
    if len(fd_parts) < 2:
        candidates = [k for k in sm_names if fd_low in k or k in fd_low]
        return candidates[0] if len(candidates) == 1 else None

    fd_surname = fd_parts[-1]
    candidates = [k for k in sm_names if k.split()[-1] == fd_surname]

    if len(candidates) == 1:
        return candidates[0]

    if candidates:
        fd_initial = fd_parts[0][0] if fd_parts[0] else ""
        for c in candidates:
            c_parts = c.split()
            if c_parts and c_parts[0][0] == fd_initial:
                return c
    return None


class CardAnalyzer:

    def __init__(self, api_key: str):
        self.pa = PenaltyAnalyzer(api_key)

    def analyze_league(self, league_key: str) -> dict:
        code = LEAGUE_CODES.get(league_key)
        if not code:
            raise ValueError(f"Unknown league: {league_key}")

        logger.info("Analyzing cards for %s (%s)", league_key, code)

        cache = self.pa._load_cache(code)
        if not cache:
            return {"matches": [], "error": "Nessuna cache disponibile. Lancia prima l'analisi Rigori per questa lega."}

        # Check if cache has player names in cards
        sample = next(iter(cache.values()), {})
        sample_cards = sample.get("cards", [])
        if sample_cards and "player" not in sample_cards[0]:
            return {"matches": [], "error": "Cache non aggiornata. Rilancia l'analisi Rigori per aggiornare i dati cartellini."}

        # Build player stats
        player_stats = self._build_player_stats(cache)

        # Build player positions map
        player_positions_map = self.pa._build_player_positions(cache)

        # Build referee stats
        referee_stats = self._build_referee_card_stats(cache)

        # Build player×referee cross-stats (#1)
        player_referee_cross = self._build_player_referee_cross(cache)

        # Get scheduled matches with referees + live fallback
        scheduled_data = self.pa._get(f"/competitions/{code}/matches",
                                       params={"status": "SCHEDULED,TIMED"})

        # Fetch live/finished for referee fallback
        today_live = self.pa._get(f"/competitions/{code}/matches",
                                   params={"status": "IN_PLAY,PAUSED,FINISHED"})
        live_referee_map = {}
        if today_live:
            from datetime import datetime, timedelta
            today_str = datetime.utcnow().strftime("%Y-%m-%d")
            yesterday_str = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")
            for m in today_live.get("matches", []):
                match_date = (m.get("utcDate") or "")[:10]
                if match_date not in (today_str, yesterday_str):
                    continue
                h_id = m["homeTeam"]["id"]
                a_id = m["awayTeam"]["id"]
                for ref in m.get("referees", []):
                    if ref.get("type") == "REFEREE":
                        live_referee_map[(h_id, a_id)] = ref.get("name")
                        break

        if not scheduled_data or not scheduled_data.get("matches"):
            if today_live and today_live.get("matches"):
                scheduled_data = today_live
            else:
                return {"matches": [], "error": "Nessuna partita programmata"}

        # Build team rosters from standings
        standings = self.pa._get_standings(code)
        team_names = {s["team_id"]: s["name"] for s in standings}
        team_positions = {s["team_id"]: s["position"] for s in standings}
        num_teams = len(standings) or 1

        # Current matchday for diffida
        finished_data = self.pa._get(f"/competitions/{code}/matches",
                                      params={"status": "FINISHED"})
        current_matchday = 0
        if finished_data:
            mdays = [m.get("matchday", 0) for m in finished_data.get("matches", []) if m.get("matchday")]
            current_matchday = max(mdays) if mdays else 0

        # Total matchdays in the season (for last-matchday detection, #8)
        total_matchdays = num_teams * 2 - 2 if num_teams > 1 else 38  # e.g. 38 for 20 teams

        # Build diffida status
        diffida_data = self.pa._build_diffida_status(cache, code, current_matchday)

        # League average cards per match
        total_cards = sum(1 for d in cache.values() for c in d.get("cards", []) if c.get("card") == "YELLOW")
        total_matches = len(cache)
        league_avg_cards_pm = total_cards / total_matches if total_matches > 0 else 4.0

        # ── League-wide behavioural averages (for normalisation) ──
        all_fouls = [p.get("fouls_per_game", 0) for p in player_stats.values()
                     if p.get("fouls_per_game") is not None and p.get("matches", 0) >= 5]
        league_avg_fpg = sum(all_fouls) / len(all_fouls) if all_fouls else 0.8

        all_tackles = [p.get("tackles_per_game", 0) for p in player_stats.values()
                       if p.get("tackles_per_game") is not None and p.get("matches", 0) >= 5]
        league_avg_tpg = sum(all_tackles) / len(all_tackles) if all_tackles else 1.2

        # ── Team-level "danger" stats: avg dribbles + fouls_drawn per game ──
        team_danger = {}   # team_id → avg dribbles_att + fouls_drawn per game by their players
        from collections import defaultdict
        _td_sum = defaultdict(lambda: {"drib": 0, "fd": 0, "n": 0})
        for pid, p in player_stats.items():
            tid = p.get("team_id")
            if tid and p.get("matches", 0) >= 5:
                _td_sum[tid]["drib"] += (p.get("dribbles_att_per_game") or 0)
                _td_sum[tid]["fd"] += (p.get("fouls_drawn_per_game") or 0)
                _td_sum[tid]["n"] += 1
        for tid, v in _td_sum.items():
            n = max(v["n"], 1)
            team_danger[tid] = round((v["drib"] + v["fd"]) / n, 2)

        all_danger = [v for v in team_danger.values() if v > 0]
        league_avg_danger = sum(all_danger) / len(all_danger) if all_danger else 1.0

        # ── Role boost map ──
        # Sportmonks position_ids: 24=GK, 25=DEF, 26=MID, 27=ATT, 28=unknown
        # Also support text-based role names from player_positions_map
        ROLE_BOOST = {
            # Sportmonks position_id based (from player_info.position_id)
            "24": 0.30,   # Goalkeeper
            "25": 1.20,   # Defender — high foul/card risk
            "26": 1.15,   # Midfielder — moderate-high
            "27": 0.90,   # Attacker — lower card risk
            "28": 1.05,   # Unknown/utility
            "221": 1.05,  # Unknown
            # Text-based role names (from FD positions map)
            "Centre-Back": 1.20, "DC": 1.20, "Defence": 1.20,
            "Defensive Midfield": 1.25, "CDM": 1.25,
            "Central Midfield": 1.15, "CEN": 1.15, "CC": 1.15, "Midfield": 1.15,
            "Left-Back": 1.10, "Right-Back": 1.10, "TS": 1.10, "TD": 1.10,
            "Attacking Midfield": 1.00, "Trequartista": 1.00,
            "Left Winger": 0.95, "Right Winger": 0.95,
            "Centre-Forward": 0.90, "Attacker": 0.90, "ATT": 0.90, "Offence": 0.90,
            "Goalkeeper": 0.30,
        }

        # Process scheduled matches
        match_results = []
        seen_matches = set()

        for m in scheduled_data.get("matches", []):
            home = m.get("homeTeam", {})
            away = m.get("awayTeam", {})
            home_id = home.get("id")
            away_id = away.get("id")
            matchday = m.get("matchday")

            match_key = f"{home_id}_{away_id}"
            if match_key in seen_matches:
                continue
            seen_matches.add(match_key)

            ref_name = None
            for ref in m.get("referees", []):
                if ref.get("type") == "REFEREE":
                    ref_name = ref.get("name")
                    break
            if not ref_name:
                ref_name = live_referee_map.get((home_id, away_id))

            ref_data = referee_stats.get(ref_name) if ref_name else None
            ref_cards_pm = ref_data["cards_per_match"] if ref_data else league_avg_cards_pm
            ref_matches_count = ref_data["matches"] if ref_data else 0

            # Bayesian smoothing on referee (K=5)
            K = 5
            if ref_data and ref_data["matches"] > 0:
                effective_ref_cpm = (ref_cards_pm * ref_data["matches"] + league_avg_cards_pm * K) / (ref_data["matches"] + K)
            else:
                effective_ref_cpm = league_avg_cards_pm

            ref_multiplier = effective_ref_cpm / league_avg_cards_pm if league_avg_cards_pm > 0 else 1.0

            # Underdog factor: team lower in standings defends more → more fouls
            home_pos = team_positions.get(home_id, num_teams // 2)
            away_pos = team_positions.get(away_id, num_teams // 2)
            pos_diff = abs(home_pos - away_pos)

            # Derby detection
            home_name = home.get("name", "")
            away_name = away.get("name", "")
            is_derby = detect_derby(home_name, away_name) is not None

            # Opponent danger factor: how much the opponent provokes fouls
            opp_danger_for_home = team_danger.get(away_id, league_avg_danger)
            opp_danger_for_away = team_danger.get(home_id, league_avg_danger)

            # Get players for both teams
            home_players = self._get_team_players(player_stats, home_id)
            away_players = self._get_team_players(player_stats, away_id)

            # ── NEW: H2H card history for this matchup (#3) ──
            h2h_stats = self._build_h2h_card_stats(cache, home_id, away_id)

            # ── NEW: Team fatigue from fixture density (#9) ──
            home_fatigue = self._compute_team_fatigue(cache, home_id, current_matchday)
            away_fatigue = self._compute_team_fatigue(cache, away_id, current_matchday)

            picks = []
            for p in home_players + away_players:
                if p["matches"] < 5:
                    continue

                pid_team = p["team_id"]
                is_home_team = pid_team == home_id

                # ════════════════════════════════════════════════
                #  COMPOSITE CARD MODEL v4
                # ════════════════════════════════════════════════
                #
                # Architecture: historical base × behavioural multipliers
                # v4 adds: H2H, recent form, away boost, red aggression,
                #          match importance, fatigue, card-minute timing
                #
                # BASE: Historical yellows/match (anchors the probability)
                hist_ypm = p.get("yellows_per_match", 0)

                # For players with 0 yellows but behavioural data (B1 fix),
                # use a behaviour-derived base instead of 0
                fpg = p.get("fouls_per_game") or 0
                if hist_ypm == 0 and fpg > 0:
                    # Estimate: ~1 yellow per 6-8 fouls committed historically
                    hist_ypm = min(fpg / 7.0, 0.15)

                # MULTIPLIER 1: Fouls committed (aggressive players foul more → more yellows)
                fouls_ratio = fpg / max(league_avg_fpg, 0.3)
                fouls_mult = 1.0 + (fouls_ratio - 1.0) * 0.30

                # MULTIPLIER 2: Tackles per game (physical engagement)
                tpg = p.get("tackles_per_game") or 0
                tackles_ratio = tpg / max(league_avg_tpg, 0.5)
                tackles_mult = 1.0 + (tackles_ratio - 1.0) * 0.15

                # MULTIPLIER 3: Role adjustment
                player_role = player_positions_map.get(p.get("player_id"), "") if p.get("player_id") else ""
                role_mult = ROLE_BOOST.get(player_role, None)
                if role_mult is None:
                    sm_pos = p.get("sm_position_id")
                    role_mult = ROLE_BOOST.get(sm_pos, 1.0)
                    if sm_pos:
                        player_role = {"24": "Goalkeeper", "25": "Defender", "26": "Midfielder", "27": "Attacker"}.get(sm_pos, player_role)

                # MULTIPLIER 4: Opponent danger (provocative opponents → more fouls)
                opp_d = opp_danger_for_home if is_home_team else opp_danger_for_away
                opp_ratio = opp_d / max(league_avg_danger, 0.3)
                opp_mult = 1.0 + (opp_ratio - 1.0) * 0.10

                # MULTIPLIER 5: Underdog boost
                underdog_mult = 1.0
                pid_pos = team_positions.get(pid_team, num_teams // 2)
                opp_pos = away_pos if is_home_team else home_pos
                if pos_diff >= 5 and pid_pos > opp_pos:
                    underdog_mult = 1.0 + min(0.15, pos_diff * 0.01)

                # Combine: base × first 5 multipliers
                adjusted_prob = hist_ypm * fouls_mult * tackles_mult * role_mult * opp_mult * ref_multiplier * underdog_mult

                # MULTIPLIER 6: Match tension (rivalry, relegation, title race, DERBY)
                tension_mult = 1.0
                if is_derby:
                    tension_mult = 1.20
                elif pos_diff <= 3 and home_pos <= 6:
                    tension_mult = 1.10
                elif home_pos >= num_teams - 3 or away_pos >= num_teams - 3:
                    tension_mult = 1.12
                elif pos_diff <= 2:
                    tension_mult = 1.08
                adjusted_prob *= tension_mult

                # MULTIPLIER 7: Minutes normalization
                mr = p.get("minutes_ratio")
                if mr is not None and mr > 0:
                    minutes_mult = max(0.70, mr)
                    adjusted_prob *= minutes_mult

                # ── NEW MULTIPLIER 8: Away boost (#2 — moved from HIGH to here) ──
                # Players away from home get ~15% more yellows statistically
                if not is_home_team:
                    adjusted_prob *= 1.12

                # ── NEW MULTIPLIER 9: H2H history (#3) ──
                pid_id = p.get("player_id")
                h2h_info = h2h_stats.get(pid_id)
                if h2h_info and h2h_info["h2h_matches"] >= 2:
                    h2h_rate = h2h_info["h2h_yellows"] / h2h_info["h2h_matches"]
                    overall_rate = p.get("yellows_per_match", 0)
                    if overall_rate > 0 and h2h_rate > overall_rate:
                        # Player gets carded more vs this opponent → boost
                        h2h_mult = 1.0 + min(0.20, (h2h_rate / overall_rate - 1.0) * 0.25)
                        adjusted_prob *= h2h_mult

                # ── NEW MULTIPLIER 10: Recent card trend (#6) ──
                recent_ratio = p.get("recent_form_ratio", 1.0)
                if recent_ratio > 1.0:
                    # Hot streak: getting more yellows recently
                    recent_mult = 1.0 + min(0.20, (recent_ratio - 1.0) * 0.15)
                    adjusted_prob *= recent_mult
                elif recent_ratio < 0.5 and p.get("yellows", 0) > 0:
                    # Cold streak: fewer yellows recently → slight decrease
                    adjusted_prob *= 0.92

                # ── NEW MULTIPLIER 11: Red card aggression (#7) ──
                if p.get("has_red"):
                    reds = p.get("reds_season", 0)
                    # Players with reds are more aggressive / reckless
                    red_mult = 1.0 + min(0.15, reds * 0.08)
                    adjusted_prob *= red_mult

                # ── NEW MULTIPLIER 12: Match importance / last matchdays (#8) ──
                if matchday and total_matchdays > 0:
                    remaining = total_matchdays - matchday
                    if remaining <= 2:
                        # Last 2 matchdays: higher tension, nothing to lose
                        adjusted_prob *= 1.10
                    elif remaining <= 5 and (home_pos <= 4 or away_pos <= 4
                                              or home_pos >= num_teams - 3 or away_pos >= num_teams - 3):
                        # Last 5 matchdays + teams fighting for title or survival
                        adjusted_prob *= 1.06

                # ── NEW MULTIPLIER 13: Card minute timing (#4) ──
                avg_min = p.get("avg_card_minute")
                if avg_min is not None and p.get("yellows", 0) >= 3:
                    # Players who get carded early (< 45') are more reckless
                    if avg_min < 40:
                        adjusted_prob *= 1.08
                    elif avg_min > 75:
                        # Late-card players: if they play full 90 it's fine,
                        # but if subbed at 60' they lose card window → slight decrease
                        avg_mins_played = p.get("avg_minutes")
                        if avg_mins_played and avg_mins_played < 75:
                            adjusted_prob *= 0.90

                # ── NEW MULTIPLIER 14: Team fatigue (#9) ──
                team_fat = home_fatigue if is_home_team else away_fatigue
                adjusted_prob *= team_fat["fatigue_mult"]

                # ── NEW MULTIPLIER 15: Player×Referee cross (#1) ──
                # If this player has a history with the assigned referee, adjust
                if ref_name and pid_id:
                    pr_key = (ref_name, pid_id)
                    pr_data = player_referee_cross.get(pr_key)
                    if pr_data and pr_data["matches"] >= 2:
                        pr_rate = pr_data["rate"]
                        overall_rate = p.get("yellows_per_match", 0)
                        if overall_rate > 0:
                            # Compare: this ref cards this player more or less than average?
                            ratio = pr_rate / overall_rate
                            if ratio > 1.0:
                                # This ref cards this player MORE → boost up to +25%
                                pr_mult = 1.0 + min(0.25, (ratio - 1.0) * 0.30)
                                adjusted_prob *= pr_mult
                            elif ratio < 0.6 and pr_data["matches"] >= 3:
                                # This ref lets this player off → dampen (only with 3+ meetings)
                                adjusted_prob *= 0.90
                        elif pr_data["yellows"] > 0:
                            # Player has 0 overall rate but got carded by THIS ref
                            adjusted_prob *= 1.15

                # FLOOR: players with high fouls/game get a minimum probability
                # even if their historical yellow rate is low (B1 enhancement)
                if fpg >= 1.0 and adjusted_prob < 0.10:
                    adjusted_prob = max(adjusted_prob, 0.08 + (fpg - 1.0) * 0.06)

                # Cap: realistic maximum ~55%
                adjusted_prob = min(0.55, adjusted_prob)

                if adjusted_prob < 0.05:
                    continue

                score = round(adjusted_prob * 100)
                team_name = team_names.get(p["team_id"], "?")

                # Check diffida status
                diffida_info = diffida_data.get(pid_id) if pid_id else None
                is_diffidato = diffida_info["diffidato"] if diffida_info else False
                player_yellows_season = diffida_info["yellows"] if diffida_info else p["yellows"]

                picks.append({
                    "player": p["player"],
                    "team": team_name,
                    "team_id": p["team_id"],
                    "is_home": is_home_team,
                    "matches": p["matches"],
                    "yellows": p["yellows"],
                    "yellows_per_match": round(p.get("yellows_per_match", 0), 2),
                    "probability": score,
                    "adjusted_prob": round(adjusted_prob, 2),
                    "diffidato": is_diffidato,
                    "yellows_season": player_yellows_season,
                    "is_underdog": underdog_mult > 1.0,
                    "position": player_role,
                    "avg_card_minute": p.get("avg_card_minute"),
                    "reds_season": p.get("reds_season", 0),
                    "recent_form_ratio": p.get("recent_form_ratio", 1.0),
                    "h2h_yellows": h2h_info["h2h_yellows"] if h2h_info else 0,
                    "h2h_matches": h2h_info["h2h_matches"] if h2h_info else 0,
                    "ref_yellows": player_referee_cross.get((ref_name, pid_id), {}).get("yellows", 0) if ref_name and pid_id else 0,
                    "ref_matches": player_referee_cross.get((ref_name, pid_id), {}).get("matches", 0) if ref_name and pid_id else 0,
                    # Advanced Sportmonks stats
                    "fouls_per_game": p.get("fouls_per_game"),
                    "tackles_per_game": p.get("tackles_per_game"),
                    "interceptions_per_game": p.get("interceptions_per_game"),
                    "fouls_drawn_per_game": p.get("fouls_drawn_per_game"),
                    "dribbles_att_per_game": p.get("dribbles_att_per_game"),
                    "aerials_per_game": p.get("aerials_per_game"),
                    "avg_minutes": p.get("avg_minutes"),
                })

            picks.sort(key=lambda x: x["probability"], reverse=True)

            # Determine which team is the underdog
            underdog_team = None
            if pos_diff >= 5:
                underdog_team = home.get("name", "?") if home_pos > away_pos else away.get("name", "?")

            match_results.append({
                "home_team": home.get("name", "?"),
                "away_team": away.get("name", "?"),
                "home_id": home_id,
                "away_id": away_id,
                "utcDate": m.get("utcDate"),
                "home_position": home_pos,
                "away_position": away_pos,
                "matchday": matchday,
                "referee": ref_name,
                "referee_matches": ref_matches_count,
                "referee_cards_pm": round(ref_cards_pm, 1) if ref_data else None,
                "referee_multiplier": round(ref_multiplier, 2),
                "league_avg_cards_pm": round(league_avg_cards_pm, 1),
                "underdog_team": underdog_team,
                "top_picks": picks[:14],
            })

        match_results.sort(key=lambda x: x.get("utcDate") or "")

        return {"matches": match_results}

    @staticmethod
    def _build_player_stats(cache: dict) -> dict:
        """Build per-player yellow card stats from cache.

        B1 FIX: Also registers players from lineups/subs who have 0 yellows,
        so they can still appear in predictions via behavioural multipliers.
        """
        players: dict[int, dict] = {}

        # Sort matches by matchday for recency weighting
        sorted_mids = sorted(cache.keys(),
                             key=lambda k: cache[k].get("matchday") or 0)
        max_matchday = max((cache[k].get("matchday") or 0) for k in cache) if cache else 0

        # ── Pass 1: Register ALL players from lineups + subs (B1 fix) ──
        # Build a player_id → name mapping from all match data
        player_names_from_cache = {}  # pid → name
        for mid, detail in cache.items():
            # From cards (has player names)
            for card in detail.get("cards", []):
                pid = card.get("player_id")
                if pid and card.get("player"):
                    player_names_from_cache[pid] = card["player"]
            # From players dict if available (value can be str or dict with "name")
            for pid_str, pval in detail.get("players", {}).items():
                try:
                    pid_int = int(pid_str)
                    if isinstance(pval, str):
                        player_names_from_cache[pid_int] = pval
                    elif isinstance(pval, dict) and pval.get("name"):
                        player_names_from_cache[pid_int] = pval["name"]
                except (ValueError, TypeError):
                    pass

        for mid, detail in cache.items():
            home_id = detail.get("home_id")
            away_id = detail.get("away_id")

            # Register from lineup_ids
            for pid in detail.get("lineup_ids", []):
                if pid is None:
                    continue
                if pid not in players:
                    # Determine team: check which side this player belongs to
                    # Use cards or player_positions to infer team
                    p_team = None
                    for card in detail.get("cards", []):
                        if card.get("player_id") == pid:
                            p_team = card.get("team_id")
                            break
                    if not p_team:
                        # Check substitutions
                        for s in detail.get("substitutions", []):
                            if s.get("player_out_id") == pid or s.get("player_in_id") == pid:
                                p_team = s.get("team_id")
                                break
                    players[pid] = {
                        "player": player_names_from_cache.get(pid, "?"),
                        "player_id": pid,
                        "team_id": p_team,
                        "yellows": 0,
                        "reds": 0,
                        "card_minutes": [],
                        "recent_yellows": 0,
                        "appearances_set": set(),
                    }
                players[pid]["appearances_set"].add(mid)

            # Register from substitutions (players subbed IN)
            for sub in detail.get("substitutions", []):
                pid = sub.get("player_in_id")
                if pid is None:
                    continue
                if pid not in players:
                    players[pid] = {
                        "player": player_names_from_cache.get(pid, "?"),
                        "player_id": pid,
                        "team_id": sub.get("team_id"),
                        "yellows": 0,
                        "reds": 0,
                        "card_minutes": [],
                        "recent_yellows": 0,
                        "appearances_set": set(),
                    }
                players[pid]["appearances_set"].add(mid)

        # ── Pass 2: Count yellows, reds, card minutes, recent form ──
        for mid, detail in cache.items():
            matchday = detail.get("matchday") or 0

            for card in detail.get("cards", []):
                pid = card.get("player_id")
                if pid is None:
                    continue

                # Register player if not yet known (edge case: card but not in lineup)
                if pid not in players:
                    players[pid] = {
                        "player": card.get("player", "?"),
                        "player_id": pid,
                        "team_id": card.get("team_id"),
                        "yellows": 0,
                        "reds": 0,
                        "card_minutes": [],
                        "recent_yellows": 0,
                        "appearances_set": set(),
                    }
                    players[pid]["appearances_set"].add(mid)

                # Update team_id if still None
                if players[pid]["team_id"] is None and card.get("team_id"):
                    players[pid]["team_id"] = card["team_id"]
                # Update name if still "?"
                if players[pid]["player"] == "?" and card.get("player"):
                    players[pid]["player"] = card["player"]

                if card.get("card") == "YELLOW":
                    players[pid]["yellows"] += 1
                    minute = card.get("minute")
                    if minute is not None:
                        players[pid]["card_minutes"].append(minute)
                    # Recent form: last 8 matchdays
                    if max_matchday > 0 and matchday >= max_matchday - 7:
                        players[pid]["recent_yellows"] += 1
                elif card.get("card") in ("RED", "YELLOW_RED", "SECOND_YELLOW"):
                    players[pid]["reds"] += 1

        # Count total matches per team to estimate player appearances
        team_matches: dict[int, int] = {}
        for detail in cache.values():
            for tid in (detail.get("home_id"), detail.get("away_id")):
                if tid is not None:
                    team_matches[tid] = team_matches.get(tid, 0) + 1

        # Connect to DB to get real appearances + advanced stats
        # Also load current team_id to handle mid-season transfers
        import sqlite3
        conn = sqlite3.connect('data/betanalyzer.db', timeout=30)
        cursor = conn.cursor()
        sportmonks_stats = {}
        current_team_by_name = {}  # name_lower → current team_id
        sm_position_ids = {}       # name_lower → position_id (24=GK,25=DEF,26=MID,27=ATT)
        try:
            # 1. Load advanced stats (only players with stats)
            cursor.execute("""
                SELECT pi.name,
                       pi.team_id,
                       pi.position_id,
                       JSON_EXTRACT(psc.stats_json, '$.appearances'),
                       JSON_EXTRACT(psc.stats_json, '$.fouls_committed'),
                       JSON_EXTRACT(psc.stats_json, '$.tackles'),
                       JSON_EXTRACT(psc.stats_json, '$.interceptions'),
                       JSON_EXTRACT(psc.stats_json, '$.fouls_drawn'),
                       JSON_EXTRACT(psc.stats_json, '$.dribbles_attempts'),
                       JSON_EXTRACT(psc.stats_json, '$.aerials_won'),
                       JSON_EXTRACT(psc.stats_json, '$.minutes_played')
                FROM player_stats_cache psc
                JOIN player_info pi ON psc.player_id = pi.player_id
                ORDER BY psc.season_id ASC
            """)
            for row in cursor.fetchall():
                name, current_tid, pos_id, apps, fouls_c, tack, inter, fouls_d, drib_att, aerials, mins = row
                if not name:
                    continue
                name_low = name.lower()
                if apps:
                    sportmonks_stats[name_low] = {
                        "appearances": int(apps),
                        "fouls_committed": int(fouls_c or 0),
                        "tackles": int(tack or 0),
                        "interceptions": int(inter or 0),
                        "fouls_drawn": int(fouls_d or 0),
                        "dribbles_attempts": int(drib_att or 0),
                        "aerials_won": int(aerials or 0),
                        "minutes_played": int(mins or 0),
                    }
                if current_tid:
                    current_team_by_name[name_low] = int(current_tid)
                if pos_id:
                    sm_position_ids[name_low] = str(int(pos_id))

            # 2. Load ALL players for team_id + position mapping (including those without stats — January transfers)
            cursor.execute("SELECT name, team_id, position_id FROM player_info WHERE name IS NOT NULL")
            for row in cursor.fetchall():
                name_low = row[0].lower()
                if name_low not in current_team_by_name and row[1]:
                    current_team_by_name[name_low] = int(row[1])
                if name_low not in sm_position_ids and row[2]:
                    sm_position_ids[name_low] = str(int(row[2]))
        except Exception as e:
            logger.warning("Sportmonks stats load error: %s", e)
        conn.close()

        # Build resolved name map with fuzzy matching
        resolved_names = {}
        for pid, data in players.items():
            fd_low = data["player"].lower()
            match = _fuzzy_name_match(fd_low, sportmonks_stats)
            if match:
                resolved_names[fd_low] = match

        result = {}
        for pid, data in players.items():
            tid = data.get("team_id")
            # Skip players without a team (can't assign to anyone)
            if tid is None:
                continue
            p_name_lower = data["player"].lower()
            if p_name_lower == "?":
                continue  # Skip unnamed players

            # Use real apps if available, else fallback to team total
            sm_key = resolved_names.get(p_name_lower)
            sm = sportmonks_stats.get(sm_key) if sm_key else None

            # Fix mid-season transfers: use current team_id from Sportmonks DB
            if p_name_lower in current_team_by_name:
                team_lookup = p_name_lower
            else:
                team_lookup = _fuzzy_name_match(p_name_lower, current_team_by_name)
                if not team_lookup:
                    team_lookup = sm_key
            if team_lookup and team_lookup in current_team_by_name:
                real_tid = current_team_by_name[team_lookup]
                # Skip reassignment when DB team_id is from a different ID system
                # (e.g. Sofascore IDs >= 900_000 vs Football-Data IDs < 100_000)
                if real_tid != tid and real_tid < 900_000:
                    logger.info(f"Transfer fix: {data['player']} team {tid} → {real_tid}")
                    data["team_id"] = real_tid
                    tid = real_tid

            # Invalidate stats if fuzzy match resolved to a different player (surname collision)
            if sm_key and team_lookup and sm_key != team_lookup:
                sm_team = current_team_by_name.get(sm_key)
                if sm_team and sm_team != tid:
                    logger.info(f"Stats mismatch: {data['player']} → SM '{sm_key}' (team {sm_team}) ≠ actual team {tid}, ignoring SM stats")
                    sm = None
                    sm_key = None

            total_team_matches = team_matches.get(tid, 1)
            est_matches = sm["appearances"] if sm else None

            # Fallback: count real appearances from appearances_set or FD cache
            if not est_matches:
                fd_apps = len(data.get("appearances_set", set()))
                if fd_apps == 0:
                    # Last resort: scan cache
                    for detail in cache.values():
                        if detail.get("home_id") != tid and detail.get("away_id") != tid:
                            continue
                        lineup_ids = detail.get("lineup_ids", [])
                        subs_list = detail.get("substitutions", [])
                        if data["player_id"] in lineup_ids:
                            fd_apps += 1
                        elif any(s.get("player_in_id") == data["player_id"] for s in subs_list):
                            fd_apps += 1
                est_matches = fd_apps if fd_apps > 0 else total_team_matches

            data["matches"] = est_matches
            data["yellows_per_match"] = data["yellows"] / est_matches if est_matches > 0 else 0

            # ── NEW: Average card minute (#4) ──
            card_mins = data.get("card_minutes", [])
            data["avg_card_minute"] = round(sum(card_mins) / len(card_mins), 1) if card_mins else None

            # ── NEW: Recent form ratio (#6) ──
            # recent_yellows = yellows in last 8 matchdays
            # Compare to overall rate: if higher → hot streak
            recent_y = data.get("recent_yellows", 0)
            # Estimate recent appearances (last 8 matchdays ≈ 8 games max)
            recent_apps = min(est_matches, 8)
            if recent_apps > 0 and est_matches > 0:
                recent_rate = recent_y / recent_apps
                overall_rate = data["yellows_per_match"]
                if overall_rate > 0:
                    data["recent_form_ratio"] = round(recent_rate / overall_rate, 2)
                else:
                    # Player has 0 overall yellows but got one recently
                    data["recent_form_ratio"] = 2.0 if recent_y > 0 else 1.0
            else:
                data["recent_form_ratio"] = 1.0

            # ── NEW: Red card aggression flag (#7) ──
            data["reds_season"] = data.get("reds", 0)
            data["has_red"] = data.get("reds", 0) > 0

            # Clean up temp fields
            data.pop("appearances_set", None)
            data.pop("card_minutes", None)
            data.pop("recent_yellows", None)
            data.pop("reds", None)

            # Attach advanced Sportmonks stats
            if sm and sm["appearances"] > 0:
                a = sm["appearances"]
                data["fouls_per_game"] = round(sm["fouls_committed"] / a, 2)
                data["tackles_per_game"] = round(sm["tackles"] / a, 2)
                data["interceptions_per_game"] = round(sm["interceptions"] / a, 2)
                data["fouls_drawn_per_game"] = round(sm["fouls_drawn"] / a, 2)
                data["dribbles_att_per_game"] = round(sm["dribbles_attempts"] / a, 2)
                data["aerials_per_game"] = round(sm["aerials_won"] / a, 2)
                mins = sm.get("minutes_played", 0)
                if mins > 0 and a > 0:
                    avg_mins = mins / a
                    data["avg_minutes"] = round(avg_mins, 1)
                    data["minutes_ratio"] = round(min(avg_mins / 90.0, 1.0), 3)
                else:
                    data["avg_minutes"] = None
                    data["minutes_ratio"] = None
            else:
                data["fouls_per_game"] = None
                data["tackles_per_game"] = None
                data["interceptions_per_game"] = None
                data["fouls_drawn_per_game"] = None
                data["dribbles_att_per_game"] = None
                data["aerials_per_game"] = None
                data["avg_minutes"] = None
                data["minutes_ratio"] = None

            # Attach Sportmonks position_id for role boost
            pos_key = p_name_lower if p_name_lower in sm_position_ids else (team_lookup if team_lookup and team_lookup in sm_position_ids else sm_key)
            data["sm_position_id"] = sm_position_ids.get(pos_key) if pos_key else None

            result[pid] = data

        return result

    @staticmethod
    def _build_h2h_card_stats(cache: dict, home_id: int, away_id: int) -> dict:
        """Build per-player card stats from head-to-head matches only (#3).

        Returns {player_id: {"h2h_yellows": N, "h2h_matches": N}} for players
        who appeared in previous encounters between these two teams.
        """
        h2h: dict[int, dict] = {}
        for mid, detail in cache.items():
            hid = detail.get("home_id")
            aid = detail.get("away_id")
            # Match must be between these two teams (either direction)
            if not ({hid, aid} == {home_id, away_id}):
                continue
            # Count appearances
            for pid in detail.get("lineup_ids", []):
                if pid not in h2h:
                    h2h[pid] = {"h2h_yellows": 0, "h2h_matches": 0}
                h2h[pid]["h2h_matches"] += 1
            for sub in detail.get("substitutions", []):
                pid = sub.get("player_in_id")
                if pid and pid not in h2h:
                    h2h[pid] = {"h2h_yellows": 0, "h2h_matches": 0}
                if pid:
                    h2h[pid]["h2h_matches"] += 1
            # Count yellows
            for card in detail.get("cards", []):
                if card.get("card") == "YELLOW":
                    pid = card.get("player_id")
                    if pid:
                        if pid not in h2h:
                            h2h[pid] = {"h2h_yellows": 0, "h2h_matches": 0}
                        h2h[pid]["h2h_yellows"] += 1
        return h2h

    @staticmethod
    def _compute_team_fatigue(cache: dict, team_id: int, current_matchday: int) -> dict:
        """Estimate team fatigue from cache fixture density (#9).

        Counts matches in the last 3 matchdays to estimate calendar congestion.
        Returns {"matches_recent": N, "fatigue_mult": float}.
        """
        if current_matchday <= 0:
            return {"matches_recent": 0, "fatigue_mult": 1.0}
        count = 0
        for detail in cache.values():
            md = detail.get("matchday") or 0
            if md < current_matchday - 2:  # last 3 matchdays
                continue
            if md > current_matchday:
                continue
            if detail.get("home_id") == team_id or detail.get("away_id") == team_id:
                count += 1
        # 3 matches in 3 matchdays is normal (1/md)
        # 4+ means cup fixtures squeezed in, or double fixtures
        if count >= 4:
            return {"matches_recent": count, "fatigue_mult": 1.12}
        elif count >= 3:
            return {"matches_recent": count, "fatigue_mult": 1.05}
        return {"matches_recent": count, "fatigue_mult": 1.0}

    @staticmethod
    def _build_player_referee_cross(cache: dict) -> dict:
        """Build player×referee yellow card cross-stats (#1 ALTO).

        Returns {(referee_name, player_id): {"yellows": N, "matches": N, "rate": float}}
        Only for pairs where the player appeared in a match directed by that referee.
        """
        # Count appearances per referee×player
        ref_player_apps: dict[tuple, int] = {}
        ref_player_yellows: dict[tuple, int] = {}

        for mid, detail in cache.items():
            ref = detail.get("referee")
            if not ref:
                continue
            # Track appearances from lineups
            for pid in detail.get("lineup_ids", []):
                if pid is None:
                    continue
                key = (ref, pid)
                ref_player_apps[key] = ref_player_apps.get(key, 0) + 1
            # Track appearances from subs in
            for sub in detail.get("substitutions", []):
                pid = sub.get("player_in_id")
                if pid is None:
                    continue
                key = (ref, pid)
                ref_player_apps[key] = ref_player_apps.get(key, 0) + 1
            # Count yellows
            for card in detail.get("cards", []):
                if card.get("card") == "YELLOW":
                    pid = card.get("player_id")
                    if pid is None:
                        continue
                    key = (ref, pid)
                    ref_player_yellows[key] = ref_player_yellows.get(key, 0) + 1

        result = {}
        for key, apps in ref_player_apps.items():
            if apps < 2:
                continue  # Need at least 2 meetings for signal
            yellows = ref_player_yellows.get(key, 0)
            result[key] = {
                "yellows": yellows,
                "matches": apps,
                "rate": round(yellows / apps, 3) if apps > 0 else 0,
            }
        return result

    @staticmethod
    def _build_referee_card_stats(cache: dict) -> dict:
        """Build per-referee yellow card stats."""
        refs: dict[str, dict] = {}
        for detail in cache.values():
            ref = detail.get("referee")
            if not ref:
                continue
            if ref not in refs:
                refs[ref] = {"matches": 0, "yellows": 0}
            refs[ref]["matches"] += 1
            for card in detail.get("cards", []):
                if card.get("card") == "YELLOW":
                    refs[ref]["yellows"] += 1

        for ref, data in refs.items():
            data["cards_per_match"] = data["yellows"] / data["matches"] if data["matches"] > 0 else 0

        return refs

    @staticmethod
    def _get_team_players(player_stats: dict, team_id: int) -> list[dict]:
        """Get all players for a team sorted by card rate."""
        players = [p for p in player_stats.values() if p["team_id"] == team_id]
        players.sort(key=lambda x: x["yellows_per_match"], reverse=True)
        return players
