"""Telegram Notifier — push approved trade alerts to a Telegram chat.

Configuration (environment variables)
--------------------------------------
TELEGRAM_BOT_TOKEN   Your Telegram bot token (from @BotFather).
TELEGRAM_CHAT_ID     Your chat / channel ID (use @userinfobot to find it).

Both must be set for notifications to send. If either is missing,
the notifier silently no-ops so the scanner continues working.

Message format (per approved opportunity)
-----------------------------------------
🚨 POLYMARKET ALERT — Stable | EV: +0.1250

📌 Will X happen before Dec 31?
   Direction : BUY YES
   Entry     : 0.2500
   🎯 TP      : 0.5400  (+116.0%)
   💵 Bet     : $5.00  |  Est. Profit: $1.52
   🔗 https://polymarket.com/event/<id>

Usage
-----
    from polymarket_scanner.notifier import TelegramNotifier
    from polymarket_scanner.scanner import ScanReport

    notifier = TelegramNotifier()          # reads env vars automatically
    notifier.send_report(report)           # sends one message per approved opp
    notifier.send_summary(report)          # sends a single summary message

All methods are safe to call even when TELEGRAM_BOT_TOKEN is not set —
they will log a warning and return without raising.
"""

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import List, Optional

from .models import SmartMoneyOpportunity, StableOpportunity, VolatilityOpportunity
from .scanner import ScanReport

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}/sendMessage"
MAX_MESSAGE_LENGTH = 4096   # Telegram hard limit


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _send_message(token: str, chat_id: str, text: str) -> bool:
    """Send a single Telegram message. Returns True on success."""
    url     = TELEGRAM_API_BASE.format(token=token)
    payload = json.dumps({
        "chat_id":    chat_id,
        "text":       text[:MAX_MESSAGE_LENGTH],
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }).encode("utf-8")

    req = urllib.request.Request(
        url,
        data    = payload,
        headers = {"Content-Type": "application/json"},
        method  = "POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            if not result.get("ok"):
                logger.warning("Telegram API returned ok=false: %s", result)
                return False
        return True
    except urllib.error.HTTPError as exc:
        logger.warning("Telegram HTTP %s: %s", exc.code, exc.reason)
    except urllib.error.URLError as exc:
        logger.warning("Telegram URL error: %s", exc.reason)
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("Telegram unexpected error: %s", exc)
    return False


def _tp_pct(entry: float, tp: float) -> str:
    if entry <= 0:
        return "n/a"
    return f"{(tp - entry) / entry * 100:+.1f}%"


# ---------------------------------------------------------------------------
# Message builders
# ---------------------------------------------------------------------------

def _build_stable_message(opp: StableOpportunity, pos: float) -> str:
    profit = opp.expected_profit * (pos / opp.suggested_position) if opp.suggested_position else 0
    tp     = getattr(opp, "take_profit_price", 0.0)
    tp_str = f"{tp:.4f}  ({_tp_pct(opp.market.price, tp)})" if tp > 0 else "n/a"
    return (
        f"🚨 <b>POLYMARKET ALERT</b> — Stable | EV: {opp.ev:+.4f}\n\n"
        f"📌 <b>{opp.market.question}</b>\n"
        f"   Direction : BUY {opp.side}\n"
        f"   Entry     : {opp.market.price:.4f}\n"
        f"   AI Prob   : {opp.market.true_prob:.4f}\n"
        f"   🎯 TP      : {tp_str}\n"
        f"   💵 Bet     : ${pos:.2f}  |  Est. Profit: ${profit:.2f}\n"
        f"   Score     : {opp.score}  |  Risk: {opp.risk_level}\n"
        f"   🔗 {opp.market.polymarket_url()}"
    )


def _build_volatility_message(opp: VolatilityOpportunity, pos: float) -> str:
    profit = opp.expected_profit * (pos / opp.suggested_position) if opp.suggested_position else 0
    tp     = getattr(opp, "take_profit_price", opp.target_price)
    tp_str = f"{tp:.4f}  ({_tp_pct(opp.entry_price, tp)})" if tp > 0 else "n/a"
    return (
        f"🚨 <b>POLYMARKET ALERT</b> — Volatility | EV: {opp.ev:+.4f}\n\n"
        f"📌 <b>{opp.market.question}</b>\n"
        f"   Direction : BUY {opp.side}\n"
        f"   Entry     : {opp.entry_price:.4f}  |  Stop: {opp.stop_loss:.4f}\n"
        f"   Max Hold  : {opp.max_hold_days}d\n"
        f"   🎯 TP      : {tp_str}\n"
        f"   💵 Bet     : ${pos:.2f}  |  Est. Profit: ${profit:.2f}\n"
        f"   🔗 {opp.market.polymarket_url()}"
    )


def _build_smart_money_message(opp: SmartMoneyOpportunity, pos: float) -> str:
    profit    = opp.expected_profit * (pos / opp.suggested_position) if opp.suggested_position else 0
    tp        = getattr(opp, "take_profit_price", 0.0)
    tp_str    = f"{tp:.4f}  ({_tp_pct(opp.market.price, tp)})" if tp > 0 else "n/a"
    badge     = {"HIGH": "🔴 HIGH", "MEDIUM": "🟡 MEDIUM"}.get(opp.confidence, opp.confidence)
    breakout  = "✅ breakout" if opp.is_breakout else "⚠ no breakout"
    return (
        f"🐋 <b>POLYMARKET ALERT</b> — Smart Money {badge} | EV: {opp.ev:+.4f}\n\n"
        f"📌 <b>{opp.market.question}</b>\n"
        f"   Direction : BUY {opp.side}  ({opp.flow_direction} flow)\n"
        f"   Entry     : {opp.market.price:.4f}  |  AI Prob: {opp.market.true_prob:.4f}\n"
        f"   Vol/Liq   : {opp.volume_spike_ratio:.2f}  "
        f"|  Price Impact: {opp.price_impact_ratio:.3f}  |  {breakout}\n"
        f"   Δ24h      : {opp.price_move_pct:+.2%}\n"
        f"   🎯 TP      : {tp_str}\n"
        f"   💵 Bet     : ${pos:.2f}  |  Est. Profit: ${profit:.2f}\n"
        f"   🔗 {opp.market.polymarket_url()}"
    )


def _build_summary_message(report: ScanReport) -> str:
    """One-liner summary when there are approved opportunities."""
    total = (
        len(report.stable_approved)
        + len(report.volatility_approved)
        + len(report.smart_money_approved)
    )
    cfg         = report.config
    capital_str = f"${cfg.total_capital:.0f}" if cfg else "N/A"
    ai_tag      = " [AI✓]" if report.ai_oracle_used else ""

    lines = [
        f"📡 <b>Polymarket Scan Complete{ai_tag}</b>",
        f"Markets scanned: {report.total_markets_scanned}  |  Capital: {capital_str}",
        f"✅ <b>{total} trade(s) approved</b>  "
        f"(Stable: {len(report.stable_approved)}  "
        f"Vol: {len(report.volatility_approved)}  "
        f"Smart$: {len(report.smart_money_approved)})",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public notifier class
# ---------------------------------------------------------------------------

class TelegramNotifier:
    """Send approved trade alerts to a Telegram chat.

    Parameters
    ----------
    bot_token :
        Telegram bot token. Defaults to env var TELEGRAM_BOT_TOKEN.
    chat_id :
        Target chat / channel ID. Defaults to env var TELEGRAM_CHAT_ID.
    send_summary :
        If True, prepend a summary message before individual alerts.
    """

    def __init__(
        self,
        bot_token:    Optional[str] = None,
        chat_id:      Optional[str] = None,
        send_summary: bool          = True,
    ):
        self.token        = bot_token or os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id      = chat_id   or os.getenv("TELEGRAM_CHAT_ID",   "")
        self.send_summary = send_summary

        if not self.token or not self.chat_id:
            logger.info(
                "TelegramNotifier: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set — "
                "notifications disabled."
            )
        self._enabled = bool(self.token and self.chat_id)

    # ------------------------------------------------------------------

    def is_enabled(self) -> bool:
        return self._enabled

    def _send(self, text: str) -> bool:
        if not self._enabled:
            return False
        return _send_message(self.token, self.chat_id, text)

    # ------------------------------------------------------------------

    def send_report(self, report: ScanReport) -> int:
        """Send one Telegram message per approved opportunity.

        Returns the number of messages successfully sent.
        """
        if not self._enabled:
            return 0

        total = (
            len(report.stable_approved)
            + len(report.volatility_approved)
            + len(report.smart_money_approved)
        )
        if total == 0:
            logger.debug("TelegramNotifier: no approved opportunities — nothing to send.")
            return 0

        sent = 0

        # Summary header
        if self.send_summary:
            if self._send(_build_summary_message(report)):
                sent += 1

        # Individual opportunity messages
        for opp, dec in report.stable_approved:
            pos = dec.approved_position or opp.suggested_position
            if self._send(_build_stable_message(opp, pos)):
                sent += 1

        for opp, dec in report.volatility_approved:
            pos = dec.approved_position or opp.suggested_position
            if self._send(_build_volatility_message(opp, pos)):
                sent += 1

        for opp, dec in report.smart_money_approved:
            pos = dec.approved_position or opp.suggested_position
            if self._send(_build_smart_money_message(opp, pos)):
                sent += 1

        logger.info("TelegramNotifier: sent %d message(s) to chat %s", sent, self.chat_id)
        return sent
