"""Report formatter.

Converts a ScanReport into a human-readable console string.
The output format matches the spec:

    Stable Opportunities:
    - Market: ...
      Score: ...
      EV: ...
      Suggested Position: ...
      Expected Profit: ...
      Risk Level: ...

    Volatility Opportunities:
    - Market: ...
      Entry: ...
      Target: ...
      Stop Loss: ...
      EV: ...
      Suggested Position: ...

If a sleeve has no approved trades, prints the Chinese fallback line.
Rejected candidates are shown as a compact audit trail at the bottom.
"""

from .models import StableOpportunity, VolatilityOpportunity, RiskDecision
from .scanner import ScanReport

_NO_TRADE_MSG    = "当前无符合纪律的交易机会。"
_NO_SM_MSG       = "当前无符合纪律的巨鲸异动。"
_DIVIDER         = "─" * 60


def _fmt_stable(opp: StableOpportunity, decision: RiskDecision) -> str:
    pos = decision.approved_position or opp.suggested_position
    # Re-scale expected profit to the approved position size
    scale = pos / opp.suggested_position if opp.suggested_position else 1
    profit = opp.expected_profit * scale
    notes = " | ".join(opp.rationale)
    return (
        f"  - Market:            {opp.market.question}\n"
        f"    ID:                {opp.market.market_id}  |  Category: {opp.market.category}\n"
        f"    Score:             {opp.score}\n"
        f"    EV (per $1):       {opp.ev:+.4f}\n"
        f"    Suggested Position: ${pos:.2f}\n"
        f"    Expected Profit:   ${profit:.2f}\n"
        f"    Risk Level:        {opp.risk_level}\n"
        f"    Rationale:         {notes}"
    )


def _fmt_volatility(opp: VolatilityOpportunity, decision: RiskDecision) -> str:
    pos = decision.approved_position or opp.suggested_position
    scale = pos / opp.suggested_position if opp.suggested_position else 1
    profit = opp.expected_profit * scale
    notes = " | ".join(opp.rationale)
    return (
        f"  - Market:            {opp.market.question}\n"
        f"    ID:                {opp.market.market_id}  |  Category: {opp.market.category}\n"
        f"    Entry:             {opp.entry_price:.3f}\n"
        f"    Target:            {opp.target_price:.3f}  (+{opp.target_price - opp.entry_price:+.3f})\n"
        f"    Stop Loss:         {opp.stop_loss:.3f}  ({opp.stop_loss - opp.entry_price:+.3f})\n"
        f"    Max Hold:          {opp.max_hold_days} days\n"
        f"    EV (per $1):       {opp.ev:+.4f}\n"
        f"    Suggested Position: ${pos:.2f}\n"
        f"    Expected Profit:   ${profit:.2f}\n"
        f"    Rationale:         {notes}"
    )


def _fmt_rejected(opp, decision: RiskDecision) -> str:
    name = opp.market.question[:55] + ("…" if len(opp.market.question) > 55 else "")
    reasons = "; ".join(decision.reasons)
    return f"    [{opp.market.market_id}] {name}\n      ↳ REJECTED: {reasons}"


def format_report(report: ScanReport) -> str:
    lines = []

    # ── Header ──────────────────────────────────────────────────────
    cfg = report.config
    capital_str = f"${cfg.total_capital:.0f}" if cfg else "N/A"
    sm  = report.smart_money
    lines += [
        _DIVIDER,
        "  POLYMARKET MARKET SCANNER  —  SCAN REPORT",
        _DIVIDER,
        f"  Markets scanned : {report.total_markets_scanned}",
        f"  Account capital : {capital_str}",
        f"  Stable approved : {len(report.stable_approved)}   "
        f"Vol approved : {len(report.volatility_approved)}   "
        f"Smart Money alerts : {len(sm.whale_alerts)}",
        _DIVIDER,
        "",
    ]

    # ── Stable Opportunities ────────────────────────────────────────
    lines.append("Stable Opportunities (80% sleeve — convergence plays):")
    if report.stable_approved:
        for opp, dec in report.stable_approved:
            lines.append(_fmt_stable(opp, dec))
            lines.append("")
    else:
        lines.append(f"  {_NO_TRADE_MSG}")
        lines.append("")

    # ── Volatility Opportunities ────────────────────────────────────
    lines.append("Volatility Opportunities (20% sleeve — bracket trades):")
    if report.volatility_approved:
        for opp, dec in report.volatility_approved:
            lines.append(_fmt_volatility(opp, dec))
            lines.append("")
    else:
        lines.append(f"  {_NO_TRADE_MSG}")
        lines.append("")

    # ── Smart Money (observation layer) ────────────────────────────
    lines.append("Smart Money (观察层 — 巨鲸异动，非开仓信号):")
    lines.append(format_smart_money_section(sm))
    lines.append("")

    # ── Audit trail: rejected candidates ───────────────────────────
    rejected_total = len(report.stable_rejected) + len(report.volatility_rejected)
    if rejected_total:
        lines += [_DIVIDER, f"  Rejected candidates ({rejected_total} total):"]
        if report.stable_rejected:
            lines.append("  [Stable]")
            for opp, dec in report.stable_rejected:
                lines.append(_fmt_rejected(opp, dec))
        if report.volatility_rejected:
            lines.append("  [Volatility]")
            for opp, dec in report.volatility_rejected:
                lines.append(_fmt_rejected(opp, dec))
        lines.append("")

    lines.append(_DIVIDER)
    return "\n".join(lines)



