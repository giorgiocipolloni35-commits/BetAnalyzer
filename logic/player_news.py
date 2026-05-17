"""
Player News Radar — Cerca news recenti sui giocatori chiave
per arricchire il contesto dell'analisi AI pre-match.

Categorie:
  🔴 INFORTUNIO    — Out, lesioni, recuperi, torna in gruppo
  🟡 SQUALIFICA    — Cartellini cumulati, diffide, squalifiche
  🟠 MERCATO       — Voci cessione, trattative, rinnovi, clausole
  🔵 CONFLITTO     — Escluso, panchina punitiva, tensione spogliatoio
  ⚡ FORMA          — Serie di gol, prestazioni top, man of the match
"""

import logging
import re
import time
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ #
#  Keywords per classificazione automatica                             #
# ------------------------------------------------------------------ #
CATEGORIES = {
    "🔴 INFORTUNIO": [
        "infortun", "injury", "injured", "lesion", "out for",
        "sidelined", "ruled out", "muscol", "operaz", "recuper",
        "torna in gruppo", "returns to training", "return to training",
        "knee", "ankle", "hamstring", "acl", "menisco", "crociato",
        "frattura", "stiramento", "distorsione", "elongazione",
        "problema fisico", "indisponibil", "ko", "si ferma",
        "stop", "out per", "assenza", "salta", "miss", "misses",
        "unavailable", "fitness", "setback", "scan", "surgery",
    ],
    "🟡 SQUALIFICA": [
        "squalific", "suspended", "suspension", "diffida",
        "yellow card", "red card", "ban", "cartellino", "espuls",
        "ammoniz", "giornate di squalifica", "banned", "booking",
        "dissent", "violent conduct", "misconduct",
    ],
    "🟠 MERCATO": [
        "mercato", "transfer", "cessione", "acquist", "trattativa",
        "offerta", "rinnov", "contratto", "addio", "partenza",
        "vuole andare", "richiesta", "clausola", "signing", "deal",
        "bid", "offer", "leave", "exit", "swap", "loan", "prestito",
        "futuro", "destinazione", "accordo", "affare", "cifra",
        "milioni", "ingaggio", "stipendio", "scadenza",
    ],
    "🔵 CONFLITTO": [
        "esclus", "panchina", "conflitto", "tensione", "lite",
        "scelta tecnica", "fuori rosa", "fuori progetto",
        "dropped", "benched", "axed", "excluded", "fallout",
        "bust-up", "clash", "unhappy", "frustrated", "disciplin",
        "multato", "fined", "dressing room", "spogliatoio",
        "rottura", "problema con", "rapporto teso",
    ],
    "⚡ FORMA": [
        "doppietta", "tripletta", "hat-trick", "hat trick", "brace",
        "man of the match", "migliore in campo", "serie positiva",
        "scoring run", "in forma", "on fire", "stellar",
        "outstanding", "superb", "brilliant", "decisivo",
        "gol vittoria", "match winner", "capocannoniere",
        "record", "milestone", "traguardo",
    ],
}

# Peso impatto per categoria (usato per ordinamento)
CATEGORY_WEIGHT = {
    "🔴 INFORTUNIO": 5,
    "🟡 SQUALIFICA": 4,
    "🔵 CONFLITTO": 3,
    "🟠 MERCATO": 2,
    "⚡ FORMA": 1,
}

# Cache semplice per evitare ricerche duplicate nella stessa sessione
_news_cache: dict[str, list] = {}
_cache_ts: float = 0
CACHE_TTL = 1800  # 30 minuti


def _clear_stale_cache():
    global _news_cache, _cache_ts
    if time.time() - _cache_ts > CACHE_TTL:
        _news_cache.clear()
        _cache_ts = time.time()


