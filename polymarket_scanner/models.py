"""Core data models.

`Market` is the raw, API-shaped record. Strategies turn markets into
typed opportunity proposals; the risk controller then either approves
or rejects each proposal.

Phase 3 additions
-----------------
- Market.polymarket_url()          convenience link for Telegram notifier
- StableOpportunity.side           trade direction: "YES" or "NO"
- StableOpportunity.take_profit_price
- VolatilityOpportunity.side
- VolatilityOpportunity.take_profit_price
- SmartMoneyOpportunity            NEW: whale-flow opportunities
- SmartMoneyOpportunity.price_impact_ratio / is_breakout
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
    category: str
    price: float                     # current YES mid-price, 0..1
    liquidity: float                 # USD depth
    volume_24h: float                # USD volume last 24 h
    volume_prev_24h: float           # USD volume 24-48 h ago
    price_change_24h: float          # signed Δ YES price over last 24 h
    days_to_expiry: int
    true_prob: float                 # AI / model estimate of fair probability
    has_political_shock: bool = False
    has_fundamental_change: bool = False

    @property
    def volume_increasing(self) -> bool:
        return self.volume_24h > self.volume_prev_24h

    @property
    def is_macro(self) -> bool:
        return self.category.lower() in {"oil", "gold", "war", "geopolitics"}

    def polymarket_url(self) -> str:
        """Best-effort link to the Polymarket event page."""
        return f"https://polymarket.com/event/{self.market_id}"


# ---------------------------------------------------------------------------
# Opportunity proposals (output of strategy layer)
# ---------------------------------------------------------------------------

@dataclass
class StableOpportunity:
    market: Market
    score: int
    ev: float                        # EV per $1 at risk
    suggested_position: float        # USD (pre-risk-controller)
    expected_profit: float           # USD
    risk_level: Literal["Low", "Medium", "High"]
    side: str = "YES"                # "YES" or "NO"
    take_profit_price: float = 0.0   # stamped by RiskController
    rationale: List[str] = field(default_factory=list)


@dataclass
class VolatilityOpportunity:
    market: Market
    entry_price: float
    target_price: float              # bracket target (also used as TP)
    stop_loss: float
    ev: float
    suggested_position: float
    expected_profit: float
    max_hold_days: int
    side: str = "YES"
    take_profit_price: float = 0.0   # stamped by RiskController (= target_price)
    rationale: List[str] = field(default_factory=list)


@dataclass
class SmartMoneyOpportunity:
    """Whale-flow opportunity with price-impact confirmation (Smart Money 2.0).

    Confidence levels
    -----------------
    HIGH   : vol spike + price impact passes + breakout confirmed
    MEDIUM : vol spike + price impact passes (no full breakout)
    """
    market: Market
    confidence: Literal["HIGH", "MEDIUM"]
    flow_direction: Literal["BUY", "SELL"]
    volume_spike_ratio: float        # volume_24h / liquidity
    price_move_pct: float            # abs(price_change_24h)
    price_impact_ratio: float        # price_move / (vol/liq) — whale filter
    is_breakout: bool                # abs(move) >= sm_breakout_threshold
    ev: float
    suggested_position: float
    expected_profit: float
    side: str = "YES"
    take_profit_price: float = 0.0   # stamped by RiskController
    rationale: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Risk controller output
# ---------------------------------------------------------------------------

@dataclass
class RiskDecision:
    approved: bool
    reasons: List[str] = field(default_factory=list)
    approved_position: Optional[float] = None
