import os
import time
import logging
from datetime import datetime, timezone
from dotenv import load_dotenv
from scraper.sportmonks import SportmonksClient
from logic.roster import RosterManager
from db.database import _get_conn, save_player_stats_cache, get_cached_player_stats

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class StatsSyncWorker:
    def __init__(self):
        load_dotenv()
        self.sm_key = os.getenv("SPORTMONKS_API_KEY")
        self.fd_key = os.getenv("FOOTBALL_DATA_API_KEY")
        self.sm_client = SportmonksClient(self.sm_key)
        self.rm = RosterManager(self.fd_key)
        self.leagues = ["italy_serie_a"] # Possiamo espandere questa lista

    def sync_all_players(self, limit_team_id=None):
        logger.info("Avvio sincronizzazione giocatori...")
        
        for league in self.leagues:
            logger.info(f"Sincronizzazione lega: {league}")
            # 1. Recupera tutte le squadre della lega tramite la classifica
            from scraper.penalties import LEAGUE_CODES
            league_code = LEAGUE_CODES.get(league)
            if not league_code:
                logger.error(f"Codice lega non trovato per {league}")
                continue
                
            teams = self.rm.pa._get_standings(league_code)
            if not teams:
                logger.warning(f"Nessuna squadra trovata per {league}")
                continue

            for team in teams:
                team_id = team["team_id"]
                team_name = team["name"]
                
                if limit_team_id and team_id != limit_team_id:
                    continue
                    
                logger.info(f"--- Sincronizzazione Squadra: {team_name} (ID: {team_id}) ---")
                
                # 2. Recupera la rosa completa
                roster = self.rm.get_team_roster(team_id)
                if not roster:
                    logger.warning(f"Rosa non trovata per {team_name}")
                    continue

                for p in roster:
                    p_id = p["id"]
                    p_name = p["name"]
                    
                    # 3. Cerca il giocatore su Sportmonks per avere l'ID corretto
                    # (Usiamo la logica di RosterManager per consistenza)
                    try:
                        # Otteniamo i dettagli completi (che triggerano il salvataggio in cache)
                        # Questo metodo internamente chiama get_player_stats che ora ha la cache
                        logger.info(f"Aggiornamento stats per: {p_name}")
                        self.rm.get_player_full_details(p_id, team_id, league)
                        
                        # Pausa per rispettare il rate limit (60-100 chiamate/minuto consigliate)
                        time.sleep(1.2) 
                    except Exception as e:
                        logger.error(f"Errore durante sync di {p_name}: {e}")
                        time.sleep(5) # Pausa più lunga in caso di errore
                        
        logger.info("Sincronizzazione completata con successo! 🌙✅")

if __name__ == "__main__":
    worker = StatsSyncWorker()
    worker.sync_all_players()
