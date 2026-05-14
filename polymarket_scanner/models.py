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
    bid: float = 0.0                 # best bid price
    ask: float = 1.0                 # best ask price
    liquidity: float                 # USD depth
    volume_24h: float                # USD volume in last 24h
    volume_prev_24h: float           # USD volume 24-48h ago (for trend detection)
    price_change_24h: float          # signed delta in YES price over last 24h
    days_to_expiry: int
    true_prob: float                 # analyst / model estimate of fair prob
    has_political_shock: bool = False         # sudden headline risk
    has_fundamental_change: bool = False      # disqualifies vol arbitrage

    @property
    def spread(self) -> float:
        """Bid-ask spread."""
        return self.ask - self.bid

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
    side: str = "YES"                # YES or NO
    kelly_bet: float = 0.0           # Kelly bet amount
    take_profit_price: float = 0.0   # TP price
    rationale: List[str] = field(default_factory=list)

    @property
    def market_id(self) -> str:
        return self.market.market_id

    @property
    def question(self) -> str:
        return self.market.question

    @property
    def true_prob(self) -> float:
        return self.market.true_prob

    @property
    def entry_price(self) -> float:
        return self.market.price


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
    side: str = "YES"                # YES or NO
    kelly_bet: float = 0.0           # Kelly bet amount
    take_profit_price: float = 0.0   # TP price
    rationale: List[str] = field(default_factory=list)

    @property
    def market_id(self) -> str:
        return self.market.market_id

    @property
    def question(self) -> str:
        return self.market.question


# ---------------------------------------------------------------------------
# Risk controller result
# ---------------------------------------------------------------------------
@dataclass
class RiskDecision:
    approved: bool
    reasons: List[str] = field(default_factory=list)   # why rejected (if any)
    approved_position: Optional[float] = None          # possibly downsized


# ---------------------------------------------------------------------------
# Additional strategy outputs
# ---------------------------------------------------------------------------
@dataclass
class SmartMoneyOpportunity:
    market: Market
    flow_direction: str              # "BUY" or "SELL"
    vol_liq_ratio: float             # volume / liquidity ratio
    price_impact: float              # price change magnitude
    confidence: str                  # "LOW", "MEDIUM", "HIGH"
    ev: float
    suggested_position: float        # USD
    expected_profit: float           # USD
    side: str = "YES"                # YES or NO
    kelly_bet: float = 0.0           # Kelly bet amount
    take_profit_price: float = 0.0   # TP price
    rationale: List[str] = field(default_factory=list)

    @property
    def market_id(self) -> str:
        return self.market.market_id

    @property
    def question(self) -> str:
        return self.market.question

    @property
    def true_prob(self) -> float:
        return self.market.true_prob

    @property
    def entry_price(self) -> float:
        return self.market.price


@dataclass
class ArbitrageOpportunity:
    poly_market: Market
    kalshi_data: dict                # raw kalshi market data
    poly_side: str                   # "YES" or "NO"
    poly_price: float
    kalshi_price: float
    spread: float                    # profit potential
    confidence: str                  # "LOW", "MEDIUM", "HIGH"
    suggested_position: float        # USD
    expected_profit: float           # USD
    rationale: List[str] = field(default_factory=list)

    @property
    def market_id(self) -> str:
        return self.poly_market.market_id

    @property
    def question(self) -> str:
        return self.poly_market.question

    @property
    def true_prob(self) -> float:
        return self.poly_market.true_prob
