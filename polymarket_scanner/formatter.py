"""Report formatter — Phase 3 edition.

New in Phase 3
--------------
- Every approved opportunity shows a 🎯 Take Profit line
- Smart Money section with price-impact ratio and breakout tag
- TP capture ratio shown in report header
"""

from .models import (
    RiskDecision,
    SmartMoneyOpportunity,
    StableOpportunity,
    VolatilityOpportunity,
)
from .scanner import ScanReport

_NO_TRADE  = "当前无符合纪律的交易机会。"
_DIVIDER   = "─" * 65
_THIN_DIV  = "·" * 65


# ---------------------------------------------------------------------------
# Shared TP helper
# ---------------------------------------------------------------------------

def _tp_line(opp) -> str:
    tp = getattr(opp, "take_profit_price", 0.0)
    if not tp:
        return "    🎯 Take Profit:      (not set)"
    entry = opp.market.price
    pct   = (tp - entry) / entry * 100 if entry > 0 else 0
    return (
        f"    🎯 Take Profit:      {tp:.4f}  "
        f"(entry {entry:.4f} → +{pct:.1f}%  |  80% of edge captured)"
    )


# ---------------------------------------------------------------------------
# Per-opportunity formatters
# ---------------------------------------------------------------------------

def _fmt_stable(opp: StableOpportunity, dec: RiskDecision) -> str:
    pos    = dec.approved_position or opp.suggested_position
    scale  = pos / opp.suggested_position if opp.suggested_position else 1
    profit = opp.expected_profit * scale
    notes  = " | ".join(opp.rationale)
    return (
        f"  - {opp.market.question}\n"
        f"    ID: {opp.market.market_id}  |  Cat: {opp.market.category}"
        f"  |  Score: {opp.score}  |  Risk: {opp.risk_level}\n"
        f"    Direction:   BUY {opp.side}\n"
        f"    Entry:       {opp.market.price:.4f}"
        f"  |  AI Prob: {opp.market.true_prob:.4f}\n"
        f"    EV (/$1):    {opp.ev:+.4f}\n"
        f"    Bet:         ${pos:.2f}  |  Est. Profit: ${profit:.2f}\n"
        f"{_tp_line(opp)}\n"
        f"    Rationale:   {notes}"
    )


def _fmt_volatility(opp: VolatilityOpportunity, dec: RiskDecision) -> str:
    pos    = dec.approved_position or opp.suggested_position
    scale  = pos / opp.suggested_position if opp.suggested_position else 1
    profit = opp.expected_profit * scale
    notes  = " | ".join(opp.rationale)
    return (
        f"  - {opp.market.question}\n"
        f"    ID: {opp.market.market_id}  |  Cat: {opp.market.category}\n"
        f"    Direction:   BUY {opp.side}\n"
        f"    Entry:       {opp.entry_price:.4f}"
        f"  |  Stop: {opp.stop_loss:.4f}"
        f"  |  Hold: {opp.max_hold_days}d\n"
        f"    EV (/$1):    {opp.ev:+.4f}\n"
        f"    Bet:         ${pos:.2f}  |  Est. Profit: ${profit:.2f}\n"
        f"{_tp_line(opp)}\n"
        f"    Rationale:   {notes}"
    )


