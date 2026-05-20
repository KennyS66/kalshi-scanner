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
  python main.py --crypto                     # Crypto live TUI (BTC/ETH strike ladder)
  python main.py --crypto-snapshot            # Crypto screen snapshot: spot vs strike
  python main.py --crypto-snapshot --json     # Crypto snapshot as JSON
  python main.py --whale-research             # UP/DOWN verdict per whale market
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
    build_crypto_screen_table, build_crypto_header,
    CryptoSpot, crypto_symbol, _extract_strike,
)
from crypto_dashboard import run_crypto_dashboard
from whale_research import (
    research_whale_markets, build_verdict_table, verdicts_to_json,
)

from rich.console import Console
from rich.panel import Panel


def snapshot_mode(scanner, alpha_engine):
    """Single scan with alpha analysis."""
    console = Console()
    crypto = CryptoSpot()
    crypto.fetch()
    spots = crypto.prices

    console.print("\n[bold cyan]KALSHI ALPHA SCANNER - SNAPSHOT[/]\n")
    if spots:
        console.print(build_crypto_header(spots))

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
        console.print(Panel(build_alpha_table(signals, limit=20, spots=spots), border_style="magenta", title="ALPHA SIGNALS"))

    # Top markets
    top = scanner.get_top_markets(15)
    console.print(Panel(build_top_markets_table(top, spots=spots), border_style="green"))

    # Whale magnets
    magnets = scanner.get_whale_magnets(10)
    console.print(Panel(build_whale_magnets_table(magnets, spots=spots), border_style="red"))

    # Crypto screen
    console.print(Panel(build_crypto_screen_table(scanner, spots, limit=25),
                        border_style="orange1", title="CRYPTO SCREEN"))

    # Recent whales
    console.print(Panel(build_whale_table(scanner.whale_alerts, limit=20), border_style="yellow"))


def crypto_mode(scanner, alpha_engine, json_output=False):
    """Single scan focused on crypto markets — spot, strike, distance, edge."""
    console = Console()
    crypto = CryptoSpot()
    crypto.fetch()
    spots = crypto.prices

    if not json_output:
        console.print("\n[bold cyan]KALSHI CRYPTO SCREEN[/]\n")
        if spots:
            console.print(build_crypto_header(spots))
        console.print("[dim]Scanning trades...[/]")
    new_whales, trade_count = scanner.scan_trades()
    if not json_output:
        console.print(f"  {trade_count} trades, {len(new_whales)} whales")
        console.print("[dim]Enriching market data...[/]")
    scanner.enrich_markets()
    if not json_output:
        console.print(f"  {len(scanner.market_snapshots)} active markets")

    # Filter scanner to crypto-only summary for stats
    crypto_markets = {
        t: s for t, s in scanner.market_snapshots.items() if crypto_symbol(t)
    }

    if json_output:
        output = {
            "spots": {sym: round(p, 6) for sym, p in spots.items()},
            "crypto_markets": [],
        }
        for ticker, snap in crypto_markets.items():
            sym = crypto_symbol(ticker)
            strike, stype = _extract_strike(ticker)
            spot = spots.get(sym, 0.0) if sym else 0.0
            diff = (spot - strike) if (spot and strike) else None
            yes = snap.yes_price or snap.last_price
            output["crypto_markets"].append({
                "ticker": ticker,
                "symbol": sym,
                "title": snap.title,
                "type": stype or None,
                "strike": strike,
                "spot": round(spot, 6) if spot else None,
                "distance": round(diff, 6) if diff is not None else None,
                "distance_pct": round(diff / strike * 100, 4) if (diff is not None and strike) else None,
                "yes_price": yes,
                "last_price": snap.last_price,
                "trade_volume": snap.trade_volume,
                "trade_notional": round(snap.trade_notional, 2),
                "whale_count": snap.recent_whale_count,
                "whale_volume": snap.recent_whale_volume,
                "buy_pressure": round(snap.buy_pressure, 2),
            })
        output["summary"] = {
            "total_crypto_markets": len(crypto_markets),
            "symbols_with_spot": sorted(spots.keys()),
        }
        print(json.dumps(output, indent=2))
        return

    console.print(f"  [bold orange1]{len(crypto_markets)} crypto markets[/]\n")
    console.print(Panel(build_crypto_screen_table(scanner, spots, limit=60),
                        border_style="orange1", title="CRYPTO SCREEN"))


