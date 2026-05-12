"""Core data models.

`Market` is the raw, API-shaped record. Strategies turn markets into
typed opportunity proposals; the risk controller then either approves
or rejects each proposal.
"""

from dataclasses import dataclass, field
from typing import List, Literal, Optional


# ---------------------------------------------------------------------------
# Raw market record
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
    has_political_shock: bool = False
    has_fundamental_change: bool = False

    @property
    def volume_increasing(self) -> bool:
        return self.volume_24h > self.volume_prev_24h

    @property
    def is_macro(self) -> bool:
        return self.category.lower() in {"oil", "gold", "war", "geopolitics"}

    def polymarket_url(self) -> str:
        """Best-effort link to the market page on Polymarket."""
        return f"https://polymarket.com/event/{self.market_id}"


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
    side: str = "YES"                # "YES" or "NO"
    take_profit_price: float = 0.0   # TP limit price: entry + (true_prob - entry) * 0.80
    rationale: List[str] = field(default_factory=list)


@dataclass
class VolatilityOpportunity:
    market: Market
    entry_price: float
    target_price: float              # strategy-computed bracket target
    stop_loss: float
    ev: float
    suggested_position: float        # USD
    expected_profit: float           # USD at target
    max_hold_days: int
    side: str = "YES"
    take_profit_price: float = 0.0   # TP limit price (same as target_price for vol)
    rationale: List[str] = field(default_factory=list)


@dataclass
class SmartMoneyOpportunity:
    """A market flagged as whale accumulation with price-impact confirmation."""
    market: Market
    confidence: Literal["HIGH", "MEDIUM", "LOW"]
    flow_direction: Literal["BUY", "SELL"]
    volume_spike_ratio: float        # volume_24h / liquidity
    price_move_pct: float            # abs(price_change_24h)
    price_impact_ratio: float        # price_move / (volume/liquidity)  ← NEW
    is_breakout: bool                # abs(move) > sm_breakout_threshold ← NEW
    ev: float
    suggested_position: float        # USD
    expected_profit: float           # USD
    side: str = "YES"
    take_profit_price: float = 0.0
    rationale: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Risk controller result
# ---------------------------------------------------------------------------
@dataclass
class RiskDecision:
    approved: bool
    reasons: List[str] = field(default_factory=list)
    approved_position: Optional[float] = None
