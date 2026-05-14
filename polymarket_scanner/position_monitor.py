"""持仓风控监控 — 检查现有仓位的 TP/SL/加仓信号。

根据持仓的入场价和当前价格，生成以下类型的警报：
- Take Profit (TP): 当前价格达到目标止盈价
- Stop Loss (SL): 当前价格触及止损线
- Average Down: 价格大幅下跌，可能适合加仓
- Breakout: 价格突破关键水平

用法
----
    from polymarket_scanner.position_monitor import PositionMonitor
    
    monitor = PositionMonitor()
    alerts = monitor.check(portfolio, all_markets)
"""

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

log = logging.getLogger(__name__)


@dataclass
class PositionAlert:
    """持仓警报。"""
    market_id: str
    question: str
    alert_type: str  # "TP", "SL", "AVG_DOWN", "BREAKOUT"
    side: str
    size: float
    avg_price: float
    current_price: float
    unrealized_pnl: float
    message: str


class PositionMonitor:
    """持仓风控监控器。"""

    def __init__(
        self,
        tp_threshold: float = 0.15,    # 15% 利润触发 TP
        sl_threshold: float = -0.10,   # -10% 亏损触发 SL
        avg_down_threshold: float = -0.20,  # -20% 考虑加仓
        breakout_threshold: float = 0.10,   # 10% 突破
    ):
        self.tp_threshold = tp_threshold
        self.sl_threshold = sl_threshold
        self.avg_down_threshold = avg_down_threshold
        self.breakout_threshold = breakout_threshold

    def _calculate_pnl_pct(self, avg_price: float, current_price: float, side: str) -> float:
        """计算盈亏百分比。"""
        if avg_price == 0:
            return 0

        if side == "YES":
            return (current_price - avg_price) / avg_price
        else:  # NO
            return ((1 - current_price) - (1 - avg_price)) / (1 - avg_price) if avg_price < 1 else 0

    def _check_position(
        self,
        position,
        current_market: Optional[dict] = None,
    ) -> Optional[PositionAlert]:
        """检查单个仓位是否触发警报。"""
        market_id = getattr(position, "market_id", "")
        question = getattr(position, "question", "")[:80]
        side = getattr(position, "side", "YES")
        size = getattr(position, "size", 0)
        avg_price = getattr(position, "avg_price", 0)
        current_price = getattr(position, "current_price", avg_price)
        unrealized_pnl = getattr(position, "unrealized_pnl", 0)

        # 如果有实时市场数据，用更准确的价格
        if current_market:
            market_price = getattr(current_market, "price", None)
            if market_price is not None:
                current_price = market_price if side == "YES" else (1 - market_price)

        pnl_pct = self._calculate_pnl_pct(avg_price, current_price, side)

        # 检查各种警报条件
        alert_type = None
        message = ""

        if pnl_pct >= self.tp_threshold:
            alert_type = "TP"
            message = f"🎯 Take Profit! +{pnl_pct:.1%} unrealized gain. Consider closing."
        elif pnl_pct <= self.sl_threshold:
            alert_type = "SL"
            message = f"🛑 Stop Loss! {pnl_pct:.1%} loss. Consider cutting position."
        elif pnl_pct <= self.avg_down_threshold:
            alert_type = "AVG_DOWN"
            message = f"📉 Average Down? {pnl_pct:.1%} drawdown. Review for potential add."

        if not alert_type:
            return None

        return PositionAlert(
            market_id=market_id,
            question=question,
            alert_type=alert_type,
            side=side,
            size=size,
            avg_price=avg_price,
            current_price=current_price,
            unrealized_pnl=unrealized_pnl,
            message=message,
        )

    def check(
        self,
        portfolio: Dict,
        markets: Optional[List] = None,
    ) -> List[str]:
        """
        检查所有持仓，返回警报消息列表。
        
        Parameters
        ----------
        portfolio : Dict[str, Position]
            持仓字典，key 是 market_id
        markets : Optional[List[Market]]
            实时市场数据列表，用于更新当前价格
        
        Returns
        -------
        List[str]
            HTML 格式的警报消息列表
        """
        if not portfolio:
            return []

        # 建立市场 ID 到 Market 的映射
        market_map: Dict = {}
        if markets:
            for m in markets:
                mid = getattr(m, "market_id", None)
                if mid:
                    market_map[mid] = m

        alerts: List[str] = []

        for market_id, position in portfolio.items():
            current_market = market_map.get(market_id)
            alert = self._check_position(position, current_market)

            if alert:
                msg = self._format_alert_html(alert)
                alerts.append(msg)
                log.info(
                    "Position alert [%s]: %s — %s",
                    alert.alert_type,
                    alert.question[:40],
                    alert.message,
                )

        return alerts

    def _format_alert_html(self, alert: PositionAlert) -> str:
        """格式化警报为 HTML。"""
        emoji_map = {
            "TP": "🎯",
            "SL": "🛑",
            "AVG_DOWN": "📉",
            "BREAKOUT": "🚀",
        }
        emoji = emoji_map.get(alert.alert_type, "⚠️")
        pnl_sign = "+" if alert.unrealized_pnl >= 0 else ""

        return (
            f"<b>{emoji} Position Alert: {alert.alert_type}</b>\n"
            f"<b>Market:</b> {alert.question}\n"
            f"<b>ID:</b> <code>{alert.market_id}</code>\n"
            f"<b>Side:</b> {alert.side}\n"
            f"<b>Size:</b> {alert.size:.2f} shares\n"
            f"<b>Entry:</b> {alert.avg_price:.4f}\n"
            f"<b>Current:</b> {alert.current_price:.4f}\n"
            f"<b>P&L:</b> {pnl_sign}${alert.unrealized_pnl:.2f}\n"
            f"<b>Action:</b> {alert.message}"
        )