def whale_research_mode(scanner, alpha_engine, json_output=False):
    """Whale-driven research: every whale market → UP/DOWN verdict with confidence."""
    console = Console()

    if not json_output:
        console.print("\n[bold cyan]KALSHI WHALE RESEARCH[/]\n")
        console.print("[dim]Scanning trades...[/]")
    new_whales, trade_count = scanner.scan_trades()
    if not json_output:
        console.print(f"  {trade_count} trades, {len(new_whales)} new whales "
                      f"(>= {scanner.whale_threshold} contracts)")
        console.print("[dim]Enriching market data...[/]")
    scanner.enrich_markets()
    if not json_output:
        console.print(f"  {len(scanner.market_snapshots)} active markets")
        console.print("[dim]Researching whale markets...[/]")

    verdicts = research_whale_markets(scanner, alpha_engine)

    if json_output:
        print(json.dumps({
            "verdicts": verdicts_to_json(verdicts),
            "summary": {
                "total_verdicts": len(verdicts),
                "up": sum(1 for v in verdicts if v.verdict == "UP"),
                "down": sum(1 for v in verdicts if v.verdict == "DOWN"),
                "neutral": sum(1 for v in verdicts if v.verdict == "NEUTRAL"),
            },
        }, indent=2))
        return

    up = sum(1 for v in verdicts if v.verdict == "UP")
    down = sum(1 for v in verdicts if v.verdict == "DOWN")
    neutral = sum(1 for v in verdicts if v.verdict == "NEUTRAL")
    console.print(f"  [bold green]{up} UP[/]  [bold red]{down} DOWN[/]  [dim]{neutral} neutral[/]\n")

    console.print(Panel(build_verdict_table(verdicts, limit=30),
                        border_style="magenta", title="WHALE → RESEARCH → VERDICT"))

    # Top 5 with full reasoning
    top = [v for v in verdicts if v.verdict != "NEUTRAL"][:5]
    if top:
        console.print("\n[bold]Top conviction trades:[/]\n")
        for v in top:
            color = "green" if v.verdict == "UP" else "red"
            console.print(f"  [bold {color}]{v.verdict}[/] [cyan]{v.ticker}[/] "
                          f"@ ${v.price:.2f}  fair ${v.fair_value:.2f}  "
                          f"({v.edge_cents:+.1f}¢, {v.confidence:.0%} conf)")
            console.print(f"    [dim]{v.reasoning}[/]\n")


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
    parser.add_argument("--whale-research", action="store_true", dest="whale_research",
                        help="Whale-driven research: UP/DOWN verdict per whale market")
    parser.add_argument("--crypto-snapshot", action="store_true", dest="crypto_mode",
                        help="Crypto screen snapshot: spot vs strike for every crypto market (one-shot)")
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

    if args.whale_research:
        whale_research_mode(scanner, alpha_engine, json_output=args.json_output)
    elif args.crypto_mode:
        crypto_mode(scanner, alpha_engine, json_output=args.json_output)
    elif args.json_output:
        json_mode(scanner, alpha_engine)
    elif args.snapshot:
        snapshot_mode(scanner, alpha_engine)
    elif args.crypto:
        run_crypto_dashboard(scanner, alpha_engine=alpha_engine, refresh_seconds=args.refresh)
    else:
        run_dashboard(scanner, alpha_engine=alpha_engine, refresh_seconds=args.refresh)


if __name__ == "__main__":
    main()
