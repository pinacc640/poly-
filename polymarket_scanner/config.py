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
    min_profit_ratio: float = 0.002          # 0.2% of total capital

    # --- Take Profit configuration ---
    # TP price = entry + (true_prob - entry) * tp_capture_ratio
    # 0.80 means we capture 80% of the gap between entry and AI fair value,
    # rather than waiting for the contract to settle at 1.0.
    tp_capture_ratio: float = 0.80

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

    # --- Smart Money 2.0 filters ---
    # Basic volume thresholds (unchanged)
    sm_min_volume_24h: float = 100_000.0
    sm_min_price_move: float = 0.15
    sm_min_vol_liq_ratio: float = 0.30

    # Price-impact / whale filter (NEW)
    # A "real" smart-money move must show price actually moving.
    # If volume is huge but price barely moved, it's wash-trading / fake volume.
    #
    # price_impact_ratio = abs(price_change_24h) / (volume_24h / liquidity)
    # Intuitively: how much price moved per unit of volume/liquidity pressure.
    # Too low → volume not causing price movement → suspect fake volume.
    sm_min_price_impact_ratio: float = 0.10   # at least 10% price impact per vol/liq unit

    # Breakout confirmation: price must breach a key level.
    # We use 1-standard-deviation proxy: abs(price_change_24h) > sm_breakout_threshold
    # to distinguish "noise" from "real directional conviction".
    sm_breakout_threshold: float = 0.10       # 10% move = confirmed breakout

    # EV boost multipliers for confirmed smart money (applied to true_prob edge)
    sm_high_confidence_edge: float = 0.10     # +10% edge boost for HIGH confidence
    sm_medium_confidence_edge: float = 0.05   # +5% edge boost for MEDIUM confidence

    sm_max_days_to_expiry: int = 30
    sm_min_liquidity: float = 100_000.0


# Default singleton. Callers can inject a custom config into MarketScanner.
DEFAULT_CONFIG = AccountConfig()