def search_google_news(query: str, days: int = 7, max_results: int = 8) -> list[dict]:
    """
    Cerca news via Google News RSS (gratuito, no API key).

    Returns lista di {"title", "date", "source", "link"}
    """
    _clear_stale_cache()
    cache_key = f"{query}|{days}"
    if cache_key in _news_cache:
        return _news_cache[cache_key]

    results = []
    # Cerca in italiano e inglese
    locales = [
        ("it", "IT", "IT:it"),
        ("en", "GB", "GB:en"),
    ]

    for hl, gl, ceid in locales:
        url = (
            f"https://news.google.com/rss/search"
            f"?q={quote_plus(query)}+when:{days}d"
            f"&hl={hl}&gl={gl}&ceid={ceid}"
        )
        try:
            resp = requests.get(url, timeout=12, headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/120.0.0.0 Safari/537.36"
            })
            if resp.status_code != 200:
                logger.debug(f"Google News HTTP {resp.status_code} for query: {query}")
                continue

            root = ET.fromstring(resp.content)
            items = root.findall(".//item")

            for item in items[:max_results]:
                title_el = item.find("title")
                date_el = item.find("pubDate")
                source_el = item.find("source")
                link_el = item.find("link")

                title = title_el.text.strip() if title_el is not None and title_el.text else ""
                pub_date = date_el.text.strip() if date_el is not None and date_el.text else ""
                source = source_el.text.strip() if source_el is not None and source_el.text else ""
                link = link_el.text.strip() if link_el is not None and link_el.text else ""

                if title:
                    results.append({
                        "title": title,
                        "date": pub_date,
                        "source": source,
                        "link": link,
                    })
        except requests.exceptions.Timeout:
            logger.warning(f"Google News timeout for: {query}")
        except ET.ParseError:
            logger.warning(f"Google News XML parse error for: {query}")
        except Exception as e:
            logger.warning(f"Google News error for '{query}': {e}")

        # Piccola pausa tra le lingue per non spammare
        time.sleep(0.3)

    _news_cache[cache_key] = results
    return results


def classify_news(title: str) -> tuple[str | None, int]:
    """
    Classifica un titolo di notizia.

    Returns (categoria, relevance_score) oppure (None, 0) se irrilevante.
    """
    title_lower = title.lower()
    best_cat = None
    best_score = 0

    for category, keywords in CATEGORIES.items():
        score = sum(1 for kw in keywords if kw in title_lower)
        if score > best_score:
            best_score = score
            best_cat = category

    return (best_cat, best_score) if best_score > 0 else (None, 0)


def get_key_players(squad: list[dict], leaders: dict = None, max_players: int = 6) -> list[dict]:
    """
    Seleziona i giocatori chiave da monitorare.
    Priorità: leader statistici → portiere titolare → giocatori con più gol/presenze.

    Args:
        squad: Lista giocatori dalla rosa [{name, position, id, ...}]
        leaders: Dict leader statistici {top_scorer: {name}, top_assistman: {name}, ...}
        max_players: Numero massimo di giocatori da monitorare

    Returns: Lista di {name, role, importance}
    """
    key_players = []
    seen_names = set()

    # 1. Aggiungi leader statistici (sicuramente importanti)
    if leaders:
        for role_key in ["top_scorer", "top_assistman"]:
            leader = leaders.get(role_key, {})
            name = leader.get("name", "")
            if name and name.lower() not in seen_names:
                seen_names.add(name.lower())
                key_players.append({
                    "name": name,
                    "role": role_key.replace("top_", "").title(),
                    "importance": "leader",
                })

    # 2. Portiere titolare (impatto enorme se assente)
    for p in squad:
        pos = (p.get("position") or "").lower()
        if pos in ("goalkeeper", "portiere", "gk") and len(key_players) < max_players:
            name = p.get("name") or p.get("display_name") or ""
            if name and name.lower() not in seen_names:
                seen_names.add(name.lower())
                key_players.append({
                    "name": name,
                    "role": "Portiere",
                    "importance": "starter",
                })
                break  # Solo un portiere

    # 3. Riempi con giocatori rimanenti (attaccanti e centrocampisti prima)
    priority_positions = ["offence", "attacco", "forward", "midfield", "centrocampo", "defence", "difesa"]
    for prio_pos in priority_positions:
        if len(key_players) >= max_players:
            break
        for p in squad:
            if len(key_players) >= max_players:
                break
            pos = (p.get("position") or "").lower()
            name = p.get("name") or p.get("display_name") or ""
            if prio_pos in pos and name and name.lower() not in seen_names:
                seen_names.add(name.lower())
                key_players.append({
                    "name": name,
                    "role": p.get("position", ""),
                    "importance": "regular",
                })

    return key_players[:max_players]


