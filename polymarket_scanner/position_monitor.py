"""Position Monitor — risk checks on existing portfolio holdings.

For each active position in portfolio.json, evaluates three conditions
against the latest scan data:

  1. 🎯 Take Profit  — current price reached the AI-derived TP target
  2. 🚨 Stop Loss    — position has lost ≥ STOP_LOSS_PCT from entry
  3. 📉 Average Down — price dropped but AI thesis still intact

Generates ready-to-send Telegram HTML alert messages for every trigger.

Usage
-----
    from polymarket_scanner.position_monitor import PositionMonitor
    from polymarket_scanner.portfolio_sync import load_portfolio

    portfolio = load_portfolio()
    monitor   = PositionMonitor()
    alerts    = monitor.check(portfolio, markets)
    for msg in alerts:
        notifier._post(msg)
"""

import logging
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Risk thresholds  (easily adjustable)
# ---------------------------------------------------------------------------
TP_CAPTURE_RATIO  = 0.80   # capture 80% of edge gap (same as scanner TP)
STOP_LOSS_PCT     = 0.30   # unrealised loss ≥ 30% of entry → cut
AVG_DOWN_DROP_PCT = 0.15   # price dropped ≥ 15% from entry
AVG_DOWN_EDGE_MIN = 0.10   # AI prob must be ≥ 10% above current price


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _pnl_yes(size: float, avg: float, current: float) -> float:
    return round(size * (current - avg), 2)


def _pnl_no(size: float, avg_yes_price: float, current_yes_price: float) -> float:
    """For a NO position, profit when YES price falls."""
    return round(size * ((1 - current_yes_price) - (1 - avg_yes_price)), 2)


def _url(market) -> str:
    """Return market URL, compatible with all Market versions."""
    if hasattr(market, "polymarket_url") and callable(market.polymarket_url):
        return market.polymarket_url()
    slug = getattr(market, "event_slug", "") or market.market_id
    return f"https://polymarket.com/event/{slug}"


def _check_tp(pos, market) -> Optional[str]:
    entry   = pos.avg_price
    current = market.price
    tp_prob = market.true_prob

    if pos.side == "YES":
        tp = entry + (tp_prob - entry) * TP_CAPTURE_RATIO
        if tp > entry and current >= tp:
            pnl    = _pnl_yes(pos.size, entry, current)
            gain_p = (current - entry) / entry * 100
            return (
                f"🎯 <b>TAKE PROFIT TRIGGERED</b>\n\n"
                f"📌 <b>{pos.question}</b>\n"
                f"   Side: YES  |  Size: {pos.size:.2f} shares\n"
                f"   Entry: {entry:.4f}  →  Current: {current:.4f}  (+{gain_p:.1f}%)\n"
                f"   TP target: {tp:.4f}  (AI true_prob: {tp_prob:.4f})\n"
                f"   💰 Unrealised PnL: ${pnl:+.2f}\n"
                f"   ⚡ <b>ACTION: Consider selling YES to lock in profit</b>\n"
                f"   🔗 {_url(market)}"
            )

    else:  # NO side — profit when YES price falls
        tp = entry - (entry - tp_prob) * TP_CAPTURE_RATIO
        if tp < entry and current <= tp:
            pnl    = _pnl_no(pos.size, entry, current)
            fall_p = (entry - current) / entry * 100
            return (
                f"🎯 <b>TAKE PROFIT TRIGGERED (NO side)</b>\n\n"
                f"📌 <b>{pos.question}</b>\n"
                f"   Side: NO  |  Size: {pos.size:.2f} shares\n"
                f"   Entry (YES price): {entry:.4f}  →  Current: {current:.4f}  (-{fall_p:.1f}%)\n"
                f"   TP target: {tp:.4f}  (AI true_prob: {tp_prob:.4f})\n"
                f"   💰 Unrealised PnL: ${pnl:+.2f}\n"
                f"   ⚡ <b>ACTION: Consider selling NO to lock in profit</b>\n"
                f"   🔗 {_url(market)}"
            )
    return None


