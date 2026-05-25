import os
import json
import logging
import requests
from datetime import datetime, timedelta
from scraper.penalties import PenaltyAnalyzer, LEAGUE_CODES

logger = logging.getLogger(__name__)

class RosterManager:
    def __init__(self, football_data_key: str):
        self.pa = PenaltyAnalyzer(football_data_key)

    def get_team_roster(self, team_id: int):
        return self.pa.get_team_roster(team_id)

    def get_all_competitions_with_stats(self, use_cache=True):
        """Returns a dict of league_name -> {stats, teams} with record highlights.
           Uses a file-based cache to speed up loading.
        """
        cache_path = os.path.join("data", "competitions_stats_cache.json")
        if use_cache and os.path.exists(cache_path):
            try:
                mtime = datetime.fromtimestamp(os.path.getmtime(cache_path))
                # Valid for 12 hours if not forced
                if datetime.now() - mtime < timedelta(hours=12):
                    with open(cache_path, "r") as f:
                        return json.load(f)
            except Exception as e:
                logger.warning(f"Errore lettura cache competizioni: {e}")

        competitions = {}
        max_values = {"goals": 0, "avg_goals": 0, "assists": 0, "penalties": 0, "penalty_pct": 0, "yellows": 0, "reds": 0, "over25_pct": 0, "btts_pct": 0}
        
        for league_key, league_code in LEAGUE_CODES.items():
            standings = self.pa._get_standings(league_code)
            if not standings: continue
            league_display_name = league_key.replace("_", " ").title()
            cache = self.pa._load_cache(league_code)
            team_names = {s["team_id"]: s["name"] for s in standings}
            team_comebacks = {} # team_id -> {name, made, suffered, matches}
            for s in standings:
                team_comebacks[s["team_id"]] = {"name": s["name"], "made": 0, "suffered": 0, "matches": 0}
            
            stats = {
                "goals": 0, "assists": 0, "yellows": 0, "reds": 0, "penalties": 0, 
                "matches": 0, "avg_goals": 0, "penalty_pct": 0,
                "over15": 0, "over25": 0, "over35": 0, "btts": 0,
                "over15_pct": 0, "over25_pct": 0, "over35_pct": 0, "btts_pct": 0, 
                "nogol_pct": 0, "under25_pct": 0, "under35_pct": 0, "avg_cards": 0
            }
            
            if cache:
                stats["matches"] = len(cache)
                penalty_matches = 0
                for mid, detail in cache.items():
                    score = detail.get("score", {}).get("fullTime", {})
                    h_s, a_s = score.get("home"), score.get("away")
                    if h_s is not None and a_s is not None:
                        tot_g = h_s + a_s
                    else:
                        tot_g = len(detail.get("goals", []))
                    stats["goals"] += tot_g
                    
                    if tot_g > 1.5: stats["over15"] += 1
                    if tot_g > 2.5: stats["over25"] += 1
                    if tot_g > 3.5: stats["over35"] += 1
                    if h_s is not None and a_s is not None:
                        if h_s > 0 and a_s > 0: stats["btts"] += 1
                    else:
                        h_g_list = sum(1 for g in detail.get("goals", []) if g.get("team_id") == detail.get("home_id"))
                        a_g_list = sum(1 for g in detail.get("goals", []) if g.get("team_id") == detail.get("away_id"))
                        if h_g_list > 0 and a_g_list > 0: stats["btts"] += 1
                    
                    # Track Ribaltoni for all teams in league
                    goals = sorted(detail.get("goals", []), key=lambda x: x.get("minute", 0) or 0)
                    h_id, a_id = detail.get("home_id"), detail.get("away_id")
                    
                    if h_id not in team_comebacks or a_id not in team_comebacks: continue
                    
                    team_comebacks[h_id]["matches"] += 1
                    team_comebacks[a_id]["matches"] += 1
                    
                    curr_h, curr_a = 0, 0
                    h_was_down, a_was_down = False, False
                    h_was_ahead, a_was_ahead = False, False
                    for g in goals:
                        if g.get("team_id") == h_id: curr_h += 1
                        else: curr_a += 1
                        if curr_a > curr_h: h_was_down = True; a_was_ahead = True
                        if curr_h > curr_a: a_was_down = True; h_was_ahead = True
                    
                    if h_s is not None and a_s is not None:
                        if h_was_down and h_s > a_s: team_comebacks[h_id]["made"] += 1
                        if a_was_down and a_s > h_s: team_comebacks[a_id]["made"] += 1
                        if h_was_ahead and a_s > h_s: team_comebacks[h_id]["suffered"] += 1
                        if a_was_ahead and h_s > a_s: team_comebacks[a_id]["suffered"] += 1

                    y_in_match = sum(1 for c in detail.get("cards", []) if c.get("card") == "YELLOW")
                    r_in_match = sum(1 for c in detail.get("cards", []) if c.get("card") != "YELLOW")
                    stats["yellows"] += y_in_match
                    stats["reds"] += r_in_match
                    
                    for g in detail.get("goals", []):
                        if g.get("assist_id"): stats["assists"] += 1
                        if g.get("type") == "PENALTY": stats["penalties"] += 1
                    if any(g.get("type") == "PENALTY" for g in detail.get("goals", [])): penalty_matches += 1
                
                if stats["matches"] > 0:
                    stats["avg_goals"] = round(stats["goals"] / stats["matches"], 2)
                    stats["penalty_pct"] = round((penalty_matches / stats["matches"]) * 100, 1)
                    stats["over15_pct"] = round((stats["over15"] / stats["matches"]) * 100, 1)
                    stats["over25_pct"] = round((stats["over25"] / stats["matches"]) * 100, 1)
                    stats["over35_pct"] = round((stats["over35"] / stats["matches"]) * 100, 1)
                    stats["under25_pct"] = round(((stats["matches"] - stats["over25"]) / stats["matches"]) * 100, 1)
                    stats["under35_pct"] = round(((stats["matches"] - stats["over35"]) / stats["matches"]) * 100, 1)
                    stats["btts_pct"] = round((stats["btts"] / stats["matches"]) * 100, 1)
                    stats["nogol_pct"] = round(((stats["matches"] - stats["btts"]) / stats["matches"]) * 100, 1)
                    stats["avg_cards"] = round((stats["yellows"] + stats["reds"]) / stats["matches"], 2)
                
                # Find top comeback teams (tiebreak by absolute count)
                top_made = sorted(team_comebacks.values(), key=lambda x: (x["made"] / x["matches"] if x["matches"] > 0 else 0, x["made"]), reverse=True)
                top_suffered = sorted(team_comebacks.values(), key=lambda x: (x["suffered"] / x["matches"] if x["matches"] > 0 else 0, x["suffered"]), reverse=True)

                if top_made and top_made[0]["made"] > 0:
                    pct = round((top_made[0]['made'] / top_made[0]['matches']) * 100, 1)
                    stats["top_comeback"] = f"{top_made[0]['name']} ({top_made[0]['made']}x · {pct}%)"
                else:
                    stats["top_comeback"] = "-"

                if top_suffered and top_suffered[0]["suffered"] > 0:
                    pct = round((top_suffered[0]['suffered'] / top_suffered[0]['matches']) * 100, 1)
                    stats["top_suffered"] = f"{top_suffered[0]['name']} ({top_suffered[0]['suffered']}x · {pct}%)"
                else:
                    stats["top_suffered"] = "-"

            for key in max_values:
                if key in stats and stats[key] > max_values[key]: max_values[key] = stats[key]

            if stats["matches"] > 0:
                competitions[league_display_name] = {
                    "league_code": league_code, 
                    "stats": stats, 
                    "teams": [{"id": s["team_id"], "name": s["name"], "position": s.get("position", i+1)} for i, s in enumerate(standings)],
                    "score_frequency": self.pa.get_score_frequency(league_code)
                }

        for league_data in competitions.values():
            league_data["records"] = {}
            for key in max_values:
                if max_values[key] > 0 and league_data["stats"].get(key, 0) == max_values[key]: league_data["records"][key] = True
        
        # Save to cache
        try:
            os.makedirs("data", exist_ok=True)
            with open(cache_path, "w") as f:
                json.dump(competitions, f)
        except Exception as e:
            logger.error(f"Errore salvataggio cache competizioni: {e}")

        return competitions

    def _empty_betting_bucket(self):
        return {
            "matches": 0, "over15": 0, "over25": 0, "over35": 0, "btts": 0,
            "goals_scored": 0, "goals_conceded": 0,
            "comebacks_made": 0, "comebacks_suffered": 0,
            "penalties_won": 0, "penalties_conceded": 0,
            "clean_sheets": 0, "wins": 0, "draws": 0, "losses": 0,
        }

    def _process_match_into_bucket(self, bucket, detail, team_id):
        score = detail.get("score", {}).get("fullTime", {})
        h_s, a_s = score.get("home", 0) or 0, score.get("away", 0) or 0
        if h_s == 0 and a_s == 0 and score.get("home") is None:
            return  # match non giocato
        tot_g = h_s + a_s
        is_home = (detail.get("home_id") == team_id)

        bucket["matches"] += 1
        if tot_g > 1.5: bucket["over15"] += 1
        if tot_g > 2.5: bucket["over25"] += 1
        if tot_g > 3.5: bucket["over35"] += 1
        if h_s > 0 and a_s > 0: bucket["btts"] += 1

        # Clean sheet
        if is_home and a_s == 0: bucket["clean_sheets"] += 1
        elif not is_home and h_s == 0: bucket["clean_sheets"] += 1

        # W/D/L
        win = (is_home and h_s > a_s) or (not is_home and a_s > h_s)
        loss = (is_home and a_s > h_s) or (not is_home and h_s > a_s)
        if win: bucket["wins"] += 1
        elif loss: bucket["losses"] += 1
        else: bucket["draws"] += 1

        # Comeback
        goals = sorted(detail.get("goals", []), key=lambda x: x.get("minute", 0) or 0)
        was_down, was_ahead = False, False
        curr_h, curr_a = 0, 0
        for g in goals:
            if g.get("team_id") == detail.get("home_id"): curr_h += 1
            else: curr_a += 1
            if is_home:
                if curr_a > curr_h: was_down = True
                if curr_h > curr_a: was_ahead = True
            else:
                if curr_h > curr_a: was_down = True
                if curr_a > curr_h: was_ahead = True
        if was_down and win: bucket["comebacks_made"] += 1
        if was_ahead and loss: bucket["comebacks_suffered"] += 1

        # Penalties
        for p in detail.get("penalties", []):
            if p.get("team_id") == team_id: bucket["penalties_won"] += 1
            else: bucket["penalties_conceded"] += 1

        if is_home:
            bucket["goals_scored"] += h_s
            bucket["goals_conceded"] += a_s
        else:
            bucket["goals_scored"] += a_s
            bucket["goals_conceded"] += h_s

    def _bucket_to_result(self, bucket):
        m = bucket["matches"]
        if m == 0: return None
        return {
            "matches": m,
            "over15_pct": (bucket["over15"] / m) * 100,
            "over25_pct": (bucket["over25"] / m) * 100,
            "over35_pct": (bucket["over35"] / m) * 100,
            "under25_pct": ((m - bucket["over25"]) / m) * 100,
            "under35_pct": ((m - bucket["over35"]) / m) * 100,
            "btts_pct": (bucket["btts"] / m) * 100,
            "nogol_pct": ((m - bucket["btts"]) / m) * 100,
            "avg_scored": bucket["goals_scored"] / m,
            "avg_conceded": bucket["goals_conceded"] / m,
            "ribaltone_si_pct": (bucket["comebacks_made"] / m) * 100,
            "ribaltone_sub_pct": (bucket["comebacks_suffered"] / m) * 100,
            "pens_won": bucket["penalties_won"],
            "pens_conceded": bucket["penalties_conceded"],
            "clean_sheet_pct": (bucket["clean_sheets"] / m) * 100,
            "win_pct": (bucket["wins"] / m) * 100,
            "draw_pct": (bucket["draws"] / m) * 100,
            "loss_pct": (bucket["losses"] / m) * 100,
        }

    def get_team_betting_stats(self, team_id: int, league_code: str):
        cache = self.pa._load_cache(league_code)
        if not cache: return None

        total = self._empty_betting_bucket()
        home_only = self._empty_betting_bucket()
        away_only = self._empty_betting_bucket()

        for mid, detail in cache.items():
            if detail.get("home_id") != team_id and detail.get("away_id") != team_id:
                continue
            is_home = (detail.get("home_id") == team_id)
            self._process_match_into_bucket(total, detail, team_id)
            if is_home:
                self._process_match_into_bucket(home_only, detail, team_id)
            else:
                self._process_match_into_bucket(away_only, detail, team_id)

        result = self._bucket_to_result(total)
        if result:
            result["home"] = self._bucket_to_result(home_only)
            result["away"] = self._bucket_to_result(away_only)
        return result

    def predict_correct_score(self, home_stats, away_stats):
        """Predicts top 3 correct scores based on averages."""
        if not home_stats or not away_stats: return []
        
        # Expected goals (simplified)
        exp_home = (home_stats["avg_scored"] + away_stats["avg_conceded"]) / 2
        exp_away = (away_stats["avg_scored"] + home_stats["avg_conceded"]) / 2
        
        # Generate possible scores
        scores = []
        for h in range(5):
            for a in range(5):
                # Simple Poisson-like probability (very simplified for UI)
                prob = (1 / (1 + abs(h - exp_home))) * (1 / (1 + abs(a - exp_away)))
                scores.append({"score": f"{h}-{a}", "prob": prob})
        
        scores.sort(key=lambda x: x["prob"], reverse=True)
        return scores[:3]

    def get_player_impact(self, team_id: int, player_id: int, league_code: str):
        """Calculates the win rate with vs without a specific player."""
        cache = self.pa._load_cache(league_code)
        if not cache: return None
        
        p_matches, p_wins = 0, 0
        a_matches, a_wins = 0, 0
        
        for mid, detail in cache.items():
            if detail.get("home_id") != team_id and detail.get("away_id") != team_id: continue
            
            # Check if player played FOR this specific team in this match
            players_data = detail.get("players", {})
            player_match_info = players_data.get(str(player_id))
            
            played = False
            if player_match_info and player_match_info.get("team_id") == team_id:
                # Count if they were in the STARTING lineup OR entered as a sub
                in_lineup = any(pid == player_id for pid in detail.get("lineup_ids", []))
                in_subs = any(s.get("player_in_id") == player_id for s in detail.get("substitutions", []))
                played = in_lineup or in_subs
                
                # Bonus: check if they got a card or scored even if not in lineup/subs (fallback for incomplete API data)
                if not played:
                    has_card = any(c.get("player_id") == player_id for c in detail.get("cards", []))
                    has_goal = any(g.get("player", {}).get("id") == player_id for g in detail.get("goals", []))
                    played = has_card or has_goal
            
            is_home = (detail.get("home_id") == team_id)
            score = detail.get("score", {}).get("fullTime", {})
            h_s, a_s = score.get("home", 0) or 0, score.get("away", 0) or 0
            win = (is_home and h_s > a_s) or (not is_home and a_s > h_s)
            
            if played:
                p_matches += 1
                if win: p_wins += 1
            else:
                a_matches += 1
                if win: a_wins += 1
        
        wr_with = (p_wins / p_matches) * 100 if p_matches > 0 else 0
        wr_without = (a_wins / a_matches) * 100 if a_matches > 0 else 0
        
        return {
            "with_player": round(wr_with, 1),
            "without_player": round(wr_without, 1),
            "drop": round(wr_with - wr_without, 1),
            "matches_played": p_matches,
            "matches_absent": a_matches
        }

    def get_upcoming_predictions(self):
        predictions = []
        for league_key, league_code in LEAGUE_CODES.items():
            resp = self.pa._get(f"/competitions/{league_code}/matches", {"status": "SCHEDULED"})
            if not resp: continue
            matches = resp.get("matches", [])[:15]
            for m in matches:
                h_team, a_team = m.get("homeTeam", {}), m.get("awayTeam", {})
                h_id, a_id = h_team.get("id"), a_team.get("id")
                if not h_id or not a_id: continue
                h_stats = self.get_team_betting_stats(h_id, league_code)
                a_stats = self.get_team_betting_stats(a_id, league_code)
                if h_stats and a_stats:
                    avg_o25 = (h_stats["over25_pct"] + a_stats["over25_pct"]) / 2
                    avg_btts = (h_stats["btts_pct"] + a_stats["btts_pct"]) / 2
                    avg_o15 = (h_stats["over15_pct"] + a_stats["over15_pct"]) / 2
                    tips, max_prob = [], 0
                    if avg_o25 > 60: tips.append({"tip": "Over 2.5", "prob": round(avg_o25), "color": "#ef4444"}); max_prob = max(max_prob, avg_o25)
                    elif avg_o15 > 75: tips.append({"tip": "Over 1.5", "prob": round(avg_o15), "color": "#3b82f6"}); max_prob = max(max_prob, avg_o15)
                    if avg_btts > 60: tips.append({"tip": "Gol / Gol", "prob": round(avg_btts), "color": "#10b981"}); max_prob = max(max_prob, avg_btts)
                    
                    # Ribaltone Alert Logic
                    # If Home can comeback and Away suffers it, OR vice versa
                    h_rib_si = h_stats.get("ribaltone_si_pct", 0)
                    a_rib_sub = a_stats.get("ribaltone_sub_pct", 0)
                    a_rib_si = a_stats.get("ribaltone_si_pct", 0)
                    h_rib_sub = h_stats.get("ribaltone_sub_pct", 0)
                    
                    if (h_rib_si > 10 and a_rib_sub > 10) or (a_rib_si > 10 and h_rib_sub > 10):
                        rib_prob = max(h_rib_si + a_rib_sub, a_rib_si + h_rib_sub)
                        tips.append({"tip": "Ribaltone SI 🔄", "prob": round(min(85, rib_prob * 2)), "color": "#f59e0b"})
                        max_prob = max(max_prob, rib_prob)
                    
                    # Penalty Magnet Alert
                    h_pens_won = h_stats.get("pens_won", 0)
                    a_pens_con = a_stats.get("pens_conceded", 0)
                    if h_pens_won > 2 and a_pens_con > 2:
                        tips.append({"tip": "Rigore SI 🎯", "prob": 65, "color": "#ec4899"})
                    
                    # Scoreflow Prediction
                    top_scores = self.predict_correct_score(h_stats, a_stats)
                    score_tips = [s["score"] for s in top_scores]

                    if tips:
                        predictions.append({
                            "league": league_key.replace("_", " ").title(), 
                            "utcDate": m.get("utcDate"), 
                            "home_team": h_team.get("name"), 
                            "away_team": a_team.get("name"), 
                            "tips": tips, 
                            "max_prob": max_prob,
                            "top_scores": score_tips
                        })
        return sorted(predictions, key=lambda x: (x["utcDate"], -x["max_prob"]))

    def get_team_details(self, team_id: int):
        team_base = None
        for league_key, league_code in LEAGUE_CODES.items():
            standings = self.pa._get_standings(league_code)
            for s in standings:
                if s["team_id"] == team_id:
                    team_base = {
                        "id": team_id, 
                        "name": s["name"], 
                        "league": league_key.replace("_", " ").title(), 
                        "league_code": league_code, 
                        "league_key": league_key,
                        "played": s.get("played", 0),
                        "position": s.get("position")
                    }
                    break
            if team_base: break
        
        if not team_base: return None
        
        # Add extra info (coach, venue, etc)
        extra = self.pa._get_team_info(team_id)
        if extra:
            team_base["coach"] = extra.get("coach", {})
            team_base["venue"] = extra.get("venue")
            team_base["founded"] = extra.get("founded")
            team_base["club_colors"] = extra.get("clubColors")
            team_base["address"] = extra.get("address")
            team_base["website"] = extra.get("website")
        
        return team_base

    def get_roster_with_stats(self, team_id: int, league_code: str, league_key: str):
        # 1. Recupera la rosa REALE dalla cache locale (team_id.json)
        team_info = self.pa._get_team_info(team_id)
        squad_players = team_info.get("squad", []) if team_info else []
        
        cache = self.pa._load_cache(league_code)
        if not cache and not squad_players: return [], {}, {}
        
        roster_map = {}
        sm_names = set() # Usiamo sm_names per compatibilità con la logica sotto
        if squad_players:
            for p in squad_players:
                p_name = p["name"]
                sm_names.add(p_name.lower())
                # Mappatura ruolo
                role = p.get("position", "Unknown")
                roster_map[p_name] = {
                    "id": p["id"], "name": p_name, "position": role, 
                    "played": 0, "goals": 0, "assists": 0, "yellow": 0, "red": 0, "impact": 0, "impact_drop": 0,
                    "goals_1h": 0, "goals_2h": 0, "yellow_cards": 0, "red_cards": 0, "appearances": 0, "starts": 0, "subs": 0, "last_matchday": 0, 
                    "status": None, "status_detail": None,
                    "arrival_team_matches": 0, "arrival_team_wins": 0
                }
        
        # Per la logica del loop sotto, definiamo sm_players per indicare che abbiamo una base
        sm_players = squad_players
        
        team_stats = {
            "goals_for": 0, "goals_against": 0, "assists": 0, "yellow_cards": 0, "red_cards": 0, 
            "penalties_for": 0, "penalties_against": 0, "wins": 0, "draws": 0, "losses": 0, "matches_played": 0,
            "goal_timing": {"scored": [0]*6, "conceded": [0]*6}
        }
        def get_bucket(minute):
            if minute is None: return 5
            if minute <= 15: return 0
            if minute <= 30: return 1
            if minute <= 45: return 2
            if minute <= 60: return 3
            if minute <= 75: return 4
            return 5
        
        # Ordiniamo i match per giornata (gestendo eventuali None)
        sorted_matches = sorted(cache.items(), key=lambda x: (x[1].get("matchday") or 0), reverse=True)
        latest_matchday = sorted_matches[0][1].get("matchday", 0) if sorted_matches else 0
        
        team_total_matches = 0
        team_total_wins = 0
        player_match_stats = {} # pid -> {played: 0, wins_with: 0}

        # Ordinamento cronologico per tracciare i trasferimenti invernali
        team_matches_list = [m for m in cache.values() if m.get("home_id") == team_id or m.get("away_id") == team_id]
        team_matches_list.sort(key=lambda x: x.get("matchday", 0))

        role_map_local = {
            "Goalkeeper": "G",
            "Defence": "D", "Centre-Back": "D", "DC": "D", "Difensore centrale": "D", "Defender": "D",
            "Left-Back": "WB", "Right-Back": "WB", "TS": "WB", "TD": "WB", "Terzino sinistro": "WB", "Terzino destro": "WB",
            "Midfield": "M", "Central Midfield": "M", "Midfielder": "M", "Right Midfield": "M", "Left Midfield": "M", "CEN": "M", "CC": "M", "Defensive Midfield": "M", "CDM": "M",
            "Offence": "A", "Centre-Forward": "A", "Left Winger": "A", "Right Winger": "A", "Second Striker": "A", "Attacker": "A", "Attacking Midfield": "A", "Trequartista": "A", "ATT": "A"
        }
        team_formations = {}
        pid_to_key = {}

        for detail in team_matches_list:
            match_id = detail.get("id")
            matchday, home_id, away_id = detail.get("matchday", 0), detail.get("home_id"), detail.get("away_id")
            
            team_stats["matches_played"] += 1
            score = detail.get("score", {}).get("fullTime", {})
            h_s, a_s = score.get("home"), score.get("away")
            is_home = (home_id == team_id)
            
            # Outcome for impact
            is_win = False
            if h_s is not None and a_s is not None:
                team_stats["goals_for"] += (h_s if is_home else a_s)
                team_stats["goals_against"] += (a_s if is_home else h_s)
                if h_s == a_s: team_stats["draws"] += 1
                elif (is_home and h_s > a_s) or (not is_home and a_s > h_s): 
                    team_stats["wins"] += 1
                    is_win = True
                else: team_stats["losses"] += 1
            else:
                h_g_list = sum(1 for g in detail.get("goals", []) if g.get("team_id") == home_id)
                a_g_list = sum(1 for g in detail.get("goals", []) if g.get("team_id") == away_id)
                team_stats["goals_for"] += (h_g_list if is_home else a_g_list)
                team_stats["goals_against"] += (a_g_list if is_home else h_g_list)
                if h_g_list == a_g_list: team_stats["draws"] += 1
                elif (is_home and h_g_list > a_g_list) or (not is_home and a_g_list > h_g_list): 
                    team_stats["wins"] += 1
                    is_win = True
                else: team_stats["losses"] += 1

            # Infer Formation
            lineup_ids = detail.get("lineup_ids", [])
            if lineup_ids:
                dcs = 0
                wbs = 0
                m_c = 0
                a_c = 0
                for pid_str, p_info in detail.get("players", {}).items():
                    if p_info.get("team_id") == team_id and int(pid_str) in lineup_ids:
                        r = role_map_local.get(p_info.get("position", "Unknown"), "")
                        if r == "D": dcs += 1
                        elif r == "WB": wbs += 1
                        elif r == "M": m_c += 1
                        elif r == "A": a_c += 1
                
                # Se abbiamo 3 o più centrali (DC), i terzini (WB) scalano a centrocampo
                if dcs >= 3:
                    d_final = dcs
                    m_final = m_c + wbs
                    a_final = a_c
                else:
                    d_final = dcs + wbs
                    m_final = m_c
                    a_final = a_c

                if (d_final + m_final + a_final) >= 9:
                    f_str = f"{d_final}-{m_final}-{a_final}"
                    team_formations[f_str] = team_formations.get(f_str, 0) + 1

            # Who played in THIS match?
            for pid_str, p_info in detail.get("players", {}).items():
                if p_info.get("team_id") == team_id:
                    pid = int(pid_str)
                    p_name = p_info.get("name", "?")
                    
                    # Se abbiamo Sportmonks, cerchiamo di mappare il giocatore per NOME
                    target_key = pid
                    if sm_players:
                        if p_name.lower() in sm_names:
                            target_key = next((k for k in roster_map.keys() if k.lower() == p_name.lower()), pid)
                        else:
                            # Se non è in Sportmonks, lo ignoriamo (per evitare allucinazioni 2024)
                            continue

                    # Map pid to target_key for later use in goals/cards
                    pid_to_key[pid] = target_key

                    if target_key not in roster_map:
                        # Tracciamo quante partite/vittorie la squadra aveva già fatto PRIMA che lui arrivasse
                        roster_map[target_key] = {"id": pid, "name": p_name, "role": p_info.get("position", "Unknown"), "goals": 0, "goals_1h": 0, "goals_2h": 0, "assists": 0, "yellow_cards": 0, "red_cards": 0, "appearances": 0, "starts": 0, "subs": 0, "last_matchday": 0, "status": None, "status_detail": None, "arrival_team_matches": team_total_matches, "arrival_team_wins": team_total_wins}
                    
                    # Track appearances (Starts vs Subs)
                    lineup_ids = detail.get("lineup_ids", [])
                    subs = detail.get("substitutions", [])
                    
                    actually_played = False
                    if lineup_ids and pid in lineup_ids:
                        roster_map[target_key]["appearances"] += 1
                        roster_map[target_key]["starts"] += 1
                        actually_played = True
                    elif subs and any(s.get("player_in_id") == pid for s in subs):
                        roster_map[target_key]["appearances"] += 1
                        roster_map[target_key]["subs"] += 1
                        actually_played = True
                    elif not lineup_ids:
                        # Fallback for old matches without detailed lineup data
                        roster_map[target_key]["appearances"] += 1
                        roster_map[target_key]["starts"] += 1
                        actually_played = True
                    
                    if matchday > roster_map[target_key]["last_matchday"]:
                        roster_map[target_key]["last_matchday"] = matchday
                    
                    # Track impact stats (ONLY if they actually played)
                    if actually_played:
                        if target_key not in player_match_stats:
                            player_match_stats[target_key] = {"played_count": 0, "wins_with": 0}
                        player_match_stats[target_key]["played_count"] += 1
                        if is_win: player_match_stats[target_key]["wins_with"] += 1
            
            # For goal timing and other stats
            for goal in detail.get("goals", []):
                gid, minute = goal.get("team_id"), goal.get("minute")
                bucket = get_bucket(minute)
                if gid == team_id:
                    team_stats["goal_timing"]["scored"][bucket] += 1
                    if goal.get("type") == "PENALTY": team_stats["penalties_for"] += 1
                    sid, aid = goal.get("scorer_id"), goal.get("assist_id")
                    
                    s_key = pid_to_key.get(sid, sid)
                    a_key = pid_to_key.get(aid, aid)
                    
                    if s_key in roster_map: 
                        roster_map[s_key]["goals"] += 1
                        if minute is not None:
                            if minute <= 45: roster_map[s_key]["goals_1h"] += 1
                            else: roster_map[s_key]["goals_2h"] += 1
                        else:
                            roster_map[s_key]["goals_2h"] += 1
                    if a_key in roster_map:
                        roster_map[a_key]["assists"] += 1
                        team_stats["assists"] += 1
                else:
                    team_stats["goal_timing"]["conceded"][bucket] += 1
                    if goal.get("type") == "PENALTY": team_stats["penalties_against"] += 1
            
            for card in detail.get("cards", []):
                cid, ctid = card.get("player_id"), card.get("team_id")
                if ctid == team_id:
                    c_key = pid_to_key.get(cid, cid)
                    if card.get("card") == "YELLOW":
                        team_stats["yellow_cards"] += 1
                        if c_key in roster_map: roster_map[c_key]["yellow_cards"] += 1
                    else:
                        team_stats["red_cards"] += 1
                        if c_key in roster_map: roster_map[c_key]["red_cards"] += 1
            
            team_total_matches += 1
            if is_win: team_total_wins += 1

        # Post-process impacts
        for pid, p in roster_map.items():
            p_stats = player_match_stats.get(pid, {"played_count": 0, "wins_with": 0})
            played = p_stats["played_count"]
            wins_with = p_stats["wins_with"]
            
            p["win_rate_with"] = round((wins_with / played * 100), 1) if played > 0 else 0
            
            # Quante partite/vittorie ha fatto la squadra DA QUANDO LUI E' ARRIVATO?
            available_team_matches = team_total_matches - p.get("arrival_team_matches", 0)
            available_team_wins = team_total_wins - p.get("arrival_team_wins", 0)

            # TEAM WIN RATE vs PLAYER WIN RATE
            team_wr = (available_team_wins / available_team_matches * 100) if available_team_matches > 0 else 0
            p["team_win_rate"] = round(team_wr, 1)
            
            without_matches = available_team_matches - played
            without_wins = available_team_wins - wins_with
            
            wr_without = (without_wins / without_matches * 100) if without_matches > 0 else 0
            p["win_rate_without"] = round(wr_without, 1)
            
            # The Impact Drop is now the difference between player performance and team average
            p["impact_drop"] = round(p["win_rate_with"] - team_wr, 1)
            p["matches_played"] = played
                        
        # Trova il modulo più usato
        most_used_formation = "4-4-2" # Fallback
        if team_formations:
            most_used_formation = max(team_formations, key=team_formations.get)
        team_stats["most_used_formation"] = most_used_formation

        leaders = {"top_scorer": {"name": "-", "val": 0}, "top_assistman": {"name": "-", "val": 0}, "top_yellows": {"name": "-", "val": 0}, "top_reds": {"name": "-", "val": 0}, "top_appearances": {"name": "-", "val": 0}}
        for p in roster_map.values():
            if p["goals"] > leaders["top_scorer"]["val"]: leaders["top_scorer"] = {"name": p["name"], "val": p["goals"]}
            if p["assists"] > leaders["top_assistman"]["val"]: leaders["top_assistman"] = {"name": p["name"], "val": p["assists"]}
            if p["yellow_cards"] > leaders["top_yellows"]["val"]: leaders["top_yellows"] = {"name": p["name"], "val": p["yellow_cards"]}
            if p["red_cards"] > leaders["top_reds"]["val"]: leaders["top_reds"] = {"name": p["name"], "val": p["red_cards"]}
            if p["appearances"] > leaders["top_appearances"]["val"]: leaders["top_appearances"] = {"name": p["name"], "val": p["appearances"]}
            
        for pid, pdata in roster_map.items():
            if latest_matchday - pdata["last_matchday"] > 5: pdata["status"], pdata["status_detail"] = "Gone", f"Ultima: G{pdata['last_matchday']}"
            
        from scraper.injuries import get_injured_by_team
        inj_data = get_injured_by_team(league_key)
        team_details = self.get_team_details(team_id)
        if not team_details:
            # Fallback for Sofascore team IDs (>= 900000): look up name from DB
            import sqlite3
            try:
                conn = sqlite3.connect('data/betanalyzer.db', timeout=10)
                row = conn.execute("SELECT team_name FROM player_info WHERE team_id = ? LIMIT 1", (team_id,)).fetchone()
                conn.close()
                team_name_lower = row[0].lower() if row else ""
            except Exception:
                team_name_lower = ""
        else:
            team_name_lower = team_details["name"].lower()
        team_injuries = inj_data.get(team_name_lower, [])
        suspended_by_team = self.pa.get_suspended_for_league(league_key)
        team_suspensions = suspended_by_team.get(team_id, [])
        for pid, pdata in roster_map.items():
            if pdata.get("status") == "Gone": continue
            p_name = pdata["name"].lower()
            for s in team_suspensions:
                if s["player"].lower() in p_name or p_name in s["player"].lower():
                    pdata["status"], pdata["status_detail"] = "Suspended", s["reason"]
                    break
            if not pdata.get("status"):
                for inj in team_injuries:
                    if inj["player"].lower() in p_name or p_name in inj["player"].lower():
                        pdata["status"], pdata["status_detail"] = "Injured", inj["injury"]
                        break
        return list(roster_map.values()), team_stats, leaders

    def get_team_rank_history(self, team_id: int, league_code: str):
        """Calculates the rank of a team for each matchday based on cached matches."""
        cache = self.pa._load_cache(league_code)
        if not cache: return []
        
        matches_by_md = {}
        for mid, detail in cache.items():
            md = detail.get("matchday")
            if md:
                if md not in matches_by_md: matches_by_md[md] = []
                matches_by_md[md].append(detail)
        
        teams_stats = {} # team_id -> {"pts": 0, "gd": 0}
        rank_history = []
        
        sorted_mds = sorted(matches_by_md.keys())
        for md in sorted_mds:
            for match in matches_by_md[md]:
                h_id, a_id = match.get("home_id"), match.get("away_id")
                score = match.get("score", {}).get("fullTime", {})
                h_s, a_s = score.get("home"), score.get("away")
                if h_s is None or a_s is None: continue
                
                if h_id not in teams_stats: teams_stats[h_id] = {"pts": 0, "gd": 0}
                if a_id not in teams_stats: teams_stats[a_id] = {"pts": 0, "gd": 0}
                
                teams_stats[h_id]["gd"] += (h_s - a_s)
                teams_stats[a_id]["gd"] += (a_s - h_s)
                
                if h_s > a_s: teams_stats[h_id]["pts"] += 3
                elif h_s < a_s: teams_stats[a_id]["pts"] += 3
                else:
                    teams_stats[h_id]["pts"] += 1
                    teams_stats[a_id]["pts"] += 1
            
            if team_id in teams_stats:
                ranking = sorted(teams_stats.items(), key=lambda x: (x[1]["pts"], x[1]["gd"]), reverse=True)
                for rank, (tid, stats) in enumerate(ranking, 1):
                    if tid == team_id:
                        rank_history.append({"matchday": md, "rank": rank})
                        break
        return rank_history
    def get_player_full_details(self, player_id: int, team_id: int, league_key: str):
        # NORMALIZZAZIONE LEAGUE KEY (Ponte tra Scouting e PenaltyAnalyzer)
        mapping = {
            "serie_a": "italy_serie_a",
            "premier_league": "england_premier_league",
            "la_liga": "spain_la_liga",
            "bundesliga": "germany_bundesliga",
            "ligue_1": "france_ligue_1",
            "eredivisie": "netherlands_eredivisie",
            "brasileirao": "brazil_serie_a",
            "primeira_liga": "portugal_primeira_liga",
            "championship": "england_championship",
        }
        league_key = mapping.get(league_key, league_key)
        
        league_code = LEAGUE_CODES.get(league_key)
        if not league_code:
            logger.error(f"League code non trovato per: {league_key}")
            return None

        # ═══════════════════════════════════════════════════════════════
        # FAST PATH: Giocatori Sofascore (ID >= 90M) → carica diretto dal DB
        # Evita chiamate API Football-Data inutili (team_id 900xxx non esiste in FD)
        # ═══════════════════════════════════════════════════════════════
        if player_id >= 90_000_000:
            logger.info(f"Sofascore player {player_id} - caricamento diretto dal DB")
            import sqlite3
            try:
                conn = sqlite3.connect('data/betanalyzer.db', timeout=30)
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()

                # Recupera info base
                cursor.execute("""
                    SELECT pi.player_id, pi.name, pi.team_id, pi.team_name, pi.league_id, pi.position_id,
                           psc.stats_json, psc.rating
                    FROM player_info pi
                    LEFT JOIN player_stats_cache psc ON pi.player_id = psc.player_id
                    WHERE pi.player_id = ?
                    ORDER BY psc.season_id DESC LIMIT 1
                """, (player_id,))
                db_row = cursor.fetchone()
                conn.close()

                if db_row:
                    s_json = json.loads(db_row['stats_json']) if db_row['stats_json'] else {}
                    pos_map = {1: "Goalkeeper", 2: "Defender", 3: "Midfielder", 4: "Forward"}
                    pos_id = db_row['position_id'] or s_json.get('position_id')

                    player_data = {
                        "id": db_row['player_id'],
                        "name": db_row['name'] or "N/A",
                        "position": pos_map.get(pos_id, "N/A"),
                        "nationality": s_json.get("nationality", "N/A"),
                        "goals": s_json.get("goals", 0),
                        "assists": s_json.get("assists", 0),
                        "appearances": s_json.get("appearances", 0),
                        "minutes": s_json.get("minutes_played", 0),
                        "yellow_cards": s_json.get("yellowCards", s_json.get("yellow_cards", 0)),
                        "red_cards": s_json.get("redCards", s_json.get("red_cards", 0)),
                        "rating": round(db_row['rating'] / 10, 1) if db_row['rating'] and db_row['rating'] > 10 else db_row['rating'],
                        "number": s_json.get("shirt_number", "-"),
                        "dateOfBirth": s_json.get("date_of_birth", ""),
                        "source": "sofascore",
                        # Campi richiesti dal template player_detail.html
                        "impact": 0, "impact_drop": 0,
                        "played": s_json.get("appearances", 0),
                        "starts": s_json.get("appearances", 0),
                        "subs": 0,
                        "goals_1h": 0, "goals_2h": 0,
                        "last_matchday": 0,
                        "status": None, "status_detail": None,
                        "arrival_team_matches": 0, "arrival_team_wins": 0,
                    }

                    # Team info dal DB (no API call per team Sofascore)
                    team_info = {
                        "id": db_row['team_id'],
                        "name": db_row['team_name'] or "N/A",
                        "crest": "",
                    }

                    # Stats avanzate direttamente dal JSON
                    advanced_stats = self._map_sm_stats(s_json)
                    player_data["advanced_stats"] = advanced_stats
                    if advanced_stats:
                        player_data["goals"] = advanced_stats["goals"]
                        player_data["assists"] = advanced_stats["assists"]
                        player_data["appearances"] = advanced_stats["appearances"]

                    # Dati Sofascore esclusivi per il template (xG, xA, duelli, ecc.)
                    player_data["sofascore_stats"] = {
                        "expected_goals": round(s_json.get("expected_goals", 0), 2),
                        "expected_assists": round(s_json.get("expected_assists", 0), 2),
                        "big_chances_created": s_json.get("big_chances_created", 0),
                        "big_chances_missed": s_json.get("big_chances_missed", 0),
                        "aerial_duels_won": s_json.get("aerial_duels_won", s_json.get("aerials_won", 0)),
                        "ground_duels_won": s_json.get("ground_duels_won", 0),
                        "ball_recovery": s_json.get("ball_recovery", 0),
                        "possession_lost": s_json.get("possession_lost", 0),
                        "penalty_goals": s_json.get("penalty_goals", 0),
                        "penalties_taken": s_json.get("penalties_taken", 0),
                        "penalty_won": s_json.get("penalty_won", 0),
                        "dribbles_attempts": s_json.get("dribbles_attempts", 0),
                        "dribbles_success": s_json.get("dribbles_success", 0),
                        "minutes_played": s_json.get("minutes_played", 0),
                        "matches_started": s_json.get("matches_started", 0),
                    }

                    # Prova bio e infortuni
                    from scraper.injuries import get_injured_by_team
                    injuries = get_injured_by_team(league_key)
                    team_name_norm = (team_info["name"] or "").lower()
                    team_injuries = injuries.get(team_name_norm, [])
                    for inj in team_injuries:
                        if inj["player"].lower() in player_data["name"].lower() or player_data["name"].lower() in inj["player"].lower():
                            player_data["injury_details"] = inj
                            break

                    # Market value
                    try:
                        conn2 = sqlite3.connect('data/betanalyzer.db', timeout=30)
                        conn2.row_factory = sqlite3.Row
                        cur2 = conn2.cursor()
                        cur2.execute("""
                            SELECT market_value_eur FROM player_market_values
                            WHERE player_id = ?
                               OR player_id IN (SELECT player_id FROM player_info WHERE name LIKE ?)
                            LIMIT 1
                        """, (player_id, f"%{player_data['name']}%"))
                        mv_row = cur2.fetchone()
                        conn2.close()
                        if mv_row and mv_row['market_value_eur'] and mv_row['market_value_eur'] > 0:
                            val = mv_row['market_value_eur']
                            if val >= 1_000_000:
                                player_data["market_value"] = f"€{val / 1_000_000:.1f}M"
                            elif val >= 1_000:
                                player_data["market_value"] = f"€{val / 1_000:.0f}K"
                            else:
                                player_data["market_value"] = f"€{val}"
                    except Exception as e:
                        logger.error(f"Errore market value Sofascore player {player_id}: {e}")

                    return {"player": player_data, "team": team_info}
            except Exception as e:
                logger.error(f"Errore fallback Sofascore per {player_id}: {e}")
            # Se il fast path Sofascore fallisce, ritorna None
            logger.warning(f"Sofascore player {player_id} non trovato nel DB")
            return None

        # ═══════════════════════════════════════════════════════════════
        # FLUSSO STANDARD: Giocatori Football-Data / Sportmonks (ID < 90M)
        # ═══════════════════════════════════════════════════════════════
        # 1. Recupera i dati base dalla rosa locale
        players, team_stats, leaders = self.get_roster_with_stats(team_id, league_code, league_key)
        team_info = self.get_team_details(team_id)

        # Prova per ID (Football-Data)
        player_data = next((p for p in players if p["id"] == player_id), None)

        # FALLBACK: Prova per Nome (se l'ID arriva da Sportmonks/Scouting)
        if not player_data:
            import sqlite3
            conn = sqlite3.connect('data/betanalyzer.db', timeout=30)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM player_info WHERE player_id = ?", (player_id,))
            row = cursor.fetchone()
            conn.close()

            search_name = row['name'] if row else None
            if search_name:
                player_data = next((p for p in players if p["name"].lower() == search_name.lower()), None)
                if not player_data:
                    player_data = next((p for p in players if search_name.lower() in p["name"].lower() or p["name"].lower() in search_name.lower()), None)

        if not player_data:
            logger.warning(f"Giocatore non trovato nella rosa: ID {player_id}")
            return None

        # IMPORTANTE: Usiamo l'ID trovato nella rosa per le chiamate successive (Bio, Infortuni, ecc.)
        player_id = player_data["id"]

        # 2. Get injuries/bio
        bio = self.pa._get_player_info(player_id)
        if bio: player_data["bio"] = bio

        from scraper.injuries import get_injured_by_team
        injuries = get_injured_by_team(league_key)
        team_name_norm = team_info["name"].lower() if team_info else ""
        team_injuries = injuries.get(team_name_norm, [])
        for inj in team_injuries:
            if inj["player"].lower() in player_data["name"].lower() or player_data["name"].lower() in inj["player"].lower():
                player_data["injury_details"] = inj
                break

        # 3. RECUPERO DATI AVANZATI DAL DB (Nuova Logica Blindata)
        import sqlite3
        advanced_stats = None
        try:
            conn = sqlite3.connect('data/betanalyzer.db', timeout=30)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            # Normalizziamo la ricerca: prima per ID, poi per nome esatto, poi LIKE
            cursor.execute("""
                SELECT stats_json
                FROM player_stats_cache
                WHERE player_id = ?
                ORDER BY season_id DESC LIMIT 1
            """, (player_id,))
            row = cursor.fetchone()
            if not row:
                # Cerca per nome nel DB (Sportmonks/Sofascore)
                cursor.execute("""
                    SELECT psc.stats_json
                    FROM player_stats_cache psc
                    JOIN player_info pi ON psc.player_id = pi.player_id
                    WHERE pi.name = ? OR pi.name LIKE ?
                    ORDER BY psc.season_id DESC LIMIT 1
                """, (player_data['name'], f"%{player_data['name']}%"))
                row = cursor.fetchone()
            conn.close()

            if row:
                s_json = json.loads(row['stats_json'])
                advanced_stats = self._map_sm_stats(s_json)

            # --- FALLBACK LIVE DISABILITATO (troppo lento, causa timeout 10min+) ---
            # Se non troviamo stats nel DB, mostriamo il giocatore senza stats avanzate
            # piuttosto che bloccare la pagina con chiamate API Sportmonks lente
            if not advanced_stats:
                logger.info(f"Nessuna stat avanzata nel DB per {player_data['name']} (ID {player_id})")
            # --------------------------------------

        except Exception as e:
            logger.error(f"Errore recupero stats avanzate per {player_id}: {e}")

        player_data["advanced_stats"] = advanced_stats

        if advanced_stats:
            player_data["goals"] = advanced_stats["goals"]
            player_data["assists"] = advanced_stats["assists"]
            player_data["appearances"] = advanced_stats["appearances"]

        # 4. RECUPERO VALORE DI MERCATO dal DB
        try:
            conn = sqlite3.connect('data/betanalyzer.db', timeout=30)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("""
                SELECT market_value_eur FROM player_market_values
                WHERE player_id = ?
                   OR player_id IN (SELECT player_id FROM player_info WHERE name LIKE ?)
                LIMIT 1
            """, (player_id, f"%{player_data['name']}%"))
            mv_row = cursor.fetchone()
            conn.close()
            if mv_row and mv_row['market_value_eur'] and mv_row['market_value_eur'] > 0:
                val = mv_row['market_value_eur']
                if val >= 1_000_000:
                    player_data["market_value"] = f"€{val / 1_000_000:.1f}M"
                elif val >= 1_000:
                    player_data["market_value"] = f"€{val / 1_000:.0f}K"
                else:
                    player_data["market_value"] = f"€{val}"
        except Exception as e:
            logger.error(f"Errore recupero market value per {player_id}: {e}")

        return {"player": player_data, "team": team_info}

    def _map_sm_stats(self, s_json):
        """Helper per mappare il JSON di Sportmonks nelle chiavi del template"""
        adv = {
            "goals": s_json.get("goals", 0),
            "assists": s_json.get("assists", 0),
            "appearances": s_json.get("appearances", 0),
            "shots_total_total": s_json.get("shots_total", 0),
            "shots_on_target_total": s_json.get("shots_on_target", 0),
            "fouls_drawn_total": s_json.get("fouls_drawn", 0),
            "fouls_committed_total": s_json.get("fouls_committed", 0),
            "key_passes_total": s_json.get("key_passes", 0),
            "dribbles_success_total": s_json.get("dribbles_success", 0),
            "interceptions_total": s_json.get("interceptions", 0),
            "tackles_total": s_json.get("tackles", 0),
            "aerials_won_total": s_json.get("aerials_won", 0),
            "accurate_passes_pct": s_json.get("accurate_passes_pct", 0),
            "big_chances_created_total": s_json.get("big_chances_created", 0),
            "position_id": s_json.get("position_id")
        }
        apps = adv["appearances"] or 1
        adv["shots_on_target_avg"] = round(adv["shots_on_target_total"] / apps, 2)
        adv["shots_total_avg"] = round(adv["shots_total_total"] / apps, 2)
        adv["fouls_drawn_avg"] = round(adv["fouls_drawn_total"] / apps, 2)
        adv["fouls_committed_avg"] = round(adv["fouls_committed_total"] / apps, 2)
        return adv

