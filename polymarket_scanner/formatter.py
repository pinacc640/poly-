"""Report formatter.

Converts a ScanReport into a human-readable console string covering
all four output sections:
  1. Stable Opportunities
  2. Volatility Opportunities
  3. Smart Money Opportunities
  4. Arbitrage Opportunities (Risk-Free)

Each approved opportunity now shows:
  - Kelly f (raw fraction) and Quarter-Kelly USD position
  - Suggested Limit Price with MAKER/TAKER execution advice
  - Bid/Ask spread

Rejected candidates are shown in a compact audit trail at the bottom.
"""

from .models import (
    ArbitrageOpportunity,
    OrderBookAdvice,
    RiskDecision,
    SmartMoneyOpportunity,
    StableOpportunity,
    VolatilityOpportunity,
)
from .scanner import ScanReport

_NO_TRADE_MSG = "当前无符合纪律的交易机会。"
_DIVIDER      = "─" * 70
_THIN_DIV     = "·" * 70


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _fmt_kelly_block(kelly_f: float, kelly_pos: float, approved_pos: float) -> str:
    """Format the Kelly sizing block shown in every opportunity."""
    pct    = kelly_f * 100
    capped = approved_pos < kelly_pos - 0.005   # capped if >half-cent diff
    cap_note = f"  ⚠ capped to ${approved_pos:.2f}" if capped else ""
    return (
        f"    Kelly f (raw):      {pct:+.2f}%  →  "
        f"¼-Kelly = ${kelly_pos:.2f}{cap_note}\n"
        f"    ✅ Suggested Bet:   ${approved_pos:.2f}"
    )


def _fmt_order_block(advice: OrderBookAdvice) -> str:
    """Format the order-book execution advice block."""
    if advice is None:
        return "    Order Advice:      N/A"

    tag        = "🟢 TAKER" if advice.order_type == "TAKER" else "🟡 MAKER"
    spread_str = f"{advice.spread:.4f}" if advice.spread > 0 else "n/a"
    return (
        f"    Spread (Bid-Ask):   {spread_str}\n"
        f"    Exec Type:         {tag}\n"
        f"    Limit Price:       {advice.limit_price:.4f}  (side={advice.side})\n"
        f"    Exec Rationale:    {advice.rationale}"
    )


def _fmt_tp_line(opp) -> str:
    """Format the Take Profit line (Phase 3)."""
    tp = getattr(opp, "take_profit_price", 0.0)
    if not tp:
        return "    🎯 Take Profit:      (not set)"
    entry = opp.market.price
    pct   = (tp - entry) / entry * 100 if entry > 0 else 0
    return (
        f"    🎯 Take Profit:      {tp:.4f}  "
        f"(entry {entry:.4f} → +{pct:.1f}%  |  80% of AI edge captured)"
    )


# ---------------------------------------------------------------------------
# Per-opportunity formatters
# ---------------------------------------------------------------------------

def _fmt_stable(opp: StableOpportunity, decision: RiskDecision) -> str:
    pos    = decision.approved_position or opp.suggested_position
    scale  = pos / opp.suggested_position if opp.suggested_position else 1
    profit = opp.expected_profit * scale
    notes  = " | ".join(opp.rationale)

    kelly_block = _fmt_kelly_block(decision.kelly_f, decision.kelly_position, pos)
    order_block = _fmt_order_block(opp.order_advice)

    return (
        f"  - Market:            {opp.market.question}\n"
        f"    ID:                {opp.market.market_id}  |  Category: {opp.market.category}\n"
        f"    Score:             {opp.score}  |  Risk: {opp.risk_level}\n"
        f"    EV (per $1):       {opp.ev:+.4f}\n"
        f"    Expected Profit:   ${profit:.2f}\n"
        f"    Rationale:         {notes}\n"
        f"{_THIN_DIV}\n"
        f"{kelly_block}\n"
        f"{_fmt_tp_line(opp)}\n"
        f"{order_block}"
    )


