
import os
import requests
import json
import time
import logging

# Configurazione logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s', datefmt='%H:%M:%S')
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

BASE_URL = "https://api.football-data.org/v4"
CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "penalties")

class PenaltyAnalyzer:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.headers = {"X-Auth-Token": api_key}
        self._mem_cache = {}
        os.makedirs(CACHE_DIR, exist_ok=True)

    def _get(self, endpoint, params=None):
        url = f"{BASE_URL}{endpoint}"
        for attempt in range(3):
            try:
                resp = requests.get(url, headers=self.headers, params=params, timeout=30)
                if resp.status_code == 200:
                    return resp.json()
                if resp.status_code == 429:
                    wait = int(resp.headers.get("Retry-After", 60))
                    logger.warning(f"Rate limited, waiting {wait}s (attempt {attempt+1}/3)")
                    time.sleep(wait + 1)
                    continue
                logger.error(f"API Error {resp.status_code} for {endpoint}")
                return None
            except Exception as e:
                logger.error(f"Request error: {e}")
                time.sleep(5)
        return None

    def _load_cache(self, league_code):
        if league_code in self._mem_cache:
            return self._mem_cache[league_code]
        path = os.path.join(CACHE_DIR, f"{league_code}_matches.json")
        if os.path.exists(path):
            with open(path, 'r') as f:
                data = json.load(f)
                self._mem_cache[league_code] = data
                return data
        return {}

    def _save_cache(self, league_code, cache):
        path = os.path.join(CACHE_DIR, f"{league_code}_matches.json")
        with open(path, 'w') as f:
            json.dump(cache, f)

    def get_team_roster(self, team_id: int):
        data = self._get_team_info(team_id)
        if data and "squad" in data:
            return data["squad"]
        return []

    def _get_team_info(self, team_id: int):
        path = os.path.join(CACHE_DIR, f"team_{team_id}.json")
        if os.path.exists(path):
            # Cache valid for 7 days for team info (coach, etc)
            if time.time() - os.path.getmtime(path) < 86400 * 7:
                try:
                    with open(path, 'r') as f:
                        return json.load(f)
                except: pass
        
        data = self._get(f"/teams/{team_id}")
        if data:
            try:
                with open(path, 'w') as f:
                    json.dump(data, f)
            except: pass
            return data
        return None

    def _get_player_info(self, player_id: int):
        path = os.path.join(CACHE_DIR, f"player_{player_id}.json")
        if os.path.exists(path):
            # Cache valid for 7 days for player info
            if time.time() - os.path.getmtime(path) < 86400 * 7:
                try:
                    with open(path, 'r') as f:
                        return json.load(f)
                except: pass
        
        data = self._get(f"/persons/{player_id}")
        if data:
            try:
                with open(path, 'w') as f:
                    json.dump(data, f)
            except: pass
            return data
        return None

    def _fetch_match_details(self, league_code, matches_list, progress_cb=None):
        cache = self._load_cache(league_code)
        to_fetch = []
        
        # Mapping score from general list
        match_scores = {}
        for m in matches_list:
            mid = str(m["id"])
            match_scores[mid] = m.get("score")
            cached = cache.get(mid)
            # Force fetch if missing, incomplete lineups (< 22 players), no score, or empty substitutions data
            if not cached or len(cached.get("lineup_ids", [])) < 22 or "score" not in cached or not cached.get("substitutions"):
                to_fetch.append(mid)
        
        for i, mid in enumerate(to_fetch):
            if progress_cb: progress_cb(i + 1, len(to_fetch), mid)
            if i > 0: time.sleep(3.0)
            
            detail = self._get(f"/matches/{mid}")
            if not detail: continue
            
            matchday = detail.get("matchday")
            home_id = detail.get("homeTeam", {}).get("id")
            away_id = detail.get("awayTeam", {}).get("id")
            ref_name = None
            var_name = None
            ref_nationality = None
            for ref in detail.get("referees", []):
                if ref.get("type") == "REFEREE":
                    ref_name = ref.get("name")
                    ref_nationality = ref.get("nationality")
                elif ref.get("type") == "VIDEO_ASSISTANT_REFEREE_N1":
                    var_name = ref.get("name")

            all_goals = []
            for g in detail.get("goals", []):
                scorer = g.get("scorer") or {}
                assist = g.get("assist") or {}
                all_goals.append({
                    "team_id": g.get("team", {}).get("id"),
                    "scorer": scorer.get("name", "?"),
                    "scorer_id": scorer.get("id"),
                    "assist": assist.get("name"),
                    "assist_id": assist.get("id"),
                    "minute": g.get("minute"),
                    "type": g.get("type", "REGULAR")
                })

            cards = []
            for c in detail.get("bookings", []):
                cards.append({
                    "team_id": c.get("team", {}).get("id"),
                    "player_id": c.get("player", {}).get("id"),
                    "player": c.get("player", {}).get("name", "?"),
                    "card": c.get("card"),
                    "minute": c.get("minute")
                })

            player_positions = {}
            players_info = {}
            lineup_ids = []
            for side in ("homeTeam", "awayTeam"):
                team_data = detail.get(side, {})
                tid = team_data.get("id")
                lineup_ids.extend([p.get("id") for p in team_data.get("lineup", []) if p.get("id")])
                for p in team_data.get("lineup", []) + team_data.get("bench", []):
                    pid = p.get("id")
                    if pid:
                        player_positions[pid] = p.get("position")
                        players_info[pid] = {"name": p.get("name"), "position": p.get("position"), "team_id": tid}

            # Extract substitutions
            subs = []
            for s in detail.get("substitutions", []):
                subs.append({
                    "team_id": s.get("team", {}).get("id"),
                    "player_in_id": s.get("playerIn", {}).get("id"),
                    "player_out_id": s.get("playerOut", {}).get("id"),
                    "minute": s.get("minute")
                })

            # Extract penalties from goals
            pens = [g for g in all_goals if g.get("type") == "PENALTY"]

            cache[mid] = {
                "matchday": matchday,
                "home_id": home_id,
                "away_id": away_id,
                "referee": ref_name,
                "var_referee": var_name,
                "referee_nationality": ref_nationality,
                "goals": all_goals,
                "cards": cards,
                "penalties": pens,
                "substitutions": subs,
                "player_positions": player_positions,
                "players": players_info,
                "lineup_ids": lineup_ids,
                "player_ids": lineup_ids,
                "score": match_scores.get(mid)
            }
            if (i + 1) % 10 == 0: self._save_cache(league_code, cache)

        self._save_cache(league_code, cache)
        return cache

    def _get_standings(self, league_code):
        resp = self._get(f"/competitions/{league_code}/standings")
        if not resp: return []
        res = []
        for table in resp.get("standings", []):
            if table.get("type") == "TOTAL":
                for entry in table.get("table", []):
                    team = entry.get("team", {})
                    res.append({
                        "team_id": team.get("id"),
                        "name": team.get("name"),
                        "position": entry.get("position"),
                        "points": entry.get("points"),
                        "played": entry.get("playedGames"),
                        "goals_for": entry.get("goalsFor"),
                        "goals_against": entry.get("goalsAgainst")
                    })
        return res

    def analyze_league(self, league_key: str, skip_fetch: bool = False) -> list:
        league_code = LEAGUE_CODES.get(league_key)
        if not league_code: return []
        
        cache = self._load_cache(league_code)
        
        if not skip_fetch:
            # Check for new finished matches to update cache
            logger.info(f"Checking for new finished matches in {league_key}...")
            data = self._get(f"/competitions/{league_code}/matches", params={"status": "FINISHED"})
            if data:
                finished = data.get("matches", [])
                to_fetch = []
                for m in finished:
                    mid = str(m["id"])
                    if mid not in cache:
                        to_fetch.append(m)
                
                if to_fetch:
                    logger.info(f"Found {len(to_fetch)} new matches for {league_key}. Fetching details...")
                    # We only fetch the missing ones
                    cache = self._fetch_match_details(league_code, finished)
        
        if not cache: return []
        
        standings = self._get_standings(league_code)
        team_stats = {}
        for s in standings:
            team_stats[s["team_id"]] = {
                "team": s["name"],
                "penalties_for": 0,
                "penalties_against": 0,
                "matches": 0,
                "played": s["played"],
                "goals_for": s.get("goals_for", 0),
                "goals_against": s.get("goals_against", 0)
            }
            
        for mid, detail in cache.items():
            h_id, a_id = detail.get("home_id"), detail.get("away_id")
            if h_id in team_stats: team_stats[h_id]["matches"] += 1
            if a_id in team_stats: team_stats[a_id]["matches"] += 1
            
            for g in detail.get("goals", []):
                if g.get("type") == "PENALTY":
                    tid = g.get("team_id")
                    if tid == h_id and h_id in team_stats: team_stats[h_id]["penalties_for"] += 1
                    if tid == a_id and a_id in team_stats: team_stats[a_id]["penalties_for"] += 1
                    # Against
                    opp_id = a_id if tid == h_id else h_id
                    if opp_id in team_stats: team_stats[opp_id]["penalties_against"] += 1

        # Load Sportmonks team-level aggregated stats
        sportmonks_team_stats = self._load_sportmonks_team_stats()

        # Get scheduled matches + today's in-play/finished (for referee data)
        scheduled = self._get(f"/competitions/{league_code}/matches", params={"status": "SCHEDULED,TIMED"})
        # Also fetch today's matches that may already be in play (they have referee assigned)
        today_live = self._get(f"/competitions/{league_code}/matches", params={"status": "IN_PLAY,PAUSED,FINISHED"})

        # Build referee map from today's live/finished matches (API assigns referee once match starts)
        live_referee_map = {}  # (home_id, away_id) -> referee_name
        if today_live:
            from datetime import datetime, timedelta
            today_str = datetime.utcnow().strftime("%Y-%m-%d")
            yesterday_str = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")
            for m in today_live.get("matches", []):
                match_date = (m.get("utcDate") or "")[:10]
                if match_date not in (today_str, yesterday_str):
                    continue  # Only recent matches
                h_id = m["homeTeam"]["id"]
                a_id = m["awayTeam"]["id"]
                for ref in m.get("referees", []):
                    if ref.get("type") == "REFEREE":
                        live_referee_map[(h_id, a_id)] = ref.get("name")
                        break

        next_matches = {}
        if scheduled:
            for m in scheduled.get("matches", []):
                h_id, a_id = m["homeTeam"]["id"], m["awayTeam"]["id"]
                h_name, a_name = m["homeTeam"]["name"], m["awayTeam"]["name"]
                # Estrai arbitro dal match schedulato
                match_referee = None
                for ref in m.get("referees", []):
                    if ref.get("type") == "REFEREE":
                        match_referee = ref.get("name")
                        break
                # Fallback: cerca arbitro dalle partite live/in corso (stesso match)
                if not match_referee:
                    match_referee = live_referee_map.get((h_id, a_id))
                if h_id not in next_matches: next_matches[h_id] = {"opponent": a_name, "is_home": True, "date": m["utcDate"], "referee": match_referee}
                if a_id not in next_matches: next_matches[a_id] = {"opponent": h_name, "is_home": False, "date": m["utcDate"], "referee": match_referee}

        # Se non abbiamo partite scheduled (API quota esaurita), usa le live come next_matches
        if not next_matches and today_live:
            for m in today_live.get("matches", []):
                h_id = m["homeTeam"]["id"]
                a_id = m["awayTeam"]["id"]
                h_name = m["homeTeam"]["name"]
                a_name = m["awayTeam"]["name"]
                match_referee = None
                for ref in m.get("referees", []):
                    if ref.get("type") == "REFEREE":
                        match_referee = ref.get("name")
                        break
                if h_id not in next_matches: next_matches[h_id] = {"opponent": a_name, "is_home": True, "date": m.get("utcDate"), "referee": match_referee}
                if a_id not in next_matches: next_matches[a_id] = {"opponent": h_name, "is_home": False, "date": m.get("utcDate"), "referee": match_referee}

        # Costruisci stats storiche per ogni arbitro dalla cache
        ref_stats_map = {}
        for mid, detail in cache.items():
            ref = detail.get("referee")
            if not ref: continue
            if ref not in ref_stats_map:
                ref_stats_map[ref] = {"matches": 0, "penalties": 0, "yellows": 0, "reds": 0, "fouls": 0}
            ref_stats_map[ref]["matches"] += 1
            ref_stats_map[ref]["penalties"] += sum(1 for g in detail.get("goals", []) if g.get("type") == "PENALTY")
            ref_stats_map[ref]["yellows"] += sum(1 for c in detail.get("cards", []) if c.get("card") == "YELLOW")
            ref_stats_map[ref]["reds"] += sum(1 for c in detail.get("cards", []) if c.get("card") != "YELLOW")

        results = []
        # Max weights for factor visualization
        max_weights = {"astinenza": 25, "attacco": 20, "difesa_avv": 20, "arbitro": 15, "casa": 10, "storico": 10}
        
        # Build player positions map for faster lookup
        player_positions = self._build_player_positions(cache)
        
        for tid, stats in team_stats.items():
            if stats["played"] < 5: continue
            
            # 1. Abstinence factor (max 25)
            # Find last matchday with penalty for this team
            last_p_md = 0
            for mid, detail in sorted(cache.items(), key=lambda x: (x[1].get("matchday") or 0), reverse=True):
                if any(g.get("team_id") == tid and g.get("type") == "PENALTY" for g in detail.get("goals", [])):
                    last_p_md = detail.get("matchday", 0)
                    break
            
            latest_md = max((d.get("matchday") or 0 for d in cache.values()), default=0)
            abstinence = latest_md - last_p_md
            f_ast = min(25, abstinence * 3)
            
            # 2. Attack factor (max 20)
            avg_g = (stats.get("goals_for", 0) / stats["played"]) if stats["played"] > 0 else 0
            f_att = min(20, round(avg_g * 10))
            
            # 3. Next opponent defense factor (max 20)
            nm = next_matches.get(tid)
            f_dif = 10 # Default
            opp_id = None
            if nm:
                # Find opponent stats
                opp_name = nm["opponent"]
                opp_id = next((sid for sid, s in team_stats.items() if s["team"] == opp_name), None)
                if opp_id and opp_id in team_stats:
                    opp_avg_g = (team_stats[opp_id].get("goals_against", 0) / team_stats[opp_id]["played"])
                    f_dif = min(20, round(opp_avg_g * 8))
            
            # 4. Referee factor (max 15)
            f_ref = 0
            ref_name = None
            ref_matches = 0
            ref_ppm = 0
            ref_cards_pm = 0
            has_referee = False
            if nm:
                ref_name = nm.get("referee")
                if ref_name and ref_name in ref_stats_map:
                    has_referee = True
                    rs = ref_stats_map[ref_name]
                    ref_matches = rs["matches"]
                    if ref_matches >= 3:  # Almeno 3 partite per essere significativo
                        ref_ppm = round(rs["penalties"] / ref_matches, 2)
                        ref_cards_pm = round((rs["yellows"] + rs["reds"]) / ref_matches, 1)
                        # Score: più rigori fischia, più alto il fattore
                        # Media Serie A ~0.25 rig/gara. >0.4 = molto propenso
                        if ref_ppm >= 0.5:
                            f_ref = 15
                        elif ref_ppm >= 0.4:
                            f_ref = 12
                        elif ref_ppm >= 0.3:
                            f_ref = 9
                        elif ref_ppm >= 0.2:
                            f_ref = 6
                        elif ref_ppm >= 0.1:
                            f_ref = 3
                        else:
                            f_ref = 1
                
            # 5. Home/Away factor (max 10)
            f_casa = 10 if nm and nm.get("is_home") else 5
            
            # 6. History factor (max 10)
            f_sto = min(10, round((stats["penalties_for"] / stats["played"]) * 15)) if stats["played"] > 0 else 0
            
            # Total score
            prob = f_ast + f_att + f_dif + f_ref + f_casa + f_sto
            
            # Penalty takers
            takers = {} # player_id -> {name, taken, scored}
            for mid, detail in cache.items():
                for g in detail.get("goals", []):
                    if g.get("team_id") == tid and g.get("type") == "PENALTY":
                        pid = g.get("scorer_id")
                        if pid:
                            if pid not in takers: takers[pid] = {"player": g.get("scorer", "Unknown"), "taken": 0, "scored": 0}
                            takers[pid]["taken"] += 1
                            takers[pid]["scored"] += 1
            
            # Sportmonks team-level advanced stats (with fuzzy team name match)
            team_low = stats["team"].lower()
            sm_team = sportmonks_team_stats.get(team_low)
            if not sm_team:
                # Try fuzzy: check if any SM team name contains or is contained in FD team name
                for sm_name, sm_data in sportmonks_team_stats.items():
                    # Strip common prefixes for matching
                    t1 = team_low.replace("fc ", "").replace("cf ", "").replace("club ", "").strip()
                    t2 = sm_name.replace("fc ", "").replace("cf ", "").replace("club ", "").strip()
                    if t1 in t2 or t2 in t1:
                        sm_team = sm_data
                        break
            team_adv = {}
            if sm_team and sm_team.get("players", 0) > 0:
                apps_avg = sm_team["total_apps"] / sm_team["players"] if sm_team["players"] > 0 else 1
                total_apps = sm_team["total_apps"] or 1
                team_adv = {
                    "fouls_drawn_pg": round(sm_team["fouls_drawn"] / total_apps * 11, 1),  # per 11 players per game
                    "dribbles_pg": round(sm_team["dribbles_success"] / total_apps * 11, 1),
                    "shots_pg": round(sm_team["shots_total"] / total_apps * 11, 1),
                    "big_chances": sm_team["big_chances_created"],
                    "key_passes_pg": round(sm_team["key_passes"] / total_apps * 11, 1),
                }

            results.append({
                "team_id": tid,
                "team": stats["team"],
                "abstinence": abstinence,
                "last_penalty": f"G{last_p_md}" if last_p_md > 0 else None,
                "penalties_total": stats["penalties_for"],
                "matches_played": stats["played"],
                "next_match": f"vs {nm['opponent']}" if nm else "",
                "has_referee": has_referee,
                "referee": ref_name,
                "referee_matches": ref_matches,
                "referee_ppm": ref_ppm,
                "referee_cards_pm": ref_cards_pm if has_referee else 0,
                "penalty_takers": sorted(takers.values(), key=lambda x: x["taken"], reverse=True),
                "factors": {
                    "astinenza": f_ast,
                    "attacco": f_att,
                    "difesa_avv": f_dif,
                    "arbitro": f_ref,
                    "casa": f_casa,
                    "storico": f_sto
                },
                "max_weights": max_weights,
                "probability_score": min(95, prob),
                "advanced_stats": team_adv,
            })
            
        return sorted(results, key=lambda x: x["probability_score"], reverse=True)

    def get_suspended_for_league(self, league_key: str):
        """Returns a dict of team_id -> list of suspended players."""
        league_code = LEAGUE_CODES.get(league_key)
        if not league_code: return {}
        cache = self._load_cache(league_code)
        if not cache: return {}
        
        suspensions = {}
        # Find matches from the last 2 matchdays to detect recent red cards
        sorted_matches = sorted(cache.items(), key=lambda x: (x[1].get("matchday") or 0), reverse=True)
        if not sorted_matches: return {}
        
        latest_md = sorted_matches[0][1].get("matchday", 0)
        # Simple logic: players with red card in the last matchday are suspended
        for mid, detail in sorted_matches:
            if detail.get("matchday", 0) < latest_md: break
            for card in detail.get("cards", []):
                if card.get("card") != "YELLOW":
                    tid = card.get("team_id")
                    if tid not in suspensions: suspensions[tid] = []
                    suspensions[tid].append({
                        "player": card.get("player"),
                        "reason": "Squalifica (Espulsione)"
                    })
        return suspensions

    @staticmethod
    def _load_sportmonks_team_stats() -> dict:
        """Load aggregated team stats from Sportmonks DB.
        Returns dict keyed by team_name.lower() with summed player stats."""
        import sqlite3
        result = {}
        try:
            conn = sqlite3.connect('data/betanalyzer.db', timeout=30)
            cursor = conn.cursor()
            cursor.execute("""
                SELECT pi.team_name,
                       COUNT(*) as num_players,
                       SUM(JSON_EXTRACT(psc.stats_json, '$.appearances')) as total_apps,
                       SUM(JSON_EXTRACT(psc.stats_json, '$.fouls_drawn')) as fouls_drawn,
                       SUM(JSON_EXTRACT(psc.stats_json, '$.dribbles_success')) as dribbles_success,
                       SUM(JSON_EXTRACT(psc.stats_json, '$.shots_total')) as shots_total,
                       SUM(JSON_EXTRACT(psc.stats_json, '$.big_chances_created')) as big_chances,
                       SUM(JSON_EXTRACT(psc.stats_json, '$.key_passes')) as key_passes
                FROM player_stats_cache psc
                JOIN player_info pi ON psc.player_id = pi.player_id
                WHERE JSON_EXTRACT(psc.stats_json, '$.appearances') > 0
                GROUP BY pi.team_name
            """)
            for row in cursor.fetchall():
                team_name, n_players, total_apps, fd, drib, shots, bc, kp = row
                if team_name:
                    result[team_name.lower()] = {
                        "players": int(n_players or 0),
                        "total_apps": int(total_apps or 0),
                        "fouls_drawn": int(fd or 0),
                        "dribbles_success": int(drib or 0),
                        "shots_total": int(shots or 0),
                        "big_chances_created": int(bc or 0),
                        "key_passes": int(kp or 0),
                    }
            conn.close()
        except Exception as e:
            logger.warning("Sportmonks team stats load error: %s", e)
        return result

    def _build_player_positions(self, cache):
        """Helper to build a map of player_id -> position from cache."""
        player_positions = {}
        for detail in cache.values():
            for pid, pos in detail.get("player_positions", {}).items():
                if pid not in player_positions:
                    player_positions[int(pid)] = pos
        return player_positions

    def get_score_frequency(self, league_code: str, limit: int = 5) -> list:
        cache = self._load_cache(league_code)
        if not cache: return []
        
        scores = {}
        total = 0
        for mid, d in cache.items():
            score_data = d.get("score", {})
            if not score_data: continue
            
            ft = score_data.get("fullTime", {})
            h, a = ft.get("home"), ft.get("away")
            
            if h is not None and a is not None:
                res = f"{h}-{a}"
                scores[res] = scores.get(res, 0) + 1
                total += 1
        
        if total == 0: return []
        
        sorted_scores = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        results = []
        for res, count in sorted_scores[:limit]:
            results.append({
                "score": res,
                "count": count,
                "percentage": round((count / total) * 100, 1)
            })
        return results

    def get_referee_data_for_league(self, league_key: str) -> dict:
        """Restituisce dati arbitro, diffidati e rigoristi per i match imminenti."""
        from .penalties import LEAGUE_CODES
        league_code = LEAGUE_CODES.get(league_key)
        if not league_code: return {}
        
        cache = self._load_cache(league_code)
        if not cache: return {}
        
        standings = self._get_standings(league_code)
        standings_map = {s["team_id"]: s for s in standings}
        
        # Statistiche Arbitri
        ref_stats_map = {} # name -> {matches, yellows, reds, penalties}
        for mid, detail in cache.items():
            for ref in detail.get("referees", []):
                name = ref.get("name")
                if not name: continue
                if name not in ref_stats_map:
                    ref_stats_map[name] = {"matches": 0, "yellows": 0, "reds": 0, "penalties": 0}
                ref_stats_map[name]["matches"] += 1
                ref_stats_map[name]["yellows"] += sum(1 for c in detail.get("cards", []) if c.get("card") == "YELLOW")
                ref_stats_map[name]["reds"] += sum(1 for c in detail.get("cards", []) if c.get("card") != "YELLOW")
                ref_stats_map[name]["penalties"] += sum(1 for g in detail.get("goals", []) if g.get("type") == "PENALTY")

        # Rigoristi e Diffidati
        current_matchday = max((m.get("matchday") or 0 for m in cache.values()), default=30)
        diffidati_map = self._build_diffida_status(cache, league_code, current_matchday)
        
        results = {}
        # Cerchiamo i match imminenti per associare l'arbitro
        scheduled = self._get(f"/competitions/{league_code}/matches", params={"status": "SCHEDULED,TIMED"})
        if scheduled:
            for m in scheduled.get("matches", []):
                h_name, a_name = m["homeTeam"]["name"], m["awayTeam"]["name"]
                h_id, a_id = m["homeTeam"]["id"], m["awayTeam"]["id"]
                
                ref_name = None
                for ref in m.get("referees", []):
                    ref_name = ref.get("name")
                    break
                
                stats = {"matches": 0, "cards_per_match": 0, "penalties_per_match": 0}
                if ref_name and ref_name in ref_stats_map:
                    rs = ref_stats_map[ref_name]
                    stats = {
                        "matches": rs["matches"],
                        "cards_per_match": round((rs["yellows"] + rs["reds"]) / rs["matches"], 2),
                        "penalties_per_match": round(rs["penalties"] / rs["matches"], 2)
                    }

                # Diffidati (tentiamo di recuperare i nomi reali)
                h_diff = []
                a_diff = []
                # (Miglioreremo questo punto in seguito con un lookup nomi)

                results[(h_name, a_name)] = {
                    "referee": ref_name or "N/D",
                    "stats": stats,
                    "home_diffidati": [], # Placeholder se non abbiamo nomi
                    "away_diffidati": [],
                    "home_position": standings_map.get(h_id, {}).get("position", 0),
                    "away_position": standings_map.get(a_id, {}).get("position", 0)
                }
        return results

    def _build_diffida_status(self, cache, league_code, current_matchday):
        """Builds a map of player_id -> (num_yellows, in_diffida)."""
        player_yellows = {}
        # Rules for top leagues: 5 yellows = 1 match suspension, then 10, 15...
        # Diffida is when player has 4, 9, 14... yellows.
        for mid, detail in cache.items():
            for card in detail.get("cards", []):
                if card.get("card") == "YELLOW":
                    pid = card.get("player_id")
                    if pid:
                        player_yellows[pid] = player_yellows.get(pid, 0) + 1
        
        diffida_status = {}
        for pid, count in player_yellows.items():
            # A bit simplified: diffida at 4, 9, 14...
            in_diffida = (count % 5 == 4)
            diffida_status[pid] = {"yellows": count, "diffidato": in_diffida}
        return diffida_status
