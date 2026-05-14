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
    min_absolute_profit: float = 0.10        # USD
    min_profit_ratio: float = 0.01           # 1% of total capital

    # --- Stable strategy filters ---
    stable_max_days_to_expiry: int = 30
    stable_price_high: float = 0.75
    stable_price_low: float = 0.25
    stable_min_liquidity: float = 100_000.0
    stable_min_score: int = 3

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
    vol_min_abs_price_change_24h: float = 0.03
    vol_min_liquidity: float = 100_000.0
    vol_target_move: float = 0.05            # +5% take profit
    vol_stop_move: float = 0.05              # -5% stop loss
    vol_max_hold_days: int = 3


# Default singleton. Callers can inject a custom config into MarketScanner.
DEFAULT_CONFIG = AccountConfig()
