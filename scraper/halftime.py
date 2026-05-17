"""
Analisi Doppio Tempo — HT/FT e GG/NG primo/secondo tempo.
Usa la cache Football-Data.org che contiene halfTime e fullTime per ogni partita.
"""
import os
import json
import logging
from scraper.penalties import PenaltyAnalyzer

logger = logging.getLogger(__name__)


# HT/FT outcomes (9 combinazioni)
HTFT_LABELS = ["1/1", "1/X", "1/2", "X/1", "X/X", "X/2", "2/1", "2/X", "2/2"]

# GG/NG 1°T + FT (4 combinazioni)
# GG = entrambe segnano (BTTS Yes), NG = almeno una non segna (BTTS No)
# Formato: "1°T / Finale"
GGNG_LABELS = ["NG/NG", "NG/GG", "GG/NG", "GG/GG"]


def _htft_result(ht_home, ht_away, ft_home, ft_away, is_home: bool) -> str:
    """Calcola esito HT/FT dal punto di vista della squadra analizzata."""
    # HT result
    if ht_home > ht_away:
        ht = "1"
    elif ht_home == ht_away:
        ht = "X"
    else:
        ht = "2"

    # FT result
    if ft_home > ft_away:
        ft = "1"
    elif ft_home == ft_away:
        ft = "X"
    else:
        ft = "2"

    return f"{ht}/{ft}"


def _ggng_result(ht_home, ht_away, ft_home, ft_away) -> str:
    """Calcola GG/NG primo tempo e GG/NG finale (fulltime).
    - Gol 1°T = entrambe segnano entro il 45'
    - Gol FT  = entrambe segnano entro il 90' (classico BTTS)
    """
    ht_gg = "GG" if (ht_home > 0 and ht_away > 0) else "NG"
    ft_gg = "GG" if (ft_home > 0 and ft_away > 0) else "NG"

    return f"{ht_gg}/{ft_gg}"


def _ggng_ft_result(ft_home, ft_away) -> str:
    """GG/NG finale (fulltime)."""
    return "Gol" if (ft_home > 0 and ft_away > 0) else "NoGol"


def _ggng_ht_result(ht_home, ht_away) -> str:
    """GG/NG primo tempo."""
    return "Gol" if (ht_home > 0 and ht_away > 0) else "NoGol"


