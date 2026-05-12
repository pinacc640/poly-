"""Report formatter.

Converts a ScanReport into a human-readable console string covering:
  1. Stable Opportunities
  2. Volatility Opportunities
  3. Smart Money Opportunities (NEW)

Each approved opportunity now displays:
  - Suggested position (USD)
  - Take Profit limit price  (TP = entry + gap * 0.80)
  - Expected profit
  - Risk level / confidence
  - Trade direction (YES / NO)

Rejected candidates are shown in a compact audit trail at the bottom.
"""

from .models import (
    RiskDecision,
    SmartMoneyOpportunity,
    StableOpportunity,
    VolatilityOpportunity,
)
from .scanner import ScanReport

_NO_TRADE_MSG = "当前无符合纪律的交易机会。"
_DIVIDER      = "─" * 65
_THIN_DIV     = "·" * 65


# ---------------------------------------------------------------------------
# Per-opportunity formatters
# ---------------------------------------------------------------------------

def _tp_line(opp) -> str:
    """Format the Take Profit line, shown for every approved opportunity."""
    tp = getattr(opp, "take_profit_price", 0.0)
    if tp <= 0:
        return "    🎯 Take Profit:      (not set)"
    entry = opp.market.price
    gap   = abs(tp - entry)
    pct   = gap / entry * 100 if entry > 0 else 0
    return (
        f"    🎯 Take Profit:      {tp:.4f}  "
        f"(entry {entry:.4f}  →  +{pct:.1f}%  |  80% of AI edge captured)"
    )


def _fmt_stable(opp: StableOpportunity, decision: RiskDecision) -> str:
    pos    = decision.approved_position or opp.suggested_position
    scale  = pos / opp.suggested_position if opp.suggested_position else 1
    profit = opp.expected_profit * scale
    notes  = " | ".join(opp.rationale)
    return (
        f"  - Market:            {opp.market.question}\n"
        f"    ID:                {opp.market.market_id}  |  Category: {opp.market.category}\n"
        f"    Score:             {opp.score}  |  Risk: {opp.risk_level}\n"
        f"    Direction:         {opp.side}\n"
        f"    Entry price:       {opp.market.price:.4f}  |  AI true_prob: {opp.market.true_prob:.4f}\n"
        f"    EV (per $1):       {opp.ev:+.4f}\n"
        f"    Suggested Position: ${pos:.2f}  |  Expected Profit: ${profit:.2f}\n"
        f"{_tp_line(opp)}\n"
        f"    Rationale:         {notes}"
    )


def _fmt_volatility(opp: VolatilityOpportunity, decision: RiskDecision) -> str:
    pos    = decision.approved_position or opp.suggested_position
    scale  = pos / opp.suggested_position if opp.suggested_position else 1
    profit = opp.expected_profit * scale
    notes  = " | ".join(opp.rationale)
    return (
        f"  - Market:            {opp.market.question}\n"
        f"    ID:                {opp.market.market_id}  |  Category: {opp.market.category}\n"
        f"    Direction:         {opp.side}\n"
        f"    Entry:             {opp.entry_price:.4f}  |  Stop: {opp.stop_loss:.4f}\n"
        f"    Max Hold:          {opp.max_hold_days} days\n"
        f"    EV (per $1):       {opp.ev:+.4f}\n"
        f"    Suggested Position: ${pos:.2f}  |  Expected Profit: ${profit:.2f}\n"
        f"{_tp_line(opp)}\n"
        f"    Rationale:         {notes}"
    )


