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

    # -------------------------------------------------------------------------
    # Phase 2 — Position Monitor thresholds
    # -------------------------------------------------------------------------

    # 止盈条件（满足任意一条即触发 TAKE_PROFIT）
    # 条件 A：价格达到绝对高位
    monitor_tp_abs_price: float = 0.90          # current_price >= 0.90

    # 条件 B：相对均价盈利超过 N 个价格点
    monitor_tp_delta: float = 0.20              # current_price >= avg_price + 0.20

    # 止损/AI 反转条件：AI 胜率下修超过此幅度（相对持仓方向）
    monitor_sl_ai_reversal: float = 0.15        # true_prob < avg_price - 0.15

    # 临近到期止盈：剩余天数 <= N 且有浮盈
    monitor_tp_expiry_days: int = 2

    # 加仓条件（全部满足才触发 ADD_POSITION）
    monitor_add_discount: float = 0.85          # current_price <= avg_price * 0.85
    monitor_add_ai_edge: float = 0.10           # true_prob >= avg_price + 0.10
    monitor_add_min_days: int = 3               # days_to_expiry >= 3
    monitor_add_min_liquidity: float = 50_000.0 # 市场流动性下限

    # AI 增强：持仓监控时送入 AI 的最大市场数（同样受 Top-N 截断）
    monitor_ai_top_n: int = 10

    # -------------------------------------------------------------------------
    # Phase 2 — Arbitrage Scanner thresholds
    # -------------------------------------------------------------------------

    # 套利信号最小利润空间：1 - pm_price - kalshi_price >= arb_min_gap
    arb_min_gap: float = 0.05                   # 5% 利润空间才值得关注

    # Jaccard 关键词相似度阈值（低于此值直接跳过，不送 AI 验证）
    arb_jaccard_threshold: float = 0.30

    # Kalshi 市场最低流动性（open_interest）
    arb_kalshi_min_oi: float = 1_000.0          # $1000 open interest

    # AI 验证：每次套利扫描最多送 DeepSeek 验证的候选对数量
    arb_ai_verify_top_n: int = 10


# Default singleton. Callers can inject a custom config into MarketScanner.
DEFAULT_CONFIG = AccountConfig()
