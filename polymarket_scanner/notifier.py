"""Telegram Notifier — push approved trade alerts to a Telegram chat.

Configuration (environment variables)
--------------------------------------
TELEGRAM_BOT_TOKEN   Your Telegram bot token  (from @BotFather).
TELEGRAM_CHAT_ID     Target chat / channel ID  (use @userinfobot to find it).

Both must be set for notifications to fire.  If either is missing the
notifier silently no-ops so the scanner keeps running undisturbed.

Message format (per approved opportunity)
-----------------------------------------
    🚨 POLYMARKET ALERT — Stable | EV: +0.1500

    📌 Will X resolve YES by Dec 31?
       Direction : BUY YES
       Entry     : 0.2500
       AI Prob   : 0.8000
       🎯 TP      : 0.7200  (+188.0%)
       💵 Bet     : $5.00  |  Est. Profit: $2.50
       🔗 https://polymarket.com/event/<id>

Usage
-----
    from polymarket_scanner.notifier import TelegramNotifier

    notifier = TelegramNotifier()      # reads env vars automatically
    notifier.send_report(report)       # one message per approved opportunity
"""

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from .models import SmartMoneyOpportunity, StableOpportunity, VolatilityOpportunity
from .scanner import ScanReport

logger = logging.getLogger(__name__)

_API_URL    = "https://api.telegram.org/bot{token}/sendMessage"
_MAX_LEN    = 4096   # Telegram hard character limit per message


# ---------------------------------------------------------------------------
# Low-level HTTP send
# ---------------------------------------------------------------------------

def _send(token: str, chat_id: str, text: str) -> bool:
    """POST one Telegram message. Returns True on success."""
    url     = _API_URL.format(token=token)
    payload = json.dumps({
        "chat_id":                  chat_id,
        "text":                     text[:_MAX_LEN],
        "parse_mode":               "HTML",
        "disable_web_page_preview": True,
    }).encode("utf-8")

    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            if not result.get("ok"):
                logger.warning("Telegram returned ok=false: %s", result)
                return False
        return True
    except urllib.error.HTTPError as exc:
        logger.warning("Telegram HTTP %s: %s", exc.code, exc.reason)
    except urllib.error.URLError as exc:
        logger.warning("Telegram URL error: %s", exc.reason)
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("Telegram unexpected error: %s", exc)
    return False


# ---------------------------------------------------------------------------
# Percentage helper
# ---------------------------------------------------------------------------

def _pct(entry: float, tp: float) -> str:
    if entry <= 0:
        return "n/a"
    return f"{(tp - entry) / entry * 100:+.1f}%"


# ---------------------------------------------------------------------------
# Per-opportunity message builders
# ---------------------------------------------------------------------------

def _msg_stable(opp: StableOpportunity, pos: float) -> str:
    scale  = pos / opp.suggested_position if opp.suggested_position else 1
    profit = opp.expected_profit * scale
    tp     = opp.take_profit_price
    tp_str = f"{tp:.4f}  ({_pct(opp.market.price, tp)})" if tp > 0 else "n/a"
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


def _msg_volatility(opp: VolatilityOpportunity, pos: float) -> str:
    scale  = pos / opp.suggested_position if opp.suggested_position else 1
    profit = opp.expected_profit * scale
    tp     = opp.take_profit_price or opp.target_price
    tp_str = f"{tp:.4f}  ({_pct(opp.entry_price, tp)})" if tp > 0 else "n/a"
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


