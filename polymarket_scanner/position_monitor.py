"""position_monitor.py — 持仓动态监控模块 (Phase 2)

对已持仓市场进行 AI 增强评估，根据最新胜率和当前价格生成
止盈 / 止损 / 加仓 / 观察 信号。

数据来源与方向对齐原则（核心设计）
──────────────────────────────────
① position.current_price  —— 价格判断唯一来源
    Polymarket Data API 直接返回该持仓方向的最新价格，已经是正确方向的数值：
    YES 仓 → current_price 是 YES 价；NO 仓 → current_price 是 NO 价。
    直接使用，绝对不做 1 - x 翻转。

② position.avg_price  —— 与 current_price 同一坐标系
    API 返回的买入均价，已经是持仓方向的价格，直接使用。

③ mkt.true_prob  —— AI 胜率，全局统一为 YES 方向
    仅 NO 仓需要翻转：position_prob = 1 - mkt.true_prob
    YES 仓直接使用：position_prob = mkt.true_prob

信号触发逻辑（按优先级排列）
─────────────────────────────
TAKE_PROFIT（止盈，优先级最高）
  条件 A：pos.current_price >= monitor_tp_abs_price（默认 0.90）
  条件 B：pos.current_price >= avg_price + monitor_tp_delta（默认 +0.20）
  条件 C：days_to_expiry <= monitor_tp_expiry_days（默认 2 天）且 unrealized_pnl > 0

STOP_LOSS（止损 / AI 胜率反转，优先级第二）
  position_prob < avg_price - monitor_sl_ai_reversal（默认 -0.15）
  含义：AI 对持仓方向的估计胜率，相对于买入均价下修超过 0.15

ADD_POSITION（加仓，全部满足才触发）
  A. pos.current_price <= avg_price * monitor_add_discount（默认 0.85，即回落 ≥ 15%）
  B. position_prob >= avg_price + monitor_add_ai_edge（默认 +0.10，AI 仍看好）
  C. days_to_expiry >= monitor_add_min_days（默认 3 天）
  D. liquidity >= monitor_add_min_liquidity（默认 $50 000）

WATCH（观察）：满足 ≥ 2 个加仓条件但未全部达标
HOLD（持仓无需操作）：一切正常
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
            即使 all_markets 中没有对应市场，也会用持仓快照构建占位 Market，
            然后仍然送 AI 增强——AI 增强不受 all_markets 是否匹配的影响。
        oracle :
            AIOracle 实例。若为 None，则跳过 AI 增强，直接用市场原始价格
            作为 true_prob。
        """
        if not positions:
            logger.info("PositionMonitor: 无持仓，跳过监控。")
            return MonitorReport()

        # ── 建立 market_id → Market 的快速查找表（来自全量市场列表）──
        global_index: Dict[str, Market] = {m.market_id: m for m in all_markets}

        # ── 为每条持仓准备 Market 对象（保证 100% 覆盖）─────────────
        # 优先从 all_markets 获取最新价格/流动性；找不到则用持仓快照构建。
        # 无论哪种来源，后续都会统一送 AI 增强，绝不因找不到就跳过。
        markets_for_positions: List[Market] = []
        for pos in positions:
            mkt = global_index.get(pos.market_id)
            if mkt is None:
                # all_markets 中没有该持仓对应的市场（例如 --no-scan 场景）
                # 用持仓快照构建占位对象，true_prob 暂用 current_price，
                # 随后会被 AI 增强覆盖
                mkt = _position_to_stub_market(pos)
                logger.debug(
                    "PositionMonitor: 市场 %s 不在 all_markets 中，"
                    "用持仓快照构建占位 Market，将送 AI 增强。",
                    pos.market_id,
                )
            markets_for_positions.append(mkt)

        # ── AI 增强（独立调用，不受 --no-scan 影响）──────────────────
        # 持仓监控的 AI 增强是独立的：即使 --no-scan 跳过了新机会 AI 分析，
        # 这里仍然必须对持仓市场单独调用 AI，否则 true_prob 无意义。
        ai_enriched_count = 0
        if oracle is not None:
            logger.info(
                "PositionMonitor: 对 %d 个持仓市场执行独立 AI 增强（Top-%d 截断）…",
                len(markets_for_positions),
                self.cfg.monitor_ai_top_n,
            )
            enriched = oracle.enrich_all(  # type: ignore[union-attr]
                markets_for_positions,
                ai_top_n=self.cfg.monitor_ai_top_n,
            )
            # 建立 AI 增强后的 index，覆盖占位对象
            enriched_index = {m.market_id: m for m in enriched}
            ai_enriched_count = len(enriched)
            logger.info(
                "PositionMonitor: AI 增强完成，%d 个持仓市场已更新 true_prob。",
                ai_enriched_count,
            )
        else:
            logger.info(
                "PositionMonitor: 未传入 oracle，将使用原始市价作为 true_prob（无 AI 修正）。"
            )
            enriched_index = {m.market_id: m for m in markets_for_positions}

        # ── 逐条持仓生成信号 ──────────────────────────────────────────
        signals: List[PositionSignal] = []
        for pos, base_mkt in zip(positions, markets_for_positions):
            # 优先使用 AI 增强后的 Market；AI 未处理时退回 base_mkt
            mkt = enriched_index.get(pos.market_id, base_mkt)

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
        """对单条持仓评估，返回 PositionSignal。

        数据来源说明
        ──────────────
        position.current_price
            API 直接返回的该持仓方向的最新价格，已经是正确方向的数值。
            YES 仓：current_price 就是 YES 的当前价
            NO  仓：current_price 就是 NO 的当前价（API 已换算好）
            → 直接使用，不做任何翻转。

        position.avg_price
            买入时该持仓方向的均价，与 current_price 同一坐标系。
            → 直接使用，不做任何翻转。

        mkt.true_prob
            AI Oracle 估计的 YES 方向概率（全局统一为 YES）。
            → YES 仓：直接使用（position_prob = mkt.true_prob）
            → NO  仓：需翻转（position_prob = 1 - mkt.true_prob）

        mkt.price
            Polymarket CLOB 报价的 YES 方向价格（全局统一为 YES）。
            → 本方法不直接用于信号判断；只用 pos.current_price 做价格判断。
              mkt.price 仅用于 market_index 查找和 AI enrich 传参。
        """
        cfg     = self.cfg
        outcome = pos.outcome.strip().upper()    # "YES" or "NO"
        is_yes  = outcome == "YES"

        # ── 价格：直接使用 API 返回的持仓方向当前价，不做翻转 ────────
        # pos.current_price 已经是该持仓方向（YES 或 NO）的真实市价
        position_price: float = pos.current_price

        # ── AI 胜率：mkt.true_prob 是 YES 方向，NO 仓需翻转 ─────────
        position_prob: float = mkt.true_prob if is_yes else (1.0 - mkt.true_prob)

        avg_price = pos.avg_price
        days_left = mkt.days_to_expiry
        liquidity = mkt.liquidity
        pnl       = pos.unrealized_pnl

        direction_label = "YES" if is_yes else "NO"

        # 摘要行：所有数值均为持仓方向视角
        rationale: list[str] = [
            f"方向={direction_label}  均价={avg_price:.3f}  "
            f"现价({direction_label})={position_price:.3f}  "
            f"AI胜率({direction_label})={position_prob:.3f}  "
            f"剩余{days_left}天"
        ]

        # ── 1. 止盈检查 ───────────────────────────────────────────────
        # 统一使用 position_price（已对齐持仓方向），与 avg_price 直接比较
        tp_reasons: list[str] = []

        if position_price >= cfg.monitor_tp_abs_price:
            tp_reasons.append(
                f"{direction_label}价格 {position_price:.3f} ≥ "
                f"绝对高位 {cfg.monitor_tp_abs_price:.2f}"
            )
        if position_price >= avg_price + cfg.monitor_tp_delta:
            tp_reasons.append(
                f"盈利 {position_price - avg_price:+.3f} ≥ "
                f"阈值 +{cfg.monitor_tp_delta:.2f}（均价 {avg_price:.3f} → 现价 {position_price:.3f}）"
            )
        if days_left <= cfg.monitor_tp_expiry_days and pnl > 0:
            tp_reasons.append(
                f"临近到期（{days_left}天）且浮盈 ${pnl:.2f}"
            )

        if tp_reasons:
            urgency = "HIGH" if position_price >= cfg.monitor_tp_abs_price else "MEDIUM"
            return PositionSignal(
                signal_type = "TAKE_PROFIT",
                position    = pos,
                market      = mkt,
                rationale   = rationale + tp_reasons,
                urgency     = urgency,
            )

        # ── 2. 止损 / AI 胜率反转检查 ────────────────────────────────
        # position_prob 已是持仓方向的 AI 胜率，直接与 avg_price 比较
        sl_threshold = avg_price - cfg.monitor_sl_ai_reversal

        if position_prob < sl_threshold:
            sl_reason = (
                f"AI {direction_label}胜率 {position_prob:.3f} < "
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
        # 条件 A：持仓方向的当前价格相对均价回落 ≥ 15%
        # 条件 B：持仓方向的 AI 胜率仍高于均价 + edge（AI 依旧看好）
        add_checks: list[tuple[bool, str]] = [
            (
                position_price <= avg_price * cfg.monitor_add_discount,
                f"{direction_label}现价 {position_price:.3f} ≤ "
                f"均价 {avg_price:.3f} × {cfg.monitor_add_discount:.2f} "
                f"= {avg_price * cfg.monitor_add_discount:.3f}（已回落 ≥ 15%）",
            ),
            (
                position_prob >= avg_price + cfg.monitor_add_ai_edge,
                f"AI {direction_label}胜率 {position_prob:.3f} ≥ "
                f"均价+edge {avg_price + cfg.monitor_add_ai_edge:.3f}（AI仍看好）",
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

        all_pass   = all(ok for ok, _ in add_checks)
        add_notes  = [note for ok, note in add_checks if ok]
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
