"""
Weather Impact Engine — Analizza le condizioni meteo previste per una partita
e genera contesto per l'AI sugli impatti betting.

Usa Open-Meteo (100% gratuito, no API key) + coordinate stadio da Sportmonks.
"""

import logging
import requests
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ #
#  WMO Weather Codes → descrizione leggibile                         #
# ------------------------------------------------------------------ #
WMO_CODES = {
    0: "☀️ Sereno",
    1: "🌤️ Prevalentemente sereno",
    2: "⛅ Parzialmente nuvoloso",
    3: "☁️ Coperto",
    45: "🌫️ Nebbia",
    48: "🌫️ Nebbia con brina",
    51: "🌦️ Pioggerella leggera",
    53: "🌦️ Pioggerella moderata",
    55: "🌧️ Pioggerella intensa",
    56: "🌧️❄️ Pioggia gelata leggera",
    57: "🌧️❄️ Pioggia gelata intensa",
    61: "🌧️ Pioggia leggera",
    63: "🌧️ Pioggia moderata",
    65: "🌧️ Pioggia forte",
    66: "🌧️❄️ Pioggia gelata leggera",
    67: "🌧️❄️ Pioggia gelata forte",
    71: "🌨️ Neve leggera",
    73: "🌨️ Neve moderata",
    75: "❄️ Neve intensa",
    77: "❄️ Granelli di neve",
    80: "🌦️ Rovesci leggeri",
    81: "🌧️ Rovesci moderati",
    82: "⛈️ Rovesci violenti",
    85: "🌨️ Rovesci di neve leggeri",
    86: "❄️ Rovesci di neve intensi",
    95: "⛈️ Temporale",
    96: "⛈️ Temporale con grandine leggera",
    99: "⛈️ Temporale con grandine forte",
}


def get_venue_coords(sm_client, team_id: str) -> dict | None:
    """
    Recupera coordinate stadio via SportmonksClient (ora usa dict statico + Nominatim).

    Returns:
        {"lat": float, "lon": float, "name": str, "city": str, "surface": str}
        oppure None
    """
    if not sm_client or not team_id:
        return None
    try:
        # Il nuovo SportmonksClient ha get_venue_coords integrato
        return sm_client.get_venue_coords(team_id)
    except Exception as e:
        logger.warning(f"Errore recupero venue team {team_id}: {e}")
        return None


def get_weather_forecast(lat: float, lon: float, match_datetime: datetime = None) -> dict | None:
    """
    Recupera previsioni meteo da Open-Meteo (gratuito, no API key).

    Args:
        lat: Latitudine stadio
        lon: Longitudine stadio
        match_datetime: Orario della partita (default: prossime 3 ore)

    Returns:
        {
            "temperature": float (°C),
            "feels_like": float (°C),
            "precipitation": float (mm),
            "rain": float (mm),
            "wind_speed": float (km/h),
            "wind_gusts": float (km/h),
            "humidity": int (%),
            "weather_code": int,
            "weather_desc": str,
            "match_hour": str (HH:00),
        }
    """
    try:
        url = "https://api.open-meteo.com/v1/forecast"
        params = {
            "latitude": lat,
            "longitude": lon,
            "hourly": (
                "temperature_2m,apparent_temperature,precipitation,"
                "rain,windspeed_10m,windgusts_10m,"
                "relativehumidity_2m,weathercode"
            ),
            "forecast_days": 3,
            "timezone": "Europe/Rome",
        }
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        hourly = data.get("hourly", {})

        times = hourly.get("time", [])
        if not times:
            return None

        # Trova l'ora più vicina al kickoff
        if match_datetime:
            target = match_datetime.strftime("%Y-%m-%dT%H:00")
        else:
            # Default: prossime 3 ore
            target = (datetime.now() + timedelta(hours=3)).strftime("%Y-%m-%dT%H:00")

        # Cerca l'indice dell'ora target
        idx = None
        for i, t in enumerate(times):
            if t == target:
                idx = i
                break

        # Se non troviamo l'ora esatta, prendiamo la più vicina
        if idx is None:
            try:
                target_dt = datetime.strptime(target, "%Y-%m-%dT%H:00")
                min_diff = float("inf")
                for i, t in enumerate(times):
                    t_dt = datetime.strptime(t, "%Y-%m-%dT%H:%M")
                    diff = abs((t_dt - target_dt).total_seconds())
                    if diff < min_diff:
                        min_diff = diff
                        idx = i
            except Exception:
                idx = 0

        if idx is None or idx >= len(times):
            return None

        return {
            "temperature": hourly.get("temperature_2m", [0])[idx],
            "feels_like": hourly.get("apparent_temperature", [0])[idx],
            "precipitation": hourly.get("precipitation", [0])[idx],
            "rain": hourly.get("rain", [0])[idx],
            "wind_speed": hourly.get("windspeed_10m", [0])[idx],
            "wind_gusts": hourly.get("windgusts_10m", [0])[idx],
            "humidity": hourly.get("relativehumidity_2m", [0])[idx],
            "weather_code": hourly.get("weathercode", [0])[idx],
            "weather_desc": WMO_CODES.get(hourly.get("weathercode", [0])[idx], "?"),
            "match_hour": times[idx],
        }

    except requests.exceptions.Timeout:
        logger.warning("Open-Meteo timeout")
        return None
    except Exception as e:
        logger.warning(f"Errore meteo: {e}")
        return None


