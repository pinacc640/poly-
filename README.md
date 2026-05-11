# Polymarket Market Scanner MVP

小资金账户（$50）专用扫描系统，采用
**80% 稳健收敛 + 20% 波动套利** 双策略框架。

---

## 项目结构

```
poly-/
├── main.py                          # 入口，直接运行
└── polymarket_scanner/
    ├── config.py                    # 账户参数 & 策略阈值（调参唯一入口）
    ├── models.py                    # Market / Opportunity / RiskDecision dataclass
    ├── mock_data.py                 # 10 条模拟市场（覆盖各种边界场景）
    ├── scanner.py                   # MarketScanner 编排器（fetch → strategies → risk）
    ├── formatter.py                 # 报告格式化输出
    ├── risk.py                      # RiskController（风控规则硬门槛）
    └── strategies/
        ├── stable.py                # stable_strategy()  — 稳健收敛
        └── volatility.py            # volatility_strategy() — 波动套利
```

---

## 快速开始

```bash
python main.py
```

---

## 账户参数（`config.py`）

| 参数 | 默认值 | 说明 |
|---|---|---|
| `total_capital` | 50.0 | 总资金 USD |
| `max_position_ratio` | 0.10 | 单笔仓位上限（10%） |
| `volatility_cap_ratio` | 0.20 | 波动仓位合计上限（20%） |
| `min_absolute_profit` | 0.50 | 最低预期盈利 USD |
| `min_profit_ratio` | 0.01 | 最低预期盈利率（1%） |

---

## 三大模块

### 1. `stable_strategy()` — 稳健收敛

- **筛选**：到期 ≤14天、价格 ≥0.80 或 ≤0.20、流动性 ≥$100K、非宏观品类
- **评分**（≥5 才进候选）：

  | 条件 | 分值 |
  |---|---|
  | 到期 ≤7天 | +3 |
  | 极端价格（≥0.90 / ≤0.10） | +2 |
  | 成交量上升 | +2 |
  | 政治突发风险 | -3 |
  | 宏观品类 | -5 |

- **EV 计算**：`EV = true_prob × profit_space − (1−true_prob) × loss_space`，EV ≤ 0 直接拒绝

### 2. `volatility_strategy()` — 波动套利

- **筛选**：24h 价格变动 ≥±5%、流动性 ≥$100K、无基本面变化
- **逻辑**：反向做（fade）— 价格暴跌买 YES，价格暴涨买 NO
- **参数**：目标 +5%、止损 -5%、最多持有 3 天

### 3. `risk_controller()` — 风控

| 规则 | 处理方式 |
|---|---|
| 单笔 > 10% 资金 | 软性截断到上限 |
| 预期利润 < $0.50 | 硬拒绝 |
| 预期利润 < 1% 资金 | 硬拒绝 |
| 波动仓位合计 > 20% | 硬拒绝 |

---

## 接入真实 API

只需继承 `MarketScanner` 并覆盖 `fetch_markets()`，其余逻辑不变：

```python
from polymarket_scanner.scanner import MarketScanner
from polymarket_scanner.models import Market

class LiveScanner(MarketScanner):
    def fetch_markets(self) -> list[Market]:
        raw = call_polymarket_api()          # 你的 API 客户端
        return [map_to_market(r) for r in raw]

scanner = LiveScanner()
report  = scanner.run()
```

---

## 模拟数据覆盖场景

| ID | 场景 | 预期结果 |
|---|---|---|
| M1 | 强稳健 YES 候选（选举，近到期，高价，量增） | ✅ 稳健通过 |
| M2 | 强稳健 NO 候选（体育，低价，量增） | ✅ 稳健通过 |
| M3 | 宏观品类（oil） | ❌ 被稳健策略过滤 |
| M4 | 流动性不足 | ❌ 流动性过滤 |
| M5 | 政治突发风险（-3 扣分） | ❌ 评分不足 |
| M6 | 波动 +6%（true_prob<0.5，NO 面 EV>0） | ✅ 波动通过 |
| M7 | 基本面变化标记 | ❌ 波动策略过滤 |
| M8 | 波动 -8%（true_prob>0.5，YES 面 EV>0） | ✅ 波动通过 |
| M9 | 稳健 NO 面 EV>0，但利润 $0.75 通过风控 | ✅ 稳健通过 |
| M10 | 利润过薄（< $0.50） | ❌ 风控拒绝 |
