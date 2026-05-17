"""
Modulo AI — Analisi quote con calcolo EV e value betting.
Usa OpenRouter (compatibile con OpenAI SDK).
Restituisce max 3 raccomandazioni ad alta convinzione per partita.
"""
import os
import json
import requests
import logging
import re
from openai import OpenAI
from models.match import Match, Recommendation
from scraper.penalties import PenaltyAnalyzer


logger = logging.getLogger(__name__)


def get_openrouter_balance(api_key: str) -> dict:
    """Restituisce credito usato e residuo da OpenRouter (saldo account reale)."""
    try:
        r = requests.get(
            "https://openrouter.ai/api/v1/credits",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=8,
        )
        r.raise_for_status()
        data = r.json().get("data", {})
        total = data.get("total_credits", 0) or 0
        usage = data.get("total_usage", 0) or 0
        remaining = round(total - usage, 4)
        return {
            "usage":     round(usage, 4),
            "limit":     total,
            "remaining": remaining,
        }
    except Exception as e:
        logger.warning(f"Impossibile recuperare saldo OpenRouter: {e}")
        return {"usage": None, "limit": None, "remaining": None}


# ------------------------------------------------------------------ #
#  Calcoli matematici (fatti in codice, non dall'AI)                    #
# ------------------------------------------------------------------ #

def _compute_market_stats(match: Match) -> list[dict]:
    """Calcola implied probability, margine bookmaker e migliore quota per ogni mercato."""
    markets = []

    def _add_pair(group_name, mkt_a, mkt_b, odds_a, bk_a, odds_b, bk_b):
        if odds_a and odds_b:
            imp_a = 1 / odds_a
            imp_b = 1 / odds_b
            overround = imp_a + imp_b
            margin = (overround - 1) * 100
            markets.append({
                "group": group_name,
                "options": [
                    {"market": mkt_a, "odds": odds_a, "bookmaker": bk_a, "implied_prob": round(imp_a * 100, 1)},
                    {"market": mkt_b, "odds": odds_b, "bookmaker": bk_b, "implied_prob": round(imp_b * 100, 1)},
                ],
                "overround": round(overround, 4),
                "margin_pct": round(margin, 2),
            })

    bh, bh_bk = match.best_home
    bd, bd_bk = match.best_draw
    ba, ba_bk = match.best_away

    if bh and bd and ba:
        imp_h, imp_d, imp_a = 1/bh, 1/bd, 1/ba
        overround = imp_h + imp_d + imp_a
        margin = (overround - 1) * 100
        markets.append({
            "group": "1X2",
            "options": [
                {"market": "1", "odds": bh, "bookmaker": bh_bk, "implied_prob": round(imp_h * 100, 1)},
                {"market": "X", "odds": bd, "bookmaker": bd_bk, "implied_prob": round(imp_d * 100, 1)},
                {"market": "2", "odds": ba, "bookmaker": ba_bk, "implied_prob": round(imp_a * 100, 1)},
            ],
            "overround": round(overround, 4),
            "margin_pct": round(margin, 2),
        })

    # Double Chance (calcolata da 1X2)
    dc1x, dc1x_bk = match.best_dc_1x
    dcx2, dcx2_bk = match.best_dc_x2
    dc12, dc12_bk = match.best_dc_12
    if dc1x and dcx2 and dc12:
        markets.append({
            "group": "Doppia Chance",
            "options": [
                {"market": "1X", "odds": dc1x, "bookmaker": dc1x_bk, "implied_prob": round((1/dc1x)*100, 1)},
                {"market": "X2", "odds": dcx2, "bookmaker": dcx2_bk, "implied_prob": round((1/dcx2)*100, 1)},
                {"market": "12", "odds": dc12, "bookmaker": dc12_bk, "implied_prob": round((1/dc12)*100, 1)},
            ],
            "overround": 0,
            "margin_pct": 0,
        })

    bo15, bo15_bk = match.best_over15
    bu15, bu15_bk = match.best_under15
    _add_pair("Over/Under 1.5", "OVER 1.5", "UNDER 1.5", bo15, bo15_bk, bu15, bu15_bk)

    bo, bo_bk = match.best_over25
    bu, bu_bk = match.best_under25
    _add_pair("Over/Under 2.5", "OVER 2.5", "UNDER 2.5", bo, bo_bk, bu, bu_bk)

    bo35, bo35_bk = match.best_over35
    bu35, bu35_bk = match.best_under35
    _add_pair("Over/Under 3.5", "OVER 3.5", "UNDER 3.5", bo35, bo35_bk, bu35, bu35_bk)

    bgg, bgg_bk = match.best_gg
    bng, bng_bk = match.best_ng
    _add_pair("GG/NG", "GG", "NG", bgg, bgg_bk, bng, bng_bk)

    return markets


