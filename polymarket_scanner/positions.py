"""positions.py — Polymarket 持仓查询模块

支持两种 API 端点（自动降级）：
  1. CLOB API  /data/positions   — 需要 L1 / L2 HMAC-SHA256 认证头
  2. Data API  /positions        — 按 proxy_address 公开端点（无需签名，部分环境可用）

认证方案：Polymarket L1 Header Authentication
  POLY-ADDRESS       : EVM 签名地址 (signer / EOA)
  POLY-SIGNATURE     : HMAC-SHA256(secret=private_key_hex, msg=timestamp)
  POLY-TIMESTAMP     : Unix 毫秒时间戳字符串
  POLY-NONCE         : 递增整数（默认 0）

环境变量
--------
POLY_PRIVATE_KEY   : 64 位十六进制私钥（不含 0x 前缀）
POLY_PROXY_ADDR    : Proxy 钱包地址（0x 格式），用于 Data API fallback
POLY_SIGNER_ADDR   : EOA / Signer 地址（0x 格式），用于 CLOB 认证头

用法
----
    from polymarket_scanner.positions import PositionFetcher
    fetcher  = PositionFetcher()
    held_ids = fetcher.held_market_ids()   # set[str] — 已持仓的 conditionId/marketId
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, List, Optional, Set

from .models import Position

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# API 端点常量
# ---------------------------------------------------------------------------
CLOB_BASE_URL  = "https://clob.polymarket.com"
DATA_BASE_URL  = "https://data-api.polymarket.com"
REQUEST_TIMEOUT = 15   # seconds


# ---------------------------------------------------------------------------
# HMAC-SHA256 签名生成
# ---------------------------------------------------------------------------

def _make_headers(private_key_hex: str, signer_address: str, nonce: int = 0) -> Dict[str, str]:
    """生成 Polymarket L1 认证 Header 字典。

    签名规则：
        message  = timestamp_ms_str            (e.g. "1715000000000")
        key      = bytes.fromhex(private_key_hex)
        sig      = HMAC-SHA256(key, message.encode()).hexdigest()
    """
    timestamp_ms = str(int(time.time() * 1000))
    key_bytes    = bytes.fromhex(private_key_hex.lstrip("0x"))
    sig          = hmac.new(key_bytes, timestamp_ms.encode("utf-8"), hashlib.sha256).hexdigest()

    return {
        "POLY-ADDRESS":   signer_address,
        "POLY-SIGNATURE": sig,
        "POLY-TIMESTAMP": timestamp_ms,
        "POLY-NONCE":     str(nonce),
        "Accept":         "application/json",
        "Content-Type":   "application/json",
    }


# ---------------------------------------------------------------------------
# CLOB API — /data/positions
# ---------------------------------------------------------------------------

def _fetch_clob_positions(
    private_key_hex: str,
    signer_address: str,
    timeout: int = REQUEST_TIMEOUT,
) -> Optional[List[dict]]:
    """用 L1 认证头从 CLOB API 获取持仓列表。

    成功返回原始 dict 列表，失败返回 None（调用方降级）。
    """
    url     = f"{CLOB_BASE_URL}/data/positions"
    headers = _make_headers(private_key_hex, signer_address)
    req     = urllib.request.Request(url, headers=headers)

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
        # CLOB 返回格式可能是列表或 {"data": [...]} 包装
        if isinstance(raw, list):
            return raw
        if isinstance(raw, dict):
            return raw.get("data") or raw.get("positions") or []
        return []
    except urllib.error.HTTPError as exc:
        logger.warning("CLOB /data/positions HTTP %s: %s", exc.code, exc.reason)
        return None
    except urllib.error.URLError as exc:
        logger.warning("CLOB /data/positions 连接失败: %s", exc.reason)
        return None
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("CLOB /data/positions 未知错误: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Data API — /positions?user=<proxy_address>   (公开端点，无需签名)
# ---------------------------------------------------------------------------

def _fetch_data_api_positions(
    proxy_address: str,
    timeout: int = REQUEST_TIMEOUT,
) -> Optional[List[dict]]:
    """从 Data API 按 proxy_address 公开端点拉取持仓。

    成功返回原始 dict 列表，失败返回 None。
    """
    params = urllib.parse.urlencode({"user": proxy_address, "limit": 500})
    url    = f"{DATA_BASE_URL}/positions?{params}"
    req    = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "polymarket-scanner/1.0"},
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
        if isinstance(raw, list):
            return raw
        if isinstance(raw, dict):
            return raw.get("data") or raw.get("positions") or []
        return []
    except urllib.error.HTTPError as exc:
        logger.warning("Data API /positions HTTP %s: %s", exc.code, exc.reason)
        return None
    except urllib.error.URLError as exc:
        logger.warning("Data API /positions 连接失败: %s", exc.reason)
        return None
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("Data API /positions 未知错误: %s", exc)
        return None


# ---------------------------------------------------------------------------
# 原始 dict → Position 对象解析
# ---------------------------------------------------------------------------

def _parse_position(raw: dict) -> Optional[Position]:
    """把 API 返回的单条持仓 dict 转成 Position 对象，解析失败返回 None。"""
    try:
        # conditionId / marketId / market 字段名因端点而异
        market_id = str(
            raw.get("conditionId")
            or raw.get("marketId")
            or raw.get("market")
            or raw.get("market_id")
            or ""
        ).strip()

        if not market_id:
            return None

        # token_id (outcome token 的 ERC-1155 tokenId)
        token_id = str(raw.get("tokenId") or raw.get("token_id") or raw.get("asset") or "")

        # outcome label
        outcome_index = int(raw.get("outcomeIndex") or raw.get("outcome_index") or 0)
        outcome_label = str(raw.get("outcome") or raw.get("outcomeName") or
                            ("YES" if outcome_index == 0 else "NO"))

        # 持仓数量 / 成本 / 当前市价
        size         = float(raw.get("size")        or raw.get("balance")       or 0)
        avg_price    = float(raw.get("avgPrice")    or raw.get("avg_price")     or
                             raw.get("averagePrice") or 0)
        current_price = float(raw.get("currentPrice") or raw.get("price")      or
                              raw.get("lastPrice")   or avg_price)

        # 估算盈亏
        cost_basis    = size * avg_price
        market_value  = size * current_price
        unrealized_pnl = market_value - cost_basis

        question = str(raw.get("question") or raw.get("title") or raw.get("marketQuestion") or "")

        return Position(
            market_id      = market_id,
            token_id       = token_id,
            outcome        = outcome_label,
            size           = size,
            avg_price      = avg_price,
            current_price  = current_price,
            cost_basis     = cost_basis,
            market_value   = market_value,
            unrealized_pnl = unrealized_pnl,
            question       = question,
        )
    except Exception as exc:  # pylint: disable=broad-except
        logger.debug("持仓解析跳过: %s — raw=%s", exc, raw)
        return None


# ---------------------------------------------------------------------------
# PositionFetcher — 对外统一接口
# ---------------------------------------------------------------------------

class PositionFetcher:
    """查询账户当前持仓，返回已持仓的 market_id 集合。

    认证优先级：
      1. CLOB API (L1 HMAC 签名)  — 需要 private_key + signer_address
      2. Data API (public)        — 仅需 proxy_address
      3. 空集合                   — 两者均失败时静默降级，扫描照常运行

    Parameters
    ----------
    private_key_hex :
        64 位十六进制私钥。默认读取环境变量 POLY_PRIVATE_KEY。
    signer_address :
        EOA 签名地址（0x 格式）。默认读取 POLY_SIGNER_ADDR。
    proxy_address :
        Proxy 合约地址（0x 格式）。默认读取 POLY_PROXY_ADDR。
    timeout :
        HTTP 超时秒数（默认 15）。
    """

    def __init__(
        self,
        private_key_hex: Optional[str] = None,
        signer_address:  Optional[str] = None,
        proxy_address:   Optional[str] = None,
        timeout:         int = REQUEST_TIMEOUT,
    ):
        self.private_key = (private_key_hex or os.getenv("POLY_PRIVATE_KEY", "")).strip()
        self.signer_addr = (signer_address  or os.getenv("POLY_SIGNER_ADDR", "")).strip()
        self.proxy_addr  = (proxy_address   or os.getenv("POLY_PROXY_ADDR",  "")).strip()
        self.timeout     = timeout

        self._clob_available  = bool(self.private_key and self.signer_addr)
        self._data_available  = bool(self.proxy_addr)

        if not self._clob_available and not self._data_available:
            logger.warning(
                "PositionFetcher: 未配置任何钱包凭证，持仓查询已禁用。"
                "请设置环境变量 POLY_PRIVATE_KEY + POLY_SIGNER_ADDR（或 POLY_PROXY_ADDR）。"
            )

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def fetch_positions(self) -> List[Position]:
        """返回账户全部持仓列表（Position 对象）。

        先尝试 CLOB API（带签名），失败则降级 Data API（公开），
        两者均失败返回空列表，不抛异常。
        """
        raw_list: Optional[List[dict]] = None

        # 1️⃣ 尝试 CLOB API
        if self._clob_available:
            logger.info("持仓查询: 尝试 CLOB API (L1 HMAC 认证)…")
            raw_list = _fetch_clob_positions(
                self.private_key, self.signer_addr, self.timeout
            )
            if raw_list is not None:
                logger.info("✅ CLOB API 持仓查询成功，共 %d 条", len(raw_list))

        # 2️⃣ 降级 Data API
        if raw_list is None and self._data_available:
            logger.info("持仓查询: 降级到 Data API (proxy_addr=%s)…", self.proxy_addr[:10] + "…")
            raw_list = _fetch_data_api_positions(self.proxy_addr, self.timeout)
            if raw_list is not None:
                logger.info("✅ Data API 持仓查询成功，共 %d 条", len(raw_list))

        # 3️⃣ 两者均失败
        if raw_list is None:
            logger.warning("⚠️  持仓 API 均不可用，将以空持仓运行（扫描结果可能含重复推送）。")
            return []

        positions = [p for raw in raw_list if (p := _parse_position(raw)) is not None]
        logger.info("解析完成: %d 条有效持仓", len(positions))
        return positions

    def held_market_ids(self) -> Set[str]:
        """返回所有已持仓市场的 market_id 集合（用于去重过滤）。"""
        return {p.market_id for p in self.fetch_positions()}

    def print_summary(self) -> None:
        """在控制台打印当前持仓概览表。"""
        positions = self.fetch_positions()
        if not positions:
            print("📭 当前无持仓（或 API 不可用）")
            return

        total_cost   = sum(p.cost_basis    for p in positions)
        total_value  = sum(p.market_value  for p in positions)
        total_pnl    = sum(p.unrealized_pnl for p in positions)
        pnl_sign     = "+" if total_pnl >= 0 else ""

        print(f"\n{'─'*72}")
        print(f"  📊  当前持仓概览  ({len(positions)} 个市场)")
        print(f"{'─'*72}")
        print(f"  {'市场 ID':<20} {'方向':<5} {'数量':>8} {'均价':>7} {'现价':>7} {'盈亏':>10}")
        print(f"{'─'*72}")

        for p in sorted(positions, key=lambda x: x.unrealized_pnl, reverse=True):
            pnl_str  = f"{'+' if p.unrealized_pnl >= 0 else ''}{p.unrealized_pnl:.2f}"
            question_short = (p.question[:28] + "…") if len(p.question) > 30 else p.question
            label    = question_short or p.market_id[:20]
            print(
                f"  {label:<30} {p.outcome:<5} {p.size:>8.2f} "
                f"${p.avg_price:>5.3f}  ${p.current_price:>5.3f}  "
                f"${pnl_str:>9}"
            )

        print(f"{'─'*72}")
        print(f"  总成本: ${total_cost:.2f}  | 市值: ${total_value:.2f}  | "
              f"未实现盈亏: ${pnl_sign}{total_pnl:.2f}")
        print(f"{'─'*72}\n")
