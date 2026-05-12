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
    min_absolute_profit: float = 0.10        # USD  (lowered from 0.50)
    min_profit_ratio: float = 0.002          # 0.2% of total capital

    # --- Take Profit ---
    # TP = entry + (true_prob - entry) * tp_capture_ratio
    # 0.80 = capture 80% of the edge gap; avoids waiting for settlement at 1.0
    tp_capture_ratio: float = 0.80

    # --- Stable strategy filters ---
    stable_max_days_to_expiry: int = 14
    stable_price_high: float = 0.80
    stable_price_low: float = 0.20
    stable_min_liquidity: float = 100_000.0
    stable_min_score: int = 5

    macro_blocklist: Tuple[str, ...] = (
        "oil",
        "gold",
        "war",
        "geopolitics",
    )

    # --- Volatility strategy filters ---
    vol_min_abs_price_change_24h: float = 0.05
    vol_min_liquidity: float = 100_000.0
    vol_target_move: float = 0.05
    vol_stop_move: float = 0.05
    vol_max_hold_days: int = 3

    # --- Smart Money 2.0 filters ---
    sm_min_volume_24h: float = 100_000.0
    sm_min_liquidity: float = 100_000.0
    sm_max_days_to_expiry: int = 30
    sm_min_vol_liq_ratio: float = 0.30       # volume / liquidity >= 0.30
    sm_min_price_move: float = 0.15          # abs(price_change_24h) for basic filter

    # Price-impact whale filter (NEW in Phase 3)
    # price_impact_ratio = abs(price_change_24h) / (volume_24h / liquidity)
    # High volume + no price move = wash trading; require >= this threshold.
    sm_min_price_impact_ratio: float = 0.10

    # Breakout threshold: abs(price_change_24h) must exceed this for HIGH confidence
    sm_breakout_threshold: float = 0.10

    # EV edge boost when confirmed smart-money signal fires
    sm_high_confidence_edge: float = 0.10
    sm_medium_confidence_edge: float = 0.05


# Default singleton used everywhere.
DEFAULT_CONFIG = AccountConfig()
