"""Dedicated crypto market TUI for the Kalshi scanner."""

import re
import time
from datetime import datetime

import requests

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from dashboard import BTCPrice, format_dollars, format_contracts, trunc, _extract_strike


# ── Multi-asset spot prices ──────────────────────────────────────────

class SpotPrices:
    """Tracks BTC and ETH spot prices from Coinbase."""

    def __init__(self):
        self.btc = BTCPrice()
        self._eth = 0.0
        self._eth_prev = 0.0
        self._eth_ts = 0.0

    def fetch(self):
        self.btc.fetch()
        if time.time() - self._eth_ts >= 10:
            try:
                r = requests.get(
                    "https://api.coinbase.com/v2/prices/ETH-USD/spot",
                    timeout=3,
                )
                self._eth_prev = self._eth
                self._eth = float(r.json()["data"]["amount"])
                self._eth_ts = time.time()
            except Exception:
                pass

    @property
    def eth(self):
        return self._eth

    @property
    def eth_direction(self):
        if self._eth_prev == 0:
            return ""
        if self._eth > self._eth_prev:
            return "up"
        elif self._eth < self._eth_prev:
            return "down"
        return "flat"


# ── Crypto market filtering ──────────────────────────────────────────

CRYPTO_PREFIXES = ("KXBTC", "KXETH", "KXSOL", "KXXBT")

def is_crypto(ticker: str) -> bool:
    return any(ticker.startswith(p) for p in CRYPTO_PREFIXES)


def get_asset(ticker: str) -> str:
    if "ETH" in ticker:
        return "ETH"
    if "SOL" in ticker:
        return "SOL"
    return "BTC"


def get_market_kind(ticker: str) -> str:
    """Classify ticker into readable category."""
    if "15M" in ticker or "1H" in ticker:
        return "15m/1h"
    if re.search(r'-T\d', ticker) or re.search(r'-B\d', ticker):
        return "strike"
    return "other"


# ── Strike Ladder ────────────────────────────────────────────────────

def build_strike_ladder(scanner, spot_prices):
    """All active crypto strike markets sorted around spot price."""
    btc_spot = spot_prices.btc.price
    eth_spot = spot_prices.eth

    rows = []
    for ticker, snap in scanner.market_snapshots.items():
        if not is_crypto(ticker):
            continue
        strike, stype = _extract_strike(ticker)
        if strike is None:
            continue

        asset = get_asset(ticker)
        spot = btc_spot if asset == "BTC" else eth_spot if asset == "ETH" else 0
        if not spot:
            continue

        diff = spot - strike
        itm = diff > 0  # above-strike market is ITM when spot > strike
        rows.append({
            "ticker": ticker,
            "snap": snap,
            "asset": asset,
            "strike": strike,
            "spot": spot,
            "diff": diff,
            "itm": itm,
            "stype": stype,
        })

    # Sort by asset then distance from spot (closest first)
    rows.sort(key=lambda r: (r["asset"], abs(r["diff"])))

    table = Table(title="Crypto Strike Ladder", expand=True, padding=(0, 1))
    table.add_column("Asset", width=4, style="dim")
    table.add_column("Strike", justify="right", width=10)
    table.add_column("vs Spot", justify="right", width=9)
    table.add_column("ITM?", width=5)
    table.add_column("Price", justify="right", width=6)
    table.add_column("Vol", justify="right", width=8)
    table.add_column("Whl", justify="right", width=4)
    table.add_column("Flow", justify="right", width=8)

    current_asset = None
    for r in rows:
        snap = r["snap"]
        price = snap.last_price or snap.yes_price

        if r["asset"] != current_asset:
            current_asset = r["asset"]
            spot_val = r["spot"]
            spot_str = f"${spot_val:,.0f}" if spot_val >= 1000 else f"${spot_val:,.2f}"
            arrow = {"up": "[green]▲[/]", "down": "[red]▼[/]", "flat": "[dim]–[/]"}.get(
                spot_prices.btc.direction if r["asset"] == "BTC" else spot_prices.eth_direction, ""
            )
            table.add_row(
                Text(r["asset"], style="bold cyan"),
                Text(f"spot {spot_str} {arrow}", style="bold white"),
                "", "", "", "", "", "",
            )

        diff_str = f"+${r['diff']:,.0f}" if r["diff"] >= 0 else f"-${abs(r['diff']):,.0f}"
        itm_text = Text("ITM", style="bold green") if r["itm"] else Text("OTM", style="bold red")
        diff_style = "green" if r["itm"] else "red"

        flow_style = "green" if snap.buy_pressure > 0 else "red" if snap.buy_pressure < 0 else "dim"
        flow_prefix = "+" if snap.buy_pressure > 0 else ""

        whale_style = "bold red" if snap.recent_whale_count >= 5 else "yellow" if snap.recent_whale_count >= 2 else "white"

        price_style = "bold green" if r["itm"] else "white"

        table.add_row(
            "",
            Text(f"${r['strike']:,.0f}", style=price_style),
            Text(diff_str, style=diff_style),
            itm_text,
            f"${price:.2f}" if price else "-",
            format_contracts(snap.trade_volume) if snap.trade_volume else "-",
            Text(str(snap.recent_whale_count), style=whale_style),
            Text(f"{flow_prefix}{format_contracts(snap.buy_pressure)}", style=flow_style),
        )

    if not rows:
        table.add_row("--", "No crypto strike markets found yet", "", "", "", "", "", "")

    return table


