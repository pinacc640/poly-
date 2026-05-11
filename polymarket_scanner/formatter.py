"""Report formatter.

Converts a ScanReport into a human-readable console string covering
all four output sections:
  1. Stable Opportunities
  2. Volatility Opportunities
  3. Smart Money Opportunities
  4. Arbitrage Opportunities (Risk-Free)

Rejected candidates are shown in a compact audit trail at the bottom.
"""

from .models import (
    ArbitrageOpportunity,
    RiskDecision,
    SmartMoneyOpportunity,
    StableOpportunity,
    VolatilityOpportunity,
)
from .scanner import ScanReport

_NO_TRADE_MSG = "当前无符合纪律的交易机会。"
_DIVIDER = "─" * 60


# ---------------------------------------------------------------------------
# Per-opportunity formatters
# ---------------------------------------------------------------------------
def _fmt_stable(opp: StableOpportunity, decision: RiskDecision) -> str:
    pos    = decision.approved_position or opp.suggested_position
    scale  = pos / opp.suggested_position if opp.suggested_position else 1
    profit = opp.expected_profit * scale
    notes  = " | ".join(opp.rationale)
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
    pos    = decision.approved_position or opp.suggested_position
    scale  = pos / opp.suggested_position if opp.suggested_position else 1
    profit = opp.expected_profit * scale
    notes  = " | ".join(opp.rationale)
    return (
        f"  - Market:            {opp.market.question}\n"
        f"    ID:                {opp.market.market_id}  |  Category: {opp.market.category}\n"
        f"    Entry:             {opp.entry_price:.3f}\n"
        f"    Target:            {opp.target_price:.3f}  "
        f"(+{opp.target_price - opp.entry_price:+.3f})\n"
        f"    Stop Loss:         {opp.stop_loss:.3f}  "
        f"({opp.stop_loss - opp.entry_price:+.3f})\n"
        f"    Max Hold:          {opp.max_hold_days} days\n"
        f"    EV (per $1):       {opp.ev:+.4f}\n"
        f"    Suggested Position: ${pos:.2f}\n"
        f"    Expected Profit:   ${profit:.2f}\n"
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
    return (
        f"  - Market:            {opp.market.question}\n"
        f"    ID:                {opp.market.market_id}  |  Category: {opp.market.category}\n"
        f"    Confidence:        {badge}\n"
        f"    Flow Direction:    {opp.flow_direction}"
        f"  |  Vol/Liq Ratio: {opp.volume_spike_ratio:.2f}\n"
        f"    Price Move 24h:    {opp.price_move_pct:+.1%}\n"
        f"    EV (per $1):       {opp.ev:+.4f}\n"
        f"    Suggested Position: ${pos:.2f}\n"
        f"    Expected Profit:   ${profit:.2f}\n"
        f"    Signals:           {notes}"
    )


def _fmt_arbitrage(opp: ArbitrageOpportunity) -> str:
    profit_pct = opp.guaranteed_profit_pct * 100
    return (
        f"  - Polymarket:        {opp.poly_market.question}\n"
        f"    Kalshi match:      {opp.kalshi_title}\n"
        f"    Kalshi ticker:     {opp.kalshi_ticker}"
        f"  |  Title similarity: {opp.title_similarity:.0%}\n"
        f"    Poly YES price:    {opp.poly_yes_price:.4f}"
        f"  |  Kalshi NO ask: {opp.kalshi_no_price:.4f}\n"
        f"    Combined cost:     {opp.combined_cost:.4f}"
        f"  →  Guaranteed edge: {profit_pct:.2f}%\n"
        f"    Suggested Position: ${opp.suggested_position:.2f}\n"
        f"    Expected Profit:   ${opp.expected_profit:.2f}  ✅ RISK-FREE"
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
    ai_tag  = "  [AI✓]" if report.ai_oracle_used else ""
    arb_tag = f"  |  Arbitrage: {len(report.arbitrage_found)}" if report.run_arbitrage else ""

    # ── Header ──────────────────────────────────────────────────────
    lines += [
        _DIVIDER,
        "  POLYMARKET MARKET SCANNER  —  SCAN REPORT",
        _DIVIDER,
        f"  Markets scanned  : {report.total_markets_scanned}{ai_tag}",
    ]
    if report.kalshi_markets_scanned:
        lines.append(f"  Kalshi markets   : {report.kalshi_markets_scanned}")
    lines += [
        f"  Account capital  : {capital_str}",
        f"  Stable approved  : {len(report.stable_approved)}"
        f"   |  Vol: {len(report.volatility_approved)}"
        f"   |  Smart$: {len(report.smart_money_approved)}"
        f"{arb_tag}"
        f"   |  Rejected: {total_rej}",
        _DIVIDER,
        "",
    ]

    # ── Stable ──────────────────────────────────────────────────────
    lines.append("Stable Opportunities (convergence plays):")
    if report.stable_approved:
        for opp, dec in report.stable_approved:
            lines += [_fmt_stable(opp, dec), ""]
    else:
        lines += [f"  {_NO_TRADE_MSG}", ""]

    # ── Volatility ───────────────────────────────────────────────────
    lines.append("Volatility Opportunities (bracket trades):")
    if report.volatility_approved:
        for opp, dec in report.volatility_approved:
            lines += [_fmt_volatility(opp, dec), ""]
    else:
        lines += [f"  {_NO_TRADE_MSG}", ""]

    # ── Smart Money ──────────────────────────────────────────────────
    lines.append("Smart Money Opportunities (follow informed flow):")
    if report.smart_money_approved:
        for opp, dec in report.smart_money_approved:
            lines += [_fmt_smart_money(opp, dec), ""]
    else:
        lines += [f"  {_NO_TRADE_MSG}", ""]

    # ── Arbitrage ────────────────────────────────────────────────────
    if report.run_arbitrage or report.arbitrage_found:
        lines.append("Arbitrage Opportunities (Risk-Free — Polymarket × Kalshi):")
        if report.arbitrage_found:
            for opp in report.arbitrage_found:
                lines += [_fmt_arbitrage(opp), ""]
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
