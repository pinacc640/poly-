"""Telegram 通知模块 — 将交易机会推送到 Telegram。"""

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import List, Optional

log = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org"


class TelegramNotifier:
    def __init__(self, bot_token: Optional[str] = None, chat_id: Optional[str] = None, timeout: int = 10):
        self.bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID", "")
        self.timeout = timeout
        self._enabled = bool(self.bot_token and self.chat_id)

        # 验证 token 是否有效
        if self._enabled:
            try:
                test_url = f"{TELEGRAM_API_BASE}/bot{self.bot_token}/getMe"
                req = urllib.request.Request(test_url)
                with urllib.request.urlopen(req, timeout=5) as resp:
                    result = json.loads(resp.read().decode("utf-8"))
                    if not result.get("ok"):
                        log.warning("Telegram Bot Token 无效（getMe 返回 false）— 推送已禁用")
                        self._enabled = False
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    log.warning("Telegram Bot Token 已失效（401）— 推送已禁用。请重新生成 token。")
                    self._enabled = False
            except Exception as e:
                log.debug("Telegram token 验证跳过: %s", e)

        if not self._enabled:
            log.info("TelegramNotifier: 未配置或 token 无效 — notifications disabled.")

    def is_enabled(self) -> bool:
        return self._enabled

    def _post(self, text: str, parse_mode: str = "HTML") -> bool:
        if not self._enabled:
            return False

        url = f"{TELEGRAM_API_BASE}/bot{self.bot_token}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": "true",
        }).encode("utf-8")

        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                return result.get("ok", False)
        except urllib.error.HTTPError as e:
            log.warning("Telegram HTTP %d: %s", e.code, e.reason)
        except urllib.error.URLError as e:
            log.warning("Telegram 连接失败: %s", e.reason)
        except Exception as e:
            log.warning("Telegram 发送错误: %s", e)
        return False

    def send_text(self, text: str) -> bool:
        return self._post(text)

    def _format_opportunity(self, opp, strategy_name: str) -> str:
        market_id = getattr(opp, "market_id", "N/A")
        question = getattr(opp, "question", "Unknown")[:80]
        side = getattr(opp, "side", "?")
        kelly_bet = getattr(opp, "kelly_bet", 0)
        expected_profit = getattr(opp, "expected_profit", 0)
        entry_price = getattr(opp, "entry_price", 0)
        take_profit = getattr(opp, "take_profit_price", 0)
        true_prob = getattr(opp, "true_prob", 0)

        return (
            f"<b>🎯 {strategy_name}</b>\n"
            f"<b>Market:</b> {question}\n"
            f"<b>ID:</b> <code>{market_id}</code>\n"
            f"<b>Side:</b> {side} @ {entry_price:.4f}\n"
            f"<b>AI Prob:</b> {true_prob:.1%}\n"
            f"<b>Bet:</b> ${kelly_bet:.2f}\n"
            f"<b>Expected:</b> +${expected_profit:.2f}\n"
            f"<b>TP:</b> {take_profit:.4f}"
        )

    def send_report(self, report) -> int:
        if not self._enabled:
            return 0

        sent = 0
        all_opps = []

        for opp in getattr(report, "stable_approved", []):
            all_opps.append((opp, "Stable"))
        for opp in getattr(report, "volatility_approved", []):
            all_opps.append((opp, "Volatility"))
        for opp in getattr(report, "smart_money_approved", []):
            all_opps.append((opp, "Smart Money"))
        for opp in getattr(report, "arbitrage_found", []):
            all_opps.append((opp, "Arbitrage"))

        if not all_opps:
            return 0

        header = f"📊 <b>Polymarket Scanner Alert</b>\nFound {len(all_opps)} opportunity(ies)\n{'─' * 30}"
        if self._post(header):
            sent += 1

        for opp, strategy in all_opps:
            msg = self._format_opportunity(opp, strategy)
            if self._post(msg):
                sent += 1

        return sent

    def send_opportunities(self, opportunities: List, strategy_name: str = "Signal") -> int:
        if not self._enabled or not opportunities:
            return 0

        sent = 0
        for opp in opportunities:
            msg = self._format_opportunity(opp, strategy_name)
            if self._post(msg):
                sent += 1
        return sent