"""Default configuration for scanner strategies and risk management."""

from dataclasses import dataclass
from typing import Tuple


@dataclass
class AccountConfig:
    total_capital: float = 50.0  # USD

    # Position sizing
    max_position_ratio: float = 0.10  # 10% of capital per trade
    kelly_fraction: float = 0.25  # 1/4 Kelly
    kelly_cap: float = 0.20  # cap at 20% of capital

    # Profit target
    min_absolute_profit: float = 0.05  # $0.05 minimum
    tp_capture_ratio: float = 0.80  # capture 80% of AI edge as TP

    # Execution
    maker_threshold: float = 0.01  # use limit orders if spread > 1c
    taker_threshold: float = 0.01  # take if spread <= 1c

    # Risk limits
    max_positions: int = 15  # max concurrent positions

    # ---- Stable (80% sleeve) ----
    stable_min_liquidity: float = 50_000.0  # $50k
    stable_max_days_to_expiry: int = 60
    stable_min_score: int = 2
    stable_price_high: float = 0.70
    stable_price_low: float = 0.30
    
    # 宏观风险黑名单
    macro_blocklist: Tuple[str, ...] = ("oil", "gold", "war", "geopolitics")

    # ---- Volatility (20% sleeve) ----
    vol_min_liquidity: float = 50_000.0
    vol_min_abs_price_change_24h: float = 0.02

    # ---- Smart Money ----
    sm_min_volume: float = 100_000.0
    sm_min_vol_ratio: float = 2.0
    sm_min_price_move: float = 0.02

    # ---- Arbitrage ----
    arb_min_spread: float = 0.02


DEFAULT_CONFIG = AccountConfig()