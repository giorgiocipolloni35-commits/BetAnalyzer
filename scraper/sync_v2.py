import os
import json
import time
import logging
import sqlite3
from datetime import datetime, timezone
from dotenv import load_dotenv
from scraper.sportmonks import SportmonksClient
from logic.roster import RosterManager
from db.database import save_player_info

# Configurazione Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
logger = logging.getLogger(__name__)

load_dotenv()

def run_smart_sync():
    api_key_fd = os.getenv('FOOTBALL_DATA_API_KEY')
    api_key_sm = os.getenv('SPORTMONKS_API_KEY')
    
    rm = RosterManager(api_key_fd)
    sm = SportmonksClient(api_key_sm)
    
    # PARTIAMO DA QUI (La Liga, Bundesliga, Ligue 1)
    LEAGUES_TO_SYNC = [
        {"id_fd": 2014, "name": "La Liga", "sm_season": 25659},
        {"id_fd": 2002, "name": "Bundesliga", "sm_season": 25646},
        {"id_fd": 2015, "name": "Ligue 1", "sm_season": 25651},
    ]
    
    logger.info("--- AVVIO SYNC V2 SMART (PARTENZA DA LA LIGA) ---")
    import requests
    headers = {"X-Auth-Token": api_key_fd}
    
    for league in LEAGUES_TO_SYNC:
        l_id_fd = league["id_fd"]
        l_name = league["name"]
        l_season_sm = league["sm_season"]
        
        logger.info(f"\n>>> SINCRONIZZAZIONE LEGA: {l_name}")
        
        try:
            url = f"https://api.football-data.org/v4/competitions/{l_id_fd}/teams"
            res = requests.get(url, headers=headers).json()
            teams = res.get('teams', [])
            
            for team in teams:
                t_id = team['id']
                t_name_raw = team['name']
                t_name = t_name_raw.replace("RCD ", "").replace("Real ", "").replace("FC ", "").strip()
                
                logger.info(f"   SQUADRA: {t_name_raw}")
                
                # Scarichiamo la rosa
                rm.get_team_roster(t_id)
                file_path = f'data/penalties/team_{t_id}.json'
                
                if os.path.exists(file_path):
                    with open(file_path, 'r') as f:
                        squad = json.load(f).get('squad', [])
                    
                    for p in squad:
                        p_name = p['name']
                        dob_fd = p.get('dateOfBirth')
                        
                        # --- CHECK RESUME ---
                        conn = sqlite3.connect('data/betanalyzer.db', timeout=30)
                        cursor = conn.cursor()
                        cursor.execute("SELECT 1 FROM player_stats_cache WHERE player_id IN (SELECT player_id FROM player_info WHERE name = ?) AND season_id = ?", (p_name, l_season_sm))
                        if cursor.fetchone():
                            conn.close()
                            continue
                        conn.close()
                        
                        # Cerchiamo su Sportmonks
                        search_res = sm.search_player(p_name)
                        if search_res:
                            p_sm = search_res[0]
                            # Logica Omonimia (Data di Nascita)
                            if len(search_res) > 1 and dob_fd:
                                for candidate in search_res:
                                    if candidate.get('date_of_birth') == dob_fd:
                                        p_sm = candidate
                                        break
                            
                            sm_id = p_sm['id']
                            pos_id = p_sm.get('position_id')
                            
                            # Download Stats
                            stats = sm.get_player_stats(sm_id, l_season_sm, team_id=t_id, team_name=t_name)
                            if stats:
                                save_player_info(sm_id, p_name, t_id, t_name_raw, l_name.lower().replace(" ", "_"), pos_id)
                                logger.info(f"      OK: {p_name}")
                            else:
                                logger.warning(f"      SALTATO: {p_name} (Nessuna statistica per questa squadra)")
                            # RITARDO DI SICUREZZA (5 secondi)
                            time.sleep(5.0)
                        
        except Exception as e:
            logger.error(f"Errore lega {l_name}: {e}")
            time.sleep(10) # Pausa extra in caso di errore

if __name__ == "__main__":
    run_smart_sync()
