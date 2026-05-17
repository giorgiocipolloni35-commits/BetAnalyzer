import os
import json
import time
import logging
from datetime import datetime, timezone
from dotenv import load_dotenv
from scraper.sportmonks import SportmonksClient
from logic.roster import RosterManager
from db.database import save_player_info

# Configurazione Logging avanzata per monitoraggio real-time
LOG_FILE = 'nightly_sync.log'
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, mode='a'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

load_dotenv()

def run_nightly_sync():
    api_key_fd = os.getenv('FOOTBALL_DATA_API_KEY')
    api_key_sm = os.getenv('SPORTMONKS_API_KEY')
    
    if not api_key_fd or not api_key_sm:
        logger.error("API Keys mancanti nel file .env!")
        return

    rm = RosterManager(api_key_fd)
    sm = SportmonksClient(api_key_sm)
    
    # Mappatura Campionati (FD ID -> SM Season ID)
    LEAGUES_TO_SYNC = [
        {"id_fd": 2019, "name": "Serie A", "sm_season": 25533},
        {"id_fd": 2021, "name": "Premier League", "sm_season": 25583},
        {"id_fd": 2014, "name": "La Liga", "sm_season": 25659},
        {"id_fd": 2002, "name": "Bundesliga", "sm_season": 25646},
        {"id_fd": 2015, "name": "Ligue 1", "sm_season": 25651},
    ]
    
    logger.info("--- AVVIO SINCRONIZZAZIONE NOTTURNA EUROPEA ---")
    
    import requests
    headers = {"X-Auth-Token": api_key_fd}
    
    missing_players = []
    import sqlite3
    from datetime import timedelta

    for idx_l, league in enumerate(LEAGUES_TO_SYNC):
        l_id_fd = league["id_fd"]
        l_name = league["name"]
        l_season_sm = league["sm_season"]

        logger.info(f"\n{'='*60}")
        logger.info(f" LEGA {idx_l+1}/{len(LEAGUES_TO_SYNC)}: {l_name}")
        logger.info(f"{'='*60}")

        try:
            # 1. Recuperiamo tutte le squadre della lega
            url = f"https://api.football-data.org/v4/competitions/{l_id_fd}/teams"
            res = requests.get(url, headers=headers).json()
            teams = res.get('teams', [])

            logger.info(f"Trovate {len(teams)} squadre in {l_name}.")

            for idx_t, team in enumerate(teams):
                t_id = team['id']
                t_name_raw = team['name']
                # Puliamo il nome per Sportmonks
                t_name = t_name_raw.replace("AC ", "").replace("AS ", "").replace("FC ", "").replace("Inter ", "").replace("SS ", "").replace("Real ", "").strip()

                logger.info(f"\n[{idx_t+1}/{len(teams)}] >>> SQUADRA: {t_name_raw} (Filtro: {t_name})")

                # 2. Scarichiamo/Aggiorniamo la rosa locale
                rm.get_team_roster(t_id)
                file_path = f'data/penalties/team_{t_id}.json'

                if os.path.exists(file_path):
                    with open(file_path, 'r') as f:
                        squad = json.load(f).get('squad', [])

                    total_p = len(squad)
                    logger.info(f"   |-- Rosa di {total_p} giocatori. Avvio sincronizzazione...")

                    for idx_p, p in enumerate(squad):
                        p_name = p['name']
                        dob_fd = p.get('dateOfBirth')  # Formato: YYYY-MM-DD
                        try:
                            # 3. Cerchiamo il giocatore su Sportmonks (con DOB per matching robusto)
                            search_res = sm.search_player(p_name, dob=dob_fd)
                            if search_res:
                                # Disambiguazione: preferisci match per DOB
                                p_sm = search_res[0]
                                if len(search_res) > 1 and dob_fd:
                                    for candidate in search_res:
                                        dob_sm = candidate.get('date_of_birth')
                                        if dob_sm and dob_fd == dob_sm:
                                            p_sm = candidate
                                            logger.info(f"   |   DOB match: {p_name} → SM id {p_sm['id']}")
                                            break

                                sm_id = p_sm['id']
                                pos_id = p_sm.get('position_id')

                                # --- LOGICA DI RIPRESA (RESUME) ---
                                conn = sqlite3.connect('data/betanalyzer.db')
                                cursor = conn.cursor()
                                cursor.execute("SELECT updated_at FROM player_stats_cache WHERE player_id = ? AND team_id = ? AND season_id = ?", (sm_id, t_id, l_season_sm))
                                row = cursor.fetchone()
                                conn.close()

                                if row:
                                    try:
                                        last_update = datetime.fromisoformat(row[0])
                                        if datetime.now() - last_update < timedelta(days=3):
                                            logger.info(f"   |   [{idx_p+1}/{total_p}] RECENTE: {p_name} (Saltato, aggiornato {row[0][:10]})")
                                            continue
                                        else:
                                            logger.info(f"   |   [{idx_p+1}/{total_p}] STALE: {p_name} (Aggiorno, ultimo update {row[0][:10]})")
                                    except:
                                        pass
                                # ---------------------------------

                                # 4. Scarichiamo le stats (Logica STRETTA per squadra corrente)
                                stats = sm.get_player_stats(sm_id, l_season_sm, team_id=t_id, team_name=t_name)

                                # 5. Salviamo le info nel DB
                                if stats:
                                    dob = p_sm.get('date_of_birth')
                                    save_player_info(sm_id, p_name, t_id, t_name_raw, l_name.lower().replace(" ", "_"), pos_id, dob)
                                    logger.info(f"   |   [{idx_p+1}/{total_p}] OK: {p_name}")
                                else:
                                    # Salva comunque player_info (posizione, team) anche senza stats
                                    # Utile per trasferimenti di gennaio con 0 stats su Sportmonks
                                    dob = p_sm.get('date_of_birth')
                                    save_player_info(sm_id, p_name, t_id, t_name_raw, l_name.lower().replace(" ", "_"), pos_id, dob)
                                    logger.warning(f"   |   [{idx_p+1}/{total_p}] INFO ONLY: {p_name} (salvato senza stats)")
                            else:
                                logger.warning(f"   |   [{idx_p+1}/{total_p}] NON TROVATO: {p_name}")
                                missing_players.append({
                                    "league": l_name,
                                    "team": t_name_raw,
                                    "player": p_name,
                                    "dob": dob_fd,
                                    "position": p.get("position"),
                                })

                            # Pausa per evitare rate limiting
                            time.sleep(3.5)

                        except Exception as e:
                            logger.error(f"   |   [{idx_p+1}/{total_p}] ERRORE {p_name}: {e}")

                logger.info(f"   |-- FINE SQUADRA: {t_name_raw}")
                # Pausa tra squadre
                time.sleep(2)

        except Exception as e:
            logger.error(f"Errore durante il sync della lega {l_name}: {e}")
            
    # Salvataggio report mancanti
    with open('missing_players.json', 'w') as f:
        json.dump(missing_players, f, indent=4)
        
    logger.info(f"\n{'#'*60}")
    logger.info(f" --- SINCRONIZZAZIONE TOTALE COMPLETATA! 🌙🌍✅ ---")
    logger.info(f" Report giocatori mancanti salvato in: missing_players.json ({len(missing_players)} nomi)")
    logger.info(f"{'#'*60}")

if __name__ == "__main__":
    run_nightly_sync()