def _compute_odds_spread(match: Match) -> dict:
    """Calcola la dispersione quote tra bookmaker (segnale di disaccordo)."""
    spreads = {}
    homes = [o.home for o in match.odds if o.home]
    draws = [o.draw for o in match.odds if o.draw]
    aways = [o.away for o in match.odds if o.away]

    if homes:
        spreads["1"] = {"min": min(homes), "max": max(homes), "spread": round(max(homes) - min(homes), 2)}
    if draws:
        spreads["X"] = {"min": min(draws), "max": max(draws), "spread": round(max(draws) - min(draws), 2)}
    if aways:
        spreads["2"] = {"min": min(aways), "max": max(aways), "spread": round(max(aways) - min(aways), 2)}

    return spreads


# ------------------------------------------------------------------ #
#  Prompt                                                              #
# ------------------------------------------------------------------ #

SYSTEM_PROMPT = """Sei un quantitative betting analyst con 20 anni di esperienza nel calcio europeo.
Il tuo obiettivo è trovare VALUE BETS: scommesse dove la probabilità reale è SUPERIORE a quella implicita nelle quote.

**METODO DI LAVORO:**
1. Ti fornisco le quote, le probabilità implicite già calcolate, il margine del bookmaker, la classifica e la forma recente.
2. Tu DEVI valutare OGNI mercato per cui ricevi dati quantitativi. Non limitarti al 1X2.
3. Per ogni mercato, stima una probabilità reale (tua_prob%) basandoti su TUTTI i dati forniti.
4. Calcola l'Expected Value: EV% = (tua_prob% x quota / 100 - 1) x 100. Un EV positivo = value bet.
5. Raccomanda le migliori scommesse con EV% > +3%.

**VINCOLO CRITICO SULLA PROBABILITA:**
- La tua stima di probabilità NON può deviare più di 10-15 punti percentuali dalla probabilità implicita nelle quote.
- I bookmaker hanno modelli statistici sofisticati. Se pensi che sbaglino di più di 15 punti, probabilmente sei tu a sbagliare.
- Un EV realistico nel betting professionistico è tra +2% e +15%. Valori sopra +20% sono quasi sempre errori di stima.

**ANALISI OBBLIGATORIA PER MERCATO:**
- 1X2: Valuta forza relativa, classifica, forma casa/trasferta
- DOPPIA CHANCE (1X, X2, 12): Copertura parziale — quota bassa ma alta probabilità. Valuta se il valore c'è.
- OVER/UNDER 1.5: La partita avrà almeno 2 gol? Valuta solidità difensiva e capacità offensiva.
- OVER/UNDER 2.5: Valuta media gol delle squadre, tendenze recenti, stile di gioco
- OVER/UNDER 3.5: Partita aperta con molti gol? Valuta squadre offensive vs difensive.
- GG/NG: Valuta capacità offensive/difensive di entrambe le squadre

**REGOLE:**
- Valuta SOLO i mercati per cui ricevi dati quantitativi (quote e probabilità implicite). Se un mercato non ha dati, NON includerlo.
- Per ogni mercato indica se play=true (valore trovato) o play=false (non conviene).
- NON raccomandare mai scommesse solo perché "la quota è buona". Serve un motivo analitico.
- Considera: classifica, forma recente, assenti, arbitro (rigori e cartellini per partita), diffidati (giocatori a rischio squalifica), dinamica favorita/sfavorita (la squadra sfavorita difende di più → più falli e cartellini), motivazione stagionale, tendenze gol.
- Se non hai abbastanza dati per una stima affidabile, metti play=false.

**FORMATO RISPOSTA — JSON array:**
[
  {"play": true, "market": "1", "confidence": "Alta", "your_prob": 55.0, "ev_pct": 8.5, "reasoning": "...", "tips": ["..."]},
  {"play": false, "market": "X", "confidence": "Moderata", "your_prob": 28.0, "ev_pct": -2.0, "reasoning": "...", "tips": []},
  {"play": true, "market": "OVER 2.5", "confidence": "Alta", "your_prob": 62.0, "ev_pct": 11.5, "reasoning": "...", "tips": ["..."]},
  {"play": true, "market": "1X", "confidence": "Media", "your_prob": 72.0, "ev_pct": 5.0, "reasoning": "...", "tips": ["..."]}
]

Mercati possibili: "1", "X", "2", "1X", "X2", "12", "OVER 1.5", "UNDER 1.5", "OVER 2.5", "UNDER 2.5", "OVER 3.5", "UNDER 3.5", "GG", "NG"
- confidence: "Alta" (EV>12%), "Media" (EV 7-12%), "Moderata" (EV 3-7%)
- your_prob: la tua stima di probabilità reale (%)
- ev_pct: Expected Value calcolato

**CASO SPECIALE — QUOTE NON DISPONIBILI:**
Se NON ricevi quote bookmaker (sezione "ANALISI QUANTITATIVA" vuota o assente), devi comunque analizzare la partita.
- Usa TUTTI i dati alternativi: classifica, forma recente, assenti, arbitro, diffidati, dinamica favorita/sfavorita, meteo, news giocatori, allenatore.
- Analizza i mercati principali: "1", "X", "2", "OVER 2.5", "UNDER 2.5", "GG", "NG".
- Stima le probabilità basandoti SOLO sui dati disponibili.
- Metti ev_pct a 0.0 (non calcolabile senza quote).
- La confidence si basa sulla solidità dei dati: "Alta" se hai molti dati coerenti, "Media" se dati parziali, "Moderata" se pochi dati.
- Spiega nel reasoning su cosa basi la tua analisi.
"""