def analyze_weather_impact(weather: dict, venue: dict = None) -> dict:
    """
    Analizza l'impatto delle condizioni meteo sulla partita e sui mercati betting.

    Returns:
        {
            "severity": str ("none", "low", "moderate", "high", "extreme"),
            "alerts": list[str],
            "betting_impact": dict {mercato: descrizione},
            "context": str (blocco testuale per l'AI),
        }
    """
    if not weather:
        return {"severity": "none", "alerts": [], "betting_impact": {}, "context": ""}

    temp = weather.get("temperature", 20)
    feels = weather.get("feels_like", temp)
    rain = weather.get("rain", 0)
    precip = weather.get("precipitation", 0)
    wind = weather.get("wind_speed", 0)
    gusts = weather.get("wind_gusts", 0)
    humidity = weather.get("humidity", 50)
    wcode = weather.get("weather_code", 0)
    desc = weather.get("weather_desc", "")

    alerts = []
    impacts = {}
    severity_level = 0  # 0=none, 1=low, 2=moderate, 3=high, 4=extreme
    patterns = []
    SEVERITY_MAP = {0: "none", 1: "low", 2: "moderate", 3: "high", 4: "extreme"}

    # ------------------------------------------------------------------ #
    #  PIOGGIA / PRECIPITAZIONI                                           #
    # ------------------------------------------------------------------ #
    if (rain >= 5 or (precip >= 5 and wcode not in (71, 73, 75, 77, 85, 86))) or wcode in (65, 82, 95, 96, 99):
        severity_level = max(severity_level, 3)
        alerts.append(f"🌧️ PIOGGIA FORTE: {rain}mm/h previsti — campo scivoloso")
        impacts["over_under"] = "Over ↑ — errori portiere/difensori su campo bagnato, palla scivolosa"
        impacts["btts"] = "BTTS Sì ↑ — errori difensivi più probabili"
        impacts["cartellini"] = "Cartellini ↑ — più scivolate, più falli"
        impacts["marcatori"] = "Difensori marcatori ↑ — corner pericolosi su campo bagnato"
        impacts["1x2"] = "Favorisce squadre fisiche/dirette, penalizza squadre di palleggio tecnico"
        patterns.append("Campo bagnato: passaggi a terra meno precisi, palla veloce, più errori individuali")
    elif (rain >= 2 or (precip >= 2 and rain > 0)) or wcode in (61, 63, 80, 81):
        severity_level = max(severity_level, 2)
        alerts.append(f"🌦️ Pioggia moderata: {rain}mm/h — campo potenzialmente scivoloso")
        impacts["over_under"] = "Over leggermente ↑ — condizioni insidiose"
        impacts["cartellini"] = "Cartellini leggermente ↑ — terreno scivoloso"
        patterns.append("Pioggia moderata: gioco leggermente più fisico e diretto")
    elif rain > 0 or wcode in (51, 53, 55):
        severity_level = max(severity_level, 1)
        alerts.append(f"🌦️ Pioggerella: {rain}mm/h — impatto minimo")

    # ------------------------------------------------------------------ #
    #  NEVE / GHIACCIO                                                    #
    # ------------------------------------------------------------------ #
    if wcode in (71, 73, 75, 77, 85, 86, 56, 57, 66, 67):
        severity_level = 4
        alerts.append(f"❄️ NEVE/GHIACCIO: condizioni estreme — rischio rinvio partita")
        impacts["over_under"] = "Under ↑↑ — condizioni rendono il gioco lentissimo"
        impacts["1x2"] = "Risultato IMPREVEDIBILE — le quote pre-match perdono valore"
        patterns.append("Neve/ghiaccio: partita da evitare per le scommesse, troppa varianza")

    # ------------------------------------------------------------------ #
    #  TEMPERATURA                                                        #
    # ------------------------------------------------------------------ #
    def _append_impact(key, text):
        existing = impacts.get(key, "")
        impacts[key] = f"{existing} | {text}" if existing else text

    if temp >= 33 or feels >= 35:
        severity_level = max(severity_level, 3)
        alerts.append(f"🔥 CALDO ESTREMO: {temp}°C (percepiti {feels}°C) — rischio cooling break")
        _append_impact("over_under", "Gol 2° tempo ↑↑ — calo fisico dopo 60'")
        _append_impact("marcatori", "Gol dopo 70' ↑ — subentranti freschi decisivi")
        _append_impact("1x2", "Favorisce panchine profonde, penalizza squadre corte e pressing alto")
        patterns.append("Caldo estremo: cooling break, cali fisici, giocatori over 32 soffrono nel finale")
    elif temp >= 30:
        severity_level = max(severity_level, 2)
        alerts.append(f"🌡️ Caldo: {temp}°C — possibile calo fisico nel secondo tempo")
        _append_impact("marcatori", "Gol nel 2° tempo più probabili")
        patterns.append("Caldo: ritmo cala nel secondo tempo, subentranti possono essere decisivi")
    elif temp <= 2:
        severity_level = max(severity_level, 2)
        alerts.append(f"🥶 FREDDO INTENSO: {temp}°C (percepiti {feels}°C) — rischio infortuni muscolari")
        _append_impact("over_under", "Under ↑ — ritmo più basso, muscoli contratti")
        _append_impact("1x2", "Penalizza giocatori non abituati al freddo")
        patterns.append("Freddo intenso: più infortuni muscolari, gioco più lento, meno dribbling")
    elif temp <= 5:
        severity_level = max(severity_level, 1)
        alerts.append(f"🌡️ Freddo: {temp}°C — condizioni rigide ma gestibili")

    # ------------------------------------------------------------------ #
    #  VENTO                                                              #
    # ------------------------------------------------------------------ #
    if gusts >= 60 or wind >= 45:
        severity_level = max(severity_level, 3)
        alerts.append(f"💨 VENTO FORTISSIMO: {wind}km/h (raffiche {gusts}km/h) — condizioni estreme")
        _append_impact("over_under", "Under ↑ — tiri/cross imprecisi")
        _append_impact("marcatori", "Meno gol da fuori area, punizioni meno precise")
        patterns.append("Vento forte: palloni alti imprevedibili, cross e calci piazzati penalizzati")
    elif gusts >= 40 or wind >= 30:
        severity_level = max(severity_level, 2)
        alerts.append(f"💨 Vento sostenuto: {wind}km/h (raffiche {gusts}km/h)")
        _append_impact("marcatori", "Calci piazzati meno precisi")
        patterns.append("Vento: impatta cross e tiri dalla distanza")
    elif wind >= 20:
        severity_level = max(severity_level, 1)
        alerts.append(f"💨 Vento moderato: {wind}km/h — impatto lieve")

    # ------------------------------------------------------------------ #
    #  NEBBIA                                                             #
    # ------------------------------------------------------------------ #
    if wcode in (45, 48):
        severity_level = max(severity_level, 2)
        alerts.append("🌫️ NEBBIA — visibilità ridotta, lanci lunghi meno efficaci")
        patterns.append("Nebbia: gioco corto favorito, lanci lunghi penalizzati")

    # ------------------------------------------------------------------ #
    #  SUPERFICIE (da venue info)                                         #
    # ------------------------------------------------------------------ #
    surface_note = ""
    if venue and venue.get("surface"):
        surface = venue["surface"].lower()
        if surface == "artificial turf" or "synthetic" in surface:
            surface_note = "⚠️ CAMPO SINTETICO: palla più veloce, rimbalzi diversi, più stress fisico"
            if rain > 0:
                surface_note += " | Su sintetico bagnato: palla velocissima, più scivolate"

    # ------------------------------------------------------------------ #
    #  Costruisci contesto AI                                             #
    # ------------------------------------------------------------------ #
    severity = SEVERITY_MAP.get(severity_level, "none")
    if severity_level == 0:
        return {"severity": "none", "alerts": [], "betting_impact": {}, "context": ""}

    venue_name = venue.get("name", "Stadio") if venue else "Stadio"
    venue_city = venue.get("city", "") if venue else ""
    match_hour = weather.get("match_hour", "")

    lines = [
        f"METEO MATCH ({venue_name}, {venue_city} — {match_hour}):",
        f"  Condizioni: {desc} | {temp}°C (percepiti {feels}°C) | "
        f"Umidità: {humidity}% | Vento: {wind}km/h (raffiche: {gusts}km/h) | "
        f"Pioggia: {rain}mm/h",
    ]

    if surface_note:
        lines.append(f"  {surface_note}")

    if alerts:
        lines.append(f"  ALERT: {' | '.join(alerts)}")

    if impacts:
        lines.append("  IMPATTO BETTING:")
        for market, desc_impact in impacts.items():
            market_label = {
                "over_under": "Over/Under",
                "btts": "BTTS",
                "cartellini": "Cartellini",
                "marcatori": "Marcatori",
                "1x2": "1X2",
            }.get(market, market)
            lines.append(f"    {market_label}: {desc_impact}")

    if patterns:
        lines.append("  PATTERN:")
        for p in patterns:
            lines.append(f"    • {p}")

    return {
        "severity": severity,
        "alerts": alerts,
        "betting_impact": impacts,
        "context": "\n".join(lines),
    }


