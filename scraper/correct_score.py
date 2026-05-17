"""
Risultato Esatto — Previsione con modello Poisson + correzione Dixon-Coles.
Usa la cache Football-Data.org per calcolare i gol attesi (λ) per squadra.
"""
import math
import logging
from scraper.penalties import PenaltyAnalyzer
from logic.match_importance import detect_derby

logger = logging.getLogger(__name__)

MAX_GOALS = 4  # grid 0-4 × 0-4 = 25 box


def _poisson_pmf(k: int, lam: float) -> float:
    """Probabilità Poisson: P(X=k) = e^(-λ) * λ^k / k!"""
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return math.exp(-lam) * (lam ** k) / math.factorial(k)


def _dixon_coles_tau(x: int, y: int, lam: float, mu: float, rho: float) -> float:
    """
    Fattore di correzione Dixon-Coles per risultati bassi (0-0, 1-0, 0-1, 1-1).
    rho < 0 → più pareggi bassi del previsto (tipico nel calcio).
    """
    if x == 0 and y == 0:
        return 1.0 - lam * mu * rho
    elif x == 0 and y == 1:
        return 1.0 + lam * rho
    elif x == 1 and y == 0:
        return 1.0 + mu * rho
    elif x == 1 and y == 1:
        return 1.0 - rho
    return 1.0