def scan_team_news(
    team_name: str,
    key_players: list[dict],
    days: int = 7,
) -> list[dict]:
    """
    Cerca news rilevanti per una squadra e i suoi giocatori chiave.

    Returns lista di news classificate:
        [{category, title, player, source, date, relevance, weight}]
    """
    all_news = []

    # 1. News a livello squadra (infortuni, squalifiche, mercato)
    team_queries = [
        f"{team_name} infortuni formazione",
        f"{team_name} injury team news",
    ]

    for query in team_queries:
        raw_news = search_google_news(query, days=days, max_results=5)
        for item in raw_news:
            cat, score = classify_news(item["title"])
            if cat and score > 0:
                all_news.append({
                    "category": cat,
                    "title": item["title"],
                    "player": team_name,  # News di squadra
                    "source": item["source"],
                    "date": item["date"],
                    "relevance": score,
                    "weight": CATEGORY_WEIGHT.get(cat, 0),
                    "is_team_news": True,
                })

    # 2. News sui singoli giocatori chiave
    for player in key_players:
        name = player["name"]
        # Cerca con nome + squadra per precisione
        raw_news = search_google_news(f"{name} {team_name}", days=days, max_results=4)
        for item in raw_news:
            cat, score = classify_news(item["title"])
            if cat and score > 0:
                all_news.append({
                    "category": cat,
                    "title": item["title"],
                    "player": name,
                    "source": item["source"],
                    "date": item["date"],
                    "relevance": score,
                    "weight": CATEGORY_WEIGHT.get(cat, 0),
                    "is_team_news": False,
                })

        # Rate limiting gentile
        time.sleep(0.4)

    # Deduplica per titolo simile
    seen_titles = set()
    unique_news = []
    for item in all_news:
        # Usa i primi 60 char del titolo lowercase come chiave
        title_key = item["title"][:60].lower()
        if title_key not in seen_titles:
            seen_titles.add(title_key)
            unique_news.append(item)

    # Ordina: peso categoria (decrescente) → relevance (decrescente)
    unique_news.sort(key=lambda x: (x["weight"], x["relevance"]), reverse=True)

    return unique_news


def _analyze_coach(coach_info: dict, team_name: str) -> str | None:
    """
    Analizza il coach e genera contesto se rilevante.

    Fasi allenatore:
      - 0-90 giorni: LUNA DI MIELE — boost motivazionale, difesa del vecchio mister
      - 91-240 giorni: ADATTAMENTO — nuovo sistema, errori difensivi
      - 241+ giorni: STABILIZZATO — dati affidabili
      - temporary=True: TRAGHETTATORE — incertezza, bassa pressione risultati

    Returns stringa di contesto o None se il coach è stabile.
    """
    if not coach_info:
        return None

    days = coach_info.get("days_in_charge", 999)
    name = coach_info.get("coach_name", "?")
    temp = coach_info.get("temporary", False)
    prev = coach_info.get("previous_coaches", 0)

    # Coach stabilizzato, nulla da segnalare
    if days > 240 and not temp and prev <= 1:
        return None

    lines = []

    if temp:
        lines.append(f"🔄 TRAGHETTATORE: {name} ({team_name}) — allenatore ad interim")
        lines.append(f"   Pattern: Incertezza ambientale, giocatori in attesa del nuovo mister, bassa pressione risultati")
    elif days <= 30:
        lines.append(f"🔄 NUOVO ALLENATORE: {name} ({team_name}) — in carica da {days} giorni")
        lines.append(f"   Fase: LUNA DI MIELE (prime settimane) — forte boost motivazionale, giocatori vogliono impressionare")
        lines.append(f"   Pattern: Risultati sopra le attese, squadra compatta, Under favorito (difesa ordinata del predecessore)")
    elif days <= 90:
        lines.append(f"🔄 NUOVO ALLENATORE: {name} ({team_name}) — in carica da {days} giorni")
        lines.append(f"   Fase: LUNA DI MIELE (1-3 mesi) — boost motivazionale ancora attivo, nuove idee tattiche")
        lines.append(f"   Pattern: Under/Over dipende dal sistema, nuove gerarchie rigori/calci piazzati possibili")
    elif days <= 240:
        lines.append(f"🔄 ALLENATORE IN RODAGGIO: {name} ({team_name}) — in carica da {days} giorni (~{days//30} mesi)")
        lines.append(f"   Fase: ADATTAMENTO — sistema tattico non ancora rodato, possibili errori difensivi")
        lines.append(f"   Pattern: Over/BTTS favoriti, difesa instabile, gerarchie in evoluzione")

    # Instabilità panchina
    if prev >= 2:
        lines.append(f"   ⚠️ PANCHINA INSTABILE: {prev} cambi allenatore negli ultimi 2 anni — ambiente turbolento")

    return "\n".join(lines) if lines else None


