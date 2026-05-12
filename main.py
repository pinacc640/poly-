#!/usr/bin/env python3
"""Polymarket Market Scanner — entry point.

Run (standard mode, mock data, no AI):
    python main.py

Run with AI Oracle enrichment (requires DEEPSEEK_API_KEY; BRAVE_API_KEY optional):
    DEEPSEEK_API_KEY=sk-... BRAVE_API_KEY=BSA... python main.py --ai

The --ai flag fetches live Brave Search news for each market and asks
DeepSeek to re-estimate the true probability before running strategies.
"""

import sys

from polymarket_scanner.scanner import MarketScanner
from polymarket_scanner.formatter import format_report


def main() -> None:
    use_ai = "--ai" in sys.argv

    if use_ai:
        # Late import so the module is only required when --ai is passed
        import os
        from polymarket_scanner.ai_oracle import AIOracle
        from polymarket_scanner.mock_data import load_mock_markets

        print("🔮 AI Oracle mode — enriching markets with DeepSeek + Brave Search…\n")

        try:
            oracle  = AIOracle()                  # reads DEEPSEEK_API_KEY / BRAVE_API_KEY
            markets = load_mock_markets()
            markets = oracle.enrich_all(markets)  # RAG-enriched true_prob values
        except ValueError as exc:
            print(f"[ERROR] {exc}")
            sys.exit(1)

        # Inject enriched markets into the scanner via the data_source hook
        scanner = MarketScanner(data_source=lambda: markets)
    else:
        scanner = MarketScanner()   # DEFAULT_CONFIG + mock data, no AI

    report = scanner.run()
    print(format_report(report))


if __name__ == "__main__":
    main()