# ── Up/Down Markets Table ────────────────────────────────────────────

def build_updown_table(scanner, spot_prices):
    """Short-term BTC/ETH up-or-down markets."""
    rows = []
    for ticker, snap in scanner.market_snapshots.items():
        if not is_crypto(ticker):
            continue
        if get_market_kind(ticker) != "15m/1h":
            continue
        rows.append(snap)

    rows.sort(key=lambda s: s.trade_volume, reverse=True)

    table = Table(title="BTC/ETH Up-Down Markets", expand=True, padding=(0, 1))
    table.add_column("Market", style="cyan", width=30)
    table.add_column("Price", justify="right", width=6)
    table.add_column("Vol", justify="right", width=8)
    table.add_column("Whl", justify="right", width=4)
    table.add_column("Flow", justify="right", width=8)

    for snap in rows[:12]:
        price = snap.last_price or snap.yes_price
        flow_style = "green" if snap.buy_pressure > 0 else "red" if snap.buy_pressure < 0 else "dim"
        flow_prefix = "+" if snap.buy_pressure > 0 else ""
        whale_style = "bold red" if snap.recent_whale_count >= 5 else "yellow" if snap.recent_whale_count >= 2 else "white"

        title = snap.title or snap.ticker
        table.add_row(
            trunc(title, 29),
            f"${price:.2f}" if price else "-",
            format_contracts(snap.trade_volume) if snap.trade_volume else "-",
            Text(str(snap.recent_whale_count), style=whale_style),
            Text(f"{flow_prefix}{format_contracts(snap.buy_pressure)}", style=flow_style),
        )

    if not rows:
        table.add_row("Scanning for up/down markets...", "", "", "", "")

    return table


# ── Crypto Whale Alerts ──────────────────────────────────────────────

def build_crypto_whales(scanner, spot_prices):
    btc_spot = spot_prices.btc.price
    alerts = [a for a in scanner.whale_alerts if is_crypto(a.ticker)]

    table = Table(title="Crypto Whale Alerts", expand=True, padding=(0, 1))
    table.add_column("Time", style="dim", width=8)
    table.add_column("Market", style="cyan", width=28)
    table.add_column("Side", width=4)
    table.add_column("Size", justify="right", width=8)
    table.add_column("Price", justify="right", width=6)
    table.add_column("Value", justify="right", width=8)
    table.add_column("vs Spot", justify="right", width=9)

    for a in alerts[:18]:
        side_style = "green bold" if a.side == "yes" else "red bold"
        size_style = "bold red" if a.contracts >= 500 else "bold yellow" if a.contracts >= 200 else "white"
        ts = a.timestamp.strftime("%H:%M:%S") if a.timestamp else "?"

        strike, _ = _extract_strike(a.ticker)
        if strike and btc_spot:
            diff = btc_spot - strike
            vs_str = f"+${diff:,.0f}" if diff >= 0 else f"-${abs(diff):,.0f}"
            vs_style = "green" if diff >= 0 else "red"
            vs_cell = Text(vs_str, style=vs_style)
        else:
            vs_cell = Text("-", style="dim")

        table.add_row(
            ts,
            trunc(a.ticker, 27),
            Text(a.side.upper(), style=side_style),
            Text(format_contracts(a.contracts), style=size_style),
            f"{a.price:.2f}",
            format_dollars(a.notional),
            vs_cell,
        )

    if not alerts:
        table.add_row("--", "No crypto whale trades yet...", "--", "--", "--", "--", "--")

    return table


# ── Crypto Alpha Signals ─────────────────────────────────────────────