def calculate_player_rating(stats: dict, pos_id: int) -> float:
    """Calcola un voto da 1 a 100 (V6 - Balanced Realism).

    Miglioramenti rispetto a V5:
    - Minimo 5 presenze per evitare outlier (1 gol in 1 partita = 98.5)
    - Bonus offensivo per difensori (gol, assist, key passes, big chances)
    - Bonus gol/assist per centrocampisti con peso maggiore
    - Penalità poche presenze, bonus consistenza (>25 app)
    - Range effettivo target: difensori 45-82, centrocampisti 45-85, attaccanti 45-95
    """
    apps = stats.get('appearances', 0)
    if not apps or apps < 1:
        return 0.0

    # Minimo presenze per un rating affidabile
    # Portieri: servono almeno 15 partite (quasi mezza stagione) per valutare
    # Altri ruoli: 5 presenze
    min_apps = 15 if pos_id == 24 else 5
    if apps < min_apps:
        return 0.0

    score = 0.0
    goals = stats.get('goals', 0)
    assists = stats.get('assists', 0)
    shots_ot = stats.get('shots_on_target', 0)
    key_passes = stats.get('key_passes', 0)
    big_chances = stats.get('big_chances_created', 0)
    dribbles = stats.get('dribbles_success', 0)
    interceptions = stats.get('interceptions', 0)
    tackles = stats.get('tackles', 0)
    aerials = stats.get('aerials_won', 0)
    blocks = stats.get('blocks', 0)
    clearances = stats.get('clearances', 0)
    pass_acc = stats.get('accurate_passes_pct', 0)
    fouls_drawn = stats.get('fouls_drawn', 0)

    # 1. ATTACCANTI (27) — peso dominante: gol e creazione
    if pos_id == 27:
        score += (goals / apps) * 45          # ~1 gol/gara = +45
        score += (assists / apps) * 12         # ~0.5 ass/gara = +6
        score += (shots_ot / apps) * 3         # ~1.5 tiri/gara = +4.5
        score += (big_chances / apps) * 6      # ~0.5 bc/gara = +3
        score += (dribbles / apps) * 2.5       # ~1 drib/gara = +2.5
        score += (key_passes / apps) * 1.5     # passaggi chiave
        score += (fouls_drawn / apps) * 1      # falli subiti = pericolosità

    # 2. CENTROCAMPISTI (26) — bilanciato: creatività + difesa + gol
    elif pos_id == 26:
        score += (goals / apps) * 18           # gol pesano di più che in V5
        score += (assists / apps) * 12         # assist importanti
        score += (key_passes / apps) * 3       # creatività
        score += (big_chances / apps) * 5      # occasioni create
        score += (pass_acc / 100) * 6          # qualità passaggio
        score += ((interceptions + tackles) / apps) * 2.5  # fase difensiva
        score += (dribbles / apps) * 2         # tecnica
        score += (shots_ot / apps) * 2         # pericolosità

    # 3. DIFENSORI (25) — v2: capped defensive volume + boosted offensive contribution
    #    Fix: squadre deboli subiscono più attacchi → difensori accumulano stats
    #    difensive gonfiate. Cap a 18pt max dalla fase difensiva pura.
    elif pos_id == 25:
        # Componente difensiva — CAPPED a 18 punti max
        # Evita che difensori di squadre basse (Genoa, Lecce) superino top players
        def_raw = 0
        def_raw += min(((interceptions + tackles) / apps) * 2.5, 8)   # ~3/gara = +7.5, cap 8
        def_raw += min((aerials / apps) * 2.0, 6)                     # ~3/gara = +6, cap 6
        def_raw += min((clearances / apps) * 0.8, 4)                  # ~5/gara = +4, cap 4
        def_raw += min((blocks / apps) * 1.5, 2)                      # ~0.5/gara = +0.75, cap 2
        score += min(def_raw, 18)  # hard cap: max 18pt from pure defending

        # Componente costruzione (max ~12 punti) — boosted: playmaking è qualità
        score += (pass_acc / 100) * 6                      # ~85% = +5.1 (was *4)
        score += (key_passes / apps) * 3                   # per terzini offensivi (was *2)

        # Componente offensiva (max ~28 punti per un Dimarco) — boosted
        score += (goals / apps) * 25                       # gol rari ma pesanti (was *20)
        score += (assists / apps) * 15                     # assist da terzino (was *12)
        score += (big_chances / apps) * 5                  # occasioni create (was *4)
        score += (dribbles / apps) * 2                     # propulsione (was *1.5)

    # 4. PORTIERI (24) — V2: bilancio realistico
    #    Principi: meno gol subiti > tante parate (parare tanto = difesa debole)
    #    Save% e CS% sono indicatori di qualità reale
    #    Presenze alte = affidabilità (titolare indiscusso)
    elif pos_id == 24:
        saves = stats.get('saves', 0)
        goals_conc = stats.get('goals_conceded', 0)
        clean_sheets = stats.get('clean_sheets', 0)
        saves_ib = stats.get('saves_insidebox', 0)
        errors_goal = stats.get('errors_lead_to_goal', 0)
        sm_rating = stats.get('sm_rating', 0)

        # 1. GOL SUBITI per partita — peso DOMINANTE (max 16pt)
        #    Scala continua: meno gol = più punti, differenza fine tra 0.8 e 1.0
        ga_pg = goals_conc / apps
        if ga_pg <= 0.5:
            score += 16                             # elite assoluta (quasi impossibile)
        elif ga_pg <= 1.5:
            # Scala lineare: 0.5 → 16, 1.5 → 0
            score += max(0, 16 - (ga_pg - 0.5) * 16)
        # >1.5 nessun bonus

        # 2. CLEAN SHEET % — premiante (max 12pt)
        #    CS% alta = dominio, un top GK tiene 40%+ CS
        cs_pct = (clean_sheets / apps) * 100
        score += min(cs_pct * 0.3, 12)             # 40% = +12

        # 3. SAVE % — efficacia pura (max 8pt)
        #    (parate / tiri subiti totali) — indica riflessi e posizionamento
        total_shots_faced = saves + goals_conc
        if total_shots_faced > 0:
            save_pct = (saves / total_shots_faced) * 100
            if save_pct >= 75:
                score += 8
            elif save_pct >= 72:
                score += 6.5
            elif save_pct >= 68:
                score += 5
            elif save_pct >= 64:
                score += 3
            elif save_pct >= 60:
                score += 1.5

        # 4. PARATE per partita — peso ridotto (max 6pt)
        #    Parare tanto NON è necessariamente positivo (difesa bucata)
        #    Ma un minimo di attività dimostra capacità di intervento
        saves_pg = saves / apps
        score += min(saves_pg * 2, 6)              # 3.0 saves/g = +6

        # 5. PARATE in area (saves inside box) — bonus decisività (max 3pt)
        #    Parate ravvicinate = riflessi top
        if saves_ib > 0:
            sib_pg = saves_ib / apps
            score += min(sib_pg * 2, 3)            # ~1.5/g = +3

        # 6. Precisione passaggi (distribuzione/piede) (max 3pt)
        score += min((pass_acc / 100) * 4, 3)      # 75% = +3

        # 7. Penalità errori che portano a gol
        score -= errors_goal * 3.0

        # 8. Bonus Sportmonks rating (ancora di realtà)
        if sm_rating >= 6.5:
            score += (sm_rating - 6.0) * 2          # 7.0 = +2, 7.5 = +3

    # 5. Fallback
    else:
        score += (goals / apps) * 15
        score += (assists / apps) * 8
        score += (pass_acc / 100) * 3

    # Bonus consistenza: chi gioca 25+ partite ha dimostrato valore
    if apps >= 25:
        score += 2.0
    elif apps >= 15:
        score += 1.0

    # Voto base + score
    base = 40.0
    raw_rating = base + score

    # ── Correzione Sportmonks Rating (per tutti i ruoli) ──
    # sm_rating scala 1-10, media ~6.5, top player ~7.5+
    # Usiamo come ancora: se il nostro voto diverge molto, aggiustiamo
    sm_rating = stats.get('sm_rating', 0)
    if sm_rating >= 5.0 and pos_id != 24:  # GK già lo usa sopra
        # Converti sm_rating (scala 1-10) in scala 0-100
        # 6.0 → ~55, 6.5 → ~62, 7.0 → ~72, 7.5 → ~82, 8.0 → ~90
        sm_mapped = (sm_rating - 5.0) * 18 + 37  # 6.0=55, 7.0=73, 7.5=82
        sm_mapped = max(40.0, min(95.0, sm_mapped))

        # Blend: 70% nostro calcolo, 30% Sportmonks
        raw_rating = raw_rating * 0.70 + sm_mapped * 0.30

    final_rating = min(95.0, max(35.0, raw_rating))
    return round(final_rating, 1)
