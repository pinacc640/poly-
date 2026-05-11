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

    # --- Sizing ---
    max_position_ratio: float = 0.10         # single position <= 10% of capital
    volatility_cap_ratio: float = 0.20       # total volatility sleeve <= 20%

    # --- Minimum profitability gates ---
    min_absolute_profit: float = 0.50        # USD
    min_profit_ratio: float = 0.01           # 1% of total capital

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

    # --- Smart Money strategy filters ---
    # Minimum 24h volume to be considered "significant activity"
    sm_min_volume_24h: float = 100_000.0
    # Minimum one-sided price move in 24h to flag directional flow
    sm_min_price_move: float = 0.15
    # Volume / liquidity ratio threshold — high ratio = volume >> resting depth
    # indicating large players are aggressively crossing the book
    sm_min_vol_liq_ratio: float = 0.30
    # EV boost applied to true_prob when all three signals fire (HIGH confidence)
    # Reflects the assumption that smart money has an information edge
    sm_high_confidence_edge: float = 0.10
    # EV boost for MEDIUM confidence (volume + price move, no ratio signal)
    sm_medium_confidence_edge: float = 0.05
    # Maximum days to expiry allowed for smart money trades
    sm_max_days_to_expiry: int = 30
    # Minimum liquidity (shallow books distort the volume signal)
    sm_min_liquidity: float = 100_000.0


# Default singleton. Callers can inject a custom config into MarketScanner.
DEFAULT_CONFIG = AccountConfig()
