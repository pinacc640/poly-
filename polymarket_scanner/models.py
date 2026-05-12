"""Core data models.

`Market` is the raw, API-shaped record. Strategies turn markets into
typed opportunity proposals; the risk controller then either approves
or rejects each proposal.
"""

from dataclasses import dataclass, field
from typing import List, Literal, Optional


# ---------------------------------------------------------------------------
# Raw market record (the shape a future Polymarket API client would produce)
# ---------------------------------------------------------------------------
@dataclass
class Market:
    market_id: str
    question: str
    category: str                    # e.g. "politics", "sports", "crypto", "oil"
    price: float                     # current YES price, 0..1
    liquidity: float                 # USD depth
    volume_24h: float                # USD volume in last 24h
    volume_prev_24h: float           # USD volume 24-48h ago (for trend detection)
    price_change_24h: float          # signed delta in YES price over last 24h
    days_to_expiry: int
    true_prob: float                 # analyst / model estimate of fair prob
    has_political_shock: bool = False         # sudden headline risk
    has_fundamental_change: bool = False      # disqualifies vol arbitrage
    # Bug fix #2: store the event slug from events[0].slug for correct URL generation.
    # Falls back to the market-level slug, then to market_id if neither is available.
    event_slug: str = ""

    @property
    def volume_increasing(self) -> bool:
        """Crude proxy for rising interest."""
        return self.volume_24h > self.volume_prev_24h

    @property
    def is_macro(self) -> bool:
        # Matching is done against the config blocklist in the strategy,
        # but this convenience property is handy for tests/debugging.
        return self.category.lower() in {"oil", "gold", "war", "geopolitics"}

    def polymarket_url(self) -> str:
        """Return the canonical Polymarket event URL.

        Uses the event-level slug (events[0].slug from the API) which maps
        to https://polymarket.com/event/{slug}. Falls back gracefully if
        no slug was stored.
        """
        slug = self.event_slug or self.market_id
        return f"https://polymarket.com/event/{slug}"


# ---------------------------------------------------------------------------
# Strategy output: opportunity proposals
# ---------------------------------------------------------------------------
@dataclass
class StableOpportunity:
    market: Market
    score: int
    ev: float                        # expected value per $1 at risk
    suggested_position: float        # USD
    expected_profit: float           # USD
    risk_level: Literal["Low", "Medium", "High"]
    rationale: List[str] = field(default_factory=list)


@dataclass
class VolatilityOpportunity:
    market: Market
    entry_price: float
    target_price: float
    stop_loss: float
    ev: float
    suggested_position: float        # USD
    expected_profit: float           # USD at target
    max_hold_days: int
    rationale: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Risk controller result
# ---------------------------------------------------------------------------
@dataclass
class RiskDecision:
    approved: bool
    reasons: List[str] = field(default_factory=list)   # why rejected (if any)
    approved_position: Optional[float] = None          # possibly downsized