class CorrectScoreAnalyzer:
    """Calcola la griglia risultati esatti per le partite programmate di un campionato."""

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

    def __init__(self, fd_api_key: str):
        self.pa = PenaltyAnalyzer(fd_api_key)

    # ─────────────────── public ───────────────────

    def analyze_league(self, league_key: str) -> dict:
        code = self.CODE_MAP.get(league_key)
        if not code:
            return {"matches": [], "error": f"Lega '{league_key}' non supportata"}

        cache = self.pa._load_cache(code)
        if not cache:
            return {"matches": [], "error": "Nessuna cache disponibile."}

        # Build league-wide and per-team stats
        league_stats, team_stats = self._compute_stats(cache)
        if league_stats["matches"] < 20:
            return {"matches": [], "error": "Dati insufficienti per il modello."}

        # Scheduled matches
        standings = self.pa._get_standings(code)
        team_names = {s["team_id"]: s["name"] for s in standings}
        team_positions = {s["team_id"]: s["position"] for s in standings}
        team_points = {s["team_id"]: s["points"] for s in standings}
        num_teams = len(standings)
        total_matchdays = (num_teams - 1) * 2  # 38 for 20 teams

        scheduled = self.pa._get(f"/competitions/{code}/matches",
                                  params={"status": "SCHEDULED,TIMED"})
        if not scheduled:
            return {"matches": [], "error": "Nessuna partita programmata."}

        results = []
        seen = set()

        for m in scheduled.get("matches", []):
            home = m.get("homeTeam", {})
            away = m.get("awayTeam", {})
            h_id, a_id = home.get("id"), away.get("id")
            key = f"{h_id}_{a_id}"
            if key in seen:
                continue
            seen.add(key)

            h_name = team_names.get(h_id, home.get("name", "?"))
            a_name = team_names.get(a_id, away.get("name", "?"))

            h_stats = team_stats.get(h_id)
            a_stats = team_stats.get(a_id)
            if not h_stats or not a_stats:
                continue

            # Calcola λ (expected goals)
            lam_home, lam_away = self._expected_goals(
                h_stats, a_stats, league_stats
            )

            # Calcola griglia Poisson + Dixon-Coles
            grid = self._compute_grid(lam_home, lam_away)

            # Aggregati
            agg = self._aggregate(grid)

            # ════════════════════════════════════════════════
            # DRAW CORRECTION: Poisson underestimates draws
            # when teams are close in standings (tactical parity).
            # We redistribute probability towards draw based on
            # position proximity and lambda similarity.
            # ════════════════════════════════════════════════
            h_pos = team_positions.get(h_id, 10)
            a_pos = team_positions.get(a_id, 10)
            pos_diff = abs(h_pos - a_pos)
            lam_ratio = min(lam_home, lam_away) / max(lam_home, lam_away) if max(lam_home, lam_away) > 0 else 1.0

            # Derby detection
            derby_name = detect_derby(h_name, a_name)
            is_derby = derby_name is not None

            # Draw boost: stronger when teams are close + lambdas are similar
            draw_boost = 0.0
            if is_derby:
                # Derby: always boost draw regardless of positions
                # Derbies are emotionally tight, neither side wants to lose
                draw_boost = 3.0
            elif pos_diff <= 3 and lam_ratio >= 0.70:
                draw_boost = 3.5   # very close teams → +3.5% draw
            elif pos_diff <= 5 and lam_ratio >= 0.65:
                draw_boost = 2.5   # moderately close
            elif pos_diff <= 7 and lam_ratio >= 0.60:
                draw_boost = 1.5   # somewhat close
            # Additional boost if both mid-table (not dominant nor weak)
            if 5 <= h_pos <= 14 and 5 <= a_pos <= 14 and pos_diff <= 4:
                draw_boost += 1.0  # mid-table teams draw more often

            # DAMPENER: reduce draw boost when offensive quality gap is large.
            # e.g. St.Pauli 28 gol vs Wolfsburg 42 gol = same points but very
            # different attacking quality → draw less likely despite close standings.
            h_total_goals = h_stats.get("home_scored", 0) + h_stats.get("away_scored", 0)
            a_total_goals = a_stats.get("home_scored", 0) + a_stats.get("away_scored", 0)
            avg_goals_scored = (h_total_goals + a_total_goals) / 2 if (h_total_goals + a_total_goals) > 0 else 1
            goals_diff_ratio = abs(h_total_goals - a_total_goals) / avg_goals_scored

            # In derbies, dampener is less aggressive (emotional factor overrides stats)
            if not is_derby:
                if goals_diff_ratio > 0.50:
                    draw_boost *= 0.30   # huge gap (e.g. 28 vs 42) → almost no boost
                elif goals_diff_ratio > 0.35:
                    draw_boost *= 0.50   # significant gap → halve the boost
                elif goals_diff_ratio > 0.20:
                    draw_boost *= 0.75   # moderate gap → slight reduction
            else:
                # Derby: only dampen if gap is extreme (>50%)
                if goals_diff_ratio > 0.50:
                    draw_boost *= 0.70   # even in derby, huge gap reduces somewhat

            if draw_boost > 0:
                # Redistribute from the stronger outcome (home_win or away_win)
                old_draw = agg["draw"]
                agg["draw"] = round(old_draw + draw_boost, 1)
                # Take proportionally from 1 and 2
                hw_share = agg["home_win"] / (agg["home_win"] + agg["away_win"]) if (agg["home_win"] + agg["away_win"]) > 0 else 0.5
                agg["home_win"] = round(agg["home_win"] - draw_boost * hw_share, 1)
                agg["away_win"] = round(agg["away_win"] - draw_boost * (1 - hw_share), 1)

            # ═════════════════════════════════════════════��══
            # HOME ADVANTAGE CORRECTION: Poisson already has a home factor
            # via league avg_home > avg_away, but it's often not enough.
            # When a team has a strong home record (win_rate >= 45%),
            # we shift probability from draw/away toward home_win.
            # ════════════════════════════════════════════════
            h_home_matches = max(h_stats.get("home_matches", 1), 1)
            h_home_wins = sum(1 for r in h_stats.get("home_results", []) if r[1] > r[2])
            h_home_wr = h_home_wins / h_home_matches

            home_boost = 0.0
            if h_home_wr >= 0.55:
                home_boost = 3.0   # dominant at home (55%+ WR)
            elif h_home_wr >= 0.45:
                home_boost = 2.0   # strong at home
            elif h_home_wr >= 0.38:
                home_boost = 1.0   # decent at home

            # Away team weakness amplifier: if away team loses a lot away
            a_away_matches = max(a_stats.get("away_matches", 1), 1)
            a_away_losses = sum(1 for r in a_stats.get("away_results", []) if r[1] < r[2])
            a_away_loss_rate = a_away_losses / a_away_matches

            if a_away_loss_rate >= 0.55:
                home_boost += 1.5  # away team is very weak on the road
            elif a_away_loss_rate >= 0.45:
                home_boost += 0.75

            if home_boost > 0:
                # Take from draw and away_win proportionally
                draw_take = home_boost * 0.4
                away_take = home_boost * 0.6
                agg["home_win"] = round(agg["home_win"] + home_boost, 1)
                agg["draw"] = round(agg["draw"] - draw_take, 1)
                agg["away_win"] = round(agg["away_win"] - away_take, 1)

            # ════════════════════════════════════════════════
            # MOTIVATION FACTOR: Late-season adjustment.
            # Teams with nothing to play for (mid-table, already champion,
            # already relegated) get penalized. Teams fighting for
            # survival or Champions League get boosted.
            # Only active in last 4 matchdays.
            # ════════════════════════════════════════════════
            matchday = m.get("matchday", 0) or 0
            remaining = total_matchdays - matchday
            motivation_shift = 0.0  # positive = favors home, negative = favors away
            h_motivation = "NORMAL"
            a_motivation = "NORMAL"

            if remaining <= 3:  # last 4 matchdays (including current)
                h_pts = team_points.get(h_id, 0)
                a_pts = team_points.get(a_id, 0)

                def _classify_motivation(pos, pts, all_standings, num_t):
                    """Classify a team's motivation level."""
                    # Relegation zone: bottom 3 for 20-team, bottom 2 for 18-team
                    releg_zone = 3 if num_t >= 20 else 2
                    # Champions League: top 4 (or top 5 for some leagues)
                    cl_zone = 4

                    # Already champion? 1st place with big gap to 2nd
                    if pos == 1:
                        second_pts = max((s["points"] for s in all_standings if s["position"] == 2), default=0)
                        gap = pts - second_pts
                        # If gap > remaining*3 → mathematically champion
                        if gap > remaining * 3:
                            return "CHAMPION"
                        # If gap is large enough that it's very likely
                        if gap >= 6 + remaining:
                            return "CHAMPION"

                    # Already relegated? Check if they can catch the team above
                    if pos >= num_t - releg_zone + 1:
                        # Team just above relegation zone
                        safe_pos = num_t - releg_zone
                        safe_pts = max((s["points"] for s in all_standings if s["position"] == safe_pos), default=0)
                        gap_to_safe = safe_pts - pts
                        # If gap > remaining*3 → mathematically relegated
                        if gap_to_safe > remaining * 3:
                            return "RELEGATED"

                    # Fighting for survival? In or near relegation zone
                    if pos >= num_t - releg_zone - 1:  # in zone or 1 above
                        safe_pos = num_t - releg_zone
                        safe_pts = max((s["points"] for s in all_standings if s["position"] == safe_pos), default=0)
                        gap_to_safe = safe_pts - pts
                        if gap_to_safe >= 0 or pos >= num_t - releg_zone + 1:
                            return "DESPERATE_SURVIVAL"

                    # Fighting for Champions League? Close to CL zone
                    if pos <= cl_zone + 2:  # in or near CL spots
                        cl_border_pts = max((s["points"] for s in all_standings if s["position"] == cl_zone), default=0)
                        gap = abs(pts - cl_border_pts)
                        if gap <= 6 + remaining:  # still in the race
                            if pos <= cl_zone:
                                return "CL_DEFENDING"  # in CL spot, defending it
                            else:
                                return "CL_CHASING"  # just outside, chasing

                    # Nothing to play for: safe from relegation, out of CL race
                    return "NOTHING"

                h_motivation = _classify_motivation(h_pos, h_pts, standings, num_teams)
                a_motivation = _classify_motivation(a_pos, a_pts, standings, num_teams)

                # ── Calculate shift based on motivation gap ──
                MOTIVATION_POWER = {
                    "DESPERATE_SURVIVAL": 3,   # maximum motivation
                    "CL_CHASING": 2,           # high motivation
                    "CL_DEFENDING": 1,         # moderate (already there, but could lose it)
                    "NORMAL": 0,
                    "NOTHING": -1,             # low motivation → penalized
                    "CHAMPION": -2,            # rotation, youth players
                    "RELEGATED": -2,           # nothing left to play for
                }

                h_power = MOTIVATION_POWER.get(h_motivation, 0)
                a_power = MOTIVATION_POWER.get(a_motivation, 0)
                power_diff = h_power - a_power  # positive = home more motivated

                # Each point of power difference = ~2.5% shift
                # Cap at ±8% to avoid extreme swings
                if power_diff != 0:
                    motivation_shift = round(min(max(power_diff * 2.5, -8.0), 8.0), 1)

                    # Apply shift: take from the less motivated side, give to the more motivated
                    if motivation_shift > 0:
                        # Home team is more motivated → boost home, reduce away
                        agg["home_win"] = round(agg["home_win"] + motivation_shift, 1)
                        agg["away_win"] = round(agg["away_win"] - motivation_shift * 0.6, 1)
                        agg["draw"] = round(agg["draw"] - motivation_shift * 0.4, 1)
                    elif motivation_shift < 0:
                        # Away team is more motivated → boost away, reduce home
                        shift = abs(motivation_shift)
                        agg["away_win"] = round(agg["away_win"] + shift, 1)
                        agg["home_win"] = round(agg["home_win"] - shift * 0.6, 1)
                        agg["draw"] = round(agg["draw"] - shift * 0.4, 1)

                    # Clamp to valid ranges
                    for k in ("home_win", "draw", "away_win"):
                        agg[k] = max(agg[k], 1.0)

                    logger.info(f"⚡ Motivation: {h_name}={h_motivation}({h_power}) vs {a_name}={a_motivation}({a_power}) → shift={motivation_shift:+.1f}%")

            # Top risultati
            flat = []
            for i in range(MAX_GOALS + 1):
                for j in range(MAX_GOALS + 1):
                    flat.append({"score": f"{i}-{j}", "home": i, "away": j,
                                 "prob": grid[i][j]})
            flat.sort(key=lambda x: x["prob"], reverse=True)

            results.append({
                "home_team": h_name,
                "away_team": a_name,
                "home_pos": h_pos,
                "away_pos": a_pos,
                "draw_boost": round(draw_boost, 1),
                "home_boost": round(home_boost, 1),
                "is_derby": is_derby,
                "derby_name": derby_name,
                "motivation_home": h_motivation,
                "motivation_away": a_motivation,
                "motivation_shift": motivation_shift,
                "utcDate": m.get("utcDate"),
                "matchday": m.get("matchday"),
                "lambda_home": round(lam_home, 2),
                "lambda_away": round(lam_away, 2),
                "grid": [[round(grid[i][j], 1) for j in range(MAX_GOALS + 1)]
                         for i in range(MAX_GOALS + 1)],
                "top_scores": flat[:8],
                "aggregates": agg,
                "home_attack": round(h_stats["home_scored"] / max(h_stats["home_matches"], 1), 2),
                "home_defense": round(h_stats["home_conceded"] / max(h_stats["home_matches"], 1), 2),
                "away_attack": round(a_stats["away_scored"] / max(a_stats["away_matches"], 1), 2),
                "away_defense": round(a_stats["away_conceded"] / max(a_stats["away_matches"], 1), 2),
                "home_attack_form": round(h_stats.get("recent_home_scored", 0), 2),
                "home_defense_form": round(h_stats.get("recent_home_conceded", 0), 2),
                "away_attack_form": round(a_stats.get("recent_away_scored", 0), 2),
                "away_defense_form": round(a_stats.get("recent_away_conceded", 0), 2),
                "home_recent_scores": h_stats.get("recent_scores", []),
                "away_recent_scores": a_stats.get("recent_scores", []),
            })

        # Ordina cronologicamente (prossimi prima)
        results.sort(key=lambda x: x.get("utcDate") or "9999")
        return {"matches": results}

    # ─────────────────── stats ───────────────────

    def _compute_stats(self, cache: dict):
        """Calcola statistiche campionato e per squadra, con split recente."""
        league = {"matches": 0, "home_goals": 0, "away_goals": 0}
        teams = {}

        for mid, detail in cache.items():
            score = detail.get("score", {})
            ft = score.get("fullTime", {})

            ft_home = ft.get("home")
            ft_away = ft.get("away")
            if ft_home is None or ft_away is None:
                continue

            h_id = detail.get("home_id")
            a_id = detail.get("away_id")
            if not h_id or not a_id:
                continue

            league["matches"] += 1
            league["home_goals"] += ft_home
            league["away_goals"] += ft_away

            matchday = detail.get("matchday", 0)

            # Init team entry
            for tid in (h_id, a_id):
                if tid not in teams:
                    teams[tid] = {
                        "home_matches": 0, "away_matches": 0,
                        "home_scored": 0, "home_conceded": 0,
                        "away_scored": 0, "away_conceded": 0,
                        # Raw per-match data for form calculation
                        "home_results": [],   # [(md, scored, conceded), ...]
                        "away_results": [],
                        "recent_scores": [],  # [(md, "2-1", is_home), ...]
                    }

            t_h = teams[h_id]
            t_a = teams[a_id]

            # Home team
            t_h["home_matches"] += 1
            t_h["home_scored"] += ft_home
            t_h["home_conceded"] += ft_away
            t_h["home_results"].append((matchday, ft_home, ft_away))
            t_h["recent_scores"].append((matchday, f"{ft_home}-{ft_away}", True))

            # Away team
            t_a["away_matches"] += 1
            t_a["away_scored"] += ft_away
            t_a["away_conceded"] += ft_home
            t_a["away_results"].append((matchday, ft_away, ft_home))
            t_a["recent_scores"].append((matchday, f"{ft_home}-{ft_away}", False))

        # Sort results and compute recent form (last 6 home / last 6 away)
        RECENT_N = 6
        for tid, t in teams.items():
            t["home_results"].sort(key=lambda x: x[0])
            t["away_results"].sort(key=lambda x: x[0])
            t["recent_scores"].sort(key=lambda x: x[0])

            # Recent home form
            recent_home = t["home_results"][-RECENT_N:]
            if recent_home:
                t["recent_home_scored"] = sum(r[1] for r in recent_home) / len(recent_home)
                t["recent_home_conceded"] = sum(r[2] for r in recent_home) / len(recent_home)
            else:
                t["recent_home_scored"] = t["home_scored"] / max(t["home_matches"], 1)
                t["recent_home_conceded"] = t["home_conceded"] / max(t["home_matches"], 1)

            # Recent away form
            recent_away = t["away_results"][-RECENT_N:]
            if recent_away:
                t["recent_away_scored"] = sum(r[1] for r in recent_away) / len(recent_away)
                t["recent_away_conceded"] = sum(r[2] for r in recent_away) / len(recent_away)
            else:
                t["recent_away_scored"] = t["away_scored"] / max(t["away_matches"], 1)
                t["recent_away_conceded"] = t["away_conceded"] / max(t["away_matches"], 1)

            # Keep last 6 results for display
            last6 = t["recent_scores"][-6:]
            t["recent_scores"] = [
                {"score": s[1], "is_home": s[2]} for s in last6
            ]

        # League averages
        if league["matches"] > 0:
            league["avg_home"] = league["home_goals"] / league["matches"]
            league["avg_away"] = league["away_goals"] / league["matches"]
        else:
            league["avg_home"] = 1.4
            league["avg_away"] = 1.1

        return league, teams

    # ─────────────────── expected goals ───────────────────

    def _expected_goals(self, h_stats: dict, a_stats: dict, league: dict):
        """
        Calcola gol attesi con blend stagione + forma recente (ultime 6).

        Formula base (Dixon-Coles standard):
          λ_home = AttackStrength_home × DefenseWeakness_away × LeagueAvgHomeGoals
          λ_away = AttackStrength_away × DefenseWeakness_home × LeagueAvgAwayGoals

        Miglioria: ogni fattore è il blend ponderato di:
          - media stagionale (peso 60%)
          - forma recente ultime 6 partite (peso 40%)

        Questo cattura trend come squadre in serie positiva (es. Villa 2.33 gol/casa
        nelle ultime 3 vs 1.56 stagionale) senza abbandonare la stabilità del campione
        largo.
        """
        SEASON_W = 0.60
        FORM_W   = 0.40

        avg_h = league["avg_home"]
        avg_a = league["avg_away"]

        hm = max(h_stats["home_matches"], 1)
        am = max(a_stats["away_matches"], 1)

        # ── Medie stagionali per giocatore ──
        season_att_home = h_stats["home_scored"] / hm
        season_def_home = h_stats["home_conceded"] / hm
        season_att_away = a_stats["away_scored"] / am
        season_def_away = a_stats["away_conceded"] / am

        # ── Medie forma recente (ultime 6, calcolate in _compute_stats) ──
        form_att_home = h_stats.get("recent_home_scored", season_att_home)
        form_def_home = h_stats.get("recent_home_conceded", season_def_home)
        form_att_away = a_stats.get("recent_away_scored", season_att_away)
        form_def_away = a_stats.get("recent_away_conceded", season_def_away)

        # ── Blend ──
        blend_att_home = SEASON_W * season_att_home + FORM_W * form_att_home
        blend_def_home = SEASON_W * season_def_home + FORM_W * form_def_home
        blend_att_away = SEASON_W * season_att_away + FORM_W * form_att_away
        blend_def_away = SEASON_W * season_def_away + FORM_W * form_def_away

        # Attack strength = team blend / league avg
        att_home = blend_att_home / max(avg_h, 0.5)
        att_away = blend_att_away / max(avg_a, 0.5)

        # Defense weakness = team blend conceded / league avg conceded
        def_home = blend_def_home / max(avg_a, 0.5)
        def_away = blend_def_away / max(avg_h, 0.5)

        lam_home = att_home * def_away * avg_h
        lam_away = att_away * def_home * avg_a

        # Clamp to reasonable range
        lam_home = max(0.3, min(lam_home, 4.5))
        lam_away = max(0.2, min(lam_away, 4.0))

        return lam_home, lam_away

    # ─────────────────── grid ───────────────────

    def _compute_grid(self, lam_home: float, lam_away: float, rho: float = -0.13):
        """
        Griglia 5×5 di probabilità (%) con Poisson + Dixon-Coles.
        rho = -0.13 è un valore standard dalla letteratura.
        """
        raw = [[0.0] * (MAX_GOALS + 1) for _ in range(MAX_GOALS + 1)]

        for i in range(MAX_GOALS + 1):
            for j in range(MAX_GOALS + 1):
                p = _poisson_pmf(i, lam_home) * _poisson_pmf(j, lam_away)
                tau = _dixon_coles_tau(i, j, lam_home, lam_away, rho)
                raw[i][j] = p * tau

        # Normalizza a 100%
        total = sum(raw[i][j] for i in range(MAX_GOALS + 1) for j in range(MAX_GOALS + 1))
        if total <= 0:
            total = 1.0

        grid = [[0.0] * (MAX_GOALS + 1) for _ in range(MAX_GOALS + 1)]
        for i in range(MAX_GOALS + 1):
            for j in range(MAX_GOALS + 1):
                grid[i][j] = round((raw[i][j] / total) * 100, 1)

        return grid

    # ─────────────────── aggregates ───────────────────

    def _aggregate(self, grid) -> dict:
        """Calcola aggregati dalla griglia."""
        home_win = 0.0
        draw = 0.0
        away_win = 0.0
        over_25 = 0.0
        under_25 = 0.0
        gg = 0.0
        ng = 0.0
        even = 0.0
        odd = 0.0

        for i in range(MAX_GOALS + 1):
            for j in range(MAX_GOALS + 1):
                p = grid[i][j]
                if i > j:
                    home_win += p
                elif i == j:
                    draw += p
                else:
                    away_win += p

                if i + j > 2:
                    over_25 += p
                else:
                    under_25 += p

                if i > 0 and j > 0:
                    gg += p
                else:
                    ng += p

                if (i + j) % 2 == 0:
                    even += p
                else:
                    odd += p

        return {
            "home_win": round(home_win, 1),
            "draw": round(draw, 1),
            "away_win": round(away_win, 1),
            "over_25": round(over_25, 1),
            "under_25": round(under_25, 1),
            "gg": round(gg, 1),
            "ng": round(ng, 1),
            "even": round(even, 1),
            "odd": round(odd, 1),
        }
