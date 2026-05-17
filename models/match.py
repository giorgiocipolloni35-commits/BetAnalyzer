from dataclasses import dataclass, field
from typing import Optional


@dataclass
class BookmakerOdds:
    bookmaker: str
    home: Optional[float] = None
    draw: Optional[float] = None
    away: Optional[float] = None
    over25: Optional[float] = None
    under25: Optional[float] = None
    over15: Optional[float] = None
    under15: Optional[float] = None
    over35: Optional[float] = None
    under35: Optional[float] = None
    gg: Optional[float] = None
    ng: Optional[float] = None

    def to_dict(self):
        return {
            "bookmaker": self.bookmaker,
            "home": self.home, "draw": self.draw, "away": self.away,
            "over25": self.over25, "under25": self.under25,
            "over15": self.over15, "under15": self.under15,
            "over35": self.over35, "under35": self.under35,
            "gg": self.gg, "ng": self.ng,
        }

    @classmethod
    def from_dict(cls, data: dict):
        return cls(
            bookmaker=data["bookmaker"],
            home=data.get("home"), draw=data.get("draw"), away=data.get("away"),
            over25=data.get("over25"), under25=data.get("under25"),
            over15=data.get("over15"), under15=data.get("under15"),
            over35=data.get("over35"), under35=data.get("under35"),
            gg=data.get("gg"), ng=data.get("ng"),
        )

    @property
    def dc_1x(self) -> Optional[float]:
        if self.home and self.draw:
            p = (1/self.home) + (1/self.draw)
            return round(1/p, 2) if p > 0 else None
        return None

    @property
    def dc_x2(self) -> Optional[float]:
        if self.draw and self.away:
            p = (1/self.draw) + (1/self.away)
            return round(1/p, 2) if p > 0 else None
        return None

    @property
    def dc_12(self) -> Optional[float]:
        if self.home and self.away:
            p = (1/self.home) + (1/self.away)
            return round(1/p, 2) if p > 0 else None
        return None


