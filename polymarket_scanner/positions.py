"""positions.py — 实时拉取 Polymarket 账户持仓。

接口：https://data-api.polymarket.com/positions?user=0x...
完全公开，无需 API Key，只需要你的钱包地址。

环境变量配置（在 .env 文件里填一行即可）：
  POLY_WALLET_ADDRESS=0x你的钱包地址

对外接口
--------
  fetch_positions(timeout, logger) -> List[Market]
      实时拉取账户当前持仓，转成 Market 对象。
      无持仓返回空列表。
      地址缺失或 API 出错时抛 PositionFetchError。
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, List, Optional

from .models import Market

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
DATA_API_BASE     = "https://data-api.polymarket.com"
GAMMA_BASE_URL    = "https://gamma-api.polymarket.com"
DEFAULT_TIMEOUT   = 15


# ---------------------------------------------------------------------------
# 错误类型
# ---------------------------------------------------------------------------
class PositionFetchError(Exception):
    """API 调用失败时抛出。"""

class AuthError(PositionFetchError):
    """钱包地址缺失。"""


# ---------------------------------------------------------------------------
# 读取 .env（不依赖 python-dotenv）
# ---------------------------------------------------------------------------
def _try_load_dotenv() -> None:
    candidates = [
        os.path.join(os.getcwd(), ".env"),
        os.path.join(os.path.dirname(__file__), "..", ".env"),
    ]
    for path in candidates:
        path = os.path.normpath(path)
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, _, v = line.partition("=")
                    k = k.strip()
                    v = v.strip().strip('"').strip("'")
                    if k and k not in os.environ:
                        os.environ[k] = v
            break


def _load_wallet_address() -> str:
    _try_load_dotenv()
    addr = os.environ.get("POLY_WALLET_ADDRESS", "").strip()
    if not addr:
        raise AuthError(
            "缺少 POLY_WALLET_ADDRESS 环境变量。\n"
            "请在 .env 文件里添加一行：\n"
            "  POLY_WALLET_ADDRESS=0x你的Polymarket钱包地址\n"
            "（在 polymarket.com/settings 可以查到你的地址）"
        )
    if not addr.startswith("0x") or len(addr) != 42:
        raise AuthError(
            f"POLY_WALLET_ADDRESS 格式不对（收到：{addr!r}）。\n"
            "正确格式：0x 开头 + 40 位十六进制，共 42 个字符。"
        )
    return addr.lower()


# ---------------------------------------------------------------------------
# Data API — 拉取持仓（公开接口，无需认证）
# ---------------------------------------------------------------------------
def _fetch_raw_positions(wallet: str, timeout: int) -> List[dict]:
    """GET https://data-api.polymarket.com/positions?user=<wallet>"""
    params = urllib.parse.urlencode({
        "user":          wallet,
        "sizeThreshold": "0.01",   # 过滤掉极小仓位噪音
        "limit":         500,
        "sortBy":        "CURRENT",
        "sortDirection": "DESC",
    })
    url = f"{DATA_API_BASE}/positions?{params}"
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "polymarket-scanner/2.0"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8")[:300]
        except Exception:
            pass
        raise PositionFetchError(f"Data API HTTP {e.code}: {e.reason}. {body}") from e
    except urllib.error.URLError as e:
        raise PositionFetchError(f"Data API 连接失败: {e.reason}") from e
    except Exception as e:
        raise PositionFetchError(f"Data API 未知错误: {e}") from e


# ---------------------------------------------------------------------------
# Gamma API — 补充流动性 / 成交量 / 分类
# ---------------------------------------------------------------------------
def _gamma_fetch_by_condition_ids(
    condition_ids: List[str],
    timeout: int,
    logger: logging.Logger,
) -> Dict[str, dict]:
    if not condition_ids:
        return {}
    params = urllib.parse.urlencode(
        [("conditionId", cid) for cid in condition_ids[:50]]  # Gamma 限制
    )
    url = f"{GAMMA_BASE_URL}/markets?{params}"
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "polymarket-scanner/2.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw_list = json.loads(resp.read().decode("utf-8"))
        if isinstance(raw_list, dict):
            raw_list = raw_list.get("data") or raw_list.get("markets") or []
    except Exception as e:
        logger.warning("Gamma 补充数据失败（%s），用 Data API 的价格字段代替。", e)
        return {}

    mapping: Dict[str, dict] = {}
    for item in raw_list:
        cid = str(item.get("conditionId") or item.get("id") or "").strip()
        if cid:
            mapping[cid] = item
    return mapping