def _fmt_smart_money(opp: SmartMoneyOpportunity, dec: RiskDecision) -> str:
    pos       = dec.approved_position or opp.suggested_position
    scale     = pos / opp.suggested_position if opp.suggested_position else 1
    profit    = opp.expected_profit * scale
    notes     = " | ".join(opp.rationale)
    badge     = "🔴 HIGH" if opp.confidence == "HIGH" else "🟡 MEDIUM"
    breakout  = "✅ breakout" if opp.is_breakout else "⚠ no breakout"
    return (
        f"  - {opp.market.question}\n"
        f"    ID: {opp.market.market_id}  |  Cat: {opp.market.category}\n"
        f"    Confidence:  {badge}  |  Flow: {opp.flow_direction}"
        f"  |  {breakout}\n"
        f"    Direction:   BUY {opp.side}\n"
        f"    Entry:       {opp.market.price:.4f}"
        f"  |  AI Prob: {opp.market.true_prob:.4f}\n"
        f"    Vol/Liq:     {opp.volume_spike_ratio:.2f}"
        f"  |  Price Impact: {opp.price_impact_ratio:.3f}"
        f"  |  Δ24h: {opp.price_move_pct:+.2%}\n"
        f"    EV (/$1):    {opp.ev:+.4f}\n"
        f"    Bet:         ${pos:.2f}  |  Est. Profit: ${profit:.2f}\n"
        f"{_tp_line(opp)}\n"
        f"    Signals:     {notes}"
    )


def _fmt_rejected(opp, dec: RiskDecision) -> str:
    name    = opp.market.question[:55] + ("…" if len(opp.market.question) > 55 else "")
    reasons = "; ".join(dec.reasons)
    return f"    [{opp.market.market_id}] {name}\n      ↳ REJECTED: {reasons}"


# ---------------------------------------------------------------------------
# Main report builder
# ---------------------------------------------------------------------------

def format_report(report: ScanReport) -> str:
    lines   = []
    cfg     = report.config
    cap_str = f"${cfg.total_capital:.0f}" if cfg else "N/A"
    tp_str  = f"{cfg.tp_capture_ratio:.0%}" if cfg else "80%"
    ai_tag  = "  [AI✓]" if report.ai_oracle_used else ""

    total_rej = (
        len(report.stable_rejected)
        + len(report.volatility_rejected)
        + len(report.smart_money_rejected)
    )

    # ── Header ──────────────────────────────────────────────────────────────
    lines += [
        _DIVIDER,
        "  POLYMARKET MARKET SCANNER  —  SCAN REPORT",
        _DIVIDER,
        f"  Markets scanned  : {report.total_markets_scanned}{ai_tag}",
        f"  Account capital  : {cap_str}  |  TP capture: {tp_str}",
        f"  Stable: {len(report.stable_approved)}"
        f"   Vol: {len(report.volatility_approved)}"
        f"   Smart$: {len(report.smart_money_approved)}"
        f"   Rejected: {total_rej}",
        _DIVIDER,
        "",
    ]

    # ── Stable ──────────────────────────────────────────────────────────────
    lines.append("📊 Stable Opportunities (convergence plays):")
    if report.stable_approved:
        for opp, dec in report.stable_approved:
            lines += [_fmt_stable(opp, dec), ""]
    else:
        lines += [f"  {_NO_TRADE}", ""]

    # ── Volatility ───────────────────────────────────────────────────────────
    lines.append("⚡ Volatility Opportunities (bracket trades):")
    if report.volatility_approved:
        for opp, dec in report.volatility_approved:
            lines += [_fmt_volatility(opp, dec), ""]
    else:
        lines += [f"  {_NO_TRADE}", ""]

    # ── Smart Money ──────────────────────────────────────────────────────────
    lines.append("🐋 Smart Money Opportunities (whale flow + price-impact filter):")
    if report.smart_money_approved:
        for opp, dec in report.smart_money_approved:
            lines += [_fmt_smart_money(opp, dec), ""]
    else:
        lines += [f"  {_NO_TRADE}", ""]

    # ── Audit trail ──────────────────────────────────────────────────────────
    if total_rej:
        lines += [_DIVIDER, f"  Rejected ({total_rej} total):"]
        for label, lst in [
            ("Stable", report.stable_rejected),
            ("Volatility", report.volatility_rejected),
            ("Smart Money", report.smart_money_rejected),
        ]:
            if lst:
                lines.append(f"  [{label}]")
                for opp, dec in lst:
                    lines.append(_fmt_rejected(opp, dec))
        lines.append("")

    lines.append(_DIVIDER)
    return "\n".join(lines)
