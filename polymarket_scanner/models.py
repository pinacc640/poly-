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

    @property
    def volume_increasing(self) -> bool:
        """Crude proxy for rising interest."""
        return self.volume_24h > self.volume_prev_24h

    @property
    def is_macro(self) -> bool:
        # Matching is done against the config blocklist in the strategy,
        # but this convenience property is handy for tests/debugging.
        return self.category.lower() in {"oil", "gold", "war", "geopolitics"}


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


@dataclass
class SmartMoneyOpportunity:
    """A market flagged as having anomalous smart-money accumulation.

    Detection signals
    -----------------
    - volume_24h exceeds a high threshold (large players are active)
    - price_change_24h exceeds a directional threshold (one-sided flow)
    - volume / liquidity ratio is elevated (volume >> resting depth)

    Confidence levels
    -----------------
    HIGH   : all three signals present
    MEDIUM : volume + price move signals present
    LOW    : only volume spike present
    """
    market: Market
    confidence: Literal["HIGH", "MEDIUM", "LOW"]
    volume_spike_ratio: float        # volume_24h / avg_daily_volume (>1 = spike)
    price_move_pct: float            # abs(price_change_24h) as a percentage 0..1
    flow_direction: Literal["BUY", "SELL"]  # direction smart money appears to be going
    ev: float                        # EV per $1 if we follow the flow
    suggested_position: float        # USD
    expected_profit: float           # USD
    rationale: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Risk controller result
# ---------------------------------------------------------------------------
@dataclass
class RiskDecision:
    approved: bool
    reasons: List[str] = field(default_factory=list)   # why rejected (if any)
    approved_position: Optional[float] = None          # possibly downsized
