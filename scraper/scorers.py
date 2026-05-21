"""
Anytime Goalscorer probability analyzer.

Uses cached match details from PenaltyAnalyzer to build player goal stats.
Crosses player scoring rate with opponent defense weakness to find value picks.
"""

import logging
from scraper.penalties import PenaltyAnalyzer, LEAGUE_CODES

logger = logging.getLogger(__name__)


from typing import Optional

def _fuzzy_name_match(fd_name: str, sm_names: dict) -> Optional[str]:
    """Match football-data.org name to Sportmonks name.
    Tries: exact, surname match, initial+surname match."""
    fd_low = fd_name.lower().strip()
    if fd_low in sm_names:
        return fd_low

    fd_parts = fd_low.replace(".", "").split()
    if len(fd_parts) < 2:
        # Single-word name: try substring match
        candidates = [k for k in sm_names if fd_low in k or k in fd_low]
        return candidates[0] if len(candidates) == 1 else None

    fd_surname = fd_parts[-1]
    candidates = [k for k in sm_names if k.split()[-1] == fd_surname]

    if len(candidates) == 1:
        return candidates[0]

    # Multiple candidates with same surname: check first initial
    if candidates:
        fd_initial = fd_parts[0][0] if fd_parts[0] else ""
        for c in candidates:
            c_parts = c.split()
            if c_parts and c_parts[0][0] == fd_initial:
                return c
    return None