def build_news_context(
    home_team: str,
    away_team: str,
    home_squad: list[dict],
    away_squad: list[dict],
    home_leaders: dict = None,
    away_leaders: dict = None,
    home_coach: dict = None,
    away_coach: dict = None,
    days: int = 7,
    max_news: int = 12,
) -> str:
    """
    Genera il blocco di contesto completo Player News Radar
    da inserire nel prompt AI.

    Args:
        home_team: Nome squadra casa
        away_team: Nome squadra trasferta
        home_squad: Rosa giocatori casa
        away_squad: Rosa giocatori trasferta
        home_leaders: Leader statistici casa
        away_leaders: Leader statistici trasferta
        home_coach: Info coach casa da Sportmonks (get_coach_info)
        away_coach: Info coach trasferta da Sportmonks (get_coach_info)
        days: Finestra temporale ricerca (giorni)
        max_news: Numero max news da includere

    Returns:
        Blocco di testo formattato per il prompt AI, oppure stringa vuota
    """
    logger.info(f"🔍 Player News Radar: scanning {home_team} vs {away_team}...")

    # Seleziona giocatori chiave
    h_key = get_key_players(home_squad, home_leaders, max_players=5)
    a_key = get_key_players(away_squad, away_leaders, max_players=5)

    logger.info(f"  Giocatori chiave {home_team}: {[p['name'] for p in h_key]}")
    logger.info(f"  Giocatori chiave {away_team}: {[p['name'] for p in a_key]}")

    # Scan news per entrambe le squadre
    h_news = scan_team_news(home_team, h_key, days=days)
    a_news = scan_team_news(away_team, a_key, days=days)

    all_news = h_news + a_news

    # Analisi coach
    h_coach_ctx = _analyze_coach(home_coach, home_team)
    a_coach_ctx = _analyze_coach(away_coach, away_team)

    # Se non ci sono né news né coach rilevanti, esci
    if not all_news and not h_coach_ctx and not a_coach_ctx:
        logger.info("  Nessuna news o segnalazione coach trovata")
        return ""

    # Prendi le top N news più impattanti
    all_news.sort(key=lambda x: (x["weight"], x["relevance"]), reverse=True)
    top_news = all_news[:max_news]

    # Costruisci il blocco
    lines = [f"PLAYER NEWS RADAR (ultimi {days} giorni):"]

    # === COACH SECTION ===
    if h_coach_ctx or a_coach_ctx:
        if h_coach_ctx:
            lines.append(f"  {h_coach_ctx}")
        if a_coach_ctx:
            lines.append(f"  {a_coach_ctx}")

    # === NEWS SECTION ===
    if top_news:
        # Raggruppa per squadra
        h_items = [n for n in top_news if n["player"] == home_team or
                   any(n["player"].lower() == p["name"].lower() for p in h_key)]
        a_items = [n for n in top_news if n not in h_items]

        if h_items:
            lines.append(f"  [{home_team}]")
            for item in h_items:
                player_tag = f"{item['player']}" if not item.get("is_team_news") else "SQUADRA"
                lines.append(f"    {item['category']}: {player_tag} — {item['title']}")

        if a_items:
            lines.append(f"  [{away_team}]")
            for item in a_items:
                player_tag = f"{item['player']}" if not item.get("is_team_news") else "SQUADRA"
                lines.append(f"    {item['category']}: {player_tag} — {item['title']}")

    # Summary impatto
    high_impact = len([n for n in top_news if n["weight"] >= 4])
    coach_alerts = (1 if h_coach_ctx else 0) + (1 if a_coach_ctx else 0)
    lines.append(f"  IMPATTO: {high_impact} news alta priorità, {coach_alerts} alert allenatore")

    logger.info(f"  ✅ Player News Radar: {len(top_news)} news + {coach_alerts} coach alert")

    return "\n".join(lines)
