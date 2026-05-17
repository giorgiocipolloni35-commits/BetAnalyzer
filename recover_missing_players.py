import os
import json
import sqlite3
import logging
import time
import difflib
from scraper.sportmonks import SportmonksClient
from dotenv import load_dotenv

load_dotenv()

# Configurazione logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

def clean_name(n):
    return n.lower().replace("club ", "").replace("ca ", "").replace("cf ", "").replace("de ", "").replace("fc ", "").replace("rc ", "").replace("ud ", "").replace("1909", "").replace("1913", "").strip()

def recover():
    sm = SportmonksClient(os.getenv("SPORTMONKS_API_KEY"))
    db_path = "data/betanalyzer.db"
    
    with open("missing_players.json", "r") as f:
        missing = json.load(f)
    
    league_name_to_id = {
        "Serie A": "384",
        "Premier League": "8",
        "La Liga": "564",
        "Bundesliga": "82",
        "Ligue 1": "301"
    }

    # 1. Recuperiamo tutte le squadre per ogni lega per avere i team_id corretti
    team_id_map = {}
    logger.info("📡 Recupero ID squadre da Sportmonks...")
    
    # Stagioni correnti (da aggiornare se necessario, o recuperare dinamicamente)
    # Per velocità le mettiamo fisse se le conosciamo, altrimenti le cerchiamo
    # In v3 possiamo cercare i team per lega o via standings
    
    for l_name, l_id in league_name_to_id.items():
        try:
            # Recuperiamo l'ultima season per questa lega
            url = f"{sm.base_url}/leagues/{l_id}"
            res = sm._make_request(f"leagues/{l_id}", {"include": "currentseason"})
            if res and res.get("data"):
                s_id = res["data"].get("current_season_id")
                if s_id:
                    standings = sm.get_standings(l_id, str(s_id))
                    for s in standings:
                        team_id_map[clean_name(s["team_name"])] = s["team_id"]
                        # Aggiungiamo anche varianti
                        team_id_map[s["team_name"].lower()] = s["team_id"]
        except Exception as e:
            logger.error(f"Errore recupero team per {l_name}: {e}")
    
    logger.info(f"Team ID Map Keys: {list(team_id_map.keys())[:10]}...")
    teams_missing = {}
    for item in missing:
        t = item["team"]
        if t not in teams_missing: teams_missing[t] = []
        teams_missing[t].append(item)

    recovered_count = 0
    total_to_recover = len(missing)
    
    for team_name, players in teams_missing.items():
        logger.info(f"🏟️ Elaborazione squadra: {team_name} ({len(players)} mancanti)")
        
        tid = team_id_map.get(clean_name(team_name)) or team_id_map.get(team_name.lower())
        
        if not tid:
            logger.warning(f"⚠️ SM Team ID non trovato per {team_name}")
            continue 

        # Scarichiamo la rosa completa
        squad = sm.get_squad(str(tid))
        if not squad:
            logger.warning(f"❌ Impossibile recuperare rosa per {team_name} (ID: {tid})")
            continue
            
        squad_names = [p["name"] for p in squad]
        
        for p_item in players:
            target_name = p_item["player"]
            # Fuzzy match
            matches = difflib.get_close_matches(target_name, squad_names, n=1, cutoff=0.5)
            
            if not matches:
                # Prova con sottostringhe
                for sn in squad_names:
                    if target_name.lower() in sn.lower() or sn.lower() in target_name.lower():
                        matches = [sn]
                        break
            
            if matches:
                best_match_name = matches[0]
                p_data = next(p for p in squad if p["name"] == best_match_name)
                
                # Salviamo nel DB
                try:
                    conn = sqlite3.connect(db_path)
                    cursor = conn.cursor()
                    cursor.execute("SELECT player_id FROM player_info WHERE player_id = ?", (p_data["id"],))
                    if not cursor.fetchone():
                        now = "2026-05-12T00:00:00Z"
                        l_key = clean_name(p_item["league"]).replace(" ", "_")
                        cursor.execute("""
                            INSERT INTO player_info (player_id, name, team_id, team_name, league_id, position_id, updated_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                        """, (p_data["id"], p_data["name"], tid, team_name, l_key, p_data.get("position_id"), now))
                        conn.commit()
                        logger.info(f"✅ RECUPERATO: {target_name} -> {p_data['name']} (ID: {p_data['id']})")
                        recovered_count += 1
                    else:
                        logger.info(f"ℹ️ Già presente: {target_name} (come {p_data['name']})")
                    conn.close()
                except Exception as e:
                    logger.error(f"Errore salvataggio DB per {target_name}: {e}")
            else:
                logger.warning(f"❌ Nessun match trovato per {target_name} nella rosa del {team_name}")
        
        time.sleep(1)

    print(f"\n🚀 OPERAZIONE COMPLETATA!")
    print(f"Giocatori totali da recuperare: {total_to_recover}")
    print(f"Giocatori recuperati con successo: {recovered_count}")

if __name__ == "__main__":
    recover()
