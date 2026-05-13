"""Core data models.

`Market` is the raw, API-shaped record. Strategies turn markets into
typed opportunity proposals; the risk controller then either approves
or rejects each proposal.
"""

from dataclasses import dataclass, field
from typing import List, Literal, Optional


# ---------------------------------------------------------------------------
# Raw market record (the shape a future Polymarket API client would produce)
# ---------------------------------------------------------------------------
@dataclass
class Market:
    market_id: str
    question: str
    category: str                    # e.g. "politics", "sports", "crypto", "oil"
    price: float                     # current YES price, 0..1
    liquidity: float                 # USD depth
    volume_24h: float                # USD volume in last 24h
    volume_prev_24h: float           # USD volume 24-48h ago (for trend detection)
    price_change_24h: float          # signed delta in YES price over last 24h
    days_to_expiry: int
    true_prob: float                 # analyst / model estimate of fair prob
    has_political_shock: bool = False         # sudden headline risk
    has_fundamental_change: bool = False      # disqualifies vol arbitrage

    @property
    def volume_increasing(self) -> bool:
        """Crude proxy for rising interest."""
        return self.volume_24h > self.volume_prev_24h

    @property
    def is_macro(self) -> bool:
        # Matching is done against the config blocklist in the strategy,
        # but this convenience property is handy for tests/debugging.
        return self.category.lower() in {"oil", "gold", "war", "geopolitics"}


# ---------------------------------------------------------------------------
# Strategy output: opportunity proposals
# ---------------------------------------------------------------------------
@dataclass
class StableOpportunity:
    market: Market
    score: int
    ev: float                        # expected value per $1 at risk
    suggested_position: float        # USD
    expected_profit: float           # USD
    risk_level: Literal["Low", "Medium", "High"]
    rationale: List[str] = field(default_factory=list)


@dataclass
class VolatilityOpportunity:
    market: Market
    entry_price: float
    target_price: float
    stop_loss: float
    ev: float
    suggested_position: float        # USD
    expected_profit: float           # USD at target
    max_hold_days: int
    rationale: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Position — 账户当前持仓记录
# ---------------------------------------------------------------------------
@dataclass
class Position:
    market_id:      str            # conditionId / marketId，与 Market.market_id 对应
    token_id:       str            # ERC-1155 outcome token ID
    outcome:        str            # "YES" / "NO" 或其他 outcome label
    size:           float          # 持有份额数量
    avg_price:      float          # 平均买入价（0..1）
    current_price:  float          # 当前市价（0..1）
    cost_basis:     float          # 总成本 USD = size * avg_price
    market_value:   float          # 当前市值 USD = size * current_price
    unrealized_pnl: float          # 未实现盈亏 USD = market_value - cost_basis
    question:       str = ""       # 市场问题描述（可选，用于展示）


# ---------------------------------------------------------------------------
# Risk controller result
# ---------------------------------------------------------------------------
@dataclass
class RiskDecision:
    approved: bool
    reasons: List[str] = field(default_factory=list)   # why rejected (if any)
    approved_position: Optional[float] = None          # possibly downsized


# ---------------------------------------------------------------------------
# Phase 2 — Position Monitor models
# ---------------------------------------------------------------------------
@dataclass
class PositionSignal:
    """单条持仓的监控信号。由 PositionMonitor 生成。"""
    signal_type: Literal["TAKE_PROFIT", "STOP_LOSS", "ADD_POSITION", "WATCH", "HOLD"]
    position:    "Position"          # forward ref；运行时已定义
    market:      Market              # 对应的 Market 对象（含 AI 更新后的 true_prob）
    rationale:   List[str] = field(default_factory=list)
    urgency:     Literal["HIGH", "MEDIUM", "LOW"] = "LOW"

    # ── 便捷属性 ──────────────────────────────────────────────────
    @property
    def is_actionable(self) -> bool:
        """TAKE_PROFIT / STOP_LOSS / ADD_POSITION 均需立即关注。"""
        return self.signal_type in {"TAKE_PROFIT", "STOP_LOSS", "ADD_POSITION"}


@dataclass
class MonitorReport:
    """PositionMonitor.run() 的输出容器。"""
    signals:           List[PositionSignal] = field(default_factory=list)
    positions_checked: int = 0
    ai_enriched:       int = 0   # 实际调用 AI 的持仓数量

    # ── 便捷过滤 ──────────────────────────────────────────────────
    @property
    def actionable(self) -> List[PositionSignal]:
        return [s for s in self.signals if s.is_actionable]

    @property
    def take_profit_signals(self) -> List[PositionSignal]:
        return [s for s in self.signals if s.signal_type == "TAKE_PROFIT"]

    @property
    def stop_loss_signals(self) -> List[PositionSignal]:
        return [s for s in self.signals if s.signal_type == "STOP_LOSS"]

    @property
    def add_position_signals(self) -> List[PositionSignal]:
        return [s for s in self.signals if s.signal_type == "ADD_POSITION"]


# ---------------------------------------------------------------------------
# Phase 2 — Arbitrage Scanner models
# ---------------------------------------------------------------------------
@dataclass
class KalshiMarket:
    """Kalshi 单市场快照。由 KalshiFetcher 生成。"""
    ticker:       str
    question:     str
    yes_price:    float          # 0..1
    no_price:     float          # 0..1  (通常 ≈ 1 - yes_price，但 spread 存在)
    volume_usd:   float = 0.0
    open_interest: float = 0.0
    category:     str = "general"
    close_time:   Optional[str] = None   # ISO 8601 字符串


@dataclass
class ArbitrageOpportunity:
    """单条套利机会。pm_yes + kalshi_no < 1 → 无风险利润空间。"""
    pm_market:            Market
    kalshi_market:        KalshiMarket
    pm_side:              Literal["YES", "NO"]     # 在 PM 买哪边
    kalshi_side:          Literal["YES", "NO"]     # 在 Kalshi 买哪边
    pm_price:             float                    # PM 那边的实际买入价
    kalshi_price:         float                    # Kalshi 那边的实际买入价
    arb_gap:              float                    # 1 - pm_price - kalshi_price（越大越好）
    expected_profit_pct:  float                    # arb_gap / total_cost
    match_confidence:     float                    # 0..1，关键词+AI 综合匹配置信度
    match_method:         Literal["keyword", "ai_verified", "ai_uncertain"]
    rationale:            List[str] = field(default_factory=list)

    @property
    def recommended_action(self) -> str:
        return (
            f"Buy PM {self.pm_side} @ {self.pm_price:.3f}  +  "
            f"Buy Kalshi {self.kalshi_side} @ {self.kalshi_price:.3f}"
        )


@dataclass
class ArbitrageReport:
    """ArbitrageScanner.scan() 的输出容器。"""
    opportunities:      List[ArbitrageOpportunity] = field(default_factory=list)
    pm_markets_checked: int = 0
    kalshi_markets_fetched: int = 0
    candidate_pairs:    int = 0    # Jaccard 初筛后的候选对数量
    ai_verified_pairs:  int = 0    # DeepSeek 验证的对数量

    @property
    def best(self) -> Optional[ArbitrageOpportunity]:
        if not self.opportunities:
            return None
        return max(self.opportunities, key=lambda o: o.arb_gap)