def _build_user_prompt(match: Match, standings: list = None, form_home: list = None, form_away: list = None) -> str:
    lines = [
        f"PARTITA: {match.home_team} vs {match.away_team}",
        f"CAMPIONATO: {match.league}",
        f"DATA: {match.commence_time}",
        f"BOOKMAKER ANALIZZATI: {match.bookmaker_count()}",
    ]

    # Calcoli matematici (solo se ci sono quote)
    if match.odds:
        market_stats = _compute_market_stats(match)
        spreads = _compute_odds_spread(match)

        lines.append("")
        lines.append("=" * 50)
        lines.append("ANALISI QUANTITATIVA (calcolata, non stimata)")
        lines.append("=" * 50)

        for mg in market_stats:
            lines.append(f"\n--- {mg['group']} (margine book: {mg['margin_pct']}%) ---")
            for opt in mg["options"]:
                spread_info = ""
                if opt["market"] in spreads:
                    s = spreads[opt["market"]]
                    spread_info = f" | Spread book: {s['min']:.2f}-{s['max']:.2f} (Δ{s['spread']:.2f})"
                lines.append(
                    f"  {opt['market']}: quota {opt['odds']:.2f} @ {opt['bookmaker']} "
                    f"| Prob.implicita={opt['implied_prob']}%{spread_info}"
                )

        # Quote dettagliate per bookmaker (top 5 per non esplodere il prompt)
        lines.append("")
        lines.append("QUOTE PER BOOKMAKER (top 5):")
        for o in match.odds[:5]:
            parts = [f"  {o.bookmaker}:"]
            if o.home:    parts.append(f"1={o.home:.2f}")
            if o.draw:    parts.append(f"X={o.draw:.2f}")
            if o.away:    parts.append(f"2={o.away:.2f}")
            if o.over15:  parts.append(f"O1.5={o.over15:.2f}")
            if o.under15: parts.append(f"U1.5={o.under15:.2f}")
            if o.over25:  parts.append(f"O2.5={o.over25:.2f}")
            if o.under25: parts.append(f"U2.5={o.under25:.2f}")
            if o.over35:  parts.append(f"O3.5={o.over35:.2f}")
            if o.under35: parts.append(f"U3.5={o.under35:.2f}")
            if o.gg:      parts.append(f"GG={o.gg:.2f}")
            if o.ng:      parts.append(f"NG={o.ng:.2f}")
            if o.dc_1x:   parts.append(f"1X={o.dc_1x:.2f}")
            if o.dc_x2:   parts.append(f"X2={o.dc_x2:.2f}")
            if o.dc_12:   parts.append(f"12={o.dc_12:.2f}")
            lines.append(" | ".join(parts))
    else:
        lines.append("")
        lines.append("=" * 50)
        lines.append("⚠ QUOTE BOOKMAKER NON DISPONIBILI")
        lines.append("=" * 50)
        lines.append("Analizza la partita basandoti su classifica, forma, assenti, arbitro, meteo, news e altri dati disponibili.")
        lines.append("Fornisci comunque le tue stime di probabilità per i mercati principali (1, X, 2, OVER 2.5, UNDER 2.5, GG, NG).")

    # Classifica
    if standings:
        lines.append("")
        lines.append("CLASSIFICA ATTUALE:")
        home_pos = away_pos = None
        for s in standings:
            if s["team_name"] and match.home_team and s["team_name"].lower() in match.home_team.lower() or match.home_team.lower() in s.get("team_name", "").lower():
                home_pos = s
            if s["team_name"] and match.away_team and s["team_name"].lower() in match.away_team.lower() or match.away_team.lower() in s.get("team_name", "").lower():
                away_pos = s
        if home_pos:
            lines.append(f"  {match.home_team}: {home_pos['position']}° posto, {home_pos['points']} pt, {home_pos['played']} giocate, diff.reti {home_pos['goals_diff']:+d}")
        if away_pos:
            lines.append(f"  {match.away_team}: {away_pos['position']}° posto, {away_pos['points']} pt, {away_pos['played']} giocate, diff.reti {away_pos['goals_diff']:+d}")
        if not home_pos and not away_pos:
            lines.append("  (dati classifica non disponibili per queste squadre)")

    # Forma recente
    if form_home:
        results = " | ".join([f"{r['text']} ({r['outcome']})" for r in form_home])
        lines.append(f"\nFORMA RECENTE {match.home_team}: {results}")
    if form_away:
        results = " | ".join([f"{r['text']} ({r['outcome']})" for r in form_away])
        lines.append(f"FORMA RECENTE {match.away_team}: {results}")

    # Arbitro
    if match.referee:
        lines.append("")
        lines.append(f"ARBITRO: {match.referee}")
        if match.referee_stats:
            rs = match.referee_stats
            if rs.get("matches") and rs["matches"] >= 3:
                lines.append(f"  - {rs['matches']} gare arbitrate questa stagione")
                if rs.get("penalties_per_match") is not None:
                    lines.append(f"  - {rs['penalties_per_match']} rigori/partita")
                if rs.get("cards_per_match") is not None:
                    lines.append(f"  - {rs['cards_per_match']} cartellini/partita")
            else:
                lines.append(f"  - Solo {rs.get('matches', 0)} gare — dati insufficienti")

    # Assenti
    if match.absentees:
        lines.append("")
        lines.append("GIOCATORI ASSENTI:")
        for a in match.absentees:
            team_name = match.home_team if a['team'] == 'home' else match.away_team
            lines.append(f"  - {team_name}: {a['player']} ({a['reason']})")

    # Rigoristi e cross-check con assenti
    absent_names = {a["player"].lower() for a in match.absentees} if match.absentees else set()

    def _format_takers(takers, team_name, side):
        if not takers:
            return
        lines.append(f"\nRIGORISTA/I {team_name}:")
        for t in takers[:3]:
            conv = f"{t['scored']}/{t['taken']}"
            is_absent = any(aname in t["player"].lower() or t["player"].lower() in aname
                           for aname in absent_names)
            absent_flag = " ⚠ ASSENTE" if is_absent else ""
            lines.append(f"  - {t['player']}: {t['taken']} rigori tirati ({conv} segnati){absent_flag}")
        absent_takers = [t for t in takers[:2]
                         if any(aname in t["player"].lower() or t["player"].lower() in aname
                                for aname in absent_names)]
        if absent_takers:
            lines.append(f"  ⚠ RIGORISTA PRINCIPALE ASSENTE — rischio conversione più basso")

    _format_takers(match.penalty_takers_home, match.home_team, "home")
    _format_takers(match.penalty_takers_away, match.away_team, "away")

    # Diffidati (players one yellow away from suspension)
    if match.home_diffidati or match.away_diffidati:
        lines.append("")
        lines.append("GIOCATORI DIFFIDATI (1 giallo dalla squalifica):")
        for d in match.home_diffidati:
            lines.append(f"  - {match.home_team}: {d['player']} ({d['yellows']} gialli)")
        for d in match.away_diffidati:
            lines.append(f"  - {match.away_team}: {d['player']} ({d['yellows']} gialli)")
        lines.append("  → I diffidati possono essere più prudenti (meno falli) o sotto pressione")

    # Underdog/favorite analysis from league positions
    if match.home_position > 0 and match.away_position > 0:
        pos_diff = abs(match.home_position - match.away_position)
        if pos_diff >= 5:
            underdog = match.home_team if match.home_position > match.away_position else match.away_team
            favorite = match.away_team if match.home_position > match.away_position else match.home_team
            lines.append("")
            lines.append(f"DINAMICA TATTICA: {favorite} ({min(match.home_position, match.away_position)}°) vs "
                         f"{underdog} ({max(match.home_position, match.away_position)}°)")
            lines.append(f"  → {underdog} probabile approccio difensivo: più falli, più cartellini, possibili rigori contro")

    lines.append("")
    if match.odds:
        lines.append("Analizza TUTTI i mercati per cui hai dati quantitativi sopra e restituisci un JSON array con una entry per ciascun mercato analizzato.")
    else:
        lines.append("QUOTE NON DISPONIBILI — analizza comunque la partita con i dati sopra. Restituisci un JSON array con le tue stime per i mercati: 1, X, 2, OVER 2.5, UNDER 2.5, GG, NG. Metti ev_pct=0.0 per tutti.")
    return "\n".join(lines)


