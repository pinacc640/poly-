"""Default configuration for scanner strategies and risk management."""

from dataclasses import dataclass


@dataclass
class AccountConfig:
    total_capital: float = 50.0  # USD

    # Position sizing
    max_position_ratio: float = 0.10  # 10% of capital per trade
    kelly_fraction: float = 0.25  # 1/4 Kelly
    kelly_cap: float = 0.20  # cap at 20% of capital

    # Profit target
    min_absolute_profit: float = 0.10  # $0.10 minimum (放宽)
    tp_capture_ratio: float = 0.80  # capture 80% of AI edge as TP

    # Execution
    maker_threshold: float = 0.01  # use limit orders if spread > 1c
    taker_threshold: float = 0.01  # take if spread <= 1c

    # Risk limits
    max_positions: int = 10  # max concurrent positions

    # ---- Stable (80% sleeve) ----
    stable_min_liquidity: float = 100_000.0  # $100k
    stable_max_days_to_expiry: int = 30  # 放宽到 30 天
    stable_min_score: int = 3  # 放宽到 3 分
    stable_price_high: float = 0.75  # 扩大到 0.75
    stable_price_low: float = 0.25  # 扩大到 0.25

    # ---- Volatility (20% sleeve) ----
    vol_min_liquidity: float = 100_000.0
    vol_min_abs_price_change_24h: float = 0.03  # 放宽到 3%

    # ---- Smart Money ----
    sm_min_volume: float = 100_000.0
    sm_min_vol_ratio: float = 2.0  # volume must 2x previous 24h
    sm_min_price_move: float = 0.02  # 2% price move

    # ---- Arbitrage ----
    arb_min_spread: float = 0.02  # 2% minimum spread


# Global default for use when caller does not provide config
DEFAULT_CONFIG = AccountConfig()