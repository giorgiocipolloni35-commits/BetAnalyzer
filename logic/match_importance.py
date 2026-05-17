"""
Match Importance Engine — Calcola il contesto e l'importanza di una partita
basandosi su classifica, fase campionato, scontri diretti e derby.
"""
from typing import Optional
import logging

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ #
#  Derby noti (coppie di team_name parziali, case-insensitive)        #
# ------------------------------------------------------------------ #
KNOWN_DERBIES = [
    # Italia
    ("inter", "milan", "Derby della Madonnina"),
    ("roma", "lazio", "Derby della Capitale"),
    ("juventus", "torino", "Derby della Mole"),
    ("genoa", "sampdoria", "Derby della Lanterna"),
    ("napoli", "salernitana", "Derby campano"),
    ("fiorentina", "empoli", "Derby toscano"),
    ("verona", "chievo", "Derby di Verona"),
    # Spagna
    ("real madrid", "atletico", "Derby di Madrid"),
    ("real madrid", "barcelona", "El Clásico"),
    ("barcelona", "espanyol", "Derby di Barcellona"),
    ("sevilla", "betis", "Derby di Siviglia"),
    ("athletic", "real sociedad", "Derby basco"),
    ("valencia", "villarreal", "Derby della Comunitat"),
    # Inghilterra
    ("liverpool", "everton", "Merseyside Derby"),
    ("manchester united", "manchester city", "Manchester Derby"),
    ("arsenal", "tottenham", "North London Derby"),
    ("chelsea", "tottenham", "London Derby"),
    ("chelsea", "arsenal", "London Derby"),
    ("aston villa", "west brom", "West Midlands Derby"),
    ("newcastle", "sunderland", "Tyne-Wear Derby"),
    # Germania
    ("dortmund", "schalke", "Revierderby"),
    ("bayern", "dortmund", "Der Klassiker"),
    ("hamburg", "werder", "Nordderby"),
    # Francia
    ("paris saint", "marseille", "Le Classique"),
    ("lyon", "saint-etienne", "Derby du Rhône"),
    ("nice", "monaco", "Derby della Costa Azzurra"),
    ("lille", "lens", "Derby du Nord"),
]

# ------------------------------------------------------------------ #
#  Zone di classifica per lega                                        #
# ------------------------------------------------------------------ #
LEAGUE_ZONES = {
    # (retro_start, europa_start, champions_end, total_teams, total_matchdays)
    "Serie A":          {"retro": 18, "europa": 5, "champions": 4, "teams": 20, "matchdays": 38},
    "Premier League":   {"retro": 18, "europa": 5, "champions": 4, "teams": 20, "matchdays": 38},
    "La Liga":          {"retro": 18, "europa": 5, "champions": 4, "teams": 20, "matchdays": 38},
    "Bundesliga":       {"retro": 16, "europa": 5, "champions": 4, "teams": 18, "matchdays": 34},
    "Ligue 1":          {"retro": 16, "europa": 5, "champions": 3, "teams": 18, "matchdays": 34},
    "Eredivisie":       {"retro": 16, "europa": 5, "champions": 3, "teams": 18, "matchdays": 34},
    "Superliga":        {"retro": 12, "europa": 3, "champions": 1, "teams": 12, "matchdays": 32},
    "Premiership":      {"retro": 11, "europa": 3, "champions": 1, "teams": 12, "matchdays": 38},
    "Champions League": {"retro": 0,  "europa": 0, "champions": 0, "teams": 36, "matchdays": 8},
}


def detect_derby(home_name: str, away_name: str) -> Optional[str]:
    """Restituisce il nome del derby se è uno scontro noto, altrimenti None."""
    h = home_name.lower()
    a = away_name.lower()
    for t1, t2, name in KNOWN_DERBIES:
        if (t1 in h and t2 in a) or (t2 in h and t1 in a):
            return name
    return None