# ---------------------------------------------------------------------------
# 持仓记录 → Market 对象
# ---------------------------------------------------------------------------
def _position_to_market(
    pos: dict,
    gamma: Optional[dict],
    logger: logging.Logger,
) -> Optional[Market]:
    """
    Data API /positions 字段说明：
      conditionId   市场 condition ID
      title         市场标题
      outcome       "Yes" / "No"
      size          持有 token 数量
      avgPrice      平均成本价（0..1）
      curPrice      当前市场价（0..1）
      currentValue  当前市值 USD
      cashPnl       已实现+未实现盈亏 USD
      endDate       到期时间
    """
    try:
        condition_id = str(pos.get("conditionId") or "").strip()
        question     = str(pos.get("title") or condition_id).strip()
        outcome      = str(pos.get("outcome") or "Yes").lower()

        # 当前价格
        cur_price = float(pos.get("curPrice") or pos.get("avgPrice") or 0.5)
        avg_price = float(pos.get("avgPrice") or cur_price)

        # NO 方向翻转（持有 NO token 时，价格是 NO 的价格）
        if outcome == "no":
            cur_price = 1.0 - cur_price
            avg_price = 1.0 - avg_price

        cur_price = max(0.001, min(0.999, cur_price))
        avg_price = max(0.001, min(0.999, avg_price))
        price_change = cur_price - avg_price

        # 到期天数
        days_to_expiry = 30
        end_date_str = pos.get("endDate") or ""
        if end_date_str:
            try:
                end_dt = datetime.datetime.fromisoformat(
                    end_date_str.replace("Z", "+00:00")
                )
                now = datetime.datetime.now(datetime.timezone.utc)
                days_to_expiry = max(0, (end_dt - now).days)
            except Exception:
                pass

        # 流动性 / 成交量 / 分类 来自 Gamma
        liquidity       = 0.0
        volume_24h      = 0.0
        volume_prev_24h = 0.0
        category        = "general"

        if gamma:
            liquidity       = float(gamma.get("liquidity")  or 0)
            volume_24h      = float(gamma.get("volume24hr") or 0)
            volume_prev_24h = float(gamma.get("volume1wk")  or 0) / 7
            tags = gamma.get("tags") or []
            if isinstance(tags, list) and tags:
                category = (tags[0].get("label") if isinstance(tags[0], dict)
                            else str(tags[0])).lower()
            else:
                category = str(gamma.get("category") or "general").lower()
            # Gamma 价格更准确时覆盖
            outcomes = gamma.get("outcomePrices") or []
            if isinstance(outcomes, list):
                idx = 1 if outcome == "no" and len(outcomes) >= 2 else 0
                if len(outcomes) > idx:
                    gp = max(0.001, min(0.999, float(outcomes[idx])))
                    price_change = gp - cur_price
                    cur_price = gp

        logger.debug(
            "持仓 %s | %s | 当前价=%.3f | PnL=%.2f USD | 到期=%d天",
            condition_id[:12] + "…", outcome,
            cur_price,
            float(pos.get("cashPnl") or 0),
            days_to_expiry,
        )

        return Market(
            market_id        = condition_id,
            question         = question,
            category         = category,
            price            = cur_price,
            liquidity        = liquidity,
            volume_24h       = volume_24h,
            volume_prev_24h  = volume_prev_24h,
            price_change_24h = price_change,
            days_to_expiry   = days_to_expiry,
            true_prob        = cur_price,  # AI Oracle 后续可覆盖
        )

    except Exception as e:
        logger.warning("解析持仓失败，跳过：%s（原始：%s）", e, str(pos)[:120])
        return None


# ---------------------------------------------------------------------------
# 公开接口
# ---------------------------------------------------------------------------
def fetch_positions(
    timeout: int = DEFAULT_TIMEOUT,
    logger: Optional[logging.Logger] = None,
) -> List[Market]:
    """实时拉取账户持仓，返回 Market 列表。每次调用均发起真实 HTTP 请求，无缓存。

    需要 .env 中设置：
        POLY_WALLET_ADDRESS=0x你的钱包地址

    Returns
    -------
    List[Market]  当前所有持仓，空账户返回 []

    Raises
    ------
    AuthError           地址缺失或格式错误
    PositionFetchError  网络或 API 异常
    """
    if logger is None:
        logger = logging.getLogger(__name__)

    wallet = _load_wallet_address()
    logger.info("📡 正在拉取账户持仓（地址: %s…%s）", wallet[:6], wallet[-4:])

    raw_list = _fetch_raw_positions(wallet, timeout)

    if not raw_list:
        logger.info("ℹ️  当前账户无持仓。")
        return []

    logger.info("✅ Data API 返回 %d 条持仓", len(raw_list))

    # 批量查 Gamma 补充数据
    condition_ids = [str(p.get("conditionId") or "").strip() for p in raw_list if p.get("conditionId")]
    gamma_map = _gamma_fetch_by_condition_ids(condition_ids, timeout, logger)
    if gamma_map:
        logger.info("📊 Gamma 补充了 %d/%d 条市场数据", len(gamma_map), len(condition_ids))

    markets: List[Market] = []
    for pos in raw_list:
        cid   = str(pos.get("conditionId") or "").strip()
        gamma = gamma_map.get(cid)
        m = _position_to_market(pos, gamma, logger)
        if m is not None:
            markets.append(m)

    logger.info("📋 有效持仓：%d 个", len(markets))
    return markets


def print_positions_summary(
    timeout: int = DEFAULT_TIMEOUT,
    logger: Optional[logging.Logger] = None,
) -> None:
    """快速打印持仓概览表。"""
    if logger is None:
        logger = logging.getLogger(__name__)

    positions = fetch_positions(timeout=timeout, logger=logger)

    if not positions:
        print("当前账户无持仓。")
        return

    print(f"\n{'='*72}")
    print(f"  实时账户持仓  —  {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*72}")
    fmt = "{:<5} {:<44} {:>8} {:>12} {:>8}"
    print(fmt.format("#", "市场（截取44字符）", "当前价", "流动性($)", "到期天"))
    print("-" * 72)
    for i, m in enumerate(positions, 1):
        q = (m.question[:41] + "...") if len(m.question) > 44 else m.question
        print(fmt.format(
            i,
            q,
            f"{m.price:.3f}",
            f"{m.liquidity:,.0f}" if m.liquidity else "N/A",
            m.days_to_expiry,
        ))
    print(f"{'='*72}\n")