def _check_sl(pos, market) -> Optional[str]:
    entry   = pos.avg_price
    current = market.price

    if pos.side == "YES":
        if entry <= 0:
            return None
        loss_pct = (entry - current) / entry
        if loss_pct >= STOP_LOSS_PCT:
            pnl = _pnl_yes(pos.size, entry, current)
            return (
                f"🚨 <b>STOP LOSS TRIGGERED</b>\n\n"
                f"📌 <b>{pos.question}</b>\n"
                f"   Side: YES  |  Size: {pos.size:.2f} shares\n"
                f"   Entry: {entry:.4f}  →  Current: {current:.4f}  (-{loss_pct*100:.1f}%)\n"
                f"   Stop threshold: -{STOP_LOSS_PCT*100:.0f}%\n"
                f"   💸 Unrealised Loss: ${pnl:+.2f}\n"
                f"   ⚡ <b>ACTION: Consider cutting YES position</b>\n"
                f"   🔗 {_url(market)}"
            )

    else:  # NO side — lose when YES price rises
        no_entry   = 1 - entry
        no_current = 1 - current
        if no_entry <= 0:
            return None
        loss_pct = (no_entry - no_current) / no_entry
        if loss_pct >= STOP_LOSS_PCT:
            pnl = _pnl_no(pos.size, entry, current)
            return (
                f"🚨 <b>STOP LOSS TRIGGERED (NO side)</b>\n\n"
                f"📌 <b>{pos.question}</b>\n"
                f"   Side: NO  |  Size: {pos.size:.2f} shares\n"
                f"   Entry (YES price): {entry:.4f}  →  Current: {current:.4f}  (+{(current-entry)*100:.1f}%)\n"
                f"   Stop threshold: -{STOP_LOSS_PCT*100:.0f}%\n"
                f"   💸 Unrealised Loss: ${pnl:+.2f}\n"
                f"   ⚡ <b>ACTION: Consider cutting NO position</b>\n"
                f"   🔗 {_url(market)}"
            )
    return None


def _check_avg_down(pos, market) -> Optional[str]:
    if pos.side != "YES":
        return None

    entry     = pos.avg_price
    current   = market.price
    true_prob = market.true_prob

    if entry <= 0:
        return None

    drop_pct      = (entry - current) / entry
    still_bullish = (true_prob - current) >= AVG_DOWN_EDGE_MIN
    under_sl      = drop_pct < STOP_LOSS_PCT

    if drop_pct >= AVG_DOWN_DROP_PCT and still_bullish and under_sl:
        edge_pct = (true_prob - current) * 100
        return (
            f"📉 <b>AVERAGE DOWN CANDIDATE</b>\n\n"
            f"📌 <b>{pos.question}</b>\n"
            f"   Side: YES  |  Size: {pos.size:.2f} shares\n"
            f"   Entry: {entry:.4f}  →  Current: {current:.4f}  (-{drop_pct*100:.1f}%)\n"
            f"   AI true_prob: {true_prob:.4f}  (still +{edge_pct:.1f}% above market)\n"
            f"   💡 <b>ACTION: Thesis intact — consider averaging down</b>\n"
            f"   🔗 {_url(market)}"
        )
    return None


# ---------------------------------------------------------------------------
# Public class
# ---------------------------------------------------------------------------

class PositionMonitor:
    """Check all active portfolio positions against current market scan data.

    Parameters
    ----------
    tp_capture_ratio : float
        Fraction of AI edge gap to use as TP target.  Default 0.80.
    stop_loss_pct : float
        Loss fraction that triggers stop loss alert.  Default 0.30.
    avg_down_drop_pct : float
        Price drop fraction that qualifies as avg-down candidate.  Default 0.15.
    avg_down_edge_min : float
        Minimum (true_prob - current_price) to suggest avg down.  Default 0.10.
    """

    def __init__(
        self,
        tp_capture_ratio:  float = TP_CAPTURE_RATIO,
        stop_loss_pct:     float = STOP_LOSS_PCT,
        avg_down_drop_pct: float = AVG_DOWN_DROP_PCT,
        avg_down_edge_min: float = AVG_DOWN_EDGE_MIN,
    ):
        self.tp_capture_ratio  = tp_capture_ratio
        self.stop_loss_pct     = stop_loss_pct
        self.avg_down_drop_pct = avg_down_drop_pct
        self.avg_down_edge_min = avg_down_edge_min

    def check(self, portfolio: dict, markets: list) -> List[str]:
        """Evaluate all positions; return Telegram HTML alert strings.

        Parameters
        ----------
        portfolio :
            Dict[market_id → Position] from load_portfolio().
        markets :
            List of Market objects from the current scan.

        Returns
        -------
        List of HTML alert strings (may be empty if nothing triggered).
        """
        if not portfolio:
            log.debug("PositionMonitor: no active positions.")
            return []

        market_lookup = {m.market_id: m for m in markets}
        alerts: List[str] = []
        n_skipped = 0

        for mid, pos in portfolio.items():
            market = market_lookup.get(mid)
            if market is None:
                log.debug("Position %s not found in current scan — skipped", mid)
                n_skipped += 1
                continue

            # Priority: TP > SL > Avg Down  (one alert per position per run)
            alert = _check_tp(pos, market)
            if not alert:
                alert = _check_sl(pos, market)
            if not alert:
                alert = _check_avg_down(pos, market)

            if alert:
                alerts.append(alert)
                log.info("Position alert fired for %s", mid)

        log.info(
            "PositionMonitor: %d alert(s) | %d positions | %d skipped (not in scan)",
            len(alerts), len(portfolio), n_skipped,
        )
        return alerts