def _find_team_in_standings(team_name: str, standings: list) -> Optional[dict]:
    """Trova una squadra nella classifica con fuzzy matching migliorato."""
    tn = team_name.lower().strip()

    # 1. Match esatto
    for s in standings:
        sn = s.get("team_name", "").lower().strip()
        if tn == sn:
            return s

    # 2. Contenimento completo (uno contiene l'altro interamente)
    for s in standings:
        sn = s.get("team_name", "").lower().strip()
        if tn in sn or sn in tn:
            return s

    # 3. Match su parole significative (>3 chars, escluse parole comuni)
    COMMON_WORDS = {"real", "club", "sporting", "athletic", "city", "united", "fc", "cf"}
    words = [w for w in tn.split() if len(w) > 3 and w not in COMMON_WORDS]
    if words:
        # Cerca il candidato con più parole in comune
        best = None
        best_count = 0
        for s in standings:
            sn = s.get("team_name", "").lower()
            count = sum(1 for w in words if w in sn)
            if count > best_count:
                best_count = count
                best = s
        if best and best_count > 0:
            return best

    # 4. Ultima risorsa: anche parole comuni, ma servono almeno 2 match
    all_words = [w for w in tn.split() if len(w) > 2]
    if len(all_words) >= 2:
        for s in standings:
            sn = s.get("team_name", "").lower()
            count = sum(1 for w in all_words if w in sn)
            if count >= 2:
                return s

    return None


