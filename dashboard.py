"""Rich terminal dashboard for the Kalshi whale/volume scanner."""

import time
from datetime import datetime

import requests

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


# ── BTC Price Tracker ────────────────────────────────────────────────

class BTCPrice:
    """Fetches and caches BTC spot price."""

    def __init__(self):
        self.price = 0.0
        self.prev_price = 0.0
        self._last_fetch = 0

    def fetch(self):
        """Fetch BTC price, cached for 10 seconds."""
        if time.time() - self._last_fetch < 10:
            return self.price
        try:
            r = requests.get(
                "https://api.coinbase.com/v2/prices/BTC-USD/spot",
                timeout=3,
            )
            self.prev_price = self.price
            self.price = float(r.json()["data"]["amount"])
            self._last_fetch = time.time()
        except Exception:
            pass
        return self.price

    @property
    def direction(self):
        if self.prev_price == 0:
            return ""
        if self.price > self.prev_price:
            return "up"
        elif self.price < self.prev_price:
            return "down"
        return "flat"


def format_dollars(n):
    if n >= 1_000_000:
        return f"${n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"${n/1_000:.1f}K"
    return f"${n:.0f}"


def format_contracts(n):
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return f"{n:.0f}"


def trunc(s, length=45):
    return s[:length] + "..." if len(s) > length else s


import re

def _extract_strike(ticker):
    """Extract strike price from BTC/ETH/crypto tickers. Returns (strike, type) or None.

    Formats:
      KXBTCD-26MAY1819-T77099.99  -> 77099.99, 'above'  (T = threshold/above)
      KXBTC-26MAY1819-B77050      -> 77050, 'bracket'
      KXBTC15M-26MAY181830-30     -> None (up/down, no strike)
      KXETHD-26MAY1819-T2149.99   -> 2149.99, 'above'
    """
    m = re.search(r'-T(\d+\.?\d*)', ticker)
    if m:
        return float(m.group(1)), "above"
    m = re.search(r'-B(\d+\.?\d*)', ticker)
    if m:
        return float(m.group(1)), "bracket"
    return None, ""


def label_market(title, ticker, btc_price=None):
    """Add spot-relative label to crypto market titles."""
    if not btc_price or btc_price <= 0:
        return title

    strike, stype = _extract_strike(ticker)
    if strike is None:
        return title

    is_btc = "BTC" in ticker
    is_eth = "ETH" in ticker
    spot = btc_price if is_btc else 0

    if not spot:
        return title

    if stype == "above":
        diff = spot - strike
        if diff > 0:
            return f">${strike:,.0f} [ITM +${diff:,.0f}]"
        else:
            return f">${strike:,.0f} [OTM ${diff:,.0f}]"
    elif stype == "bracket":
        diff = spot - strike
        if abs(diff) < 100:
            return f"~${strike:,.0f} [AT SPOT]"
        elif diff > 0:
            return f"${strike:,.0f} [spot +${diff:,.0f}]"
        else:
            return f"${strike:,.0f} [spot ${diff:,.0f}]"

    return title


# ── Alpha Signals Table ──────────────────────────────────────────────

SIGNAL_STYLES = {
    "flow_divergence": ("bold magenta", "FLOW"),
    "arb":             ("bold red",     "ARB"),
    "momentum":        ("bold yellow",  "MNTM"),
    "odds_edge":       ("bold cyan",    "ODDS"),
}


def build_alpha_table(signals, limit=15, btc_spot=0):
    table = Table(title="Alpha Signals", expand=True, padding=(0, 1))
    table.add_column("Type", width=5)
    table.add_column("Market", style="cyan", width=32)
    table.add_column("Side", width=4)
    table.add_column("Kalshi", justify="right", width=7)
    table.add_column("Fair", justify="right", width=7)
    table.add_column("Edge", justify="right", width=6)
    table.add_column("Str", justify="right", width=4)
    table.add_column("Detail", width=45)

    for s in signals[:limit]:
        style, label = SIGNAL_STYLES.get(s.signal_type, ("white", "?"))
        side_style = "green bold" if s.direction == "yes" else "red bold"

        edge_val = s.edge_cents
        edge_style = "bold green" if edge_val > 0 else "bold red"

        str_style = "bold green" if s.strength > 0.7 else "yellow" if s.strength > 0.4 else "dim"

        display = label_market(s.title, s.ticker, btc_spot) if btc_spot else s.title
        if display == s.title:
            display = s.title if s.title != s.ticker else s.ticker

        table.add_row(
            Text(label, style=style),
            trunc(display, 31),
            Text(s.direction.upper(), style=side_style),
            f"${s.kalshi_price:.2f}",
            f"${s.fair_value:.2f}",
            Text(f"{'+' if edge_val > 0 else ''}{edge_val:.0f}¢", style=edge_style),
            Text(f"{s.strength:.1f}", style=str_style),
            trunc(s.detail, 44),
        )

    if not signals:
        table.add_row("--", "Scanning for alpha...", "--", "--", "--", "--", "--", "--")

    return table