@dataclass
class Match:
    id: str
    home_team: str
    away_team: str
    league: str
    commence_time: str
    league_id: Optional[str] = None
    season_id: Optional[str] = None
    home_id: Optional[str] = None
    away_id: Optional[str] = None
    absentees: list[dict] = field(default_factory=list) # [{'player': '...', 'reason': '...', 'team': 'home'/'away'}]
    referee: Optional[str] = None
    referee_stats: Optional[dict] = None  # {"matches": int, "penalties_per_match": float, "cards_per_match": float}
    penalty_takers_home: list[dict] = field(default_factory=list)  # [{"player": str, "taken": int, "scored": int}]
    penalty_takers_away: list[dict] = field(default_factory=list)
    home_position: int = 0
    away_position: int = 0
    home_diffidati: list[dict] = field(default_factory=list)  # [{"player": str, "yellows": int}]
    away_diffidati: list[dict] = field(default_factory=list)
    matchday: Optional[int] = None
    odds: list[BookmakerOdds] = field(default_factory=list)
    recommendations: list["Recommendation"] = field(default_factory=list)

    def to_dict(self):
        return {
            "id": self.id,
            "home_team": self.home_team,
            "away_team": self.away_team,
            "league": self.league,
            "commence_time": self.commence_time,
            "league_id": self.league_id,
            "season_id": self.season_id,
            "home_id": self.home_id,
            "away_id": self.away_id,
            "absentees": self.absentees,
            "referee": self.referee,
            "referee_stats": self.referee_stats,
            "penalty_takers_home": self.penalty_takers_home,
            "penalty_takers_away": self.penalty_takers_away,
            "home_position": self.home_position,
            "away_position": self.away_position,
            "home_diffidati": self.home_diffidati,
            "away_diffidati": self.away_diffidati,
            "matchday": self.matchday,
            "odds": [o.to_dict() for o in self.odds],
            "recommendations": [r.to_dict() for r in self.recommendations]
        }

    @classmethod
    def from_dict(cls, data: dict):
        m = cls(
            id=data["id"],
            home_team=data["home_team"],
            away_team=data["away_team"],
            league=data["league"],
            commence_time=data["commence_time"],
            league_id=data.get("league_id"),
            season_id=data.get("season_id"),
            home_id=data.get("home_id"),
            away_id=data.get("away_id"),
            absentees=data.get("absentees", []),
            referee=data.get("referee"),
            referee_stats=data.get("referee_stats"),
            penalty_takers_home=data.get("penalty_takers_home", []),
            penalty_takers_away=data.get("penalty_takers_away", []),
            home_position=data.get("home_position", 0),
            away_position=data.get("away_position", 0),
            home_diffidati=data.get("home_diffidati", []),
            away_diffidati=data.get("away_diffidati", []),
            matchday=data.get("matchday"),
        )
        m.odds = [BookmakerOdds.from_dict(o) for o in data.get("odds", [])]
        m.recommendations = [Recommendation.from_dict(r) for r in data.get("recommendations", [])]
        return m

    @property
    def best_home(self) -> tuple[float, str]:
        best = max((o for o in self.odds if o.home), key=lambda o: o.home, default=None)
        return (best.home, best.bookmaker) if best else (0.0, "N/A")

    @property
    def best_draw(self) -> tuple[float, str]:
        best = max((o for o in self.odds if o.draw), key=lambda o: o.draw, default=None)
        return (best.draw, best.bookmaker) if best else (0.0, "N/A")

    @property
    def best_away(self) -> tuple[float, str]:
        best = max((o for o in self.odds if o.away), key=lambda o: o.away, default=None)
        return (best.away, best.bookmaker) if best else (0.0, "N/A")

    @property
    def best_over25(self) -> tuple[float, str]:
        best = max((o for o in self.odds if o.over25), key=lambda o: o.over25, default=None)
        return (best.over25, best.bookmaker) if best else (0.0, "N/A")

    @property
    def best_under25(self) -> tuple[float, str]:
        best = max((o for o in self.odds if o.under25), key=lambda o: o.under25, default=None)
        return (best.under25, best.bookmaker) if best else (0.0, "N/A")

    @property
    def best_over15(self) -> tuple[float, str]:
        best = max((o for o in self.odds if o.over15), key=lambda o: o.over15, default=None)
        return (best.over15, best.bookmaker) if best else (0.0, "N/A")

    @property
    def best_under15(self) -> tuple[float, str]:
        best = max((o for o in self.odds if o.under15), key=lambda o: o.under15, default=None)
        return (best.under15, best.bookmaker) if best else (0.0, "N/A")

    @property
    def best_over35(self) -> tuple[float, str]:
        best = max((o for o in self.odds if o.over35), key=lambda o: o.over35, default=None)
        return (best.over35, best.bookmaker) if best else (0.0, "N/A")

    @property
    def best_under35(self) -> tuple[float, str]:
        best = max((o for o in self.odds if o.under35), key=lambda o: o.under35, default=None)
        return (best.under35, best.bookmaker) if best else (0.0, "N/A")

    @property
    def best_gg(self) -> tuple[float, str]:
        best = max((o for o in self.odds if o.gg), key=lambda o: o.gg, default=None)
        return (best.gg, best.bookmaker) if best else (0.0, "N/A")

    @property
    def best_ng(self) -> tuple[float, str]:
        best = max((o for o in self.odds if o.ng), key=lambda o: o.ng, default=None)
        return (best.ng, best.bookmaker) if best else (0.0, "N/A")

    @property
    def best_dc_1x(self) -> tuple[float, str]:
        best = max((o for o in self.odds if o.dc_1x), key=lambda o: o.dc_1x, default=None)
        return (best.dc_1x, best.bookmaker) if best else (0.0, "N/A")

    @property
    def best_dc_x2(self) -> tuple[float, str]:
        best = max((o for o in self.odds if o.dc_x2), key=lambda o: o.dc_x2, default=None)
        return (best.dc_x2, best.bookmaker) if best else (0.0, "N/A")

    @property
    def best_dc_12(self) -> tuple[float, str]:
        best = max((o for o in self.odds if o.dc_12), key=lambda o: o.dc_12, default=None)
        return (best.dc_12, best.bookmaker) if best else (0.0, "N/A")

    def bookmaker_count(self) -> int:
        return len(self.odds)

    def get_recommendation(self, market_name: str) -> Optional["Recommendation"]:
        return next((r for r in self.recommendations if r.market.upper() == market_name.upper()), None)


@dataclass
class Recommendation:
    play: bool                # True = consigliato
    market: str               # "1", "X", "2", "OVER 2.5", "GG", ecc.
    odds_value: float         # quota consigliata
    bookmaker: str            # bookmaker con la quota migliore
    confidence: str           # "Alto", "Medio", "Basso"
    value_rating: float       # 0.0 - 10.0
    reasoning: str            # spiegazione AI
    tips: list[str] = field(default_factory=list)  # bullet points extra

    @property
    def confidence_class(self) -> str:
        return {
            "Alta": "high", "Alto": "high",
            "Media": "medium", "Medio": "medium",
            "Moderata": "low", "Basso": "low",
        }.get(self.confidence, "low")

    @property
    def play_label(self) -> str:
        return "✅ GIOCA" if self.play else "⛔ SKIP"

    def to_dict(self):
        return {
            "play": self.play, "market": self.market,
            "odds_value": self.odds_value, "bookmaker": self.bookmaker,
            "confidence": self.confidence, "value_rating": self.value_rating,
            "reasoning": self.reasoning, "tips": self.tips,
        }

    @classmethod
    def from_dict(cls, data: dict):
        return cls(
            play=data.get("play", False),
            market=data.get("market", "?"),
            odds_value=data.get("odds_value", 0.0),
            bookmaker=data.get("bookmaker", "N/A"),
            confidence=data.get("confidence", "Moderata"),
            value_rating=data.get("value_rating", 0.0),
            reasoning=data.get("reasoning", ""),
            tips=data.get("tips", []),
        )