def calculate_match_importance(
    home_name: str,
    away_name: str,
    league_name: str,
    standings: list,
    current_matchday: Optional[int] = None,
) -> dict:
    """
    Calcola l'importanza della partita e genera contesto testuale.

    Returns:
        {
            "score": float (0-10),
            "tags": list[str],
            "home_context": str,
            "away_context": str,
            "narrative": str,      # testo per l'AI
            "derby": Optional[str],
        }
    """
    zones = LEAGUE_ZONES.get(league_name, LEAGUE_ZONES.get("Serie A"))
    total_md = zones["matchdays"]
    retro_line = zones["retro"]
    europa_line = zones["europa"]
    champ_line = zones["champions"]
    total_teams = zones["teams"]

    # Trova le squadre in classifica
    home_st = _find_team_in_standings(home_name, standings)
    away_st = _find_team_in_standings(away_name, standings)

    if not home_st or not away_st:
        logger.warning(f"Match importance: squadre non trovate in classifica ({home_name}/{away_name})")
        derby = detect_derby(home_name, away_name)
        return {
            "score": 5.0,
            "tags": ["⚔️ " + derby] if derby else [],
            "home_context": "",
            "away_context": "",
            "narrative": f"DERBY: {derby}" if derby else "",
            "derby": derby,
        }

    h_pos = home_st.get("position", 10)
    a_pos = away_st.get("position", 10)
    h_pts = home_st.get("points", 0)
    a_pts = away_st.get("points", 0)
    h_played = home_st.get("played", 0)
    a_played = away_st.get("played", 0)

    # Stima matchday corrente
    if not current_matchday:
        current_matchday = max(h_played, a_played)
    remaining = max(1, total_md - current_matchday)

    # ------------------------------------------------------------------ #
    #  Analisi zona per ogni squadra                                      #
    # ------------------------------------------------------------------ #
    def _analyze_team(name, pos, pts, played):
        tags = []
        zone = "mid"
        zone_label = "metà classifica"
        pressure = 0  # 0-10

        # Calcola distanza dalle zone critiche
        # Punti della squadra al limite retro
        retro_team = next((s for s in standings if s.get("position") == retro_line), None)
        retro_pts = retro_team.get("points", 0) if retro_team else 0
        safe_team = next((s for s in standings if s.get("position") == retro_line - 1), None)
        safe_pts = safe_team.get("points", 0) if safe_team else 0

        # Punti per Europa/Champions
        europa_team = next((s for s in standings if s.get("position") == europa_line), None)
        europa_pts = europa_team.get("points", 0) if europa_team else 0
        champ_team = next((s for s in standings if s.get("position") == champ_line), None)
        champ_pts = champ_team.get("points", 0) if champ_team else 0
        leader_team = next((s for s in standings if s.get("position") == 1), None)
        leader_pts = leader_team.get("points", 0) if leader_team else 0

        gap_to_retro = pts - retro_pts  # positivo = sopra la zona
        gap_to_europa = europa_pts - pts  # positivo = sotto la zona europa
        gap_to_champ = champ_pts - pts
        gap_to_leader = leader_pts - pts

        # Retrocessione
        if pos >= retro_line:
            zone = "relegation"
            zone_label = "🔴 IN ZONA RETROCESSIONE"
            tags.append("🔴 RETROCESSIONE")
            pressure = min(10, 7 + (remaining <= 5) * 2 + (remaining <= 3))
        elif gap_to_retro <= 3 and pos >= retro_line - 3:
            zone = "relegation_danger"
            zone_label = f"🟡 {gap_to_retro}pt dalla zona retro"
            tags.append("🟡 RISCHIO RETROCESSIONE")
            pressure = min(10, 5 + (3 - gap_to_retro) + (remaining <= 5) * 2)
        elif gap_to_retro <= 6 and pos >= retro_line - 5:
            zone = "lower_mid"
            zone_label = f"🟠 {gap_to_retro}pt di margine sulla retro"
            pressure = 3

        # Champions League
        elif pos <= champ_line:
            zone = "champions"
            zone_label = f"🟣 {pos}° posto — zona Champions"
            tags.append("🟣 CHAMPIONS LEAGUE")
            # Pressione se inseguito
            chaser = next((s for s in standings if s.get("position") == champ_line + 1), None)
            chaser_pts = chaser.get("points", 0) if chaser else 0
            gap_below = pts - chaser_pts
            if gap_below <= 3:
                pressure = min(10, 6 + (3 - gap_below) + (remaining <= 5))
                tags.append(f"⚡ {gap_below}pt di vantaggio sul {champ_line+1}°")
            else:
                pressure = 4

        # Europa
        elif pos <= europa_line + 2 and gap_to_europa <= 4:
            zone = "europa"
            if pos <= europa_line:
                zone_label = f"🔵 {pos}° posto — zona Europa"
                tags.append("🔵 EUROPA")
            else:
                zone_label = f"🔵 {gap_to_europa}pt dalla zona Europa"
                tags.append("🔵 CORSA EUROPA")
            pressure = min(10, 5 + (remaining <= 5) * 2)

        # Lotta scudetto
        if pos <= 3 and gap_to_leader <= 6:
            zone = "title_race"
            if pos == 1:
                zone_label = f"🏆 CAPOLISTA ({gap_to_leader}pt dal 2°)"
                tags.append("🏆 LOTTA SCUDETTO")
            else:
                zone_label = f"🏆 {gap_to_leader}pt dalla vetta"
                tags.append("🏆 LOTTA SCUDETTO")
            pressure = min(10, 7 + (remaining <= 5) * 2)

        # Niente da giocarsi
        if not tags and pos > champ_line and gap_to_retro > 10:
            zone = "nothing"
            zone_label = "⚪ Nulla in palio"
            pressure = 1

        # Amplifica pressione nelle ultime giornate
        if remaining <= 5:
            pressure = min(10, pressure + 1)
        if remaining <= 3:
            pressure = min(10, pressure + 1)

        return {
            "zone": zone,
            "zone_label": zone_label,
            "tags": tags,
            "pressure": pressure,
            "position": pos,
            "points": pts,
            "remaining": remaining,
            "gap_to_retro": gap_to_retro,
        }

    h_ctx = _analyze_team(home_name, h_pos, h_pts, h_played)
    a_ctx = _analyze_team(away_name, a_pos, a_pts, a_played)

    # ------------------------------------------------------------------ #
    #  Punteggio complessivo e narrative                                  #
    # ------------------------------------------------------------------ #
    base_score = (h_ctx["pressure"] + a_ctx["pressure"]) / 2
    tags = []
    narratives = []

    # Derby
    derby = detect_derby(home_name, away_name)
    if derby:
        tags.append(f"⚔️ {derby}")
        base_score = min(10, base_score + 1.5)
        narratives.append(f"DERBY: {derby} — partita ad alta intensità emotiva e tattica.")

    # Scontro diretto per stesso obiettivo
    if h_ctx["zone"] == a_ctx["zone"] and h_ctx["zone"] not in ("mid", "nothing", "lower_mid"):
        zone_names = {
            "relegation": "SALVEZZA", "relegation_danger": "SALVEZZA",
            "champions": "CHAMPIONS LEAGUE", "europa": "EUROPA",
            "title_race": "SCUDETTO",
        }
        zn = zone_names.get(h_ctx["zone"], h_ctx["zone"])
        tags.append(f"🎯 SCONTRO DIRETTO PER {zn}")
        base_score = min(10, base_score + 2)
        narratives.append(f"Scontro diretto per la {zn.lower()}: entrambe le squadre lottano per lo stesso obiettivo.")

    # Differenza di motivazione (una ha tutto, l'altra niente)
    pressure_diff = abs(h_ctx["pressure"] - a_ctx["pressure"])
    if pressure_diff >= 5:
        relaxed = home_name if h_ctx["pressure"] < a_ctx["pressure"] else away_name
        desperate = away_name if h_ctx["pressure"] < a_ctx["pressure"] else home_name
        tags.append("⚠️ MOTIVAZIONE ASIMMETRICA")
        narratives.append(f"Attenzione: {desperate} ha molto più in gioco di {relaxed}. Le squadre disperate possono sorprendere o crollare sotto pressione.")

    # Fase campionato
    season_pct = current_matchday / total_md
    if season_pct >= 0.85:
        tags.append("🔥 FINALE DI STAGIONE")
        base_score = min(10, base_score + 0.5)
        narratives.append(f"Fase finale del campionato (giornata {current_matchday}/{total_md}, {remaining} rimaste): ogni punto è decisivo.")
    elif season_pct >= 0.75:
        tags.append("📈 RUSH FINALE")

    # Merge tags
    all_tags = list(set(tags + h_ctx["tags"] + a_ctx["tags"]))

    # Home/Away context strings
    h_line = f"{home_name}: {h_pos}° ({h_pts}pt) — {h_ctx['zone_label']}"
    a_line = f"{away_name}: {a_pos}° ({a_pts}pt) — {a_ctx['zone_label']}"

    # Build narrative
    narrative_parts = [
        f"CONTESTO PARTITA (importanza: {min(10, round(base_score, 1))}/10)",
        f"  {h_line}",
        f"  {a_line}",
        f"  Giornata {current_matchday}/{total_md} — {remaining} partite rimanenti",
    ]
    if all_tags:
        narrative_parts.append(f"  Tags: {', '.join(all_tags)}")
    narrative_parts.extend(f"  → {n}" for n in narratives)

    # Pattern betting suggeriti dal contesto
    betting_hints = []
    if h_ctx["zone"] in ("relegation", "relegation_danger") and a_ctx["zone"] in ("relegation", "relegation_danger"):
        betting_hints.append("Scontri salvezza tendono a partite chiuse: Under e pochi gol favoriti.")
    if h_ctx["zone"] == "nothing" and a_ctx["zone"] in ("relegation", "relegation_danger"):
        betting_hints.append(f"{away_name} disperata in trasferta: potrebbe giocare molto aggressiva (Over possibile).")
    if h_ctx["zone"] == "title_race" or a_ctx["zone"] == "title_race":
        betting_hints.append("Squadre in lotta scudetto tendono a gestire il risultato: partite tattiche.")
    if derby:
        betting_hints.append("I derby sono imprevedibili: cartellini alti, ritmo intenso, risultati stretti.")

    if betting_hints:
        narrative_parts.append("  PATTERN BETTING:")
        for bh in betting_hints:
            narrative_parts.append(f"    • {bh}")

    return {
        "score": min(10, round(base_score, 1)),
        "tags": all_tags,
        "home_context": h_line,
        "away_context": a_line,
        "narrative": "\n".join(narrative_parts),
        "derby": derby,
        "home_pressure": h_ctx["pressure"],
        "away_pressure": a_ctx["pressure"],
    }