# =============================================================================
# Phase 2 — Position Monitor formatter
# =============================================================================

from .models import MonitorReport, PositionSignal   # noqa: E402 (append context)

_SIGNAL_EMOJI = {
    "TAKE_PROFIT":  "💰",
    "STOP_LOSS":    "🛑",
    "ADD_POSITION": "➕",
    "WATCH":        "👀",
    "HOLD":         "🔒",
}
_URGENCY_LABEL = {"HIGH": "⚡ 紧急", "MEDIUM": "⚠ 关注", "LOW": "ℹ 信息"}


def _fmt_signal(sig: PositionSignal) -> str:
    pos  = sig.position
    mkt  = sig.market
    emoji = _SIGNAL_EMOJI.get(sig.signal_type, "•")
    urg   = _URGENCY_LABEL.get(sig.urgency, sig.urgency)

    pnl_sign = "+" if pos.unrealized_pnl >= 0 else ""
    lines = [
        f"  {emoji} [{sig.signal_type}]  {urg}",
        f"     市场: {mkt.question[:70]}",
        f"     ID:   {pos.market_id}",
        f"     方向: {pos.outcome}  份额: {pos.size:.2f}  均价: {pos.avg_price:.3f}",
        f"     现价: {mkt.price:.3f}  AI胜率: {mkt.true_prob:.3f}  "
        f"浮盈: ${pnl_sign}{pos.unrealized_pnl:.2f}",
    ]
    # 只显示前 3 条 rationale（第一条是摘要行，后面是原因）
    for note in sig.rationale[1:4]:
        lines.append(f"     → {note}")
    return "\n".join(lines)


def format_monitor_report(report: MonitorReport) -> str:
    """将 MonitorReport 格式化为可读的控制台字符串。"""
    lines: list[str] = []
    W = 60

    lines += [
        "─" * W,
        "  📊  持仓动态监控报告  (Phase 2)",
        "─" * W,
        f"  检查持仓: {report.positions_checked} 个  |  "
        f"AI 增强: {report.ai_enriched} 个",
        "",
    ]

    actionable = report.actionable
    if actionable:
        lines.append(f"  ⚡ 需要操作的信号（共 {len(actionable)} 条）：")
        lines.append("")
        for sig in sorted(actionable, key=lambda s: ("HIGH", "MEDIUM", "LOW").index(s.urgency)):
            lines.append(_fmt_signal(sig))
            lines.append("")
    else:
        lines.append("  ✅ 所有持仓状态正常，无需操作。")
        lines.append("")

    # 非操作性信号（WATCH / HOLD）折叠展示
    passive = [s for s in report.signals if not s.is_actionable]
    if passive:
        lines.append(f"  观察 / 持有（{len(passive)} 条）：")
        for sig in passive:
            emoji = _SIGNAL_EMOJI.get(sig.signal_type, "•")
            pos   = sig.position
            mkt   = sig.market
            pnl_s = f"+{pos.unrealized_pnl:.2f}" if pos.unrealized_pnl >= 0 \
                    else f"{pos.unrealized_pnl:.2f}"
            lines.append(
                f"    {emoji} {sig.signal_type:<12} "
                f"{mkt.question[:45]:<47} "
                f"AI={mkt.true_prob:.2f}  PnL=${pnl_s}"
            )
        lines.append("")

    lines.append("─" * W)
    return "\n".join(lines)


# =============================================================================
# Phase 2 — Arbitrage Scanner formatter
# =============================================================================

from .models import ArbitrageReport, ArbitrageOpportunity   # noqa: E402


