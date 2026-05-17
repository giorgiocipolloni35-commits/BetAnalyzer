"""
Modulo ibrido: merge delle quote multi-bookmaker di Odds API
dentro i match Sportmonks (che hanno classifica, forma, assenti).
"""
import logging
from difflib import SequenceMatcher
from models.match import Match

logger = logging.getLogger(__name__)


def _normalize_team(name: str) -> str:
    """Normalizza nome squadra per matching fuzzy."""
    return (name.lower()
            .replace("fc ", "").replace(" fc", "")
            .replace("afc ", "").replace(" afc", "")
            .replace("sc ", "").replace(" sc", "")
            .replace("if ", "").replace(" if", "")
            .replace("ff ", "").replace(" ff", "")
            .replace("bk ", "").replace(" bk", "")
            .strip())


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, _normalize_team(a), _normalize_team(b)).ratio()


def _find_matching_odds_match(sm_match: Match, odds_matches: list[Match]) -> Match | None:
    """Trova il match Odds API corrispondente a un match Sportmonks."""
    best_match = None
    best_score = 0.0

    for om in odds_matches:
        home_sim = _similarity(sm_match.home_team, om.home_team)
        away_sim = _similarity(sm_match.away_team, om.away_team)
        avg_sim = (home_sim + away_sim) / 2

        if avg_sim > best_score and avg_sim > 0.5:
            best_score = avg_sim
            best_match = om

    if best_match:
        logger.debug(
            f"Match trovato: {sm_match.home_team} vs {sm_match.away_team} "
            f"↔ {best_match.home_team} vs {best_match.away_team} "
            f"(sim: {best_score:.2f})"
        )
    return best_match


def merge_odds_into_matches(sm_matches: list[Match], odds_matches: list[Match]):
    """
    Sostituisce le quote aggregate di Sportmonks con le quote
    dettagliate per bookmaker di Odds API.
    Mantiene i dati extra di Sportmonks (assenti, IDs, league_id, ecc.)
    """
    merged = 0
    for sm in sm_matches:
        om = _find_matching_odds_match(sm, odds_matches)
        if om and om.odds:
            sm.odds = om.odds
            merged += 1

    logger.info(f"Quote merged: {merged}/{len(sm_matches)} partite arricchite con Odds API")