# ------------------------------------------------------------------ #
#  Analisi AI                                                          #
# ------------------------------------------------------------------ #

def analyze_match(match: Match, client: OpenAI, model: str,
                  standings: list = None, form_home: list = None, form_away: list = None) -> list[Recommendation]:
    """Chiama l'AI e restituisce una lista di raccomandazioni (max 3)."""
    prompt = _build_user_prompt(match, standings, form_home, form_away)

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user",   "content": prompt},
            ],
            temperature=0.2,
            max_tokens=3500,
        )
        raw = response.choices[0].message.content.strip()
        logger.info(f"AI Response for {match.home_team} vs {match.away_team}: {raw}")

        json_match = re.search(r'\[.*\]', raw, re.DOTALL)
        if not json_match:
            obj_match = re.search(r'\{.*\}', raw, re.DOTALL)
            if obj_match:
                data = [json.loads(obj_match.group())]
            else:
                logger.warning(f"Nessun JSON nella risposta per {match.home_team} vs {match.away_team}: {raw[:200]}")
                return []
        else:
            data = json.loads(json_match.group())

        if not isinstance(data, list):
            data = [data]

        recommendations = []
        seen_markets = set()

        for item in data:
            market = item.get("market", "?").upper()
            if market in seen_markets:
                continue
            seen_markets.add(market)

            your_prob = float(item.get("your_prob", 0))

            best_odds = _get_best_odds_for_market(match, market)
            best_bk = _get_bookmaker_for_market(match, market)
            has_odds = best_odds > 0

            if has_odds:
                # Guardrail: limita la deviazione della probabilità AI
                # rispetto alla probabilità implicita nelle quote
                MAX_DEVIATION = 15.0  # punti percentuali
                MAX_EV = 25.0  # EV massimo realistico

                implied_prob = 100.0 / best_odds

                if your_prob > 0:
                    deviation = your_prob - implied_prob
                    if abs(deviation) > MAX_DEVIATION:
                        your_prob = implied_prob + (MAX_DEVIATION if deviation > 0 else -MAX_DEVIATION)
                        your_prob = max(5.0, min(95.0, your_prob))

                    calculated_ev = (your_prob / 100) * best_odds - 1
                    calculated_ev_pct = round(calculated_ev * 100, 1)
                    calculated_ev_pct = min(MAX_EV, calculated_ev_pct)
                else:
                    calculated_ev_pct = 0.0

                is_play = bool(item.get("play", False)) and calculated_ev_pct > 2

                if is_play:
                    if calculated_ev_pct >= 15:
                        confidence = "Alta"
                    elif calculated_ev_pct >= 8:
                        confidence = "Media"
                    else:
                        confidence = "Moderata"
                    value_rating = min(10.0, max(5.0, calculated_ev_pct * 0.3 + 5))
                else:
                    confidence = "Basso"
                    value_rating = max(0.0, min(4.0, calculated_ev_pct * 0.3 + 2))

                reasoning = item.get("reasoning", "")
                reasoning += f"\n[EV: {calculated_ev_pct:+.1f}% | Prob.stimata: {your_prob:.1f}% | Prob.implicita: {implied_prob:.1f}% | Quota: {best_odds:.2f}]"
            else:
                # Modalità senza quote — analisi basata solo su dati statistici
                calculated_ev_pct = 0.0
                is_play = bool(item.get("play", False))

                # Confidence basata sulla probabilità stimata dall'AI
                ai_confidence = item.get("confidence", "Moderata")
                if ai_confidence in ("Alta", "Media", "Moderata"):
                    confidence = ai_confidence
                elif your_prob >= 65:
                    confidence = "Alta"
                elif your_prob >= 50:
                    confidence = "Media"
                else:
                    confidence = "Moderata"

                if is_play:
                    value_rating = min(8.0, max(5.0, your_prob * 0.08))
                else:
                    confidence = "Basso"
                    value_rating = max(1.0, min(4.0, your_prob * 0.05))

                reasoning = item.get("reasoning", "")
                reasoning += f"\n[⚠ Quote non disponibili | Prob.stimata: {your_prob:.1f}% | Analisi basata su dati statistici]"
                best_bk = "N/A (no quote)"

            rec = Recommendation(
                play=is_play,
                market=market,
                odds_value=best_odds,
                bookmaker=best_bk,
                confidence=confidence,
                value_rating=round(value_rating, 1),
                reasoning=reasoning,
                tips=item.get("tips", []),
            )
            recommendations.append(rec)

        recommendations.sort(key=lambda r: (r.play, r.value_rating), reverse=True)
        return recommendations

    except Exception as e:
        logger.error(f"Errore analisi AI {match.home_team} vs {match.away_team}: {e}")
        return []


