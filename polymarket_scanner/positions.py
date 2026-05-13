"""positions.py — Polymarket 持仓查询模块

⚡ 简化真相：查询持仓 **不需要任何签名或私钥**。
   Data API  GET /positions?user=<address>  是完全公开端点。

架构
----
  优先端点：Data API  https://data-api.polymarket.com/positions?user=<signer_address>
  降级端点：（同一端点，用 proxy_address 再试一次）
  最终降级：空列表，扫描照常运行，只是不做持仓去重

API 返回的关键字段（已验证）
--------------------------
  conditionId   — 市场 ID，与 Market.market_id 对应
  asset         — outcome token ID (ERC-1155)
  outcome       — "Yes" / "No"
  size          — 持有份额数
  avgPrice      — 平均买入价 (0–1)
  curPrice      — 当前市价 (0–1)
  cashPnl       — 绝对盈亏 USD
  percentPnl    — 百分比盈亏
  title         — 市场标题
  initialValue  — 买入时总成本 USD
  currentValue  — 当前市值 USD

关于 Relayer API Key
--------------------
  RELAYER_API_KEY 是用于提交交易（下单/撤单）的，与查询持仓无关。
  如果你将来需要程序化下单，可通过以下 Header 使用它：
      RELAYER_API_KEY: <your-api-key>
      RELAYER_API_KEY_ADDRESS: 0x1139Fe3b54cF43A2aAD1E6E8C09aedf73E5270bf

环境变量（只需设置地址即可）
-----------------------------
  POLY_ADDRESS   : 你的 Signer / Proxy 地址（0x 格式）
                   例：0x1139Fe3b54cF43A2aAD1E6E8C09aedf73E5270bf

用法
----
    from polymarket_scanner.positions import PositionFetcher

    fetcher  = PositionFetcher()                    # 读取 POLY_ADDRESS 环境变量
    # 或者
    fetcher  = PositionFetcher(address="0x1139...")

    held_ids = fetcher.held_market_ids()            # set[str]
    fetcher.print_summary()                         # 打印持仓概览表
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import List, Optional, Set

from .models import Position

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# API 端点
# ---------------------------------------------------------------------------
DATA_API_BASE   = "https://data-api.polymarket.com"
REQUEST_TIMEOUT = 15   # 秒


# ---------------------------------------------------------------------------
# 核心网络请求（公开端点，无需任何认证）
# ---------------------------------------------------------------------------

def _fetch_positions_for_address(
    address: str,
    timeout: int = REQUEST_TIMEOUT,
) -> Optional[List[dict]]:
    """向 Data API 请求指定地址的当前持仓。

    GET https://data-api.polymarket.com/positions?user=<address>&limit=500

    成功返回原始 dict 列表，失败返回 None（调用方决定是否降级）。
    此端点完全公开，不需要任何认证 header。
    """
    params = urllib.parse.urlencode({
        "user":          address,
        "limit":         500,
        "sizeThreshold": 0,      # 0 = 返回全部，本地再过滤零仓位
        "sortBy":        "CURRENT",
        "sortDirection": "DESC",
    })
    url = f"{DATA_API_BASE}/positions?{params}"
    req = urllib.request.Request(
        url,
        headers={
            "Accept":     "application/json",
            "User-Agent": "polymarket-scanner/1.0",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = json.loads(resp.read().decode("utf-8"))

        # 返回格式为列表或 {"data": [...]}
        if isinstance(raw, list):
            return raw
        if isinstance(raw, dict):
            return raw.get("data") or raw.get("positions") or []
        return []

    except urllib.error.HTTPError as exc:
        logger.warning("Data API /positions HTTP %s (%s): %s", exc.code, address[:10], exc.reason)
        return None
    except urllib.error.URLError as exc:
        logger.warning("Data API /positions 连接失败: %s", exc.reason)
        return None
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("Data API /positions 未知错误: %s", exc)
        return None


# ---------------------------------------------------------------------------
# 原始 dict → Position 对象
# ---------------------------------------------------------------------------

def _parse_position(raw: dict) -> Optional[Position]:
    """把 Data API 返回的单条持仓 dict 转成 Position 对象。

    字段映射（来自官方 OpenAPI spec）：
      conditionId  → market_id
      asset        → token_id
      outcome      → outcome
      size         → size
      avgPrice     → avg_price
      curPrice     → current_price
      initialValue → cost_basis   （totalBought 或 size*avgPrice 作备选）
      currentValue → market_value （size*curPrice 作备选）
      cashPnl      → unrealized_pnl
      title        → question
    """
    try:
        market_id = str(raw.get("conditionId") or raw.get("market_id") or "").strip()
        if not market_id:
            return None

        token_id      = str(raw.get("asset")    or raw.get("tokenId") or "")
        outcome_label = str(raw.get("outcome")  or "YES")
        size          = float(raw.get("size")   or 0)

        if size <= 0:
            return None   # 已清仓，跳过

        avg_price     = float(raw.get("avgPrice")     or raw.get("avg_price") or 0)
        current_price = float(raw.get("curPrice")     or raw.get("currentPrice") or avg_price)

        # 成本 / 市值 / 盈亏
        cost_basis    = float(raw.get("initialValue") or raw.get("totalBought") or size * avg_price)
        market_value  = float(raw.get("currentValue") or size * current_price)
        unrealized_pnl = float(raw.get("cashPnl")    or (market_value - cost_basis))

        question = str(
            raw.get("title") or raw.get("question") or raw.get("marketQuestion") or ""
        )

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
    """查询账户当前持仓，提供持仓 market_id 集合供去重使用。

    认证说明
    --------
    Polymarket Data API 的持仓查询端点是 **公开** 的，只需传入钱包地址即可。
    无需私钥、无需 API Key、无需签名。

    你提供的 Relayer API Key 用于提交交易（下单），与查询持仓无关。

    Parameters
    ----------
    address :
        Signer 地址 或 Proxy 地址（0x 格式，40 位十六进制）。
        优先读取此参数；未传则读取环境变量 POLY_ADDRESS。
    timeout :
        HTTP 超时秒数（默认 15）。

    示例
    ----
        # 方式 1：环境变量
        os.environ["POLY_ADDRESS"] = "0x1139Fe3b54cF43A2aAD1E6E8C09aedf73E5270bf"
        fetcher = PositionFetcher()

        # 方式 2：直接传参
        fetcher = PositionFetcher(address="0x1139Fe3b54cF43A2aAD1E6E8C09aedf73E5270bf")

        held = fetcher.held_market_ids()   # set[str]
        fetcher.print_summary()
    """

    # Proxy 地址（页面上显示的主地址，持仓存放在这里）
    KNOWN_PROXY  = "0xbF5B386FCC49FFe6d1Fc3dA202cf8A799043Dc6b"
    # Signer 地址（Relayer API Key Address，用于下单签名）
    KNOWN_SIGNER = "0x1139Fe3b54cF43A2aAD1E6E8C09aedf73E5270bf"

    def __init__(
        self,
        address: Optional[str] = None,
        timeout: int = REQUEST_TIMEOUT,
    ):
        # 地址解析优先级：参数 > 环境变量 > Proxy 地址（持仓在这里）> Signer 地址
        resolved = (
            address
            or os.getenv("POLY_ADDRESS", "")
            or os.getenv("POLY_PROXY_ADDR", "")
            or os.getenv("POLY_SIGNER_ADDR", "")
        ).strip()

        self.address = resolved or self.KNOWN_PROXY
        self.timeout = timeout

        if not resolved:
            logger.info(
                "PositionFetcher: 未设置 POLY_ADDRESS，使用内置 Proxy 地址 %s",
                self.KNOWN_PROXY[:14] + "…",
            )

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def fetch_positions(self) -> List[Position]:
        """返回账户全部有效持仓列表（size > 0 的 Position 对象）。

        流程：
          1. 用 self.address 查询 Data API
          2. 失败则返回空列表（不抛异常，扫描照常运行）
        """
        logger.info("📊 查询持仓: %s…", self.address[:14] + "…")
        raw_list = _fetch_positions_for_address(self.address, self.timeout)

        if raw_list is None:
            logger.warning(
                "⚠️  持仓 API 不可用（地址: %s），将以空持仓运行。", self.address[:14] + "…"
            )
            return []

        positions = [p for raw in raw_list if (p := _parse_position(raw)) is not None]

        if positions:
            logger.info("✅ 持仓查询成功：%d 个市场有持仓", len(positions))
        else:
            logger.info("📭 当前账户无持仓（或仓位 < 0.01 份额）")

        return positions

    def held_market_ids(self) -> Set[str]:
        """返回所有已持仓市场的 conditionId 集合。

        用于 MarketScanner 在策略评估前过滤掉这些市场，防止重复推送。
        """
        return {p.market_id for p in self.fetch_positions()}

    def print_summary(self) -> None:
        """在控制台打印当前持仓概览表（含盈亏汇总）。"""
        positions = self.fetch_positions()

        if not positions:
            print(f"\n📭 账户 {self.address[:14]}… 当前无持仓\n")
            return

        total_cost    = sum(p.cost_basis     for p in positions)
        total_value   = sum(p.market_value   for p in positions)
        total_pnl     = sum(p.unrealized_pnl for p in positions)
        pnl_color     = "🟢" if total_pnl >= 0 else "🔴"

        W = 74
        print(f"\n{'─' * W}")
        print(f"  📊  持仓概览  —  {self.address[:14]}…  ({len(positions)} 个市场)")
        print(f"{'─' * W}")
        print(f"  {'标题':<32} {'方向':<4} {'份额':>7} {'均价':>6} {'现价':>6} {'盈亏 USD':>10}")
        print(f"{'─' * W}")

        for p in sorted(positions, key=lambda x: x.unrealized_pnl, reverse=True):
            label   = (p.question[:31] + "…") if len(p.question) > 33 else p.question
            label   = label or p.market_id[:14] + "…"
            sign    = "+" if p.unrealized_pnl >= 0 else ""
            pnl_str = f"{sign}{p.unrealized_pnl:.2f}"
            print(
                f"  {label:<33} {p.outcome:<4} {p.size:>7.2f}"
                f"  {p.avg_price:>5.3f}  {p.current_price:>5.3f}  ${pnl_str:>9}"
            )

        print(f"{'─' * W}")
        pnl_sign = "+" if total_pnl >= 0 else ""
        print(
            f"  {pnl_color} 总成本: ${total_cost:.2f}  |  "
            f"当前市值: ${total_value:.2f}  |  "
            f"未实现盈亏: ${pnl_sign}{total_pnl:.2f}"
        )
        print(f"{'─' * W}\n")