def _fmt_smart_money(opp: SmartMoneyOpportunity, decision: RiskDecision) -> str:
    pos    = decision.approved_position or opp.suggested_position
    scale  = pos / opp.suggested_position if opp.suggested_position else 1
    profit = opp.expected_profit * scale
    notes  = " | ".join(opp.rationale)
    badge  = {"HIGH": "🔴 HIGH", "MEDIUM": "🟡 MEDIUM", "LOW": "⚪ LOW"}.get(
        opp.confidence, opp.confidence
    )
    breakout_tag = "✅ breakout confirmed" if opp.is_breakout else "⚠ no breakout"
    return (
        f"  - Market:            {opp.market.question}\n"
        f"    ID:                {opp.market.market_id}  |  Category: {opp.market.category}\n"
        f"    Confidence:        {badge}  |  Flow: {opp.flow_direction}  |  {breakout_tag}\n"
        f"    Direction:         {opp.side}\n"
        f"    Vol/Liq:           {opp.volume_spike_ratio:.2f}  "
        f"|  Price Impact Ratio: {opp.price_impact_ratio:.3f}  "
        f"|  Δ24h: {opp.price_move_pct:+.2%}\n"
        f"    Entry price:       {opp.market.price:.4f}  |  AI true_prob: {opp.market.true_prob:.4f}\n"
        f"    EV (per $1):       {opp.ev:+.4f}\n"
        f"    Suggested Position: ${pos:.2f}  |  Expected Profit: ${profit:.2f}\n"
        f"{_tp_line(opp)}\n"
        f"    Signals:           {notes}"
    )


def _fmt_rejected(opp, decision: RiskDecision) -> str:
    name    = opp.market.question[:55] + ("…" if len(opp.market.question) > 55 else "")
    reasons = "; ".join(decision.reasons)
    return f"    [{opp.market.market_id}] {name}\n      ↳ REJECTED: {reasons}"


# ---------------------------------------------------------------------------
# Main report builder
# ---------------------------------------------------------------------------

def format_report(report: ScanReport) -> str:
    lines = []
    cfg   = report.config
    capital_str = f"${cfg.total_capital:.0f}" if cfg else "N/A"

    total_rej = (
        len(report.stable_rejected)
        + len(report.volatility_rejected)
        + len(report.smart_money_rejected)
    )
    ai_tag = "  [AI✓]" if report.ai_oracle_used else ""

    # ── Header ──────────────────────────────────────────────────────
    lines += [
        _DIVIDER,
        "  POLYMARKET MARKET SCANNER  —  SCAN REPORT",
        _DIVIDER,
        f"  Markets scanned  : {report.total_markets_scanned}{ai_tag}",
        f"  Account capital  : {capital_str}",
        f"  TP capture ratio : {cfg.tp_capture_ratio:.0%}" if cfg else "",
        f"  Stable approved  : {len(report.stable_approved)}"
        f"   |  Vol: {len(report.volatility_approved)}"
        f"   |  Smart$: {len(report.smart_money_approved)}"
        f"   |  Rejected: {total_rej}",
        _DIVIDER,
        "",
    ]

    # ── Stable ──────────────────────────────────────────────────────
    lines.append("📊 Stable Opportunities (convergence plays):")
    if report.stable_approved:
        for opp, dec in report.stable_approved:
            lines += [_fmt_stable(opp, dec), ""]
    else:
        lines += [f"  {_NO_TRADE_MSG}", ""]

    # ── Volatility ───────────────────────────────────────────────────
    lines.append("⚡ Volatility Opportunities (bracket trades):")
    if report.volatility_approved:
        for opp, dec in report.volatility_approved:
            lines += [_fmt_volatility(opp, dec), ""]
    else:
        lines += [f"  {_NO_TRADE_MSG}", ""]

    # ── Smart Money ──────────────────────────────────────────────────
    lines.append("🐋 Smart Money Opportunities (whale flow + price-impact filter):")
    if report.smart_money_approved:
        for opp, dec in report.smart_money_approved:
            lines += [_fmt_smart_money(opp, dec), ""]
    else:
        lines += [f"  {_NO_TRADE_MSG}", ""]

    # ── Audit trail ──────────────────────────────────────────────────
    if total_rej:
        lines += [_DIVIDER, f"  Rejected candidates ({total_rej} total):"]
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
