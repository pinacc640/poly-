#!/usr/bin/env python3
"""Polymarket Market Scanner — entry point.

Run:
    python main.py

使用模拟数据运行三大策略：
  - stable_strategy()      稳健收敛扫描
  - volatility_strategy()  波动套利扫描
  - smart_money_strategy() 聪明钱追踪扫描（Alpha 信号）

To override capital or any parameter, edit config.py or pass a custom
AccountConfig to MarketScanner:

    from polymarket_scanner.config import AccountConfig
    from polymarket_scanner.scanner import MarketScanner

    scanner = MarketScanner(cfg=AccountConfig(total_capital=200))
    report  = scanner.run()
"""

from polymarket_scanner.scanner import MarketScanner
from polymarket_scanner.formatter import format_report


def main() -> None:
    scanner = MarketScanner()       # uses DEFAULT_CONFIG + mock data
    report  = scanner.run()
    print(format_report(report))


if __name__ == "__main__":
    main()