# ── Whale Alerts Table ───────────────────────────────────────────────

def build_whale_table(alerts, limit=20):
    table = Table(title="Whale Alerts", expand=True, padding=(0, 1))
    table.add_column("Time", style="dim", width=8)
    table.add_column("Ticker", style="cyan", width=30)
    table.add_column("Side", width=4)
    table.add_column("Size", justify="right", width=8)
    table.add_column("Price", justify="right", width=6)
    table.add_column("Value", justify="right", width=8)

    for a in alerts[:limit]:
        side_style = "green bold" if a.side == "yes" else "red bold"

        if a.contracts >= 500:
            size_style = "bold red"
        elif a.contracts >= 200:
            size_style = "bold yellow"
        else:
            size_style = "white"

        ts = a.timestamp.strftime("%H:%M:%S") if a.timestamp else "?"

        table.add_row(
            ts,
            trunc(a.ticker, 29),
            Text(a.side.upper(), style=side_style),
            Text(format_contracts(a.contracts), style=size_style),
            f"{a.price:.2f}",
            format_dollars(a.notional),
        )

    if not alerts:
        table.add_row("--", "Scanning for whale trades...", "--", "--", "--", "--")

    return table


# ── Top Markets Table ────────────────────────────────────────────────

def build_top_markets_table(markets, btc_spot=0):
    table = Table(title="Top Markets by Activity Score", expand=True, padding=(0, 1))
    table.add_column("#", width=3, style="dim")
    table.add_column("Market", style="cyan", width=35)
    table.add_column("Trades", justify="right", width=6)
    table.add_column("Vol", justify="right", width=8)
    table.add_column("$Not", justify="right", width=8)
    table.add_column("OI", justify="right", width=7)
    table.add_column("Whl", justify="right", width=4)
    table.add_column("Flow", justify="right", width=7)
    table.add_column("Scr", justify="right", width=5)

    for i, m in enumerate(markets, 1):
        score_style = "bold green" if m.score > 0.6 else "yellow" if m.score > 0.3 else "dim"
        whale_style = "bold red" if m.recent_whale_count >= 5 else "yellow" if m.recent_whale_count >= 2 else "white"
        flow_style = "green" if m.buy_pressure > 0 else "red" if m.buy_pressure < 0 else "dim"
        flow_prefix = "+" if m.buy_pressure > 0 else ""

        display = label_market(m.title, m.ticker, btc_spot) if btc_spot else m.title
        if display == m.title:
            display = m.title if m.title != m.ticker else m.ticker
        table.add_row(
            str(i),
            trunc(display, 34),
            str(m.trade_count),
            format_contracts(m.trade_volume),
            format_dollars(m.trade_notional),
            format_contracts(m.open_interest) if m.open_interest else "-",
            Text(str(m.recent_whale_count), style=whale_style),
            Text(f"{flow_prefix}{format_contracts(m.buy_pressure)}", style=flow_style),
            Text(f"{m.score:.2f}", style=score_style),
        )

    return table


# ── Whale Magnets Table ──────────────────────────────────────────────

def build_whale_magnets_table(markets, btc_spot=0):
    table = Table(title="Whale Magnets", expand=True, padding=(0, 1))
    table.add_column("#", width=3, style="dim")
    table.add_column("Market", style="cyan", width=38)
    table.add_column("Whales", justify="right", width=6)
    table.add_column("W.Vol", justify="right", width=8)
    table.add_column("Last $", justify="right", width=7)
    table.add_column("Flow", justify="right", width=8)

    for i, m in enumerate(markets, 1):
        if m.recent_whale_count == 0:
            continue
        flow_style = "green" if m.buy_pressure > 0 else "red" if m.buy_pressure < 0 else "dim"
        flow_prefix = "+" if m.buy_pressure > 0 else ""
        display = label_market(m.title, m.ticker, btc_spot) if btc_spot else m.title
        if display == m.title:
            display = m.title if m.title != m.ticker else m.ticker
        title = display

        table.add_row(
            str(i),
            trunc(title, 37),
            str(m.recent_whale_count),
            format_contracts(m.recent_whale_volume),
            f"${m.last_price:.2f}" if m.last_price else "-",
            Text(f"{flow_prefix}{format_contracts(m.buy_pressure)}", style=flow_style),
        )

    return table