class HalfTimeAnalyzer:
    def __init__(self, fd_api_key: str):
        self.pa = PenaltyAnalyzer(fd_api_key)

    def analyze_league(self, league_key: str) -> dict:
        """Analizza una lega e ritorna statistiche HT/FT per le partite programmate."""
        CODE_MAP = {
            "italy_serie_a": "SA",
            "england_premier_league": "PL",
            "spain_la_liga": "PD",
            "germany_bundesliga": "BL1",
            "france_ligue_1": "FL1",
            "netherlands_eredivisie": "DED",
            "champions_league": "CL",
            "england_championship": "ELC",
            "portugal_primeira_liga": "PPL",
            "brazil_serie_a": "BSA",
            "world_cup": "WC",
        }
        code = CODE_MAP.get(league_key)
        if not code:
            return {"matches": [], "error": f"Lega '{league_key}' non supportata"}

        cache = self.pa._load_cache(code)
        if not cache:
            return {"matches": [], "error": "Nessuna cache disponibile. Lancia prima il precache."}

        # Check if cache has score data
        has_scores = sum(1 for d in cache.values() if d.get("score", {}).get("fullTime"))
        if has_scores < len(cache) * 0.3:
            return {"matches": [], "error": f"Cache non aggiornata ({has_scores}/{len(cache)} con score)."}

        # Build team stats from historical matches
        team_stats = self._build_team_stats(cache)

        # Get standings for team names and positions
        standings = self.pa._get_standings(code)
        team_names = {s["team_id"]: s["name"] for s in standings}
        team_positions = {s["team_id"]: s["position"] for s in standings}

        # Get scheduled matches (with live fallback if API quota exhausted for SCHEDULED)
        scheduled_data = self.pa._get(f"/competitions/{code}/matches",
                                       params={"status": "SCHEDULED,TIMED"})
        if not scheduled_data or not scheduled_data.get("matches"):
            today_live = self.pa._get(f"/competitions/{code}/matches",
                                       params={"status": "IN_PLAY,PAUSED,FINISHED"})
            if today_live and today_live.get("matches"):
                scheduled_data = today_live
            else:
                return {"matches": [], "error": "Nessuna partita programmata"}

        match_results = []
        seen = set()

        for m in scheduled_data.get("matches", []):
            home = m.get("homeTeam", {})
            away = m.get("awayTeam", {})
            home_id = home.get("id")
            away_id = away.get("id")

            key = f"{home_id}_{away_id}"
            if key in seen:
                continue
            seen.add(key)

            h_stats = team_stats.get(home_id)
            a_stats = team_stats.get(away_id)

            if not h_stats or not a_stats:
                continue

            # Combine home/away analysis
            analysis = self._analyze_matchup(
                h_stats, a_stats,
                team_names.get(home_id, home.get("name", "?")),
                team_names.get(away_id, away.get("name", "?")),
                team_positions.get(home_id, 0),
                team_positions.get(away_id, 0),
            )

            match_results.append({
                "home_team": team_names.get(home_id, home.get("name", "?")),
                "away_team": team_names.get(away_id, away.get("name", "?")),
                "home_pos": team_positions.get(home_id, 0),
                "away_pos": team_positions.get(away_id, 0),
                "utcDate": m.get("utcDate"),
                "matchday": m.get("matchday"),
                "home_stats": self._format_team_summary(h_stats, "home"),
                "away_stats": self._format_team_summary(a_stats, "away"),
                "analysis": analysis,
            })

        # Sort by confidence of best prediction
        match_results.sort(
            key=lambda x: max((p.get("confidence", 0) for p in x["analysis"].get("predictions", [])), default=0),
            reverse=True
        )

        return {"matches": match_results}

    def _build_team_stats(self, cache: dict) -> dict:
        """Build HT/FT and GG/NG stats per team from cache."""
        teams = {}

        for mid, detail in cache.items():
            score = detail.get("score", {})
            ft = score.get("fullTime", {})
            ht = score.get("halfTime", {})

            ft_home = ft.get("home")
            ft_away = ft.get("away")
            ht_home = ht.get("home")
            ht_away = ht.get("away")

            if ft_home is None or ft_away is None or ht_home is None or ht_away is None:
                continue

            home_id = detail.get("home_id")
            away_id = detail.get("away_id")

            for is_home, tid in [(True, home_id), (False, away_id)]:
                if tid is None:
                    continue

                if tid not in teams:
                    teams[tid] = {
                        "total": 0,
                        "home_matches": 0,
                        "away_matches": 0,
                        # HT/FT distribution
                        "htft_home": {k: 0 for k in HTFT_LABELS},
                        "htft_away": {k: 0 for k in HTFT_LABELS},
                        "htft_all": {k: 0 for k in HTFT_LABELS},
                        # GG/NG HT+ST distribution
                        "ggng_home": {k: 0 for k in GGNG_LABELS},
                        "ggng_away": {k: 0 for k in GGNG_LABELS},
                        "ggng_all": {k: 0 for k in GGNG_LABELS},
                        # Simple stats
                        "gg_ht": 0, "ng_ht": 0,  # GG/NG al primo tempo
                        "gg_ft": 0, "ng_ft": 0,  # GG/NG finale
                        "gg_st": 0, "ng_st": 0,  # GG/NG secondo tempo
                        "winning_ht": 0,          # in vantaggio al primo tempo
                        "losing_ht": 0,           # in svantaggio al primo tempo
                        "drawing_ht": 0,          # pari al primo tempo
                        "comeback_wins": 0,       # svantaggio HT → vittoria FT
                        "bottled_leads": 0,        # vantaggio HT → non vittoria FT
                        # Goals per half
                        "goals_1h_scored": 0,
                        "goals_2h_scored": 0,
                        "goals_1h_conceded": 0,
                        "goals_2h_conceded": 0,
                        # Last 5 for recent form
                        "recent_htft": [],
                        "recent_ggng": [],
                    }

                t = teams[tid]
                t["total"] += 1
                loc = "home" if is_home else "away"
                t[f"{loc}_matches"] += 1

                # HT/FT result
                htft = _htft_result(ht_home, ht_away, ft_home, ft_away, is_home)
                t[f"htft_{loc}"][htft] = t[f"htft_{loc}"].get(htft, 0) + 1
                t["htft_all"][htft] = t["htft_all"].get(htft, 0) + 1

                # GG/NG primo tempo + secondo tempo
                ggng = _ggng_result(ht_home, ht_away, ft_home, ft_away)
                t[f"ggng_{loc}"][ggng] = t[f"ggng_{loc}"].get(ggng, 0) + 1
                t["ggng_all"][ggng] = t["ggng_all"].get(ggng, 0) + 1

                # Simple GG/NG stats
                if ht_home > 0 and ht_away > 0:
                    t["gg_ht"] += 1
                else:
                    t["ng_ht"] += 1

                if ft_home > 0 and ft_away > 0:
                    t["gg_ft"] += 1
                else:
                    t["ng_ft"] += 1

                st_home = ft_home - ht_home
                st_away = ft_away - ht_away
                if st_home > 0 and st_away > 0:
                    t["gg_st"] += 1
                else:
                    t["ng_st"] += 1

                # HT position
                my_ht = ht_home if is_home else ht_away
                opp_ht = ht_away if is_home else ht_home
                my_ft = ft_home if is_home else ft_away
                opp_ft = ft_away if is_home else ft_home

                if my_ht > opp_ht:
                    t["winning_ht"] += 1
                    if my_ft <= opp_ft:
                        t["bottled_leads"] += 1
                elif my_ht < opp_ht:
                    t["losing_ht"] += 1
                    if my_ft > opp_ft:
                        t["comeback_wins"] += 1
                else:
                    t["drawing_ht"] += 1

                # Goals per half
                t["goals_1h_scored"] += my_ht
                t["goals_2h_scored"] += (my_ft - my_ht)
                t["goals_1h_conceded"] += opp_ht
                t["goals_2h_conceded"] += (opp_ft - opp_ht)

                # Recent form (keep last 5 by matchday)
                md = detail.get("matchday", 0)
                t["recent_htft"].append((md, htft))
                t["recent_ggng"].append((md, ggng))

        # Sort recent and keep last 5
        for tid, t in teams.items():
            t["recent_htft"] = [r[1] for r in sorted(t["recent_htft"], key=lambda x: x[0])[-5:]]
            t["recent_ggng"] = [r[1] for r in sorted(t["recent_ggng"], key=lambda x: x[0])[-5:]]

        return teams

    def _format_team_summary(self, stats: dict, location: str) -> dict:
        """Format team stats for JSON output."""
        total = stats["total"] or 1
        loc_m = stats[f"{location}_matches"] or 1

        # Top 3 HT/FT outcomes (home or away specific)
        htft_loc = stats[f"htft_{location}"]
        htft_sorted = sorted(htft_loc.items(), key=lambda x: x[1], reverse=True)
        top_htft = [{"label": k, "count": v, "pct": round(v / loc_m * 100, 1)}
                    for k, v in htft_sorted if v > 0][:4]

        # Top GG/NG outcomes
        ggng_loc = stats[f"ggng_{location}"]
        ggng_sorted = sorted(ggng_loc.items(), key=lambda x: x[1], reverse=True)
        top_ggng = [{"label": k, "count": v, "pct": round(v / loc_m * 100, 1)}
                    for k, v in ggng_sorted if v > 0]

        return {
            "total_matches": total,
            "location_matches": loc_m,
            "top_htft": top_htft,
            "top_ggng": top_ggng,
            "gg_ht_pct": round(stats["gg_ht"] / total * 100, 1),
            "gg_ft_pct": round(stats["gg_ft"] / total * 100, 1),
            "gg_st_pct": round(stats["gg_st"] / total * 100, 1),
            "goals_1h_avg": round(stats["goals_1h_scored"] / total, 2),
            "goals_2h_avg": round(stats["goals_2h_scored"] / total, 2),
            "goals_1h_conceded_avg": round(stats["goals_1h_conceded"] / total, 2),
            "goals_2h_conceded_avg": round(stats["goals_2h_conceded"] / total, 2),
            "winning_ht_pct": round(stats["winning_ht"] / total * 100, 1),
            "drawing_ht_pct": round(stats["drawing_ht"] / total * 100, 1),
            "losing_ht_pct": round(stats["losing_ht"] / total * 100, 1),
            "comeback_wins": stats["comeback_wins"],
            "bottled_leads": stats["bottled_leads"],
            "recent_htft": stats["recent_htft"],
            "recent_ggng": stats["recent_ggng"],
        }

    def _analyze_matchup(self, h_stats, a_stats, h_name, a_name, h_pos, a_pos) -> dict:
        """Combine home/away team stats to predict HT/FT and GG/NG outcomes."""
        predictions = []
        insights = []

        h_total = h_stats["total"] or 1
        a_total = a_stats["total"] or 1
        h_home = h_stats["home_matches"] or 1
        a_away = a_stats["away_matches"] or 1

        # ===== HT/FT PREDICTIONS =====
        # Combine home team's home HT/FT with away team's away HT/FT
        htft_scores = {}
        for label in HTFT_LABELS:
            h_pct = h_stats["htft_home"].get(label, 0) / h_home
            a_pct = a_stats["htft_away"].get(label, 0) / a_away
            # Weighted average (60% home, 40% away since home advantage matters)
            combined = h_pct * 0.6 + a_pct * 0.4
            htft_scores[label] = round(combined * 100, 1)

        # Sort and pick top predictions
        htft_sorted = sorted(htft_scores.items(), key=lambda x: x[1], reverse=True)
        for label, pct in htft_sorted[:4]:
            if pct >= 8:
                predictions.append({
                    "type": "htft",
                    "market": f"HT/FT {label}",
                    "probability": pct,
                    "confidence": self._confidence_level(pct),
                    "emoji": self._htft_emoji(label),
                })

        # ===== GG/NG PREDICTIONS =====
        # GG/NG primo tempo
        h_gg_ht = h_stats["gg_ht"] / h_total
        a_gg_ht = a_stats["gg_ht"] / a_total
        gg_ht_combined = round((h_gg_ht * 0.5 + a_gg_ht * 0.5) * 100, 1)

        # GG/NG secondo tempo
        h_gg_st = h_stats["gg_st"] / h_total
        a_gg_st = a_stats["gg_st"] / a_total
        gg_st_combined = round((h_gg_st * 0.5 + a_gg_st * 0.5) * 100, 1)

        # GG/NG finale
        h_gg_ft = h_stats["gg_ft"] / h_total
        a_gg_ft = a_stats["gg_ft"] / a_total
        gg_ft_combined = round((h_gg_ft * 0.5 + a_gg_ft * 0.5) * 100, 1)

        # GG/NG combo HT+ST
        ggng_scores = {}
        for label in GGNG_LABELS:
            h_pct = h_stats["ggng_home"].get(label, 0) / h_home
            a_pct = a_stats["ggng_away"].get(label, 0) / a_away
            combined = h_pct * 0.5 + a_pct * 0.5
            ggng_scores[label] = round(combined * 100, 1)

        ggng_sorted = sorted(ggng_scores.items(), key=lambda x: x[1], reverse=True)
        for label, pct in ggng_sorted:
            if pct >= 5:
                predictions.append({
                    "type": "ggng",
                    "market": f"GG/NG {label}",
                    "probability": pct,
                    "confidence": self._confidence_level(pct),
                    "emoji": self._ggng_emoji(label),
                })

        # ===== INSIGHTS =====
        # Goals per half comparison
        h_1h = h_stats["goals_1h_scored"] / h_total
        h_2h = h_stats["goals_2h_scored"] / h_total
        a_1h = a_stats["goals_1h_scored"] / a_total
        a_2h = a_stats["goals_2h_scored"] / a_total

        if h_2h > h_1h * 1.3:
            insights.append(f"🔥 {h_name} segna di più nel 2° tempo ({h_2h:.2f} vs {h_1h:.2f} gol/partita)")
        elif h_1h > h_2h * 1.3:
            insights.append(f"⚡ {h_name} parte forte: {h_1h:.2f} gol/partita nel 1° tempo")

        if a_2h > a_1h * 1.3:
            insights.append(f"🔥 {a_name} segna di più nel 2° tempo ({a_2h:.2f} vs {a_1h:.2f} gol/partita)")
        elif a_1h > a_2h * 1.3:
            insights.append(f"⚡ {a_name} parte forte: {a_1h:.2f} gol/partita nel 1° tempo")

        # Comeback kings
        if h_stats["comeback_wins"] >= 2:
            insights.append(f"💪 {h_name}: {h_stats['comeback_wins']} rimonte (da svantaggio HT a vittoria FT)")
        if a_stats["comeback_wins"] >= 2:
            insights.append(f"💪 {a_name}: {a_stats['comeback_wins']} rimonte questa stagione")

        # Bottled leads
        if h_stats["bottled_leads"] >= 2:
            insights.append(f"⚠️ {h_name}: {h_stats['bottled_leads']} vantaggi HT sprecati")
        if a_stats["bottled_leads"] >= 2:
            insights.append(f"⚠️ {a_name}: {a_stats['bottled_leads']} vantaggi HT sprecati")

        # Draw at HT pattern
        h_draw_ht = h_stats["drawing_ht"] / h_total * 100
        a_draw_ht = a_stats["drawing_ht"] / a_total * 100
        if h_draw_ht > 55 and a_draw_ht > 55:
            insights.append(f"🤝 Entrambe spesso in parità al HT: {h_name} {h_draw_ht:.0f}%, {a_name} {a_draw_ht:.0f}%")

        # GG pattern
        if gg_ft_combined > 60:
            insights.append(f"⚽ Alta probabilità GG finale: {gg_ft_combined:.0f}%")
        if gg_ft_combined < 35:
            insights.append(f"🛡️ Alta probabilità NG finale: {100-gg_ft_combined:.0f}%")

        predictions.sort(key=lambda x: x["probability"], reverse=True)

        return {
            "predictions": predictions,
            "insights": insights,
            "gg_ht_pct": gg_ht_combined,
            "gg_st_pct": gg_st_combined,
            "gg_ft_pct": gg_ft_combined,
            "htft_distribution": dict(htft_sorted),
            "ggng_distribution": dict(ggng_sorted),
        }

    @staticmethod
    def _confidence_level(pct: float) -> str:
        if pct >= 35:
            return "Alta"
        elif pct >= 25:
            return "Media"
        elif pct >= 15:
            return "Moderata"
        return "Bassa"

    @staticmethod
    def _htft_emoji(label: str) -> str:
        emoji_map = {
            "1/1": "🏠✅", "1/X": "🏠➡️🤝", "1/2": "🏠➡️🔄",
            "X/1": "🤝➡️🏠", "X/X": "🤝🤝", "X/2": "🤝➡️✈️",
            "2/1": "✈️➡️🔄", "2/X": "✈️➡️🤝", "2/2": "✈️✅",
        }
        return emoji_map.get(label, "")

    @staticmethod
    def _ggng_emoji(label: str) -> str:
        emoji_map = {
            "GG/GG": "⚽⚽", "GG/NG": "⚽🛡️",
            "NG/GG": "🛡️⚽", "NG/NG": "🛡️🛡️",
        }
        return emoji_map.get(label, "")
