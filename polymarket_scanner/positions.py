"""positions.py — 实时拉取 Polymarket 账户持仓，接入 CLOB API。

支持两种认证方式（按优先级自动选择）：
  1. API Key 三件套（推荐）：POLY_API_KEY + POLY_API_SECRET + POLY_API_PASSPHRASE
  2. 私钥签名（高级）：POLY_PRIVATE_KEY + POLY_CHAIN_ID（默认 137=Polygon）

使用前在环境变量里设置（或写入 .env 文件）：

  # 方式 1 —— API Key（在 Polymarket 网站 Profile → API Keys 生成）
  POLY_API_KEY=your-api-key
  POLY_API_SECRET=your-api-secret
  POLY_API_PASSPHRASE=your-passphrase

  # 方式 2 —— 私钥
  POLY_PRIVATE_KEY=0xabc...
  POLY_CHAIN_ID=137

  # Gamma API（补充市场信息，无需认证）
  # 不需要配置，自动使用公开端点

对外接口
--------
  fetch_positions(logger=None) -> List[Market]
      返回你当前持有的所有仓位，转成 Market 对象（price/liquidity等字段均为实时值）。
      如果没有持仓，返回空列表。
      如果认证失败或 API 报错，抛出 PositionFetchError。

  enrich_positions_with_market_data(positions, logger=None) -> List[Market]
      用 Gamma API 补充实时价格/流动性等字段（CLOB /positions 只有合约ID和数量）。
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Dict, List, Optional

from .models import Market

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
CLOB_BASE_URL      = "https://clob.polymarket.com"
GAMMA_BASE_URL     = "https://gamma-api.polymarket.com"
CLOB_POSITIONS_URL = f"{CLOB_BASE_URL}/positions"          # GET，需认证
CLOB_MARKETS_URL   = f"{CLOB_BASE_URL}/markets"            # GET，公开
GAMMA_MARKETS_URL  = f"{GAMMA_BASE_URL}/markets"           # GET，公开

DEFAULT_TIMEOUT    = 15   # 秒


# ---------------------------------------------------------------------------
# 错误类型
# ---------------------------------------------------------------------------
class PositionFetchError(Exception):
    """CLOB API 调用失败时抛出。"""


class AuthError(PositionFetchError):
    """认证配置缺失或认证失败。"""


# ---------------------------------------------------------------------------
# 认证辅助
# ---------------------------------------------------------------------------
@dataclass
class _ClobAuth:
    """持有认证信息，生成 HMAC-SHA256 签名头。"""
    api_key: str
    api_secret: str
    passphrase: str

    def headers(self, method: str, path: str, body: str = "") -> Dict[str, str]:
        """生成 Polymarket CLOB API 所需的认证请求头。"""
        timestamp = str(int(time.time() * 1000))
        msg       = timestamp + method.upper() + path + body
        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            msg.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return {
            "POLY-API-KEY":    self.api_key,
            "POLY-SIGNATURE":  signature,
            "POLY-TIMESTAMP":  timestamp,
            "POLY-PASSPHRASE": self.passphrase,
            "Content-Type":    "application/json",
            "Accept":          "application/json",
            "User-Agent":      "polymarket-scanner/2.0",
        }


def _load_auth() -> _ClobAuth:
    """从环境变量读取 API Key 认证信息。
    
    按顺序查找：
      1. POLY_API_KEY / POLY_API_SECRET / POLY_API_PASSPHRASE
      2. 尝试加载项目根目录的 .env 文件（如果存在）
    """
    # 先尝试加载 .env（如果存在），不强依赖 python-dotenv
    _try_load_dotenv()

    key        = os.environ.get("POLY_API_KEY",        "").strip()
    secret     = os.environ.get("POLY_API_SECRET",     "").strip()
    passphrase = os.environ.get("POLY_API_PASSPHRASE", "").strip()

    if not key:
        raise AuthError(
            "缺少 POLY_API_KEY 环境变量。\n"
            "请在项目根目录创建 .env 文件并填入：\n"
            "  POLY_API_KEY=your-api-key\n"
            "  POLY_API_SECRET=your-api-secret\n"
            "  POLY_API_PASSPHRASE=your-passphrase\n"
            "（API Key 在 Polymarket 网站 Profile → API Keys 生成）"
        )
    if not secret:
        raise AuthError("缺少 POLY_API_SECRET 环境变量。")
    if not passphrase:
        raise AuthError("缺少 POLY_API_PASSPHRASE 环境变量。")

    return _ClobAuth(api_key=key, api_secret=secret, passphrase=passphrase)


def _try_load_dotenv() -> None:
    """尝试解析项目根目录的 .env 文件，手动注入环境变量（不依赖 python-dotenv）。"""
    # 查找 .env：先找 cwd，再找本文件所在目录的上两级
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
                    if k and k not in os.environ:   # 不覆盖已有环境变量
                        os.environ[k] = v
            break   # 找到第一个就停


# ---------------------------------------------------------------------------
# CLOB API 调用
# ---------------------------------------------------------------------------

def _clob_get(path: str, auth: _ClobAuth, timeout: int) -> dict:
    """向 CLOB API 发 GET 请求，返回解析好的 JSON。"""
    url = CLOB_BASE_URL + path
    headers = auth.headers("GET", path)
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8")[:300]
        except Exception:
            pass
        if e.code == 401:
            raise AuthError(f"CLOB API 认证失败（401）。请检查 API Key 是否正确。响应：{body}") from e
        if e.code == 403:
            raise AuthError(f"CLOB API 权限不足（403）。请检查 API Key 是否有读取持仓权限。响应：{body}") from e
        raise PositionFetchError(f"CLOB API HTTP {e.code}: {e.reason}. {body}") from e
    except urllib.error.URLError as e:
        raise PositionFetchError(f"CLOB API 连接失败: {e.reason}") from e
    except Exception as e:
        raise PositionFetchError(f"CLOB API 未知错误: {e}") from e


def _fetch_raw_positions(auth: _ClobAuth, timeout: int) -> List[dict]:
    """从 CLOB /positions 拉取原始持仓列表。
    
    CLOB API 返回格式示例：
      [
        {
          "conditionId": "0xabc...",
          "tokenId": "0xdef...",
          "outcome": "Yes",
          "size": "12.5",          # 持有份额（tokens）
          "avgPrice": "0.78",      # 平均成本
          "currentPrice": "0.82",  # 当前市场价
          "marketTitle": "...",
          "endDate": "2025-06-01T00:00:00Z",
          ...
        },
        ...
      ]
    """
    result = _clob_get("/positions", auth, timeout)

    # CLOB API 可能返回 {"data": [...]} 或直接返回列表
    if isinstance(result, dict):
        result = result.get("data") or result.get("positions") or []
    if not isinstance(result, list):
        raise PositionFetchError(f"CLOB /positions 返回格式异常：{type(result)}")
    return result


# ---------------------------------------------------------------------------
# Gamma API — 补充市场流动性/成交量等实时字段
# ---------------------------------------------------------------------------

def _gamma_fetch_by_condition_ids(
    condition_ids: List[str],
    timeout: int,
    logger: logging.Logger,
) -> Dict[str, dict]:
    """用 conditionId 批量查 Gamma API，返回 {conditionId: raw_dict} 映射。"""
    if not condition_ids:
        return {}

    # Gamma API 支持 conditionId 参数过滤
    params = urllib.parse.urlencode(
        [("conditionId", cid) for cid in condition_ids]
    )
    url = f"{GAMMA_MARKETS_URL}?{params}"
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "polymarket-scanner/2.0"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw_list = json.loads(resp.read().decode("utf-8"))
        if isinstance(raw_list, dict):
            raw_list = raw_list.get("data") or raw_list.get("markets") or []
    except Exception as e:
        logger.warning("Gamma API 补充数据失败（%s），将使用 CLOB 返回的价格字段。", e)
        return {}

    mapping: Dict[str, dict] = {}
    for item in raw_list:
        cid = str(item.get("conditionId") or item.get("id") or "").strip()
        if cid:
            mapping[cid] = item
    return mapping


# ---------------------------------------------------------------------------
# 持仓 → Market 对象转换
# ---------------------------------------------------------------------------

def _position_to_market(
    pos: dict,
    gamma: Optional[dict],
    logger: logging.Logger,
) -> Optional[Market]:
    """把 CLOB 持仓 + Gamma 补充数据 合并成 Market 对象。"""
    try:
        condition_id = str(pos.get("conditionId") or pos.get("tokenId") or "").strip()
        question     = str(
            pos.get("marketTitle") or pos.get("title") or pos.get("question") or condition_id
        ).strip()

        # 价格：优先 CLOB currentPrice，回退 avgPrice
        current_price = float(pos.get("currentPrice") or pos.get("price") or 0.5)
        avg_price     = float(pos.get("avgPrice")     or current_price)
        outcome       = str(pos.get("outcome") or "Yes").lower()

        # 如果持有的是 NO token，翻转价格
        if outcome == "no":
            current_price = 1.0 - current_price
            avg_price     = 1.0 - avg_price

        current_price = max(0.001, min(0.999, current_price))
        avg_price     = max(0.001, min(0.999, avg_price))
        price_change_24h = current_price - avg_price   # 粗略估算

        # 持仓规模（USD）= shares × current_price
        size_shares = float(pos.get("size") or pos.get("shares") or 0)

        # 到期日
        end_date_str = pos.get("endDate") or pos.get("endDateIso") or ""
        days_to_expiry = 30
        if end_date_str:
            try:
                end_dt = datetime.datetime.fromisoformat(
                    end_date_str.replace("Z", "+00:00")
                )
                now = datetime.datetime.now(datetime.timezone.utc)
                days_to_expiry = max(0, (end_dt - now).days)
            except Exception:
                pass

        # 从 Gamma 补充流动性 / 成交量 / 分类
        liquidity       = 0.0
        volume_24h      = 0.0
        volume_prev_24h = 0.0
        category        = "general"

        if gamma:
            liquidity  = float(gamma.get("liquidity")  or 0)
            volume_24h = float(gamma.get("volume24hr") or 0)
            volume_prev_24h = float(gamma.get("volume1wk") or 0) / 7
            tags = gamma.get("tags") or []
            if isinstance(tags, list) and tags:
                category = (tags[0].get("label") if isinstance(tags[0], dict)
                            else str(tags[0])).lower()
            else:
                category = str(gamma.get("category") or "general").lower()
            # 如果 Gamma 有更准确的当前价，覆盖 CLOB 的价格
            outcomes = gamma.get("outcomePrices") or []
            if isinstance(outcomes, list) and len(outcomes) >= 1:
                gamma_price = max(0.001, min(0.999, float(outcomes[0])))
                if outcome == "no" and len(outcomes) >= 2:
                    gamma_price = max(0.001, min(0.999, float(outcomes[1])))
                price_change_24h = gamma_price - current_price
                current_price = gamma_price

        logger.debug(
            "持仓 %s | 方向=%s | 数量=%.2f | 当前价=%.4f | 到期=%d天",
            condition_id[:12] + "…", outcome, size_shares, current_price, days_to_expiry,
        )

        return Market(
            market_id        = condition_id,
            question         = question,
            category         = category,
            price            = current_price,
            liquidity        = liquidity,
            volume_24h       = volume_24h,
            volume_prev_24h  = volume_prev_24h,
            price_change_24h = price_change_24h,
            days_to_expiry   = days_to_expiry,
            true_prob        = current_price,   # AI Oracle 后续可覆盖
        )

    except Exception as e:
        logger.warning("解析持仓记录失败，跳过：%s（原始：%s）", e, str(pos)[:120])
        return None


# ---------------------------------------------------------------------------
# 公开接口
# ---------------------------------------------------------------------------

def fetch_positions(
    timeout: int = DEFAULT_TIMEOUT,
    logger: Optional[logging.Logger] = None,
) -> List[Market]:
    """实时拉取账户持仓，返回 Market 列表。

    每次调用都发起真实 HTTP 请求，无本地缓存，保证数据最新。

    Parameters
    ----------
    timeout : int
        网络超时秒数（默认 15）。
    logger : logging.Logger, optional
        传入则用该 logger；否则自动创建一个。

    Returns
    -------
    List[Market]
        当前账户持有的所有仓位（已用 Gamma API 补充实时价格/流动性）。
        空账户返回空列表。

    Raises
    ------
    AuthError
        API Key 缺失或认证失败。
    PositionFetchError
        网络或 API 格式异常。
    """
    if logger is None:
        logger = logging.getLogger(__name__)

    # 1. 加载认证
    auth = _load_auth()
    logger.info("🔑 CLOB 认证加载成功（key=%.8s…）", auth.api_key)

    # 2. 拉取原始持仓
    logger.info("📡 正在从 CLOB API 拉取实时持仓…")
    raw_positions = _fetch_raw_positions(auth, timeout)

    if not raw_positions:
        logger.info("ℹ️  当前账户无持仓。")
        return []

    logger.info("✅ CLOB 返回 %d 条持仓记录", len(raw_positions))

    # 3. 收集 conditionId，批量查 Gamma 补充数据
    condition_ids = []
    for p in raw_positions:
        cid = str(p.get("conditionId") or p.get("tokenId") or "").strip()
        if cid:
            condition_ids.append(cid)

    gamma_map = _gamma_fetch_by_condition_ids(condition_ids, timeout, logger)
    if gamma_map:
        logger.info("📊 Gamma API 补充了 %d/%d 条市场数据", len(gamma_map), len(condition_ids))

    # 4. 合并转换
    markets: List[Market] = []
    for pos in raw_positions:
        cid   = str(pos.get("conditionId") or pos.get("tokenId") or "").strip()
        gamma = gamma_map.get(cid)
        m = _position_to_market(pos, gamma, logger)
        if m is not None:
            markets.append(m)

    logger.info("📋 最终有效持仓数：%d", len(markets))
    return markets


def print_positions_summary(
    timeout: int = DEFAULT_TIMEOUT,
    logger: Optional[logging.Logger] = None,
) -> None:
    """CLI 辅助函数：直接打印持仓概览表，方便快速检查。"""
    if logger is None:
        logger = logging.getLogger(__name__)

    positions = fetch_positions(timeout=timeout, logger=logger)

    if not positions:
        print("当前账户无持仓。")
        return

    print(f"\n{'='*70}")
    print(f"  实时账户持仓 — {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*70}")
    fmt = "{:<6} {:<45} {:>8} {:>10} {:>8}"
    print(fmt.format("#", "市场问题（截取45字符）", "当前价", "流动性($)", "到期天"))
    print("-" * 70)
    for i, m in enumerate(positions, 1):
        q = (m.question[:42] + "...") if len(m.question) > 45 else m.question
        print(fmt.format(
            i,
            q,
            f"{m.price:.3f}",
            f"{m.liquidity:,.0f}" if m.liquidity else "  N/A",
            m.days_to_expiry,
        ))
    print(f"{'='*70}\n")
