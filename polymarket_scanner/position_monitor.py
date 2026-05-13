"""position_monitor.py — 持仓动态监控模块 (Phase 2)

对已持仓市场进行 AI 增强评估，根据最新胜率和当前价格生成
止盈 / 止损 / 加仓 / 观察 信号。

信号触发逻辑（按优先级排列）
─────────────────────────────
TAKE_PROFIT（止盈，优先级最高）
  条件 A：current_price >= monitor_tp_abs_price（默认 0.90）
          价格已进入极高概率区间，边际收益极低，落袋为安
  条件 B：current_price >= avg_price + monitor_tp_delta（默认 +0.20）
          相对均价已盈利 20 个价格点
  条件 C：days_to_expiry <= monitor_tp_expiry_days（默认 2 天）且 unrealized_pnl > 0
          临近结算且浮盈，不值得继续持有风险

STOP_LOSS（止损 / AI 胜率反转，优先级第二）
  若持 YES 仓：true_prob < avg_price - monitor_sl_ai_reversal（默认 -0.15）
  若持 NO  仓：(1 - true_prob) < avg_price - monitor_sl_ai_reversal
  含义：AI 认为基本面已显著逆转，建议出清

ADD_POSITION（加仓，全部满足才触发）
  A. current_price <= avg_price * monitor_add_discount（默认 0.85，即回落 ≥ 15%）
  B. AI 仍看好（方向正确且 edge >= monitor_add_ai_edge，默认 +0.10）
  C. days_to_expiry >= monitor_add_min_days（默认 3 天）
  D. liquidity >= monitor_add_min_liquidity（默认 $50 000）

WATCH（观察）：价格轻微偏离但尚未达到任何阈值
HOLD（持仓无需操作）：一切正常

用法
────
    from polymarket_scanner.position_monitor import PositionMonitor
    from polymarket_scanner.positions import PositionFetcher

    positions = PositionFetcher().fetch_positions()
    monitor   = PositionMonitor(cfg)
    report    = monitor.run(positions, all_markets, oracle)
    print(format_monitor_report(report))
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

from .config import AccountConfig, DEFAULT_CONFIG
from .models import Market, MonitorReport, Position, PositionSignal

logger = logging.getLogger(__name__)


class PositionMonitor:
    """对已持仓市场执行 AI 增强并生成操作信号。

    Parameters
    ----------
    cfg :
        策略配置，止盈/加仓/止损阈值均来自此处。默认 DEFAULT_CONFIG。
    """

    def __init__(self, cfg: AccountConfig = DEFAULT_CONFIG):
        self.cfg = cfg

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    def run(
        self,
        positions: List[Position],
        all_markets: List[Market],
        oracle: Optional[object] = None,   # AIOracle | None
    ) -> MonitorReport:
        """扫描所有持仓，返回 MonitorReport（含每条持仓的操作信号）。

        Parameters
        ----------
        positions :
            PositionFetcher.fetch_positions() 返回的持仓列表。
        all_markets :
            本轮已拉取的全量 Market 列表（用于查询最新价格/流动性）。
            若找不到对应 market，则跳过该持仓的 AI 增强，直接用持仓快照数据。
        oracle :
            AIOracle 实例。若为 None，则跳过 AI 增强，直接用市场原始价格
            作为 true_prob。
        """
        if not positions:
            logger.info("PositionMonitor: 无持仓，跳过监控。")
            return MonitorReport()

        # ── 建立 market_id → Market 的快速查找表 ──────────────────────
        market_index: Dict[str, Market] = {m.market_id: m for m in all_markets}

        # ── 找到持仓对应的 Market 对象 ────────────────────────────────
        held_markets: List[Market] = []
        pos_market_pairs: List[tuple[Position, Optional[Market]]] = []

        for pos in positions:
            mkt = market_index.get(pos.market_id)
            pos_market_pairs.append((pos, mkt))
            if mkt is not None:
                held_markets.append(mkt)

        # ── AI 增强（对已持仓市场，同样应用 Top-N 截断）──────────────
        ai_enriched_count = 0
        if oracle is not None and held_markets:
            logger.info(
                "PositionMonitor: 对 %d 个持仓市场执行 AI 增强（Top-%d 截断）…",
                len(held_markets),
                self.cfg.monitor_ai_top_n,
            )
            enriched = oracle.enrich_all(  # type: ignore[union-attr]
                held_markets,
                ai_top_n=self.cfg.monitor_ai_top_n,
            )
            # 用 AI 更新后的 Market 替换 index
            enriched_index: Dict[str, Market] = {m.market_id: m for m in enriched}
            market_index.update(enriched_index)
            ai_enriched_count = len(enriched)
        else:
            if oracle is None:
                logger.info("PositionMonitor: 未传入 oracle，使用原始市价作为 true_prob。")

        # ── 逐条持仓生成信号 ──────────────────────────────────────────
        signals: List[PositionSignal] = []
        for pos, raw_mkt in pos_market_pairs:
            # 优先用 AI 更新后的 Market；若找不到则用占位 Market
            mkt = market_index.get(pos.market_id, raw_mkt)
            if mkt is None:
                # 完全找不到市场数据，只能用持仓快照构造一个占位 Market
                mkt = _position_to_stub_market(pos)
                logger.debug(
                    "PositionMonitor: 市场 %s 不在当前 all_markets 中，使用持仓快照。",
                    pos.market_id,
                )

            signal = self._evaluate(pos, mkt)
            signals.append(signal)
            logger.info(
                "持仓监控 [%s] → %s (%s)  |  %s",
                pos.market_id[:16] + "…",
                signal.signal_type,
                signal.urgency,
                " | ".join(signal.rationale[:2]),
            )

        return MonitorReport(
            signals           = signals,
            positions_checked = len(positions),
            ai_enriched       = ai_enriched_count,
        )

    # ------------------------------------------------------------------
    # 信号判断核心逻辑
    # ------------------------------------------------------------------

    def _evaluate(self, pos: Position, mkt: Market) -> PositionSignal:
        """对单条持仓评估，返回 PositionSignal。"""
        cfg      = self.cfg
        outcome  = pos.outcome.upper()          # "YES" or "NO"
        is_yes   = outcome in {"YES", "YES "}

        cur_price  = mkt.price                  # 最新市场价
        true_prob  = mkt.true_prob              # AI 估计胜率
        avg_price  = pos.avg_price
        days_left  = mkt.days_to_expiry
        liquidity  = mkt.liquidity
        pnl        = pos.unrealized_pnl

        rationale: list[str] = []
        rationale.append(
            f"均价={avg_price:.3f}  现价={cur_price:.3f}  "
            f"AI胜率={true_prob:.3f}  剩余{days_left}天"
        )

        # ── 1. 止盈检查 ───────────────────────────────────────────────
        tp_reasons: list[str] = []

        if cur_price >= cfg.monitor_tp_abs_price:
            tp_reasons.append(
                f"价格 {cur_price:.3f} ≥ 绝对高位 {cfg.monitor_tp_abs_price:.2f}"
            )
        if cur_price >= avg_price + cfg.monitor_tp_delta:
            tp_reasons.append(
                f"盈利 {cur_price - avg_price:+.3f} ≥ 阈值 +{cfg.monitor_tp_delta:.2f}"
            )
        if days_left <= cfg.monitor_tp_expiry_days and pnl > 0:
            tp_reasons.append(
                f"临近到期（{days_left}天）且浮盈 ${pnl:.2f}"
            )

        if tp_reasons:
            urgency = "HIGH" if cur_price >= cfg.monitor_tp_abs_price else "MEDIUM"
            return PositionSignal(
                signal_type = "TAKE_PROFIT",
                position    = pos,
                market      = mkt,
                rationale   = rationale + tp_reasons,
                urgency     = urgency,
            )

        # ── 2. 止损 / AI 反转检查 ─────────────────────────────────────
        # 判断方向：YES 仓看 true_prob；NO 仓看 1 - true_prob
        effective_prob = true_prob if is_yes else (1.0 - true_prob)
        sl_threshold   = avg_price - cfg.monitor_sl_ai_reversal

        if effective_prob < sl_threshold:
            sl_reason = (
                f"AI {'胜率' if is_yes else '败率'} {effective_prob:.3f} < "
                f"均价 {avg_price:.3f} - {cfg.monitor_sl_ai_reversal:.2f} "
                f"= {sl_threshold:.3f}，基本面可能反转"
            )
            return PositionSignal(
                signal_type = "STOP_LOSS",
                position    = pos,
                market      = mkt,
                rationale   = rationale + [sl_reason],
                urgency     = "HIGH",
            )

        # ── 3. 加仓检查（全部条件须同时满足）─────────────────────────
        add_checks: list[tuple[bool, str]] = [
            (
                cur_price <= avg_price * cfg.monitor_add_discount,
                f"现价 {cur_price:.3f} ≤ 均价 {avg_price:.3f} × {cfg.monitor_add_discount:.2f} "
                f"= {avg_price * cfg.monitor_add_discount:.3f}（已回落 ≥ 15%）",
            ),
            (
                effective_prob >= avg_price + cfg.monitor_add_ai_edge,
                f"AI方向概率 {effective_prob:.3f} ≥ 均价+edge "
                f"{avg_price + cfg.monitor_add_ai_edge:.3f}（AI仍看好）",
            ),
            (
                days_left >= cfg.monitor_add_min_days,
                f"剩余 {days_left} 天 ≥ 最低 {cfg.monitor_add_min_days} 天",
            ),
            (
                liquidity >= cfg.monitor_add_min_liquidity,
                f"流动性 ${liquidity:,.0f} ≥ ${cfg.monitor_add_min_liquidity:,.0f}",
            ),
        ]

        all_pass  = all(ok for ok, _ in add_checks)
        add_notes = [note for ok, note in add_checks if ok]
        fail_notes = [note for ok, note in add_checks if not ok]

        if all_pass:
            return PositionSignal(
                signal_type = "ADD_POSITION",
                position    = pos,
                market      = mkt,
                rationale   = rationale + add_notes,
                urgency     = "MEDIUM",
            )

        # ── 4. 观察（部分加仓条件满足但未全部达标）───────────────────
        n_pass = sum(ok for ok, _ in add_checks)
        if n_pass >= 2:
            watch_note = f"已满足 {n_pass}/4 个加仓条件，建议持续观察"
            return PositionSignal(
                signal_type = "WATCH",
                position    = pos,
                market      = mkt,
                rationale   = rationale + [watch_note] + fail_notes,
                urgency     = "LOW",
            )

        # ── 5. 默认 HOLD ──────────────────────────────────────────────
        return PositionSignal(
            signal_type = "HOLD",
            position    = pos,
            market      = mkt,
            rationale   = rationale + ["无明显操作信号，继续持有。"],
            urgency     = "LOW",
        )


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------

def _position_to_stub_market(pos: Position) -> Market:
    """当全局 market_index 中找不到持仓对应的市场时，
    用持仓快照数据构造一个最小化的占位 Market 对象。
    true_prob 设为 current_price（没有 AI 校正）。
    """
    from .models import Market
    return Market(
        market_id        = pos.market_id,
        question         = pos.question or pos.market_id,
        category         = "unknown",
        price            = pos.current_price,
        liquidity        = 0.0,
        volume_24h       = 0.0,
        volume_prev_24h  = 0.0,
        price_change_24h = 0.0,
        days_to_expiry   = 30,
        true_prob        = pos.current_price,
    )
