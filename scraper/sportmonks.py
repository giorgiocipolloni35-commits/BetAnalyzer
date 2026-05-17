import logging
import requests
import json
from pathlib import Path
from datetime import datetime, timedelta
from models.match import Match, BookmakerOdds

logger = logging.getLogger(__name__)

class SportmonksClient:
    def __init__(self, api_key: str, cache_minutes: int = 30):
        self.api_key = api_key
        self.cache_minutes = cache_minutes
        self.cache_dir = Path(__file__).parent.parent / "cache"
        self.cache_dir.mkdir(exist_ok=True)
        self.base_url = "https://api.sportmonks.com/v3/football"
        self.league_map = {
            "italy_serie_a":           "384",
            "england_premier_league":  "8",
            "spain_la_liga":           "564",
            "germany_bundesliga":      "82",
            "france_ligue_1":          "301",
            "denmark_superliga":       "271",
            "scotland_premiership":    "501",
            "netherlands_eredivisie":  "72",
            "champions_league":        "2",
            "england_championship":    "9",
            "portugal_primeira_liga":  "462",
        }

    def get_all_matches(self, league_keys: list[str] = None) -> list[Match]:
        cache_file = self.cache_dir / "sportmonks_matches.json"
        
        # Check cache
        if cache_file.exists() and not league_keys:
            mtime = datetime.fromtimestamp(cache_file.stat().st_mtime)
            if datetime.now() - mtime < timedelta(minutes=self.cache_minutes):
                logger.info("Caricamento dati Sportmonks da cache")
                with open(cache_file, "r") as f:
                    data = json.load(f)
                    return [Match.from_dict(m) for m in data]

        logger.info("Recupero dati da Sportmonks API...")
        
        if not league_keys:
            logger.warning("Nessuna lega selezionata per Sportmonks")
            return []

        selected_ids = [self.league_map[k] for k in league_keys if k in self.league_map]
        
        if not selected_ids:
            logger.warning("Nessun ID valido trovato per le leghe selezionate su Sportmonks")
            return []

        logger.info(f"Leghe selezionate (ID): {', '.join(selected_ids)}")

        # 2. Recupera i fixture per i prossimi 7 giorni
        start = datetime.now().strftime("%Y-%m-%d")
        end = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")
        
        matches = []
        try:
            # Usiamo l'endpoint fixtures/between
            url = f"{self.base_url}/fixtures/between/{start}/{end}"
            params = {
                "api_token": self.api_key,
                "include": "participants;league",
                "filters": f"fixtureLeagues:{','.join(selected_ids)}"
            }
            # Aggiungiamo esplicitamente i mercati che vogliamo se possibile, 
            # ma l'include odds.market di solito li prende tutti.
            # Alcuni piani Sportmonks richiedono di specificare i market IDs nelle fiches.
            
            r = requests.get(url, params=params)
            r.raise_for_status()
            res_json = r.json()
            fixtures = res_json.get("data", [])
            
            if not fixtures and "No result(s) found" in res_json.get("message", ""):
                logger.warning("Sportmonks: Nessun risultato trovato. Potrebbe essere un limite del tuo piano (Free Plan).")
            
            for f in fixtures:
                m = self._parse_fixture(f)
                if m:
                    matches.append(m)
                    
        except Exception as e:
            logger.error(f"Errore recupero fixtures Sportmonks: {e}")
            if "403" in str(e) or "400" in str(e):
                logger.warning("Possibile problema di sottoscrizione o parametri con Sportmonks.")

        # Salva in cache
        with open(cache_file, "w") as f:
            json.dump([m.to_dict() for m in matches], f, indent=2)
            
        return matches

    def _parse_fixture(self, f) -> Match:
        try:
            fixture_id = str(f["id"])
            home_team = ""
            away_team = ""
            home_id = None
            away_id = None
            
            participants = f.get("participants", [])
            for p in participants:
                if p.get("meta", {}).get("location") == "home":
                    home_team = p.get("name")
                    home_id = str(p.get("id"))
                else:
                    away_team = p.get("name")
                    away_id = str(p.get("id"))
            
            if not home_team or not away_team:
                # Se non troviamo meta location, proviamo per ordine
                if len(participants) >= 2:
                    home_team = participants[0].get("name")
                    home_id = str(participants[0].get("id"))
                    away_team = participants[1].get("name")
                    away_id = str(participants[1].get("id"))
                else:
                    return None

            # Parsing Absentees (Infortuni/Squalifiche)
            absentees_data = []
            raw_absentees = f.get("absentees", [])
            for a in raw_absentees:
                p_name = a.get("player", {}).get("display_name", "Unknown Player")
                reason = a.get("reason", {}).get("name", "Unknown Reason")
                team_id_abs = str(a.get("team_id"))
                loc = "home" if team_id_abs == home_id else "away"
                absentees_data.append({
                    "player": p_name,
                    "reason": reason,
                    "team": loc
                })

            match = Match(
                id=f"sm_{fixture_id}",
                league=f.get("league", {}).get("name", "Unknown"),
                league_id=str(f.get("league_id")),
                season_id=str(f.get("season_id")),
                home_team=home_team,
                away_team=away_team,
                commence_time=f.get("starting_at"),
                home_id=home_id,
                away_id=away_id,
                absentees=absentees_data
            )

            # Parsing Odds (solo se disponibili)
            odds_list = f.get("odds", [])
            if not odds_list or not isinstance(odds_list, list):
                return match

            # Struttura per raccogliere le quote raggruppate (come OddsAPI)
            # Ma qui abbiamo una lista piatta di tutte le quote di vari bookmaker
            # Per semplicità prendiamo la migliore quota per ogni esito
            
            best_odds = {
                "h2h": {"1": 0.0, "X": 0.0, "2": 0.0},
                "totals": {"Over": 0.0, "Under": 0.0},
                "btts": {"Yes": 0.0, "No": 0.0},
                "double_chance": {"1X": 0.0, "12": 0.0, "X2": 0.0},
                "dnb": {"1": 0.0, "2": 0.0}
            }

            for o in odds_list:
                market = o.get("market", {})
                market_name = market.get("name", "").lower()
                label = str(o.get("label", "")).strip()
                try:
                    value = float(o.get("value", 0))
                except:
                    continue

                # 1X2 (ID 1: Fulltime Result)
                m_id = market.get("id")
                if m_id == 1 or "3way result" in market_name or "full time result" in market_name:
                    lbl = label.lower()
                    if lbl in ["home", "1"]: best_odds["h2h"]["1"] = max(best_odds["h2h"]["1"], value)
                    elif lbl in ["draw", "x"]: best_odds["h2h"]["X"] = max(best_odds["h2h"]["X"], value)
                    elif lbl in ["away", "2"]: best_odds["h2h"]["2"] = max(best_odds["h2h"]["2"], value)
                
                # Over/Under 2.5 (ID 14)
                elif m_id == 14 or "over/under" in market_name:
                    total = str(o.get("total", ""))
                    if total == "2.5":
                        lbl = label.lower()
                        if "over" in lbl: best_odds["totals"]["Over"] = max(best_odds["totals"]["Over"], value)
                        elif "under" in lbl: best_odds["totals"]["Under"] = max(best_odds["totals"]["Under"], value)

                # BTTS (ID 80)
                elif m_id == 80 or "both teams to score" in market_name:
                    lbl = label.lower()
                    if lbl == "yes": best_odds["btts"]["Yes"] = max(best_odds["btts"]["Yes"], value)
                    elif lbl == "no": best_odds["btts"]["No"] = max(best_odds["btts"]["No"], value)

                # Double Chance (ID 12)
                elif m_id == 12 or "double chance" in market_name:
                    if "1X" in label: best_odds["double_chance"]["1X"] = max(best_odds["double_chance"]["1X"], value)
                    elif "X2" in label: best_odds["double_chance"]["X2"] = max(best_odds["double_chance"]["X2"], value)
                    elif "12" in label: best_odds["double_chance"]["12"] = max(best_odds["double_chance"]["12"], value)

                # Draw No Bet (ID 9)
                elif m_id == 9 or "draw no bet" in market_name:
                    lbl = label.lower()
                    if lbl in ["home", "1"]: best_odds["dnb"]["1"] = max(best_odds["dnb"]["1"], value)
                    elif lbl in ["away", "2"]: best_odds["dnb"]["2"] = max(best_odds["dnb"]["2"], value)

            # Converti in oggetti BookmakerOdds
            sm_odds = BookmakerOdds(
                bookmaker="Sportmonks",
                home=best_odds["h2h"]["1"] if best_odds["h2h"]["1"] > 0 else None,
                draw=best_odds["h2h"]["X"] if best_odds["h2h"]["X"] > 0 else None,
                away=best_odds["h2h"]["2"] if best_odds["h2h"]["2"] > 0 else None,
                over25=best_odds["totals"]["Over"] if best_odds["totals"]["Over"] > 0 else None,
                under25=best_odds["totals"]["Under"] if best_odds["totals"]["Under"] > 0 else None,
                gg=best_odds["btts"]["Yes"] if best_odds["btts"]["Yes"] > 0 else None,
                ng=best_odds["btts"]["No"] if best_odds["btts"]["No"] > 0 else None,
                double_chance=best_odds["double_chance"],
                dnb=best_odds["dnb"]
            )
            
            if any([sm_odds.home, sm_odds.over25, sm_odds.gg, sm_odds.double_chance["1X"]]):
                match.odds = [sm_odds]
                
            return match
        except Exception as e:
            logger.error(f"Errore parsing fixture Sportmonks: {e}")
            return None

    def get_head_to_head(self, team1_id: str, team2_id: str, limit: int = 10) -> list[dict]:
        """Recupera gli ultimi scontri diretti tra due squadre."""
        if not team1_id or not team2_id:
            return []
        try:
            url = f"{self.base_url}/fixtures/head-to-head/{team1_id}/{team2_id}"
            params = {
                "api_token": self.api_key,
                "include": "participants;scores",
                "per_page": limit,
            }
            r = requests.get(url, params=params, timeout=10)
            r.raise_for_status()
            data = r.json().get("data", [])

            results = []
            for f in data:
                parts = f.get("participants", [])
                h_name, a_name, h_id, a_id = "?", "?", None, None
                for p in parts:
                    if p.get("meta", {}).get("location") == "home":
                        h_name = p.get("name", "?")
                        h_id = str(p.get("id"))
                    else:
                        a_name = p.get("name", "?")
                        a_id = str(p.get("id"))

                h_goals, a_goals = 0, 0
                for s in f.get("scores", []):
                    if s.get("description") == "CURRENT":
                        if s["score"]["participant"] == "home":
                            h_goals = s["score"].get("goals", 0)
                        else:
                            a_goals = s["score"].get("goals", 0)

                date_str = (f.get("starting_at") or "")[:10]
                results.append({
                    "date": date_str,
                    "home": h_name,
                    "away": a_name,
                    "home_goals": h_goals,
                    "away_goals": a_goals,
                    "score": f"{h_goals}-{a_goals}",
                })

            logger.info(f"H2H {team1_id} vs {team2_id}: {len(results)} scontri diretti")
            return results
        except Exception as e:
            logger.error(f"Errore recupero H2H {team1_id} vs {team2_id}: {e}")
            return []

    def get_last_results(self, team_id: str, limit: int = 3) -> list:
        """Recupera gli ultimi N risultati di una squadra usando l'endpoint 'between' di v3."""
        if not team_id:
            return []
        try:
            from datetime import datetime, timedelta
            end_date = datetime.now().strftime("%Y-%m-%d")
            start_date = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")
            
            url = f"https://api.sportmonks.com/v3/football/fixtures/between/{start_date}/{end_date}/{team_id}"
            params = {
                "api_token": self.api_key,
                "include": "participants;scores",
                "order": "desc" # Prendi i più recenti prima
            }
            r = requests.get(url, params=params)
            r.raise_for_status()
            data = r.json().get("data", [])
            
            # Filtra solo quelli FINITI (che hanno score CURRENT)
            finished = []
            for f in data:
                scores = f.get("scores", [])
                is_finished = any(s.get("description") == "CURRENT" for s in scores)
                if is_finished:
                    finished.append(f)
            
            results = []
            for f in finished[:limit]:
                parts = f.get("participants", [])
                h_name, a_name = "?", "?"
                for p in parts:
                    if p.get("meta", {}).get("location") == "home": h_name = p.get("name")
                    else: a_name = p.get("name")
                
                h_score, a_score = 0, 0
                scores = f.get("scores", [])
                for s in scores:
                    if s.get("description") == "CURRENT":
                        participant = s.get("score", {}).get("participant")
                        goals = s.get("score", {}).get("goals", 0)
                        if participant == "home": h_score = goals
                        else: a_score = goals
                
                res_str = f"{h_name} {h_score}-{a_score} {a_name}"
                
                outcome = "D"
                is_home = False
                for p in parts:
                    if str(p.get("id")) == str(team_id) and p.get("meta", {}).get("location") == "home":
                        is_home = True
                        break
                
                if h_score > a_score: outcome = "W" if is_home else "L"
                elif a_score > h_score: outcome = "L" if is_home else "W"
                
                results.append({"text": res_str, "outcome": outcome, "date": f.get("starting_at")})
            return results
        except Exception as e:
            logger.error(f"Errore recupero ultimi risultati team {team_id}: {e}")
            return []

    def get_standings(self, league_id: str, season_id: str = None) -> list[dict]:
        """Recupera la classifica per un campionato usando il formato flat v3 verificato via curl."""
        try:
            if not season_id: return []
            
            # Includiamo 'details' per avere le statistiche complete (DR, partite giocate, ecc.)
            params = {
                "api_token": self.api_key,
                "include": "participant;details"
            }
            
            # Rimosso il prefisso duplicato /football/
            url = f"{self.base_url}/standings/seasons/{season_id}"
            logger.info(f"Richiesta classifica Season (Full Stats): {url}")
            r = requests.get(url, params=params, timeout=10)
            res_json = r.json()
            
            data = res_json.get("data", [])
            if not data: 
                logger.warning(f"Nessun dato classifica per Season {season_id}")
                return []
            
            # Sportmonks v3 restituisce una lista piatta.
            # Poiché possono esserci più stage (Regular Season, Playoff),
            # raggruppiamo per team_id e prendiamo l'entry con più punti (la più recente).
            team_map = {}
            for row in data:
                team = row.get("participant", {})
                if not team: continue
                
                tid = str(team.get("id"))
                points = row.get("points", 0)
                
                # Se abbiamo già questo team, sovrascriviamo solo se l'entry attuale ha più punti
                # (indice di uno stage più avanzato)
                if tid not in team_map or points > team_map[tid]["points"]:
                    team_map[tid] = {
                        "team_id": tid,
                        "team_name": team.get("name"),
                        "position": row.get("position"),
                        "points": points,
                        "played": row.get("overall", {}).get("games_played", 0),
                        "goals_diff": row.get("overall", {}).get("goals_diff", 0),
                        "stage_id": row.get("stage_id")
                    }
            
            # Convertiamo la mappa in una lista ordinata per PUNTI (descendente)
            # In questo modo, anche se ci sono più stage, vediamo chi ha più punti in cima.
            standings = list(team_map.values())
            standings.sort(key=lambda x: x["points"], reverse=True)
            
            logger.info(f"Recuperate {len(standings)} posizioni in classifica (filtrate per stage).")
            return standings
        except Exception as e:
            logger.error(f"Errore critico recupero classifica: {e}")
            return []

    def get_squad(self, team_id: str) -> list[dict]:
        """Recupera la rosa attuale di una squadra da Sportmonks v3."""
        if not team_id: return []
        try:
            url = f"{self.base_url}/teams/{team_id}"
            params = {
                "api_token": self.api_key,
                "include": "squad.player"
            }
            r = requests.get(url, params=params, timeout=10)
            r.raise_for_status()
            data = r.json().get("data", {})
            squad_raw = data.get("squad", [])
            
            players = []
            for item in squad_raw:
                p = item.get("player", {})
                if not p: continue
                
                # Mappatura ruoli v3 -> Semplice
                pos_id = p.get("position_id")
                # Sportmonks IDs: 24=G, 25=D, 26=M, 27=A (approssimativo)
                role = "Unknown"
                if pos_id == 24: role = "Goalkeeper"
                elif pos_id == 25: role = "Defence"
                elif pos_id == 26: role = "Midfield"
                elif pos_id == 27: role = "Offence"
                
                players.append({
                    "id": p.get("id"),
                    "name": p.get("display_name") or p.get("name"),
                    "position": role,
                    "team_id": team_id,
                    "position_id": pos_id
                })
            return players
        except Exception as e:
            logger.error(f"Errore recupero squad team {team_id}: {e}")
            return []

    def get_official_lineups(self, fixture_id: str) -> dict:
        """Recupera le formazioni ufficiali per un fixture specifico."""
        if not fixture_id: return {}
        # Rimuovi prefisso "sm_" se presente
        clean_id = str(fixture_id).replace("sm_", "")
        try:
            url = f"{self.base_url}/fixtures/{clean_id}"
            params = {
                "api_token": self.api_key,
                "include": "lineups.player;participants"
            }
            r = requests.get(url, params=params, timeout=10)
            r.raise_for_status()
            data = r.json().get("data", {})
            lineups_raw = data.get("lineups", [])
            participants = data.get("participants", [])
            
            # Identificazione più robusta home/away
            home_id = None
            away_id = None
            for p in participants:
                loc = p.get("meta", {}).get("location")
                if loc == "home": home_id = p["id"]
                elif loc == "away": away_id = p["id"]
            
            # Fallback: se meta.location manca, prendiamo i primi due in ordine (di solito home è il primo)
            if not home_id and len(participants) >= 2:
                home_id = participants[0]["id"]
                away_id = participants[1]["id"]

            result = {"home": [], "away": [], "formation": {"home": data.get("formation_home"), "away": data.get("formation_away")}}
            
            for l in lineups_raw:
                if l.get("type_id") != 11: continue
                
                player = l.get("player", {})
                pos_id = player.get("position_id", 5) # 1=GK, 2=DEF, 3=MID, 4=ATK
                
                p_data = {
                    "id": player.get("id"),
                    "name": player.get("display_name") or player.get("name") or "Unknown",
                    "role_id": pos_id,
                    "number": l.get("jersey_number"),
                    "season_id": data.get("season_id")
                }
                
                if l.get("team_id") == home_id:
                    result["home"].append(p_data)
                elif l.get("team_id") == away_id:
                    result["away"].append(p_data)
            
            # Ordinamento tattico per ID posizione
            result["home"].sort(key=lambda x: x["role_id"])
            result["away"].sort(key=lambda x: x["role_id"])
            
            logger.info(f"Lineups recuperate e ordinate: Home={len(result['home'])}, Away={len(result['away'])}")
            return result
        except Exception as e:
            logger.error(f"Errore recupero lineups fixture {fixture_id}: {e}")
            return {}

    def get_coach_info(self, team_id: str) -> dict | None:
        """
        Recupera info sul coach attuale di una squadra.

        Returns:
            {
                "coach_name": str,
                "coach_id": int,
                "start_date": str (YYYY-MM-DD),
                "days_in_charge": int,
                "temporary": bool,
                "previous_coaches": int,  # cambi negli ultimi 2 anni
            }
            oppure None se non trovato.
        """
        if not team_id:
            return None
        try:
            url = f"{self.base_url}/teams/{team_id}"
            params = {
                "api_token": self.api_key,
                "include": "coaches.coach",
            }
            r = requests.get(url, params=params, timeout=10)
            r.raise_for_status()
            data = r.json().get("data", {})
            coaches = data.get("coaches", [])
            if not coaches:
                return None

            # Coach attivo
            active = next((c for c in coaches if c.get("active")), None)
            if not active:
                return None

            coach_data = active.get("coach", {})
            coach_name = (
                coach_data.get("display_name")
                or coach_data.get("common_name")
                or coach_data.get("name")
                or "Sconosciuto"
            )

            start_str = active.get("start", "")
            days_in_charge = 0
            if start_str:
                from datetime import datetime, date
                try:
                    start_date = datetime.strptime(start_str, "%Y-%m-%d").date()
                    days_in_charge = (date.today() - start_date).days
                except ValueError:
                    pass

            # Conta cambi allenatore negli ultimi 2 anni
            from datetime import datetime, date, timedelta
            two_years_ago = date.today() - timedelta(days=730)
            recent_changes = 0
            for c in coaches:
                c_start = c.get("start", "")
                if c_start:
                    try:
                        cs = datetime.strptime(c_start, "%Y-%m-%d").date()
                        if cs >= two_years_ago:
                            recent_changes += 1
                    except ValueError:
                        pass

            return {
                "coach_name": coach_name,
                "coach_id": active.get("coach_id"),
                "start_date": start_str,
                "days_in_charge": days_in_charge,
                "temporary": active.get("temporary", False),
                "previous_coaches": max(0, recent_changes - 1),  # escludi l'attuale
            }
        except Exception as e:
            logger.error(f"Errore recupero coach team {team_id}: {e}")
            return None

    def _make_request(self, endpoint, params=None):
        import time
        url = f"{self.base_url}/{endpoint}"
        all_params = {"api_token": self.api_key}
        if params:
            all_params.update(params)
            
        max_retries = 7
        retry_delay = 15 # Secondi iniziali più generosi
        
        for attempt in range(max_retries):
            try:
                response = requests.get(url, params=all_params)
                
                if response.status_code == 429:
                    wait = retry_delay * (attempt + 1)
                    if attempt >= 3:
                        wait = 60 # Dopo 3 fallimenti, aspetta un minuto intero
                    print(f"\n[RATE LIMIT] Superato limite Sportmonks. Attesa di {wait} secondi (Tentativo {attempt+1}/{max_retries})...")
                    time.sleep(wait)
                    continue
                    
                response.raise_for_status()
                return response.json()
            except requests.exceptions.RequestException as e:
                if attempt == max_retries - 1:
                    print(f"Errore API Sportmonks dopo {max_retries} tentativi: {e}")
                    return None
                time.sleep(2)
        return None

    def _remove_accents(self, input_str):
        import unicodedata
        nfkd_form = unicodedata.normalize('NFKD', input_str)
        return "".join([c for c in nfkd_form if not unicodedata.combining(c)])

    def search_player(self, name: str, dob: str = None) -> list[dict]:
        """Cerca un giocatore per nome su Sportmonks con fallback multipli.

        Strategia di ricerca (si ferma al primo risultato):
        1. Nome completo originale
        2. Nome senza accenti
        3. Nome senza trattini (Akpa-Akpro → Akpa Akpro)
        4. Nome invertito (Hwang Heechan → Heechan Hwang) — nomi asiatici
        5. Solo cognome con filtro nome
        6. Solo primo nome (per cognomi composti/diversi tra API)
        7. Se DOB disponibile: cognome + filtro data nascita (catch-all robusto)
        """
        import urllib.parse

        clean_name = self._remove_accents(name)
        name_parts = clean_name.split()

        # Helper: cerca e ritorna se trovato
        def _try_search(query):
            safe_q = urllib.parse.quote(query, safe='')
            data = self._make_request(f"players/search/{safe_q}")
            if data and data.get("data"):
                return data["data"]
            return None

        def _name_overlaps(sm_player, ref_name_clean):
            """Check if SM player name overlaps with reference name."""
            sm_full = self._remove_accents(sm_player.get("name", "")).lower()
            sm_disp = self._remove_accents(sm_player.get("display_name", "")).lower()
            ref = ref_name_clean.lower()
            ref_parts = set(ref.split())
            sm_parts = set(sm_full.split()) | set(sm_disp.split())
            # At least 1 significant part (>2 chars) must overlap
            overlap = ref_parts & sm_parts
            sig_overlap = [w for w in overlap if len(w) > 2]
            return len(sig_overlap) >= 1

        def _filter_by_dob(results, date_of_birth):
            """Filter results by date of birth if available."""
            if not date_of_birth:
                return results
            return [p for p in results if p.get("date_of_birth") == date_of_birth]

        # 1. Nome completo originale
        res = _try_search(name)
        if res:
            if dob:
                dob_match = _filter_by_dob(res, dob)
                if dob_match:
                    return dob_match
            return res

        # 2. Senza accenti
        if clean_name != name:
            res = _try_search(clean_name)
            if res:
                if dob:
                    dob_match = _filter_by_dob(res, dob)
                    if dob_match:
                        return dob_match
                return res

        # 3. Senza trattini (Akpa-Akpro → Akpa Akpro, N'Soki → NSoki)
        no_hyphen = clean_name.replace("-", " ").replace("'", "")
        if no_hyphen != clean_name:
            res = _try_search(no_hyphen)
            if res:
                if dob:
                    dob_match = _filter_by_dob(res, dob)
                    if dob_match:
                        return dob_match
                return res

        # 4. Nome invertito (per nomi asiatici: Hwang Heechan → Heechan Hwang)
        if len(name_parts) == 2:
            reversed_name = f"{name_parts[1]} {name_parts[0]}"
            res = _try_search(reversed_name)
            if res:
                if dob:
                    dob_match = _filter_by_dob(res, dob)
                    if dob_match:
                        return dob_match
                # Filter by name overlap
                filtered = [p for p in res if _name_overlaps(p, clean_name)]
                if filtered:
                    return filtered
                return res

        # 5. Solo cognome con filtro nome
        if len(name_parts) > 1:
            last_name = name_parts[-1]
            if len(last_name) > 2:  # Skip short surnames
                res = _try_search(last_name)
                if res:
                    # Prefer DOB match
                    if dob:
                        dob_match = _filter_by_dob(res, dob)
                        if dob_match:
                            return dob_match
                    # Filter by name overlap
                    filtered = [p for p in res if _name_overlaps(p, clean_name)]
                    if filtered:
                        return filtered

        # 6. Solo primo nome (per cognomi completamente diversi tra API, es. Silas Katompa → Silas)
        if len(name_parts) > 1:
            first_name = name_parts[0]
            if len(first_name) > 3:  # Skip very short first names
                res = _try_search(first_name)
                if res:
                    if dob:
                        dob_match = _filter_by_dob(res, dob)
                        if dob_match:
                            return dob_match

        # 7. Ogni parte del nome individualmente con filtro DOB (ultimo tentativo)
        if dob and len(name_parts) > 2:
            for part in name_parts:
                if len(part) <= 2:
                    continue
                res = _try_search(part)
                if res:
                    dob_match = _filter_by_dob(res, dob)
                    if dob_match:
                        return dob_match

        return []

    def get_player_stats(self, player_id: int, season_id: int, team_id: int = None, team_name: str = None) -> dict:
        """Recupera le statistiche dettagliate filtrando STRETTAMENTE per squadra (per nome o ID)"""
        from db.database import get_cached_player_stats, save_player_stats_cache
        
        # 1. Prova dalla cache
        if team_id:
            cached = get_cached_player_stats(player_id, season_id, team_id)
            if cached:
                return cached
            
        try:
            url = f"{self.base_url}/players/{player_id}"
            # Includiamo la squadra per poter filtrare per nome
            params = {"api_token": self.api_key, "include": "statistics.details.type;statistics.team;statistics.season"}
            r = requests.get(url, params=params, timeout=10)
            r.raise_for_status()
            full_data = r.json().get("data", {})
            stats_list = full_data.get("statistics", [])
            
            # LOGICA DI FILTRO STRETTA PER SQUADRA
            season_stats = None
            if team_name:
                # Cerchiamo la riga dove il nome della squadra contiene quello cercato (es. "Milan" o "Roma")
                tn_lower = team_name.lower()
                season_stats = next((s for s in stats_list if s.get("season_id") == season_id and tn_lower in s.get("team", {}).get("name", "").lower()), None)
            
            # Se non abbiamo il nome o non abbiamo trovato nulla, proviamo con l'ID (se fornito)
            if not season_stats and team_id:
                season_stats = next((s for s in stats_list if s.get("season_id") == season_id and s.get("team_id") == team_id), None)

            # Se ancora nulla, prendiamo l'ultima riga della stagione (Fallback estremo)
            if not season_stats:
                season_stats = next((s for s in stats_list if s.get("season_id") == season_id), None)
            
            if not season_stats: return {}
            
            details = season_stats.get("details", [])
            result = {
                "shots_on_target": 0, "shots_total": 0, "goals": 0, "assists": 0,
                "fouls_committed": 0, "fouls_drawn": 0, "appearances": 0,
                "interceptions": 0, "tackles": 0, "blocks": 0, "clearances": 0, "aerials_won": 0,
                "accurate_passes_pct": 0, "key_passes": 0, "through_balls": 0, "long_balls": 0,
                "dribbles_success": 0, "dribbles_attempts": 0, "big_chances_created": 0,
                "saves": 0, "goals_conceded": 0, "clean_sheets": 0,
                "saves_insidebox": 0, "errors_lead_to_goal": 0, "sm_rating": 0,
            }
            
            for d in details:
                type_obj = d.get("type", {})
                tname = type_obj.get("name", "").lower()
                tid = d.get("type_id")
                
                val = d.get("value", {})
                v = val.get("all", val.get("total", val.get("count", val.get("average", 0))))
                if isinstance(v, dict): v = v.get("total", 0)
                if v is None: v = 0
                if "appearances" in tname or tid in [311, 321, 322]: result["appearances"] = max(result["appearances"], v)
                elif "shots on target" in tname or tid == 86: result["shots_on_target"] = max(result["shots_on_target"], v)
                elif "shots total" in tname or tid == 85: result["shots_total"] = max(result["shots_total"], v)
                elif "goals" == tname or tid == 52: result["goals"] = max(result["goals"], v)
                elif "assists" == tname or tid == 79: result["assists"] = max(result["assists"], v)
                elif "fouls committed" in tname or "fouls" == tname or tid in [56, 75]: result["fouls_committed"] = max(result["fouls_committed"], v)
                elif "fouls drawn" in tname or tid in [96, 76]: result["fouls_drawn"] = max(result["fouls_drawn"], v)
                elif "intercept" in tname: result["interceptions"] = max(result["interceptions"], v)
                elif "tackle" in tname: result["tackles"] = max(result["tackles"], v)
                elif "block" in tname: result["blocks"] = max(result["blocks"], v)
                elif "clearance" in tname: result["clearances"] = max(result["clearances"], v)
                elif "aerials won" in tname: result["aerials_won"] = max(result["aerials_won"], v)
                elif "accurate passes percentage" in tname: result["accurate_passes_pct"] = v
                elif "key passes" in tname: result["key_passes"] = max(result["key_passes"], v)
                elif "through_balls" in tname: result["through_balls"] = max(result["through_balls"], v)
                elif "long balls" in tname: result["long_balls"] = max(result["long_balls"], v)
                elif "successful dribbles" in tname: result["dribbles_success"] = max(result["dribbles_success"], v)
                elif "dribble attempts" in tname: result["dribbles_attempts"] = max(result["dribbles_attempts"], v)
                elif "big chances created" in tname: result["big_chances_created"] = max(result["big_chances_created"], v)
                # GK stats
                elif "saves" == tname or tid == 57: result["saves"] = max(result["saves"], v)
                elif "saves insidebox" in tname or tid == 104: result["saves_insidebox"] = max(result["saves_insidebox"], v)
                elif "goals conceded" in tname or tid == 88: result["goals_conceded"] = max(result["goals_conceded"], v)
                elif "cleansheets" in tname or "clean sheet" in tname or tid == 194: result["clean_sheets"] = max(result["clean_sheets"], v)
                elif "error lead to goal" in tname or tid == 571: result["errors_lead_to_goal"] = max(result["errors_lead_to_goal"], v)
                elif tid == 118:  # Rating
                    avg_rating = val.get("average", 0)
                    if avg_rating: result["sm_rating"] = avg_rating

            if team_id:
                # --- CALCOLO RATING (VOTO 1-100) ---
                from logic.roster import calculate_player_rating
                # Cerchiamo il ruolo (position_id) nei dati del giocatore
                pos_id = full_data.get("position_id", 0)
                
                rating = calculate_player_rating(result, pos_id)
                from db.database import save_player_stats_cache
                save_player_stats_cache(player_id, season_id, team_id, result, rating)
            
            return result

        except Exception as e:
            logger.error(f"Errore recupero stats player {player_id}: {e}")
            return {}

    def get_quota_usage(self) -> dict:
        # Sportmonks non ha un endpoint facile per la quota residua in v3 senza headers specifici
        return {"remaining": "Unlimited", "used": "N/A"}
