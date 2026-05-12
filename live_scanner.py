#!/usr/bin/env python3
"""live_scanner.py — Production-style CLI entry point for the Polymarket scanner.

Examples
--------
# Basic run, mock data, no AI, default $50 capital:
    python live_scanner.py

# With AI Oracle (DeepSeek + Brave Search RAG), custom capital:
    DEEPSEEK_API_KEY=sk-... BRAVE_API_KEY=BSA... \\
        python live_scanner.py --use-ai --capital 70

# AI-only (no Brave), verbose logging:
    DEEPSEEK_API_KEY=sk-... \\
        python live_scanner.py --use-ai --capital 100 --verbose

# Fine-tune the oracle:
    python live_scanner.py --use-ai --capital 70 \\
        --timeout 20 --model deepseek-chat --max-results 5

CLI Flags
---------
--use-ai          Enable AI Oracle (requires DEEPSEEK_API_KEY env var).
--capital FLOAT   Total account capital in USD (default: 50).
--timeout INT     HTTP timeout in seconds for DeepSeek / Brave calls (default: 15).
--model STR       DeepSeek model name (default: deepseek-chat).
--max-results INT Number of Brave Search results to fetch per market (default: 5).
--temperature FLOAT
                  DeepSeek sampling temperature 0-1 (default: 0.2).
--max-tokens INT  Max tokens in DeepSeek response (default: 256).
--no-fallback     Raise instead of silently falling back on AI errors.
--verbose         Set log level to DEBUG (shows raw API responses).
"""

import argparse
import logging
import sys

from polymarket_scanner.config import AccountConfig
from polymarket_scanner.formatter import format_report
from polymarket_scanner.mock_data import load_mock_markets
from polymarket_scanner.scanner import MarketScanner


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="live_scanner",
        description="Polymarket Market Scanner — AI-powered edition",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # --- AI toggle ---
    p.add_argument(
        "--use-ai",
        action="store_true",
        default=False,
        help="Enable AI Oracle (DeepSeek + optional Brave Search RAG). "
             "Requires DEEPSEEK_API_KEY env var.",
    )

    # --- Capital ---
    p.add_argument(
        "--capital",
        type=float,
        default=50.0,
        metavar="USD",
        help="Total account capital in USD (default: 50).",
    )

    # --- Oracle tuning ---
    oracle_group = p.add_argument_group("AI Oracle options (only used with --use-ai)")
    oracle_group.add_argument(
        "--timeout",
        type=int,
        default=15,
        metavar="SEC",
        help="HTTP request timeout for DeepSeek / Brave calls in seconds (default: 15).",
    )
    oracle_group.add_argument(
        "--model",
        type=str,
        default="deepseek-chat",
        metavar="NAME",
        help="DeepSeek model name (default: deepseek-chat).",
    )
    oracle_group.add_argument(
        "--max-results",
        type=int,
        default=5,
        metavar="N",
        dest="max_results",
        help="Number of Brave Search results per market (default: 5).",
    )
    oracle_group.add_argument(
        "--temperature",
        type=float,
        default=0.2,
        metavar="T",
        help="DeepSeek sampling temperature 0-1 (default: 0.2).",
    )
    oracle_group.add_argument(
        "--max-tokens",
        type=int,
        default=256,
        metavar="N",
        dest="max_tokens",
        help="Max tokens in DeepSeek response (default: 256).",
    )
    oracle_group.add_argument(
        "--no-fallback",
        action="store_true",
        default=False,
        help="Raise instead of silently keeping original true_prob on AI errors.",
    )

    # --- Misc ---
    p.add_argument(
        "--verbose", "-v",
        action="store_true",
        default=False,
        help="Enable DEBUG logging (shows raw DeepSeek responses, etc.).",
    )

    return p


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    args = _build_parser().parse_args()

    # --- Logging setup ---
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logger = logging.getLogger("live_scanner")

    # --- Build AccountConfig with CLI capital ---
    cfg = AccountConfig(total_capital=args.capital)
    logger.info("Account capital: $%.2f", args.capital)

    # --- Load markets (mock data for now; swap fetch_markets() for live API) ---
    markets = load_mock_markets()
    logger.info("Loaded %d markets", len(markets))

    # --- Optional AI Oracle enrichment ---
    if args.use_ai:
        from polymarket_scanner.ai_oracle import AIOracle

        logger.info(
            "AI Oracle enabled — model=%s  timeout=%ds  max_results=%d  temperature=%.2f",
            args.model, args.timeout, args.max_results, args.temperature,
        )
        print("🔮 AI Oracle mode — enriching markets with DeepSeek + Brave Search…\n")

        try:
            oracle = AIOracle(
                fallback_on_error=not args.no_fallback,
                timeout=args.timeout,
                model=args.model,
                max_results=args.max_results,
                temperature=args.temperature,
                max_tokens=args.max_tokens,
            )
        except ValueError as exc:
            print(f"[ERROR] {exc}")
            sys.exit(1)

        markets = oracle.enrich_all(markets)
        logger.info("AI enrichment complete")

    # --- Run scanner ---
    scanner = MarketScanner(cfg=cfg, data_source=lambda: markets)
    report  = scanner.run()

    # --- Print report ---
    print(format_report(report))


if __name__ == "__main__":
    main()