def _fmt_arb_opp(opp: ArbitrageOpportunity, rank: int) -> str:
    pm  = opp.pm_market
    kal = opp.kalshi_market
    confidence_bar = "█" * int(opp.match_confidence * 10) + "░" * (10 - int(opp.match_confidence * 10))

    lines = [
        f"  #{rank}  套利空间: {opp.arb_gap:+.4f}  "
        f"预期收益: {opp.expected_profit_pct:.1%}  "
        f"匹配置信度: {opp.match_confidence:.0%} [{confidence_bar}]  "
        f"方法: {opp.match_method}",
        f"     PM    [{opp.pm_side}]: {pm.question[:65]}",
        f"            价格={pm.price:.3f}  流动性=${pm.liquidity:,.0f}",
        f"     Kalshi[{opp.kalshi_side}]: {kal.question[:65]}",
        f"            价格={kal.yes_price:.3f}/{kal.no_price:.3f}  "
        f"OI=${kal.open_interest:,.0f}",
        f"     操作: {opp.recommended_action}",
    ]
    for note in opp.rationale[:3]:
        lines.append(f"     • {note}")
    return "\n".join(lines)


def format_arb_report(report: ArbitrageReport) -> str:
    """将 ArbitrageReport 格式化为可读的控制台字符串。"""
    lines: list[str] = []
    W = 60

    lines += [
        "─" * W,
        "  ⚡  Polymarket × Kalshi 跨平台套利报告  (Phase 2)",
        "─" * W,
        f"  PM 市场: {report.pm_markets_checked} 个  |  "
        f"Kalshi 市场: {report.kalshi_markets_fetched} 个  |  "
        f"候选对: {report.candidate_pairs} 个  |  "
        f"AI 验证: {report.ai_verified_pairs} 对",
        "",
    ]

    if report.opportunities:
        lines.append(f"  发现 {len(report.opportunities)} 个套利机会（按利润空间降序）：")
        lines.append("")
        for i, opp in enumerate(report.opportunities, 1):
            lines.append(_fmt_arb_opp(opp, i))
            lines.append("")
    else:
        lines.append("  当前无满足阈值的跨平台套利机会。")
        lines.append("")

    lines.append("─" * W)
    return "\n".join(lines)



# =============================================================================
# Smart Money — 巨鲸异动观察层格式化
# =============================================================================

from .models import SmartMoneyReport, SmartMoneySignal   # noqa: E402

_SM_SIGNAL_EMOJI = {"WHALE_ALERT": "🐋", "WATCH": "👁"}


def _fmt_smart_money_signal(sig: SmartMoneySignal) -> str:
    emoji = _SM_SIGNAL_EMOJI.get(sig.signal_type, "•")
    tag   = "⚡ 巨鲸预警" if sig.signal_type == "WHALE_ALERT" else "👁 观察"
    lines = [
        f"  {emoji} [{tag}]  {sig.market.question[:70]}",
        f"     市场 ID    : {sig.market.market_id}",
    ]
    for note in sig.rationale:
        lines.append(f"     {note}")
    return "\n".join(lines)


def format_smart_money_section(report: SmartMoneyReport) -> str:
    """在 ScanReport 内嵌输出 Smart Money 分区（单行或多行）。"""
    if not report.signals:
        return f"  {_NO_SM_MSG}"

    lines: list[str] = []
    if report.whale_alerts:
        lines.append(f"  🐋 WHALE ALERT（{len(report.whale_alerts)} 条）：")
        for sig in report.whale_alerts:
            lines.append(_fmt_smart_money_signal(sig))
            lines.append("")
    if report.watch_signals:
        lines.append(f"  👁 WATCH（{len(report.watch_signals)} 条）：")
        for sig in report.watch_signals:
            lines.append(_fmt_smart_money_signal(sig))
            lines.append("")
    return "\n".join(lines).rstrip()


def format_smart_money_report(report: SmartMoneyReport) -> str:
    """独立输出完整 Smart Money 报告（供 live_scanner Telegram 预警使用）。"""
    W = 60
    lines: list[str] = [
        "─" * W,
        "  🐋  Smart Money — 巨鲸异动观察层",
        "─" * W,
        f"  扫描市场: {report.markets_scanned} 个  |  "
        f"WHALE ALERT: {len(report.whale_alerts)} 条  |  "
        f"WATCH: {len(report.watch_signals)} 条",
        "",
    ]

    if not report.signals:
        lines.append(f"  {_NO_SM_MSG}")
    else:
        if report.whale_alerts:
            lines.append(f"  🐋 WHALE ALERT（共 {len(report.whale_alerts)} 条）：")
            lines.append("")
            for sig in report.whale_alerts:
                lines.append(_fmt_smart_money_signal(sig))
                lines.append("")
        if report.watch_signals:
            lines.append(f"  👁 WATCH（共 {len(report.watch_signals)} 条）：")
            lines.append("")
            for sig in report.watch_signals:
                lines.append(_fmt_smart_money_signal(sig))
                lines.append("")

    lines.append("─" * W)
    lines.append("  ⚠️  以上均为观察信号，非开仓建议，请结合基本面独立决策。")
    lines.append("─" * W)
    return "\n".join(lines)