# ── Status Bar ───────────────────────────────────────────────────────

def build_status_bar(scan_count, market_count, trade_count, whale_count, signal_count, threshold, btc_price=None):
    now = datetime.now().strftime("%H:%M:%S")

    btc_str = ""
    if btc_price and btc_price.price > 0:
        arrow = {"up": "[green]^[/]", "down": "[red]v[/]", "flat": "[dim]-[/]"}.get(btc_price.direction, "")
        btc_str = f"[bold orange1]BTC ${btc_price.price:,.0f}[/]{arrow} | "

    return Text.from_markup(
        f" [bold white]KALSHI ALPHA SCANNER[/] | "
        f"[dim]{now}[/] | "
        f"{btc_str}"
        f"Scan #{scan_count} | "
        f"[cyan]{market_count}[/] markets | "
        f"{trade_count} trades | "
        f"[yellow]{whale_count}[/] whales | "
        f"[bold magenta]{signal_count}[/] signals | "
        f"[dim]Ctrl+C to quit[/]"
    )


# ── Main Dashboard Loop ─────────────────────────────────────────────

def run_dashboard(scanner, alpha_engine=None, refresh_seconds=30):
    """Main dashboard loop with alpha signals."""
    console = Console()
    scan_count = 0
    total_trades = 0
    btc = BTCPrice()

    console.print("[bold cyan]KALSHI ALPHA SCANNER[/]")
    console.print(f"Whale threshold: {scanner.whale_threshold} contracts")
    console.print(f"Lookback: {scanner.lookback_minutes} minutes")
    console.print(f"Alpha engine: {'ON' if alpha_engine else 'OFF (no odds API key)'}")
    console.print(f"Refresh: {refresh_seconds}s")
    console.print("[dim]Starting initial scan...[/]\n")

    with Live(console=console, refresh_per_second=1, screen=True) as live:
        while True:
            try:
                scan_count += 1

                # Fetch BTC price
                btc.fetch()

                # Scan trades
                new_whales, new_trades = scanner.scan_trades()
                total_trades += new_trades

                # Enrich markets
                scanner.enrich_markets()

                # Alpha signals
                alpha_signals = []
                if alpha_engine:
                    alpha_signals = alpha_engine.get_top_signals(15)

                # Ranked views
                top_markets = scanner.get_top_markets(12)
                whale_magnets = scanner.get_whale_magnets(8)

                # Build layout
                layout = Layout()
                layout.split_column(
                    Layout(name="header", size=1),
                    Layout(name="body"),
                )
                layout["body"].split_row(
                    Layout(name="left", ratio=1),
                    Layout(name="right", ratio=1),
                )
                layout["left"].split_column(
                    Layout(name="alpha", ratio=2),
                    Layout(name="whales", ratio=2),
                )
                layout["right"].split_column(
                    Layout(name="top", ratio=3),
                    Layout(name="magnets", ratio=2),
                )

                layout["header"].update(
                    build_status_bar(
                        scan_count,
                        len(scanner.market_snapshots),
                        total_trades,
                        len(scanner.whale_alerts),
                        len(alpha_signals),
                        scanner.whale_threshold,
                        btc_price=btc,
                    )
                )
                spot = btc.price

                layout["alpha"].update(
                    Panel(build_alpha_table(alpha_signals, btc_spot=spot), border_style="magenta")
                )
                layout["whales"].update(
                    Panel(build_whale_table(scanner.whale_alerts), border_style="yellow")
                )
                layout["top"].update(
                    Panel(build_top_markets_table(top_markets, btc_spot=spot), border_style="green")
                )
                layout["magnets"].update(
                    Panel(build_whale_magnets_table(whale_magnets, btc_spot=spot), border_style="red")
                )

                live.update(layout)
                time.sleep(refresh_seconds)

            except KeyboardInterrupt:
                break
            except Exception as e:
                live.update(
                    Panel(
                        f"[red]Error: {e}[/]\n\nRetrying in {refresh_seconds}s...",
                        title="Scanner Error",
                    )
                )
                time.sleep(refresh_seconds)
