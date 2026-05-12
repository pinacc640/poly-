"""Account-level configuration and strategy thresholds.

All numeric knobs live here so that behavior can be tuned without
touching strategy logic. When real capital grows, only this file
needs to change.
"""

from dataclasses import dataclass, field
from typing import Tuple


@dataclass(frozen=True)
class AccountConfig:
    # --- Capital ---
    total_capital: float = 50.0

    # --- Legacy sizing (used as INITIAL suggested_position in strategies) ---
    # The risk controller will REPLACE this with a Kelly-derived size.
    max_position_ratio: float = 0.10         # strategies use this as a starting guess
    volatility_cap_ratio: float = 0.20       # total volatility sleeve <= 20%

    # --- Kelly Criterion position sizing ---
    # Quarter-Kelly fraction: multiply raw Kelly f by this to avoid ruin.
    # 0.25 is the industry standard for binary-outcome markets.
    kelly_fraction: float = 0.25
    # Hard cap: even a sky-high Kelly signal cannot exceed this % of capital.
    max_kelly_position_ratio: float = 0.20   # 20% absolute ceiling per trade

    # --- Minimum profitability gates ---
    min_absolute_profit: float = 0.10        # USD  ← lowered to catch more opportunities
    min_profit_ratio: float = 0.002          # 0.2% of total capital

    # --- Order-book / spread thresholds ---
    # If bid-ask spread is <= this value, treat as "tight" → suggest Taker order.
    # If spread > this value, suggest Maker order (limit at best_bid + 0.01).
    taker_spread_threshold: float = 0.01     # 1¢ spread = go Taker; > 1¢ = go Maker

    # --- Stable strategy filters ---
    stable_max_days_to_expiry: int = 14
    stable_price_high: float = 0.80
    stable_price_low: float = 0.20
    stable_min_liquidity: float = 100_000.0
    stable_min_score: int = 5

    # Categories that the stable strategy refuses to touch because
    # they carry macro/headline tail risk disproportionate to a
    # convergence thesis.
    macro_blocklist: Tuple[str, ...] = (
        "oil",
        "gold",
        "war",
        "geopolitics",
    )

    # --- Volatility strategy filters ---
    vol_min_abs_price_change_24h: float = 0.05
    vol_min_liquidity: float = 100_000.0
    vol_target_move: float = 0.05            # +5% take profit
    vol_stop_move: float = 0.05              # -5% stop loss
    vol_max_hold_days: int = 3

    # --- Take Profit (Phase 3) ---
    # TP = entry + (true_prob - entry) * tp_capture_ratio
    # 0.80 = capture 80% of the AI edge gap; avoids waiting for settlement.
    tp_capture_ratio: float = 0.80

    # --- Smart Money strategy filters ---
    sm_min_volume_24h: float = 100_000.0
    sm_min_price_move: float = 0.15
    sm_min_vol_liq_ratio: float = 0.30
    sm_high_confidence_edge: float = 0.10
    sm_medium_confidence_edge: float = 0.05
    sm_max_days_to_expiry: int = 30
    sm_min_liquidity: float = 100_000.0

    # --- Smart Money 2.0: price-impact whale filter (Phase 3) ---
    # price_impact_ratio = abs(price_change_24h) / (volume_24h / liquidity)
    # High volume + no price movement = wash trading → reject.
    sm_min_price_impact_ratio: float = 0.10
    # Breakout: abs(price_change_24h) must exceed this for HIGH confidence.
    sm_breakout_threshold: float = 0.10

    # --- Arbitrage strategy parameters ---
    arb_threshold: float = 0.98
    arb_min_title_similarity: float = 0.30
    arb_kalshi_limit: int = 300

    # --- AI Oracle parameters ---
    ai_oracle_timeout: float = 20.0
    ai_oracle_max_markets: int = 50


# Default singleton. Callers can inject a custom config into MarketScanner.
DEFAULT_CONFIG = AccountConfig()
