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

            picks = []
            for p in home_players + away_players:
                if p["matches"] < 5:
                    continue

                pid_team = p["team_id"]
                is_home_team = pid_team == home_id

                # ════════════════════════════════════════════════
                #  COMPOSITE CARD MODEL v3
                # ════════════════════════════════════════════════
                #
                # Architecture: historical base × behavioural multipliers
                # (NOT additive — signals amplify/dampen the base, never dominate it)
                #
                # BASE: Historical yellows/match (anchors the probability)
                hist_ypm = p.get("yellows_per_match", 0)

                # MULTIPLIER 1: Fouls committed (aggressive players foul more → more yellows)
                fpg = p.get("fouls_per_game") or 0
                fouls_ratio = fpg / max(league_avg_fpg, 0.3)
                # Damp: ratio 2.0 → +30%, ratio 0.5 → -15%
                fouls_mult = 1.0 + (fouls_ratio - 1.0) * 0.30

                # MULTIPLIER 2: Tackles per game (physical engagement)
                tpg = p.get("tackles_per_game") or 0
                tackles_ratio = tpg / max(league_avg_tpg, 0.5)
                tackles_mult = 1.0 + (tackles_ratio - 1.0) * 0.15

                # MULTIPLIER 3: Role adjustment
                # Try FD position map first, then Sportmonks position_id
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

                # Combine: base × all multipliers (tension added below)
                adjusted_prob = hist_ypm * fouls_mult * tackles_mult * role_mult * opp_mult * ref_multiplier * underdog_mult

                # MULTIPLIER 6: Match tension (rivalry, relegation, title race, DERBY)
                # Matches with high positional stakes have more cards
                tension_mult = 1.0
                if is_derby:
                    tension_mult = 1.20  # derby → +20% cards (highest tension)
                elif pos_diff <= 3 and home_pos <= 6:
                    tension_mult = 1.10  # top-table clash
                elif home_pos >= num_teams - 3 or away_pos >= num_teams - 3:
                    tension_mult = 1.12  # relegation battle
                elif pos_diff <= 2:
                    tension_mult = 1.08  # close rivals

                # Combine: base × all multipliers
                adjusted_prob = adjusted_prob * tension_mult

                # MULTIPLIER 7: Minutes normalization
                # A player averaging 60 min/game has ~67% of a full-timer's card risk
                mr = p.get("minutes_ratio")
                if mr is not None and mr > 0:
                    minutes_mult = max(0.70, mr)
                    adjusted_prob = adjusted_prob * minutes_mult

                # FLOOR: players with high fouls/game get a minimum probability
                # even if their historical yellow rate is low
                if fpg >= 1.2 and adjusted_prob < 0.15:
                    adjusted_prob = max(adjusted_prob, 0.12 + (fpg - 1.2) * 0.05)

                # Cap: realistic maximum ~55% (very few players above 50% in reality)
                adjusted_prob = min(0.55, adjusted_prob)

                if adjusted_prob < 0.05:
                    continue

                score = round(adjusted_prob * 100)
                team_name = team_names.get(p["team_id"], "?")

                # Check diffida status
                pid_id = p.get("player_id")
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
        """Build per-player yellow card stats from cache."""
        players: dict[int, dict] = {}

        for mid, detail in cache.items():
            home_id = detail.get("home_id")
            away_id = detail.get("away_id")

            # Track appearances
            for tid in (home_id, away_id):
                if tid is None:
                    continue

            for card in detail.get("cards", []):
                if card.get("card") != "YELLOW":
                    continue
                pid = card.get("player_id")
                if pid is None:
                    continue

                if pid not in players:
                    players[pid] = {
                        "player": card.get("player", "?"),
                        "player_id": pid,
                        "team_id": card.get("team_id"),
                        "yellows": 0,
                        "matches_with_team": set(),
                    }
                players[pid]["yellows"] += 1
                players[pid]["matches_with_team"].add(mid)

        # Count total matches per team to estimate player appearances
        team_matches: dict[int, int] = {}
        for detail in cache.values():
            for tid in (detail.get("home_id"), detail.get("away_id")):
                if tid is not None:
                    team_matches[tid] = team_matches.get(tid, 0) + 1

        # Connect to DB to get real appearances + advanced stats
        # Also load current team_id to handle mid-season transfers
        import sqlite3
        conn = sqlite3.connect('data/betanalyzer.db')
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
            tid = data["team_id"]
            p_name_lower = data["player"].lower()

            # Use real apps if available, else fallback to team total
            sm_key = resolved_names.get(p_name_lower)
            sm = sportmonks_stats.get(sm_key) if sm_key else None

            # Fix mid-season transfers: use current team_id from Sportmonks DB
            # Prefer direct name match over fuzzy-matched SM name (avoids wrong surname collisions)
            if p_name_lower in current_team_by_name:
                team_lookup = p_name_lower
            else:
                team_lookup = _fuzzy_name_match(p_name_lower, current_team_by_name)
                if not team_lookup:
                    team_lookup = sm_key  # last resort: use stats-matched name
            if team_lookup and team_lookup in current_team_by_name:
                real_tid = current_team_by_name[team_lookup]
                if real_tid != tid:
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

            # Fallback: count real appearances from FD cache for players without SM stats
            if not est_matches:
                fd_apps = 0
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
            del data["matches_with_team"]

            # Attach advanced Sportmonks stats
            if sm and sm["appearances"] > 0:
                a = sm["appearances"]
                data["fouls_per_game"] = round(sm["fouls_committed"] / a, 2)
                data["tackles_per_game"] = round(sm["tackles"] / a, 2)
                data["interceptions_per_game"] = round(sm["interceptions"] / a, 2)
                data["fouls_drawn_per_game"] = round(sm["fouls_drawn"] / a, 2)
                data["dribbles_att_per_game"] = round(sm["dribbles_attempts"] / a, 2)
                data["aerials_per_game"] = round(sm["aerials_won"] / a, 2)
                # Minutes normalization
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
            # Prefer direct name, then fuzzy-matched team_lookup, then sm_key
            pos_key = p_name_lower if p_name_lower in sm_position_ids else (team_lookup if team_lookup and team_lookup in sm_position_ids else sm_key)
            data["sm_position_id"] = sm_position_ids.get(pos_key) if pos_key else None

            result[pid] = data

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