def get_match_weather_context(
    sm_client,
    home_team_id: str,
    match_datetime: datetime = None,
) -> str:
    """
    Funzione principale: recupera venue, meteo e genera contesto AI.

    Args:
        sm_client: SportmonksClient instance
        home_team_id: ID team casa (per lo stadio)
        match_datetime: Orario del match

    Returns:
        Blocco di testo per il prompt AI, oppure stringa vuota se meteo non impattante
    """
    # 1. Coordinate stadio
    venue = get_venue_coords(sm_client, home_team_id)
    if not venue:
        logger.info("  Meteo: coordinate stadio non disponibili")
        return ""

    logger.info(f"  🌤️ Meteo: {venue['name']}, {venue['city']} ({venue['lat']}, {venue['lon']})")

    # 2. Previsioni meteo
    weather = get_weather_forecast(venue["lat"], venue["lon"], match_datetime)
    if not weather:
        logger.info("  Meteo: previsioni non disponibili")
        return ""

    logger.info(f"  🌤️ Meteo: {weather['weather_desc']} {weather['temperature']}°C "
                f"pioggia:{weather['rain']}mm vento:{weather['wind_speed']}km/h")

    # 3. Analisi impatto
    result = analyze_weather_impact(weather, venue)

    if result["severity"] == "none":
        logger.info("  🌤️ Meteo: condizioni normali, nessun impatto")
        return ""

    logger.info(f"  ⚠️ Meteo: severità {result['severity'].upper()}, {len(result['alerts'])} alert")
    return result["context"]