def _league_key_from_name(league_name: str) -> str | None:
    mapping = {
        "Serie A": "italy_serie_a",
        "Premier League": "england_premier_league",
        "La Liga": "spain_la_liga",
        "Bundesliga": "germany_bundesliga",
        "Superliga": "denmark_superliga",
        "Premiership": "scotland_premiership",
    }
    return mapping.get(league_name)


def analyze_all(matches: list[Match], api_key: str, model: str,
                on_progress=None, sportmonks_client=None,
                football_data_client=None) -> list[Match]:
    """Analizza tutte le partite e aggiunge le raccomandazioni."""
    client = OpenAI(
        api_key=api_key,
        base_url="https://openrouter.ai/api/v1",
        default_headers={
            "HTTP-Referer": "http://localhost:5000",
            "X-Title": "BetAnalyzer",
        }
    )

    # Pre-carica classifica
    standings_cache = {}
    if sportmonks_client:
        for match in matches:
            if match.league_id and match.season_id:
                cache_key = f"{match.league_id}_{match.season_id}"
                if cache_key not in standings_cache:
                    standings_cache[cache_key] = sportmonks_client.get_standings(
                        match.league_id, match.season_id
                    )

    if football_data_client:
        leagues_seen = set()
        for match in matches:
            lk = _league_key_from_name(match.league)
            if lk and lk not in leagues_seen:
                leagues_seen.add(lk)
                st = football_data_client.get_standings(lk)
                if st:
                    standings_cache[f"fd_{lk}"] = st

    # Load referee data from penalty cache (no extra API calls for match details)
    referee_cache: dict[str, dict] = {}
    fd_api_key = os.getenv("FOOTBALL_DATA_API_KEY", "")
    if fd_api_key:
        try:
            pa = PenaltyAnalyzer(fd_api_key)
            leagues_for_ref = set()
            for match in matches:
                lk = _league_key_from_name(match.league)
                if lk and lk not in leagues_for_ref:
                    leagues_for_ref.add(lk)
                    ref_data = pa.get_referee_data_for_league(lk)
                    referee_cache.update(ref_data)
            logger.info("Referee data loaded for %d matches", len(referee_cache))
        except Exception as e:
            logger.warning("Could not load referee data: %s", e)

    # Assign referee to matches (fuzzy name matching)
    for match in matches:
        ref_info = referee_cache.get((match.home_team, match.away_team))
        if not ref_info:
            for (h, a), ri in referee_cache.items():
                h_low, a_low = h.lower(), a.lower()
                mh, ma = match.home_team.lower(), match.away_team.lower()
                if (mh in h_low or h_low in mh) and (ma in a_low or a_low in ma):
                    ref_info = ri
                    break
        if ref_info:
            match.referee = ref_info.get("referee")
            match.referee_stats = ref_info.get("stats")
            match.penalty_takers_home = ref_info.get("home_penalty_takers", [])
            match.penalty_takers_away = ref_info.get("away_penalty_takers", [])
            match.home_position = ref_info.get("home_position", 0)
            match.away_position = ref_info.get("away_position", 0)
            match.home_diffidati = ref_info.get("home_diffidati", [])
            match.away_diffidati = ref_info.get("away_diffidati", [])
            match.matchday = ref_info.get("matchday")

    total = len(matches)
    for i, match in enumerate(matches):
        has_odds = bool(match.odds)
        if not has_odds:
            logger.info(f"Nessuna quota per {match.home_team} vs {match.away_team} — analisi senza quote")
        label = f"{match.home_team} vs {match.away_team}"
        logger.info(f"[{i+1}/{total}] Analizzo: {label}")
        if on_progress:
            on_progress(i + 1, total, label)

        standings = None
        form_home = None
        form_away = None

        if sportmonks_client:
            cache_key = f"{match.league_id}_{match.season_id}"
            standings = standings_cache.get(cache_key)
            if match.home_id:
                form_home = sportmonks_client.get_last_results(match.home_id, limit=5)
            if match.away_id:
                form_away = sportmonks_client.get_last_results(match.away_id, limit=5)

        if football_data_client and not standings:
            lk = _league_key_from_name(match.league)
            if lk:
                standings = standings_cache.get(f"fd_{lk}")
                form_home = football_data_client.get_team_form(match.home_team, lk, limit=5)
                form_away = football_data_client.get_team_form(match.away_team, lk, limit=5)

        match.recommendations = analyze_match(
            match, client, model,
            standings=standings, form_home=form_home, form_away=form_away
        )

    return matches