def build_crypto_alpha(alpha_engine, spot_prices):
    btc_spot = spot_prices.btc.price
    if not alpha_engine:
        t = Table(title="Crypto Alpha Signals", expand=True)
        t.add_column("Info")
        t.add_row("[dim]Alpha engine not running[/]")
        return t

    all_signals = alpha_engine.get_top_signals(50)
    signals = [s for s in all_signals if is_crypto(s.ticker)]

    table = Table(title="Crypto Alpha Signals", expand=True, padding=(0, 1))
    table.add_column("Type", width=5)
    table.add_column("Market", style="cyan", width=26)
    table.add_column("Side", width=4)
    table.add_column("Price", justify="right", width=6)
    table.add_column("Fair", justify="right", width=6)
    table.add_column("Edge", justify="right", width=6)
    table.add_column("Str", justify="right", width=4)
    table.add_column("vs Spot", justify="right", width=9)

    SIGNAL_STYLES = {
        "flow_divergence": ("bold magenta", "FLOW"),
        "arb":             ("bold red",     "ARB"),
        "momentum":        ("bold yellow",  "MNTM"),
        "odds_edge":       ("bold cyan",    "ODDS"),
    }

    for s in signals[:15]:
        style, label = SIGNAL_STYLES.get(s.signal_type, ("white", "?"))
        side_style = "green bold" if s.direction == "yes" else "red bold"
        edge_val = s.edge_cents
        edge_style = "bold green" if edge_val > 0 else "bold red"
        str_style = "bold green" if s.strength > 0.7 else "yellow" if s.strength > 0.4 else "dim"

        strike, _ = _extract_strike(s.ticker)
        if strike and btc_spot:
            diff = btc_spot - strike
            vs_str = f"+${diff:,.0f}" if diff >= 0 else f"-${abs(diff):,.0f}"
            vs_style = "green" if diff >= 0 else "red"
            vs_cell = Text(vs_str, style=vs_style)
        else:
            vs_cell = Text("-", style="dim")

        table.add_row(
            Text(label, style=style),
            trunc(s.title or s.ticker, 25),
            Text(s.direction.upper(), style=side_style),
            f"${s.kalshi_price:.2f}",
            f"${s.fair_value:.2f}",
            Text(f"{'+' if edge_val > 0 else ''}{edge_val:.0f}¢", style=edge_style),
            Text(f"{s.strength:.1f}", style=str_style),
            vs_cell,
        )

    if not signals:
        table.add_row("--", "No crypto alpha signals yet", "--", "--", "--", "--", "--", "--")

    return table


# ── Header Bar ───────────────────────────────────────────────────────

def build_crypto_header(scan_count, scanner, spot_prices, signal_count):
    now = datetime.now().strftime("%H:%M:%S")
    btc = spot_prices.btc
    eth_price = spot_prices.eth

    arrow_map = {"up": "[green]▲[/]", "down": "[red]▼[/]", "flat": "[dim]–[/]"}

    btc_str = ""
    if btc.price > 0:
        a = arrow_map.get(btc.direction, "")
        btc_str = f"[bold orange1]BTC ${btc.price:,.0f}[/]{a}"

    eth_str = ""
    if eth_price > 0:
        a = arrow_map.get(spot_prices.eth_direction, "")
        eth_str = f"  [bold blue]ETH ${eth_price:,.0f}[/]{a}"

    crypto_markets = sum(1 for t in scanner.market_snapshots if is_crypto(t))
    crypto_whales = sum(1 for a in scanner.whale_alerts if is_crypto(a.ticker))

    return Text.from_markup(
        f" [bold white]KALSHI CRYPTO SCANNER[/] | "
        f"[dim]{now}[/] | "
        f"{btc_str}{eth_str}  | "
        f"Scan #{scan_count} | "
        f"[cyan]{crypto_markets}[/] crypto markets | "
        f"[yellow]{crypto_whales}[/] crypto whales | "
        f"[bold magenta]{signal_count}[/] signals | "
        f"[dim]Ctrl+C to quit[/]"
    )


# ── Main Dashboard ───────────────────────────────────────────────────

def run_crypto_dashboard(scanner, alpha_engine=None, refresh_seconds=30):
    console = Console()
    scan_count = 0
    spot = SpotPrices()

    console.print("[bold cyan]KALSHI CRYPTO SCANNER[/]")
    console.print(f"Whale threshold: {scanner.whale_threshold} contracts")
    console.print(f"Lookback: {scanner.lookback_minutes} minutes")
    console.print(f"Refresh: {refresh_seconds}s")
    console.print("[dim]Starting initial scan...[/]\n")

    with Live(console=console, refresh_per_second=1, screen=True) as live:
        while True:
            try:
                scan_count += 1

                spot.fetch()
                scanner.scan_trades()
                scanner.enrich_markets()

                crypto_signals = 0
                if alpha_engine:
                    sigs = alpha_engine.get_top_signals(50)
                    crypto_signals = sum(1 for s in sigs if is_crypto(s.ticker))

                layout = Layout()
                layout.split_column(
                    Layout(name="header", size=1),
                    Layout(name="body"),
                )
                layout["body"].split_row(
                    Layout(name="left", ratio=5),
                    Layout(name="right", ratio=4),
                )
                layout["left"].split_column(
                    Layout(name="ladder", ratio=3),
                    Layout(name="updown", ratio=2),
                )
                layout["right"].split_column(
                    Layout(name="alpha", ratio=2),
                    Layout(name="whales", ratio=3),
                )

                layout["header"].update(
                    build_crypto_header(scan_count, scanner, spot, crypto_signals)
                )
                layout["ladder"].update(
                    Panel(build_strike_ladder(scanner, spot), border_style="orange1")
                )
                layout["updown"].update(
                    Panel(build_updown_table(scanner, spot), border_style="blue")
                )
                layout["alpha"].update(
                    Panel(build_crypto_alpha(alpha_engine, spot), border_style="magenta")
                )
                layout["whales"].update(
                    Panel(build_crypto_whales(scanner, spot), border_style="yellow")
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
