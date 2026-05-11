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

from .models import StableOpportunity, VolatilityOpportunity, SmartMoneyOpportunity, RiskDecision
from .scanner import ScanReport

_NO_TRADE_MSG = "当前无符合纪律的交易机会。"
_DIVIDER = "─" * 60


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


def _fmt_smart_money(opp: SmartMoneyOpportunity, decision: RiskDecision) -> str:
    pos = decision.approved_position or opp.suggested_position
    scale = pos / opp.suggested_position if opp.suggested_position else 1
    profit = opp.expected_profit * scale
    notes = " | ".join(opp.rationale)
    # Confidence badge
    badge = {"HIGH": "🔴 HIGH", "MEDIUM": "🟡 MEDIUM", "LOW": "⚪ LOW"}.get(opp.confidence, opp.confidence)
    return (
        f"  - Market:            {opp.market.question}\n"
        f"    ID:                {opp.market.market_id}  |  Category: {opp.market.category}\n"
        f"    Confidence:        {badge}\n"
        f"    Flow Direction:    {opp.flow_direction}  |  Vol/Liq Ratio: {opp.volume_spike_ratio:.2f}\n"
        f"    Price Move 24h:    {opp.price_move_pct:+.1%}\n"
        f"    EV (per $1):       {opp.ev:+.4f}\n"
        f"    Suggested Position: ${pos:.2f}\n"
        f"    Expected Profit:   ${profit:.2f}\n"
        f"    Signals:           {notes}"
    )


def format_report(report: ScanReport) -> str:
    lines = []

    # ── Header ──────────────────────────────────────────────────────
    cfg = report.config
    capital_str = f"${cfg.total_capital:.0f}" if cfg else "N/A"
    total_rej = (len(report.stable_rejected) + len(report.volatility_rejected)
                 + len(report.smart_money_rejected))
    lines += [
        _DIVIDER,
        "  POLYMARKET MARKET SCANNER  —  SCAN REPORT",
        _DIVIDER,
        f"  Markets scanned  : {report.total_markets_scanned}",
        f"  Account capital  : {capital_str}",
        f"  Stable approved  : {len(report.stable_approved)}"
        f"   |  Vol approved  : {len(report.volatility_approved)}"
        f"   |  SmartMoney    : {len(report.smart_money_approved)}"
        f"   |  Rejected      : {total_rej}",
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

    # ── Smart Money Opportunities ───────────────────────────────────
    lines.append("Smart Money Opportunities (alpha sleeve — follow informed flow):")
    if report.smart_money_approved:
        for opp, dec in report.smart_money_approved:
            lines.append(_fmt_smart_money(opp, dec))
            lines.append("")
    else:
        lines.append(f"  {_NO_TRADE_MSG}")
        lines.append("")

    # ── Audit trail: rejected candidates ───────────────────────────
    rejected_total = (len(report.stable_rejected) + len(report.volatility_rejected)
                      + len(report.smart_money_rejected))
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
        if report.smart_money_rejected:
            lines.append("  [Smart Money]")
            for opp, dec in report.smart_money_rejected:
                lines.append(_fmt_rejected(opp, dec))
        lines.append("")

    lines.append(_DIVIDER)
    return "\n".join(lines)