# ------------------------------------------------------------------ #
#  Utility                                                             #
# ------------------------------------------------------------------ #

def _market_map(match: Match) -> dict:
    return {
        "1":         match.best_home,
        "X":         match.best_draw,
        "2":         match.best_away,
        "1X":        match.best_dc_1x,
        "X2":        match.best_dc_x2,
        "12":        match.best_dc_12,
        "OVER 1.5":  match.best_over15,
        "UNDER 1.5": match.best_under15,
        "OVER 2.5":  match.best_over25,
        "UNDER 2.5": match.best_under25,
        "OVER 3.5":  match.best_over35,
        "UNDER 3.5": match.best_under35,
        "GG":        match.best_gg,
        "NG":        match.best_ng,
    }


def _get_best_odds_for_market(match: Match, market: str) -> float:
    result = _market_map(match).get(market)
    return result[0] if result else 0.0


def _get_bookmaker_for_market(match: Match, market: str) -> str:
    result = _market_map(match).get(market)
    return result[1] if result else "N/A"


def deep_analyze_match(match: Match, match_context: str, client: OpenAI, model: str) -> dict:
    """Analisi profonda basata su dati reali: quote, classifica, forma, assenti."""
    prompt = f"""ANALISI INTELLIGENCE: {match.home_team} vs {match.away_team}
CAMPIONATO: {match.league} | DATA: {match.commence_time}

DATI DISPONIBILI:
{match_context}

COMPITO:
Valuta il match secondo i 10 criteri sotto. Usa SOLO i dati forniti sopra.
Per ogni criterio assegna uno score da -1.0 a +1.0:
  - Positivo (+) = vantaggio per la squadra di casa
  - Negativo (-) = vantaggio per la squadra ospite
  - 0.0 = neutro o dati insufficienti

CRITERI:
1. Forma Recente: Analizza le ultime partite di entrambe. Chi è in serie positiva/negativa?
2. Posizione in Classifica: Differenza di qualità tra le due squadre in base a punti e posizione.
3. Motivazione Stagionale: Lotta salvezza, corsa al titolo, Europa, o nulla da chiedere?
4. Forza Offensiva: Basandoti sui risultati recenti, chi segna di più? Quanti gol per partita?
5. Solidità Difensiva: Chi subisce meno gol? Quanti clean sheet recenti?
6. Impatto Assenti: Ci sono giocatori chiave assenti? Quanto impattano sulla tattica?
7. Fattore Casa/Trasferta: La squadra di casa è forte in casa? L'ospite rende in trasferta?
8. Valore Quote: Le quote riflettono il reale rapporto di forza o c'è discrepanza?
9. Trend Gol: Le partite di queste squadre tendono all'Over o Under 2.5? GG o NG?
10. Convergenza Segnali: I vari indicatori puntano nella stessa direzione o sono contraddittori?

Rispondi in JSON:
{{
    "reasoning": "Sintesi di 4-5 righe che collega i criteri alla decisione di betting",
    "criteria": [
        {{"name": "Forma Recente", "score": 0.7, "note": "Breve spiegazione con dati specifici"}},
        ... (tutti i 10 criteri)
    ],
    "total_adjustment": 0.0,
    "verdict": "Conferma Valore / Cautela / Evitare",
    "best_bet": "Il mercato migliore da giocare (es: OVER 2.5, 1, GG) o 'Nessuno' se troppo rischioso"
}}"""

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "Sei un analista quantitativo di scommesse sportive. Basi ogni valutazione su dati concreti, mai su impressioni. Se un dato manca, lo dici esplicitamente e assegni score 0."},
                {"role": "user", "content": prompt}
            ],
            response_format={"type": "json_object"},
            temperature=0.2,
            max_tokens=2000,
        )
        raw = response.choices[0].message.content.strip()
        logger.info(f"Deep Analysis for {match.home_team} vs {match.away_team}: {raw[:300]}")

        json_match = re.search(r'\{.*\}', raw, re.DOTALL)
        if json_match:
            return json.loads(json_match.group())

        return json.loads(raw)
    except Exception as e:
        logger.error(f"Errore Deep Analysis AI: {e}")
        return {
            "reasoning": "Errore durante l'analisi profonda.",
            "criteria": [],
            "total_adjustment": 0,
            "verdict": "Errore"
        }
