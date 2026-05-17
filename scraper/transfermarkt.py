import requests
import re
import json
import os
import logging
import time
from datetime import datetime

logger = logging.getLogger(__name__)

class TransfermarktScraper:
    def __init__(self):
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }
        self.cache_file = os.path.join("data", "transfermarkt_cache.json")
        self.cache = self._load_cache()

    def _load_cache(self):
        if os.path.exists(self.cache_file):
            try:
                with open(self.cache_file, "r") as f:
                    return json.load(f)
            except:
                return {}
        return {}

    def _save_cache(self):
        os.makedirs("data", exist_ok=True)
        try:
            with open(self.cache_file, "w") as f:
                json.dump(self.cache, f, indent=2)
        except Exception as e:
            logger.error(f"Errore salvataggio cache TM: {e}")

    def get_player_info(self, player_name, team_name=None):
        # Check cache first
        cache_key = f"{player_name}_{team_name}" if team_name else player_name
        if cache_key in self.cache:
            return self.cache[cache_key]

        query = player_name
        if team_name:
            query += f" {team_name}"

        logger.info(f"Ricerca Transfermarkt per: {query}")
        search_url = f"https://www.transfermarkt.it/schnellsuche/ergebnis/schnellsuche?query={query.replace(' ', '+')}"
        
        try:
            resp = requests.get(search_url, headers=self.headers, timeout=10)
            if resp.status_code != 200:
                return None

            # Trova il link al profilo (cerchiamo il primo spieler/profil che abbia un titolo)
            match_link = re.search(r'href="([^"]+/profil/spieler/\d+)" title="[^"]*"', resp.text)
            if not match_link:
                # Fallback: primo link spieler/profil
                match_link = re.search(r'href="([^"]+/profil/spieler/\d+)"', resp.text)
            
            if not match_link:
                logger.warning(f"Giocatore non trovato su TM: {player_name}")
                return None

            profile_url = "https://www.transfermarkt.it" + match_link.group(1)
            time.sleep(1) 
            
            resp_p = requests.get(profile_url, headers=self.headers, timeout=10)
            if resp_p.status_code != 200:
                return None

            html = resp_p.text

            # 1. Valore di Mercato (Più flessibile)
            # Cerchiamo il numero seguito da mln o mila
            value_match = re.search(r'market-value-wrapper">([\d,]+)\s*<span[^>]*>([^<]+)</span>', html)
            market_value = "N/D"
            if value_match:
                market_value = f"{value_match.group(1).strip()} {value_match.group(2).strip()}"
            else:
                # Fallback per altro formato
                value_match = re.search(r'right-column">.*?">([\d,]+\s*(?:mln|mila)\s*€)', html, re.DOTALL)
                if value_match:
                    market_value = value_match.group(1).strip()

            # 2. Ruolo Dettagliato (Cerca nella tabella info)
            role_match = re.search(r'Posizione:.*?<span[^>]*>\s*([^<]+)</span>', html, re.DOTALL | re.IGNORECASE)
            if not role_match:
                role_match = re.search(r'Ruolo:.*?<span[^>]*>\s*([^<]+)</span>', html, re.DOTALL | re.IGNORECASE)
            
            detailed_role = "N/D"
            if role_match:
                detailed_role = role_match.group(1).strip()

            # 3. Piede
            foot_match = re.search(r'Piede:.*?<span[^>]*>\s*([^<]+)</span>', html, re.DOTALL | re.IGNORECASE)
            foot = "N/D"
            if foot_match:
                foot = foot_match.group(1).strip()

            result = {
                "market_value": market_value,
                "detailed_role": detailed_role,
                "foot": foot,
                "last_update": datetime.now().strftime("%Y-%m-%d")
            }

            # Save to cache
            self.cache[cache_key] = result
            self._save_cache()
            return result

        except Exception as e:
            logger.error(f"Errore scraping TM per {player_name}: {e}")
            return None

    def get_team_formation(self, team_name):
        """Recupera il modulo più usato dall'allenatore della squadra in modo robusto"""
        cache_key = f"formation_{team_name}"
        if cache_key in self.cache:
            return self.cache[cache_key]

        logger.info(f"Ricerca modulo automatica per: {team_name}")
        # Cerchiamo la squadra con un termine più specifico
        search_query = team_name if "AS" in team_name or "AC" in team_name else f"{team_name}"
        search_url = f"https://www.transfermarkt.it/schnellsuche/ergebnis/schnellsuche?query={search_query.replace(' ', '+')}"
        
        try:
            resp = requests.get(search_url, headers=self.headers, timeout=10)
            
            # 1. Trova il link alla squadra - Cerchiamo il link che contiene il nome nel title
            # Transfermarkt spesso usa immagini, quindi cerchiamo il title nell'<a> o nell'<img>
            team_match = re.search(r'href="([^"]+/startseite/verein/\d+)"[^>]*title="[^"]*' + re.escape(team_name) + r'[^"]*"', resp.text, re.IGNORECASE)
            
            if not team_match:
                # Fallback: cerchiamo nel contenuto dell'<a> se c'è un'immagine con quel title
                team_match = re.search(r'href="([^"]+/startseite/verein/\d+)"[^>]*>.*?title="[^"]*' + re.escape(team_name) + r'[^"]*"', resp.text, re.IGNORECASE | re.DOTALL)
                
            if not team_match:
                # Fallback estremo: se la ricerca era 'Milan', proviamo a cercare 'AC Milan'
                if team_name == "Milan":
                    team_match = re.search(r'href="([^"]+/startseite/verein/5)"', resp.text)
                elif team_name == "Roma":
                    team_match = re.search(r'href="([^"]+/as-roma/startseite/verein/12)"', resp.text)
                
            if not team_match:
                # Fallback generico: primo link verein utile
                team_match = re.search(r'href="([^"]+/startseite/verein/\d+)"', resp.text)

            team_url = "https://www.transfermarkt.it" + team_match.group(1)
            time.sleep(1)
            resp_t = requests.get(team_url, headers=self.headers, timeout=10)
            
            # 2. Trova l'allenatore nella pagina squadra
            # Cerchiamo l'etichetta "Allenatore:" e il link successivo
            coach_section = re.search(r'Allenatore:.*?href="([^"]+/profil/trainer/\d+)"', resp_t.text, re.DOTALL | re.IGNORECASE)
            if not coach_section:
                # Prova alternativa: link con 'profil/trainer' generico
                coach_section = re.search(r'href="([^"]+/profil/trainer/\d+)"', resp_t.text)
                
            if not coach_section:
                return None
            
            coach_url = "https://www.transfermarkt.it" + coach_section.group(1)
            time.sleep(1)
            resp_c = requests.get(coach_url, headers=self.headers, timeout=10)
            
            # 3. Estrai il modulo
            formation_match = re.search(r'Modulo pi\xf9 utilizzato ultimi 2 anni\s*:.*?<span[^>]*>([^<]+)</span>', resp_c.text, re.DOTALL | re.IGNORECASE)
            if not formation_match:
                formation_match = re.search(r'Modulo preferito:.*?<span[^>]*>([^<]+)</span>', resp_c.text, re.DOTALL | re.IGNORECASE)

            if formation_match:
                formation = formation_match.group(1).strip()
                logger.info(f"Modulo trovato per {team_name}: {formation}")
                self.cache[cache_key] = formation
                self._save_cache()
                return formation

            return None
        except Exception as e:
            logger.error(f"Errore nello scouting tattico per {team_name}: {e}")
            return None