def _fmt_volatility(opp: VolatilityOpportunity, decision: RiskDecision) -> str:
    pos    = decision.approved_position or opp.suggested_position
    scale  = pos / opp.suggested_position if opp.suggested_position else 1
    profit = opp.expected_profit * scale
    notes  = " | ".join(opp.rationale)

    kelly_block = _fmt_kelly_block(decision.kelly_f, decision.kelly_position, pos)
    order_block = _fmt_order_block(opp.order_advice)

    return (
        f"  - Market:            {opp.market.question}\n"
        f"    ID:                {opp.market.market_id}  |  Category: {opp.market.category}\n"
        f"    Entry:             {opp.entry_price:.4f}  →  "
        f"Target: {opp.target_price:.4f}  |  Stop: {opp.stop_loss:.4f}\n"
        f"    Max Hold:          {opp.max_hold_days} days\n"
        f"    EV (per $1):       {opp.ev:+.4f}\n"
        f"    Expected Profit:   ${profit:.2f}\n"
        f"    Rationale:         {notes}\n"
        f"{_THIN_DIV}\n"
        f"{kelly_block}\n"
        f"{_fmt_tp_line(opp)}\n"
        f"{order_block}"
    )


def _fmt_smart_money(opp: SmartMoneyOpportunity, decision: RiskDecision) -> str:
    pos    = decision.approved_position or opp.suggested_position
    scale  = pos / opp.suggested_position if opp.suggested_position else 1
    profit = opp.expected_profit * scale
    notes  = " | ".join(opp.rationale)
    badge  = {"HIGH": "🔴 HIGH", "MEDIUM": "🟡 MEDIUM", "LOW": "⚪ LOW"}.get(
        opp.confidence, opp.confidence
    )
    # Phase 3: price-impact and breakout display
    impact_str   = f"{opp.price_impact_ratio:.3f}" if opp.price_impact_ratio > 0 else "n/a"
    breakout_tag = "✅ breakout" if opp.is_breakout else "⚠ no breakout"

    kelly_block = _fmt_kelly_block(decision.kelly_f, decision.kelly_position, pos)
    order_block = _fmt_order_block(opp.order_advice)

    return (
        f"  - Market:            {opp.market.question}\n"
        f"    ID:                {opp.market.market_id}  |  Category: {opp.market.category}\n"
        f"    Confidence:        {badge}  |  {breakout_tag}\n"
        f"    Flow:              {opp.flow_direction}  "
        f"|  Vol/Liq={opp.volume_spike_ratio:.2f}  "
        f"|  Price Move={opp.price_move_pct:+.1%}  "
        f"|  Impact={impact_str}\n"
        f"    EV (per $1):       {opp.ev:+.4f}\n"
        f"    Expected Profit:   ${profit:.2f}\n"
        f"    Signals:           {notes}\n"
        f"{_THIN_DIV}\n"
        f"{kelly_block}\n"
        f"{_fmt_tp_line(opp)}\n"
        f"{order_block}"
    )


def _fmt_arbitrage(opp: ArbitrageOpportunity) -> str:
    profit_pct = opp.guaranteed_profit_pct * 100
    return (
        f"  - Polymarket:        {opp.poly_market.question}\n"
        f"    Kalshi match:      {opp.kalshi_title}\n"
        f"    Kalshi ticker:     {opp.kalshi_ticker}"
        f"  |  Similarity: {opp.title_similarity:.0%}\n"
        f"    Poly YES price:    {opp.poly_yes_price:.4f}"
        f"  |  Kalshi NO: {opp.kalshi_no_price:.4f}\n"
        f"    Combined cost:     {opp.combined_cost:.4f}"
        f"  →  Guaranteed edge: {profit_pct:.2f}%\n"
        f"    Suggested Position: ${opp.suggested_position:.2f}\n"
        f"    Expected Profit:   ${opp.expected_profit:.2f}  ✅ RISK-FREE"
    )


def _fmt_rejected(opp, decision: RiskDecision) -> str:
    name       = opp.market.question[:55] + ("…" if len(opp.market.question) > 55 else "")
    reasons    = "; ".join(decision.reasons)
    kelly_note = (
        f" [Kelly f={decision.kelly_f:+.4f}]" if decision.kelly_f != 0.0 else ""
    )
    return f"    [{opp.market.market_id}] {name}\n      ↳ REJECTED{kelly_note}: {reasons}"


# ---------------------------------------------------------------------------
# Kelly legend (shown once in the header)
# ---------------------------------------------------------------------------
def _kelly_legend(cfg) -> str:
    if cfg is None:
        return ""
    return (
        f"  Kelly settings:  fraction=¼ ({cfg.kelly_fraction:.2f})"
        f"  |  cap={cfg.max_kelly_position_ratio:.0%} of capital"
        f"  |  Maker threshold={cfg.taker_spread_threshold:.2f}"
        f"  |  TP capture={cfg.tp_capture_ratio:.0%}"
    )


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
    arb_tag = f"  |  Arb: {len(report.arbitrage_found)}" if report.run_arbitrage else ""

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
        _kelly_legend(cfg),
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
