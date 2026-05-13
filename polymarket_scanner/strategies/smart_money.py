"""Smart Money 巨鲸异动策略（观察层）

设计理念
--------
Smart Money 是**纯观察层**，不生成仓位建议，不进入风险控制器。
它扫描全市场，识别资金流速（Vol/Liq）和价格异动两个维度的异常，
将结果分两级输出：

  WHALE_ALERT  ── Vol/Liq >= 0.50 且 |price_change_24h| >= 2%
                  高置信度巨鲸行为，触发 Telegram 独立预警
  WATCH        ── Vol/Liq >= 0.20 且 |price_change_24h| >= 1%
                  观察标的，记入报告但不预警

两个维度说明
-----------
1. Vol/Liq 比率（资金流速）
   = volume_24h / liquidity
   反映市场深度相对于当日成交量的倍数。
   - >= 0.50 → 当日成交量超过流动性深度的 50%，机构级别的资金参与
   - >= 0.20 → 明显高于正常水平，值得关注

2. 24h 价格变动（|price_change_24h|）
   反映事件驱动或定向资金的信息优势。
   - >= 2% → 高幅价格异动
   - >= 1% → 中等价格异动

为什么与 Stable / Volatility 解耦
----------------------------------
Smart Money 信号不代表 EV 正，不保证均值回归，可能是超前的定向下注。
在 EV 没有被确认之前，将其混入执行层会制造虚假信号。
因此 Smart Money 只作为"信息层"输出，由操作者人工决策。
"""

from __future__ import annotations

from typing import List

from ..models import Market, SmartMoneySignal


# ---------------------------------------------------------------------------
# 阈值常量（可通过 AccountConfig 未来扩展为配置项）
# ---------------------------------------------------------------------------
WHALE_VOL_LIQ_THRESHOLD    = 0.50   # Vol/Liq >= 此值 → WHALE_ALERT
WHALE_PRICE_MOVE_THRESHOLD = 0.02   # |24h price move| >= 此值 → WHALE_ALERT
WATCH_VOL_LIQ_THRESHOLD    = 0.20   # Vol/Liq >= 此值 → WATCH
WATCH_PRICE_MOVE_THRESHOLD = 0.01   # |24h price move| >= 此值 → WATCH
MIN_LIQUIDITY              = 10_000  # 流动性下限，过滤无意义尾部市场


def smart_money_strategy(markets: List[Market]) -> List[SmartMoneySignal]:
    """扫描全量市场，返回 SmartMoneySignal 列表（按 vol_liq_ratio 降序）。

    Parameters
    ----------
    markets :
        经过持仓分流后的新市场列表（与 Stable/Vol 同源，不含已持仓市场）。

    Returns
    -------
    List[SmartMoneySignal]
        WHALE_ALERT 信号排在前面，同级别内按 vol_liq_ratio 降序。
        调用方负责决定是否推送 Telegram 以及如何格式化输出。
    """
    signals: List[SmartMoneySignal] = []

    for m in markets:
        # 1. 基础流动性门槛过滤
        if m.liquidity < MIN_LIQUIDITY:
            continue

        # 2. 计算核心指标
        vol_liq_ratio  = m.volume_24h / m.liquidity if m.liquidity > 0 else 0.0
        abs_price_move = abs(m.price_change_24h)

        # 3. 分级判断
        if vol_liq_ratio >= WHALE_VOL_LIQ_THRESHOLD and abs_price_move >= WHALE_PRICE_MOVE_THRESHOLD:
            signal_type = "WHALE_ALERT"
        elif vol_liq_ratio >= WATCH_VOL_LIQ_THRESHOLD and abs_price_move >= WATCH_PRICE_MOVE_THRESHOLD:
            signal_type = "WATCH"
        else:
            continue   # 不符合任何门槛，跳过

        # 4. 生成信号描述
        rationale: List[str] = [
            f"Vol/Liq = {vol_liq_ratio:.2f}",
            f"24h 价格变动 = {m.price_change_24h:+.2%}",
            f"当前价格 = {m.price:.3f}",
            f"流动性 = ${m.liquidity:,.0f}",
            f"24h 成交量 = ${m.volume_24h:,.0f}",
        ]

        signals.append(SmartMoneySignal(
            market         = m,
            signal_type    = signal_type,
            vol_liq_ratio  = round(vol_liq_ratio, 4),
            abs_price_move = round(abs_price_move, 4),
            rationale      = rationale,
        ))

    # WHALE_ALERT 优先，同级别按 vol_liq_ratio 降序
    signals.sort(
        key=lambda s: (0 if s.signal_type == "WHALE_ALERT" else 1, -s.vol_liq_ratio)
    )
    return signals
