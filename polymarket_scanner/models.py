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
    price: float                     # current YES mid-price, 0..1
    liquidity: float                 # USD depth
    volume_24h: float                # USD volume in last 24h
    volume_prev_24h: float           # USD volume 24-48h ago (for trend detection)
    price_change_24h: float          # signed delta in YES price over last 24h
    days_to_expiry: int
    true_prob: float                 # analyst / model estimate of fair prob
    has_political_shock: bool = False
    has_fundamental_change: bool = False

    # --- Order-book fields (populated by mapper from Gamma API) ---
    best_bid: float = 0.0            # best buy price in the order book
    best_ask: float = 0.0            # best sell price in the order book

    @property
    def spread(self) -> float:
        """Bid-ask spread. Returns 0 if bid/ask not populated."""
        if self.best_bid <= 0 or self.best_ask <= 0:
            return 0.0
        return round(self.best_ask - self.best_bid, 4)

    @property
    def volume_increasing(self) -> bool:
        """Crude proxy for rising interest."""
        return self.volume_24h > self.volume_prev_24h

    @property
    def is_macro(self) -> bool:
        return self.category.lower() in {"oil", "gold", "war", "geopolitics"}

    def polymarket_url(self) -> str:
        """Best-effort link to the Polymarket event page."""
        return f"https://polymarket.com/event/{self.market_id}"


# ---------------------------------------------------------------------------
# Order-book execution advice (attached to every opportunity)
# ---------------------------------------------------------------------------
@dataclass
class OrderBookAdvice:
    """Execution suggestion derived from the bid-ask spread.

    Attributes
    ----------
    side : "YES" or "NO" — which token to buy
    order_type : "MAKER" or "TAKER"
        MAKER = place a limit order and wait for fill (better price, slower)
        TAKER = execute at market (immediate, pays the spread)
    limit_price : float
        For MAKER orders: suggested limit price (best_bid + 0.01).
        For TAKER orders: best_ask (the price you'll pay immediately).
    spread : float
        The bid-ask spread at time of scan.
    rationale : str
        One-line human-readable explanation.
    """
    side: Literal["YES", "NO"]
    order_type: Literal["MAKER", "TAKER"]
    limit_price: float
    spread: float
    rationale: str


# ---------------------------------------------------------------------------
# Strategy output: opportunity proposals
# ---------------------------------------------------------------------------
@dataclass
class StableOpportunity:
    market: Market
    score: int
    ev: float                        # expected value per $1 at risk
    suggested_position: float        # USD — raw strategy suggestion (pre-Kelly)
    expected_profit: float           # USD
    risk_level: Literal["Low", "Medium", "High"]
    rationale: List[str] = field(default_factory=list)

    # Filled in by RiskController after Kelly sizing
    kelly_f: float = 0.0             # raw Kelly fraction (before quarter-Kelly cap)
    kelly_position: float = 0.0      # Quarter-Kelly sized USD amount
    order_advice: Optional[OrderBookAdvice] = None
    # Phase 3: Take Profit price (stamped by RiskController)
    take_profit_price: float = 0.0


@dataclass
class VolatilityOpportunity:
    market: Market
    entry_price: float
    target_price: float
    stop_loss: float
    ev: float
    suggested_position: float        # USD — raw strategy suggestion (pre-Kelly)
    expected_profit: float           # USD at target
    max_hold_days: int
    rationale: List[str] = field(default_factory=list)

    # Filled in by RiskController after Kelly sizing
    kelly_f: float = 0.0
    kelly_position: float = 0.0
    order_advice: Optional[OrderBookAdvice] = None
    # Phase 3: Take Profit price (stamped by RiskController; equals target_price for vol)
    take_profit_price: float = 0.0


@dataclass
class SmartMoneyOpportunity:
    market: Market
    confidence: Literal["HIGH", "MEDIUM", "LOW"]
    volume_spike_ratio: float
    price_move_pct: float
    flow_direction: Literal["BUY", "SELL"]
    ev: float
    suggested_position: float        # USD — raw strategy suggestion (pre-Kelly)
    expected_profit: float           # USD
    rationale: List[str] = field(default_factory=list)

    # Filled in by RiskController after Kelly sizing
    kelly_f: float = 0.0
    kelly_position: float = 0.0
    order_advice: Optional[OrderBookAdvice] = None
    # Phase 3: price-impact whale filter fields + Take Profit
    price_impact_ratio: float = 0.0  # abs(Δprice) / (vol/liq) — wash-trading filter
    is_breakout: bool = False         # abs(move) >= sm_breakout_threshold
    take_profit_price: float = 0.0


# ---------------------------------------------------------------------------
# Cross-platform arbitrage opportunity
# ---------------------------------------------------------------------------
@dataclass
class ArbitrageOpportunity:
    poly_market: Market
    kalshi_ticker: str
    kalshi_title: str
    title_similarity: float
    poly_yes_price: float
    kalshi_no_price: float
    combined_cost: float
    guaranteed_profit_pct: float
    suggested_position: float
    expected_profit: float


# ---------------------------------------------------------------------------
# Risk controller result
# ---------------------------------------------------------------------------
@dataclass
class RiskDecision:
    approved: bool
    reasons: List[str] = field(default_factory=list)
    approved_position: Optional[float] = None   # Kelly-sized, possibly capped
    kelly_f: float = 0.0                        # raw Kelly fraction for display
    kelly_position: float = 0.0                 # quarter-Kelly USD before cap