def _msg_smart_money(opp: SmartMoneyOpportunity, pos: float) -> str:
    scale     = pos / opp.suggested_position if opp.suggested_position else 1
    profit    = opp.expected_profit * scale
    tp        = opp.take_profit_price
    tp_str    = f"{tp:.4f}  ({_pct(opp.market.price, tp)})" if tp > 0 else "n/a"
    badge     = "🔴 HIGH" if opp.confidence == "HIGH" else "🟡 MEDIUM"
    breakout  = "✅ breakout" if opp.is_breakout else "⚠ no breakout"
    return (
        f"🐋 <b>POLYMARKET ALERT</b> — Smart Money {badge} | EV: {opp.ev:+.4f}\n\n"
        f"📌 <b>{opp.market.question}</b>\n"
        f"   Direction : BUY {opp.side}  ({opp.flow_direction} flow)\n"
        f"   Entry     : {opp.market.price:.4f}  |  AI Prob: {opp.market.true_prob:.4f}\n"
        f"   Vol/Liq   : {opp.volume_spike_ratio:.2f}  "
        f"|  Impact: {opp.price_impact_ratio:.3f}  |  {breakout}\n"
        f"   Δ24h      : {opp.price_move_pct:+.2%}\n"
        f"   🎯 TP      : {tp_str}\n"
        f"   💵 Bet     : ${pos:.2f}  |  Est. Profit: ${profit:.2f}\n"
        f"   🔗 {opp.market.polymarket_url()}"
    )


def _msg_summary(report: ScanReport) -> str:
    total = (
        len(report.stable_approved)
        + len(report.volatility_approved)
        + len(report.smart_money_approved)
    )
    cfg = report.config
    cap = f"${cfg.total_capital:.0f}" if cfg else "N/A"
    ai  = " [AI✓]" if report.ai_oracle_used else ""
    return (
        f"📡 <b>Polymarket Scan Complete{ai}</b>\n"
        f"Markets: {report.total_markets_scanned}  |  Capital: {cap}\n"
        f"✅ <b>{total} trade(s) approved</b>  "
        f"(Stable: {len(report.stable_approved)}  "
        f"Vol: {len(report.volatility_approved)}  "
        f"Smart$: {len(report.smart_money_approved)})"
    )


# ---------------------------------------------------------------------------
# Public class
# ---------------------------------------------------------------------------

class TelegramNotifier:
    """Send approved trade alerts to Telegram.

    Parameters
    ----------
    bot_token :
        Telegram bot token. Falls back to env var TELEGRAM_BOT_TOKEN.
    chat_id :
        Target chat ID. Falls back to env var TELEGRAM_CHAT_ID.
    send_summary :
        If True, prepend a summary message before individual alerts.
    """

    def __init__(
        self,
        bot_token:    Optional[str] = None,
        chat_id:      Optional[str] = None,
        send_summary: bool = True,
    ):
        self.token       = bot_token or os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id     = chat_id   or os.getenv("TELEGRAM_CHAT_ID",   "")
        self._send_sum   = send_summary
        self._enabled    = bool(self.token and self.chat_id)

        if not self._enabled:
            logger.info(
                "TelegramNotifier: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not "
                "set — notifications disabled."
            )

    def is_enabled(self) -> bool:
        return self._enabled

    def _post(self, text: str) -> bool:
        if not self._enabled:
            return False
        return _send(self.token, self.chat_id, text)

    def send_report(self, report: ScanReport) -> int:
        """Send one Telegram message per approved opportunity.

        Returns the number of messages successfully sent.
        Silently returns 0 if notifier is disabled or no opportunities exist.
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

        if self._send_sum and self._post(_msg_summary(report)):
            sent += 1

        for opp, dec in report.stable_approved:
            pos = dec.approved_position or opp.suggested_position
            if self._post(_msg_stable(opp, pos)):
                sent += 1

        for opp, dec in report.volatility_approved:
            pos = dec.approved_position or opp.suggested_position
            if self._post(_msg_volatility(opp, pos)):
                sent += 1

        for opp, dec in report.smart_money_approved:
            pos = dec.approved_position or opp.suggested_position
            if self._post(_msg_smart_money(opp, pos)):
                sent += 1

        logger.info("TelegramNotifier: sent %d message(s) to %s", sent, self.chat_id)
        return sent
