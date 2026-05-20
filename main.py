#!/usr/bin/env python3
"""
Kalshi Alpha Scanner

Monitors Kalshi prediction markets for edge opportunities:
  - Whale flow tracking and momentum detection
  - Flow-price divergence (whale money vs market price)
  - Cross-market arbitrage (inconsistently priced related markets)
  - External odds comparison (Kalshi vs sportsbook consensus)

Usage:
  python main.py                              # Live dashboard
  python main.py --snapshot                   # Single scan
  python main.py --json                       # JSON output
  python main.py --threshold 100              # Bigger whale threshold
  python main.py --lookback 120               # 2 hour lookback
  python main.py --odds-key YOUR_KEY          # Enable sportsbook comparison
"""

import argparse
import json
import os

from api import KalshiAPI
from scanner import Scanner
from alpha import AlphaEngine
from dashboard import (
    run_dashboard, build_whale_table, build_top_markets_table,
    build_whale_magnets_table, build_alpha_table,
)
from crypto_dashboard import run_crypto_dashboard

from rich.console import Console
from rich.panel import Panel

from dashboard import BTCPrice


def snapshot_mode(scanner, alpha_engine):
    """Single scan with alpha analysis."""
    console = Console()
    btc = BTCPrice()
    btc.fetch()
    spot = btc.price

    console.print("\n[bold cyan]KALSHI ALPHA SCANNER - SNAPSHOT[/]\n")
    if spot:
        console.print(f"  [bold orange1]BTC spot: ${spot:,.2f}[/]")

    console.print("[dim]Scanning trades...[/]")
    new_whales, trade_count = scanner.scan_trades()
    console.print(f"  {trade_count} trades, {len(new_whales)} whales (>= {scanner.whale_threshold} contracts)")

    console.print("[dim]Enriching market data...[/]")
    scanner.enrich_markets()
    console.print(f"  {len(scanner.market_snapshots)} active markets")

    console.print("[dim]Running alpha analysis...[/]")
    signals = alpha_engine.get_top_signals(20)
    console.print(f"  [bold magenta]{len(signals)} alpha signals detected[/]\n")

    # Alpha signals first — this is what matters
    if signals:
        console.print(Panel(build_alpha_table(signals, limit=20, btc_spot=spot), border_style="magenta", title="ALPHA SIGNALS"))

    # Top markets
    top = scanner.get_top_markets(15)
    console.print(Panel(build_top_markets_table(top, btc_spot=spot), border_style="green"))

    # Whale magnets
    magnets = scanner.get_whale_magnets(10)
    console.print(Panel(build_whale_magnets_table(magnets, btc_spot=spot), border_style="red"))

    # Recent whales
    console.print(Panel(build_whale_table(scanner.whale_alerts, limit=20), border_style="yellow"))


def json_mode(scanner, alpha_engine):
    """Single scan with JSON output including alpha signals."""
    scanner.scan_trades()
    scanner.enrich_markets()
    signals = alpha_engine.get_top_signals(30)
    top = scanner.get_top_markets(30)

    output = {
        "alpha_signals": [
            {
                "ticker": s.ticker,
                "title": s.title,
                "type": s.signal_type,
                "direction": s.direction,
                "strength": s.strength,
                "edge_pct": s.edge_pct,
                "kalshi_price": s.kalshi_price,
                "fair_value": s.fair_value,
                "edge_cents": s.edge_cents,
                "detail": s.detail,
            }
            for s in signals
        ],
        "top_markets": [
            {
                "ticker": m.ticker,
                "title": m.title,
                "trade_count": m.trade_count,
                "trade_volume": m.trade_volume,
                "trade_notional": round(m.trade_notional, 2),
                "open_interest": m.open_interest,
                "whale_count": m.recent_whale_count,
                "whale_volume": m.recent_whale_volume,
                "buy_pressure": round(m.buy_pressure, 2),
                "last_price": m.last_price,
                "score": round(m.score, 4),
            }
            for m in top
        ],
        "whale_alerts": [
            {
                "ticker": a.ticker,
                "contracts": a.contracts,
                "price": a.price,
                "side": a.side,
                "notional": round(a.notional, 2),
                "timestamp": a.timestamp.isoformat(),
            }
            for a in scanner.whale_alerts[:50]
        ],
        "summary": {
            "total_markets": len(scanner.market_snapshots),
            "total_whales": len(scanner.whale_alerts),
            "alpha_signals": len(signals),
            "whale_threshold": scanner.whale_threshold,
        },
    }
    print(json.dumps(output, indent=2))


def main():
    parser = argparse.ArgumentParser(description="Kalshi Alpha Scanner")
    parser.add_argument("--threshold", type=int, default=50,
                        help="Min contracts to flag as whale (default: 50)")
    parser.add_argument("--lookback", type=int, default=60,
                        help="Minutes to look back for trades (default: 60)")
    parser.add_argument("--refresh", type=int, default=30,
                        help="Seconds between scans in live mode (default: 30)")
    parser.add_argument("--snapshot", action="store_true",
                        help="Single scan, no live dashboard")
    parser.add_argument("--json", action="store_true", dest="json_output",
                        help="JSON output (implies single scan)")
    parser.add_argument("--demo", action="store_true",
                        help="Use Kalshi demo API")
    parser.add_argument("--api-key", type=str, default=None,
                        help="Kalshi API key (for orderbook)")
    parser.add_argument("--key-file", type=str, default=None,
                        help="Path to RSA private key PEM file")
    parser.add_argument("--odds-key", type=str, default=None,
                        help="The Odds API key (free at the-odds-api.com)")
    parser.add_argument("--crypto", action="store_true",
                        help="Crypto-only TUI (BTC/ETH strike ladder, whale flow, alpha)")
    parser.add_argument("--web", action="store_true",
                        help="Start web dashboard at /whales (BTC signal cards + whale feed)")
    parser.add_argument("--web-port", type=int, default=9050,
                        help="Web dashboard port (default: 9050)")

    args = parser.parse_args()

    # Check env vars as fallback
    odds_key = args.odds_key or os.environ.get("ODDS_API_KEY")

    api = KalshiAPI(
        api_key=args.api_key,
        private_key_path=args.key_file,
        use_demo=args.demo,
    )

    scanner = Scanner(
        api=api,
        whale_threshold=args.threshold,
        lookback_minutes=args.lookback,
    )

    alpha_engine = AlphaEngine(
        scanner=scanner,
        odds_api_key=odds_key,
    )

    if args.web:
        from web import init as web_init, start_background
        web_init(scanner, alpha_engine)
        start_background(args.web_port)
        console = Console()
        console.print(f"  [bold cyan]Web dashboard: http://localhost:{args.web_port}/whales[/]")
        console.print(f"  [bold cyan]Crypto dashboard: http://localhost:{args.web_port}/crypto[/]")

    if args.json_output:
        json_mode(scanner, alpha_engine)
    elif args.snapshot:
        snapshot_mode(scanner, alpha_engine)
    elif args.crypto:
        run_crypto_dashboard(scanner, alpha_engine=alpha_engine, refresh_seconds=args.refresh)
    else:
        run_dashboard(scanner, alpha_engine=alpha_engine, refresh_seconds=args.refresh)


if __name__ == "__main__":
    main()