class ScorerAnalyzer:

    def __init__(self, api_key: str):
        self.pa = PenaltyAnalyzer(api_key)

    def analyze_league(self, league_key: str) -> dict:
        code = LEAGUE_CODES.get(league_key)
        if not code:
            raise ValueError(f"Unknown league: {league_key}")

        logger.info("Analyzing scorers for %s (%s)", league_key, code)

        cache = self.pa._load_cache(code)
        if not cache:
            return {"matches": [], "error": "Nessuna cache disponibile. Lancia prima il precache."}

        # Check if cache has goals data (at least 50% of matches)
        has_goals = sum(1 for d in cache.values() if "goals" in d)
        if has_goals < len(cache) * 0.5:
            return {"matches": [], "error": f"Cache non aggiornata ({has_goals}/{len(cache)} con dati gol). Rilancia il precache."}

        # Build player goal stats
        player_stats = self._build_player_goal_stats(cache)

        # Build player positions
        player_positions = self.pa._build_player_positions(cache)

        # Get standings for team info + defense stats
        standings = self.pa._get_standings(code)
        team_names = {s["team_id"]: s["name"] for s in standings}
        team_positions = {s["team_id"]: s["position"] for s in standings}
        num_teams = len(standings) or 1

        # Team defense stats (goals conceded per match)
        team_ga_pm: dict[int, float] = {}
        for s in standings:
            if s["played"] > 0:
                team_ga_pm[s["team_id"]] = s["goals_against"] / s["played"]

        played_total = sum(s["played"] for s in standings)
        league_avg_ga = sum(s["goals_against"] for s in standings) / played_total if played_total > 0 else 1.2

        # Team match count from cache
        team_match_count: dict[int, int] = {}
        for detail in cache.values():
            for tid in (detail.get("home_id"), detail.get("away_id")):
                if tid is not None:
                    team_match_count[tid] = team_match_count.get(tid, 0) + 1

        # Get scheduled matches + today's live (for referee data)
        scheduled_data = self.pa._get(f"/competitions/{code}/matches",
                                       params={"status": "SCHEDULED,TIMED"})

        # Also fetch live/finished for referee fallback
        today_live = self.pa._get(f"/competitions/{code}/matches",
                                   params={"status": "IN_PLAY,PAUSED,FINISHED"})

        # Build referee map from live matches
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

        # Use live matches as fallback if no scheduled
        if not scheduled_data or not scheduled_data.get("matches"):
            if today_live and today_live.get("matches"):
                scheduled_data = today_live
            else:
                return {"matches": [], "error": "Nessuna partita programmata"}

        # Build referee stats from cache (same approach as penalties.py)
        ref_stats_map = {}
        for detail in cache.values():
            ref = detail.get("referee")
            if not ref:
                continue
            if ref not in ref_stats_map:
                ref_stats_map[ref] = {"matches": 0, "penalties": 0, "yellows": 0, "reds": 0}
            ref_stats_map[ref]["matches"] += 1
            ref_stats_map[ref]["penalties"] += sum(1 for g in detail.get("goals", []) if g.get("type") == "PENALTY")
            ref_stats_map[ref]["yellows"] += sum(1 for c in detail.get("cards", []) if c.get("card") == "YELLOW")
            ref_stats_map[ref]["reds"] += sum(1 for c in detail.get("cards", []) if c.get("card") != "YELLOW")

        # League average cards per match (for referee tendency)
        total_cache_matches = len(cache)
        total_cards = sum(rs["yellows"] + rs["reds"] for rs in ref_stats_map.values())
        league_avg_cpm = round(total_cards / total_cache_matches, 2) if total_cache_matches > 0 else 4.0

        # Process scheduled matches
        match_results = []
        seen_matches = set()

        for m in scheduled_data.get("matches", []):
            home = m.get("homeTeam", {})
            away = m.get("awayTeam", {})
            home_id = home.get("id")
            away_id = away.get("id")
            matchday = m.get("matchday")

            # Extract referee from scheduled match, fallback to live
            match_referee = None
            for ref in m.get("referees", []):
                if ref.get("type") == "REFEREE":
                    match_referee = ref.get("name")
                    break
            if not match_referee:
                match_referee = live_referee_map.get((home_id, away_id))

            match_key = f"{home_id}_{away_id}"
            if match_key in seen_matches:
                continue
            seen_matches.add(match_key)

            home_pos = team_positions.get(home_id, num_teams // 2)
            away_pos = team_positions.get(away_id, num_teams // 2)

            # Opponent defense weakness multiplier
            home_opp_ga = team_ga_pm.get(away_id, league_avg_ga)  # away team concedes
            away_opp_ga = team_ga_pm.get(home_id, league_avg_ga)  # home team concedes
            home_def_mult = home_opp_ga / league_avg_ga if league_avg_ga > 0 else 1.0
            away_def_mult = away_opp_ga / league_avg_ga if league_avg_ga > 0 else 1.0

            # Get scorers for both teams
            home_scorers = self._get_team_scorers(player_stats, home_id)
            away_scorers = self._get_team_scorers(player_stats, away_id)

            picks = []
            for p in home_scorers + away_scorers:
                if p["matches"] < 5:
                    continue

                raw_gpm = p["goals_per_match"]
                is_home = p["team_id"] == home_id

                # ════════════════════════════════════════════════
                #  SCORER MODEL v2 — composite signals
                # ════════════════════════════════════════════════

                # Bayesian regression to league mean for small samples.
                # K=8: with 8 appearances, 50/50 blend; with 30+, almost pure player rate.
                K_REGRESS = 8
                league_avg_gpm = league_avg_ga / 11  # avg goals per player ≈ team GA / 11
                n_matches = max(p["matches"], 1)
                base_prob = (raw_gpm * n_matches + league_avg_gpm * K_REGRESS) / (n_matches + K_REGRESS)

                # MULT 1: Home advantage (+10% home, -5% away)
                home_mult = 1.10 if is_home else 0.95

                # MULT 2: Opponent defense weakness
                def_mult = home_def_mult if is_home else away_def_mult

                # MULT 3: Position weight (strikers > midfielders > defenders)
                pid = p.get("player_id")
                position = player_positions.get(pid, "") if pid else ""
                # v2: detect set piece/penalty scorers — less position penalty
                penalty_goals = p.get("penalty_goals", 0)
                has_set_piece_goals = penalty_goals > 0 or (
                    position in ("Centre-Back", "Defence", "Left-Back", "Right-Back",
                                 "Defensive Midfield", "Central Midfield", "Midfield")
                    and p["goals"] >= 2  # scored 2+ → likely set piece specialist
                )
                pos_mult = self._position_scoring_mult(position, has_set_piece_goals)

                # MULT 4: Drought penalty / hot streak bonus
                # Smart drought: uses EFFECTIVE drought (estimated games played
                # since last goal), not raw matchdays.  A player with 13 apps
                # in 36 matchdays who hasn't scored in 17 matchdays was likely
                # injured/suspended — effective drought ≈ 17 × (13/36) ≈ 6.
                drought_md = p.get("drought", 0)
                total_team_m = team_match_count.get(p["team_id"], 1)
                attendance_rate = min(1.0, n_matches / max(total_team_m, 1))
                eff_drought = round(drought_md * attendance_rate)

                if eff_drought == 0:
                    form_mult = 1.12   # scored last matchday → hot
                elif eff_drought <= 2:
                    form_mult = 1.05   # recent scorer
                elif eff_drought <= 5:
                    form_mult = 1.0    # neutral
                elif eff_drought <= 8:
                    form_mult = 0.90   # cooling off
                else:
                    form_mult = 0.80   # long drought → significant penalty

                # MULT 5: Shots signal (more shots → more chances to score)
                shots_mult = 1.0
                spg = p.get("shots_per_game")
                if spg is not None and spg > 0:
                    # League top strikers: ~3 shots/game, average ~1.5
                    if spg >= 2.5:
                        shots_mult = 1.10
                    elif spg >= 1.5:
                        shots_mult = 1.05
                    elif spg < 0.8:
                        shots_mult = 0.90

                # MULT 6: Penalty taker boost
                # Players with 2+ penalty goals are likely designated penalty takers
                penalty_mult = 1.0
                is_penalty_taker = penalty_goals >= 2
                if is_penalty_taker:
                    penalty_mult = 1.15   # penalty takers have ~76% conversion → significant edge
                elif penalty_goals == 1:
                    penalty_mult = 1.05   # might be backup taker

                # MULT 7: Fouls drawn boost (players who win penalties for the team)
                fouls_drawn_mult = 1.0
                fdpg = p.get("fouls_drawn_per_game")
                if fdpg is not None and fdpg >= 2.0:
                    fouls_drawn_mult = 1.06   # draws lots of fouls → more penalty chances
                elif fdpg is not None and fdpg >= 1.5:
                    fouls_drawn_mult = 1.03

                # MULT 8: Referee tendency (permissive ref → more flowing play → more goals)
                ref_mult = 1.0
                if match_referee and match_referee in ref_stats_map:
                    rs = ref_stats_map[match_referee]
                    if rs["matches"] >= 3:
                        ref_cpm = (rs["yellows"] + rs["reds"]) / rs["matches"]
                        # Bayesian smoothing with K=5
                        eff_cpm = (ref_cpm * rs["matches"] + league_avg_cpm * 5) / (rs["matches"] + 5)
                        ratio = eff_cpm / league_avg_cpm if league_avg_cpm > 0 else 1.0
                        # Permissive ref (fewer cards) → more flowing play → slight goal boost
                        # Strict ref (more cards) → more stoppages → slight goal reduction
                        if ratio < 0.85:
                            ref_mult = 1.05    # permissive → more goals
                        elif ratio > 1.15:
                            ref_mult = 0.95    # strict → fewer goals
                        # else stays 1.0 (average ref)

                # MULT 9: Minutes normalization
                # A player averaging 60 min/game has ~67% of a 90-min player's chance
                minutes_mult = 1.0
                mr = p.get("minutes_ratio")
                if mr is not None and mr > 0:
                    # Smooth: don't punish too harshly (min 0.70 multiplier)
                    minutes_mult = max(0.70, mr)

                # Cap: even Haaland/Mbappé don't score more than ~60% of matches
                adjusted_prob = min(0.60, base_prob * home_mult * def_mult * pos_mult * form_mult * shots_mult * penalty_mult * fouls_drawn_mult * ref_mult * minutes_mult)

                # Soglia differenziata: difensori/terzini possono entrare con prob più bassa
                # (segnano raramente ma da piazzato — vedi caso Diks 3% che segna)
                is_defender = position in ("Centre-Back", "Defence", "Left-Back", "Right-Back")
                threshold = 0.03 if is_defender else 0.05
                if adjusted_prob < threshold:
                    continue

                score = round(adjusted_prob * 100)
                team_name = team_names.get(p["team_id"], "?")

                # Position-based label for sorting context
                pos_order = self._position_order(position)

                # Transfer flag: goals scored for different team than current
                is_transfer = p.get("goals_team_id") != p.get("team_id")

                picks.append({
                    "player": p["player"],
                    "team": team_name,
                    "team_id": p["team_id"],
                    "is_home": is_home,
                    "is_transfer": is_transfer,
                    "is_penalty_taker": is_penalty_taker,
                    "penalty_goals": penalty_goals,
                    "matches": p["matches"],
                    "goals": p["goals"],
                    "goals_per_match": round(p["goals_per_match"], 2),
                    "probability": score,
                    "position": position,
                    "pos_order": pos_order,
                    "last_goal_md": p.get("last_goal_md"),
                    "drought": drought_md,
                    "eff_drought": eff_drought,
                    # Advanced Sportmonks stats
                    "shots_per_game": p.get("shots_per_game"),
                    "shots_on_target_pct": p.get("shots_on_target_pct"),
                    "big_chances_created": p.get("big_chances_created"),
                    "key_passes_per_game": p.get("key_passes_per_game"),
                    "fouls_drawn_per_game": p.get("fouls_drawn_per_game"),
                    "dribbles_per_game": p.get("dribbles_per_game"),
                    "assists_total": p.get("assists_total"),
                    "avg_minutes": p.get("avg_minutes"),
                    "minutes_ratio": p.get("minutes_ratio"),
                })

            picks.sort(key=lambda x: x["probability"], reverse=True)

            # Remove pos_order from output
            for p in picks:
                del p["pos_order"]

            # Build referee info for this match
            ref_info = {"referee": match_referee, "has_referee": False}
            if match_referee and match_referee in ref_stats_map:
                rs = ref_stats_map[match_referee]
                if rs["matches"] >= 3:
                    ref_info["has_referee"] = True
                    ref_info["referee_matches"] = rs["matches"]
                    ref_info["referee_cards_pm"] = round((rs["yellows"] + rs["reds"]) / rs["matches"], 1)
                    ref_info["referee_penalties_pm"] = round(rs["penalties"] / rs["matches"], 2)
                    eff_cpm = (ref_info["referee_cards_pm"] * rs["matches"] + league_avg_cpm * 5) / (rs["matches"] + 5)
                    ratio = eff_cpm / league_avg_cpm if league_avg_cpm > 0 else 1.0
                    ref_info["referee_multiplier"] = round(ratio, 2)
                    ref_info["referee_tendency"] = "SEVERO" if ratio > 1.15 else ("PERMISSIVO" if ratio < 0.85 else "NELLA MEDIA")

            match_results.append({
                "home_id": home_id,
                "away_id": away_id,
                "home_team": home.get("name", "?"),
                "away_team": away.get("name", "?"),
                "utcDate": m.get("utcDate"),
                "home_position": home_pos,
                "away_position": away_pos,
                "matchday": matchday,
                "home_def_weakness": round(home_def_mult, 2),
                "away_def_weakness": round(away_def_mult, 2),
                "top_picks": picks[:10],
                **ref_info,
            })

        match_results.sort(key=lambda x: x.get("utcDate") or "")

        return {"matches": match_results}

    @staticmethod
    def _build_player_goal_stats(cache: dict) -> dict:
        """Build per-player goal stats from cache.
        Excludes OWN_GOAL type."""
        players: dict[int, dict] = {}

        for mid, detail in cache.items():
            matchday = detail.get("matchday", 0)
            home_id = detail.get("home_id")
            away_id = detail.get("away_id")

            for goal in detail.get("goals", []):
                if goal.get("type") == "OWN_GOAL":
                    continue

                sid = goal.get("scorer_id")
                if sid is None:
                    continue

                if sid not in players:
                    players[sid] = {
                        "player": goal.get("scorer", "?"),
                        "player_id": sid,
                        "team_id": goal.get("team_id"),
                        "goals": 0,
                        "penalty_goals": 0,
                        "matches_with_goals": set(),
                        "last_goal_md": 0,
                    }

                players[sid]["goals"] += 1
                # Track penalty goals for penalty taker identification
                if goal.get("type") == "PENALTY":
                    players[sid]["penalty_goals"] = players[sid].get("penalty_goals", 0) + 1
                players[sid]["matches_with_goals"].add(mid)
                if matchday > players[sid]["last_goal_md"]:
                    players[sid]["last_goal_md"] = matchday

        # Count total matches per team
        team_matches: dict[int, int] = {}
        for detail in cache.values():
            for tid in (detail.get("home_id"), detail.get("away_id")):
                if tid is not None:
                    team_matches[tid] = team_matches.get(tid, 0) + 1

        # Current matchday
        matchdays = [d.get("matchday", 0) for d in cache.values() if d.get("matchday")]
        current_md = max(matchdays) if matchdays else 0

        # Connect to DB to get real appearances + advanced stats (using NAME as bridge)
        # Also load current team_id from player_info to handle mid-season transfers
        import sqlite3
        conn = sqlite3.connect('data/betanalyzer.db', timeout=30)
        cursor = conn.cursor()
        sportmonks_stats = {}
        current_team_by_name = {}  # name_lower → current team_id (from Sportmonks)
        try:
            cursor.execute("""
                SELECT pi.name,
                       pi.team_id,
                       JSON_EXTRACT(psc.stats_json, '$.appearances'),
                       JSON_EXTRACT(psc.stats_json, '$.shots_total'),
                       JSON_EXTRACT(psc.stats_json, '$.shots_on_target'),
                       JSON_EXTRACT(psc.stats_json, '$.big_chances_created'),
                       JSON_EXTRACT(psc.stats_json, '$.key_passes'),
                       JSON_EXTRACT(psc.stats_json, '$.fouls_drawn'),
                       JSON_EXTRACT(psc.stats_json, '$.dribbles_success'),
                       JSON_EXTRACT(psc.stats_json, '$.assists'),
                       JSON_EXTRACT(psc.stats_json, '$.minutes_played')
                FROM player_stats_cache psc
                JOIN player_info pi ON psc.player_id = pi.player_id
            """)
            for row in cursor.fetchall():
                name, current_tid, apps, shots, sot, bcc, kp, fd, drib, ast, mins = row
                if not name:
                    continue
                name_low = name.lower()
                if apps:
                    sportmonks_stats[name_low] = {
                        "appearances": int(apps),
                        "shots_total": int(shots or 0),
                        "shots_on_target": int(sot or 0),
                        "big_chances_created": int(bcc or 0),
                        "key_passes": int(kp or 0),
                        "fouls_drawn": int(fd or 0),
                        "dribbles_success": int(drib or 0),
                        "assists": int(ast or 0),
                        "minutes_played": int(mins or 0),
                    }
                if current_tid:
                    current_team_by_name[name_low] = int(current_tid)

            # Load ALL players for team_id mapping (including those without stats — January transfers)
            cursor.execute("SELECT name, team_id FROM player_info WHERE name IS NOT NULL")
            for row in cursor.fetchall():
                name_low = row[0].lower()
                if name_low not in current_team_by_name and row[1]:
                    current_team_by_name[name_low] = int(row[1])
        except Exception as e:
            logger.warning("Sportmonks stats load error: %s", e)
        conn.close()

        # Build resolved name map (fd_name -> sm_name) with fuzzy matching
        resolved_names = {}
        for sid, data in players.items():
            fd_low = data["player"].lower()
            match = _fuzzy_name_match(fd_low, sportmonks_stats)
            if match:
                resolved_names[fd_low] = match

        result = {}
        for sid, data in players.items():
            tid = data["team_id"]
            p_name_lower = data["player"].lower()

            # Use real apps from DB (bridged by name) if available
            sm_key = resolved_names.get(p_name_lower)
            sm = sportmonks_stats.get(sm_key) if sm_key else None

            # Transfer handling: keep goals_team_id (where goals were scored)
            # but also store current_team_id (where player is NOW) for squad filtering.
            # Goals stay with the original team; the player shows up for their current team.
            data["goals_team_id"] = tid   # team where goals were scored
            if p_name_lower in current_team_by_name:
                real_tid = current_team_by_name[p_name_lower]
                if real_tid != tid:
                    logger.info(f"Transfer detected: {data['player']} scored for {tid}, now at {real_tid}")
                    data["team_id"] = real_tid   # current team for squad listing
                    tid = real_tid
            elif sm_key and sm_key in current_team_by_name:
                real_tid = current_team_by_name[sm_key]
                if real_tid != tid:
                    logger.info(f"Transfer detected: {data['player']} scored for {tid}, now at {real_tid}")
                    data["team_id"] = real_tid
                    tid = real_tid

            # Invalidate stats if fuzzy match resolved to a different player (surname collision)
            if sm_key and sm_key != p_name_lower:
                sm_team = current_team_by_name.get(sm_key)
                if sm_team and sm_team != tid:
                    logger.info(f"Stats mismatch: {data['player']} → SM '{sm_key}' (team {sm_team}) ≠ actual team {tid}, ignoring SM stats")
                    sm = None
                    sm_key = None

            total_team_matches = team_matches.get(data["goals_team_id"], 1)
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
            data["goals_per_match"] = data["goals"] / est_matches if est_matches > 0 else 0
            data["drought"] = current_md - data["last_goal_md"] if data["last_goal_md"] else current_md
            del data["matches_with_goals"]

            # Attach advanced Sportmonks stats
            if sm and sm["appearances"] > 0:
                a = sm["appearances"]
                data["shots_per_game"] = round(sm["shots_total"] / a, 2)
                data["shots_on_target_pct"] = round(sm["shots_on_target"] / sm["shots_total"] * 100) if sm["shots_total"] > 0 else 0
                data["big_chances_created"] = sm["big_chances_created"]
                data["key_passes_per_game"] = round(sm["key_passes"] / a, 2)
                data["fouls_drawn_per_game"] = round(sm["fouls_drawn"] / a, 2)
                data["dribbles_per_game"] = round(sm["dribbles_success"] / a, 2)
                data["assists_total"] = sm["assists"]
                # Minutes normalization: avg minutes per appearance vs 90
                mins = sm.get("minutes_played", 0)
                if mins > 0 and a > 0:
                    avg_mins = mins / a
                    data["avg_minutes"] = round(avg_mins, 1)
                    # Ratio: 1.0 = plays full 90, 0.67 = avg 60 min
                    data["minutes_ratio"] = round(min(avg_mins / 90.0, 1.0), 3)
                else:
                    data["avg_minutes"] = None
                    data["minutes_ratio"] = None
            else:
                data["shots_per_game"] = None
                data["shots_on_target_pct"] = None
                data["big_chances_created"] = None
                data["key_passes_per_game"] = None
                data["fouls_drawn_per_game"] = None
                data["dribbles_per_game"] = None
                data["assists_total"] = None
                data["avg_minutes"] = None
                data["minutes_ratio"] = None

            result[sid] = data

        return result

    @staticmethod
    def _get_team_scorers(player_stats: dict, team_id: int) -> list[dict]:
        """Get all scorers for a team sorted by goals per match."""
        scorers = [p for p in player_stats.values() if p["team_id"] == team_id]
        scorers.sort(key=lambda x: x["goals_per_match"], reverse=True)
        return scorers

    @staticmethod
    def _position_scoring_mult(position: str, has_set_piece_goals: bool = False) -> float:
        """Multiplier based on position — attackers have better conversion context.
        v2: less punishing for defenders/midfielders with set piece history."""
        pos = position.strip()
        if pos in ("Centre-Forward", "Second Striker", "Offence"):
            return 1.05     # slight boost for natural scorers
        elif pos in ("Left Winger", "Right Winger"):
            return 1.00
        elif pos in ("Attacking Midfield", "Trequartista"):
            return 0.98
        elif pos in ("Central Midfield", "Midfield", "Left Midfield", "Right Midfield"):
            # v2: midfielders with goals from set pieces get less penalty
            return 0.96 if has_set_piece_goals else 0.92
        elif pos in ("Defensive Midfield",):
            return 0.92 if has_set_piece_goals else 0.85
        elif pos in ("Left-Back", "Right-Back", "Centre-Back", "Defence"):
            # v2: defenders who score (set pieces, penalties) get 0.90 instead of 0.80
            return 0.90 if has_set_piece_goals else 0.82
        elif pos in ("Goalkeeper",):
            return 0.50
        return 0.95   # unknown → slight penalty

    @staticmethod
    def _position_order(position: str) -> int:
        """Return sort order by position (attackers first)."""
        pos_map = {
            "Centre-Forward": 1, "Second Striker": 2,
            "Left Winger": 3, "Right Winger": 3,
            "Offence": 2,
            "Attacking Midfield": 4,
            "Central Midfield": 5, "Left Midfield": 5, "Right Midfield": 5,
            "Midfield": 5,
            "Defensive Midfield": 6,
            "Left-Back": 7, "Right-Back": 7,
            "Centre-Back": 8, "Defence": 8,
            "Goalkeeper": 9,
        }
        return pos_map.get(position, 5)
