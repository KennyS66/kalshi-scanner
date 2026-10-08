"""Rich terminal dashboard for the Kalshi whale/volume scanner."""

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


# ── Crypto Spot Tracker ──────────────────────────────────────────────

# Symbols Kalshi runs prediction markets on. Extend if Kalshi adds more.
CRYPTO_SYMBOLS = [
    "BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "AVAX", "BNB", "LINK", "LTC",
]


class CryptoSpot:
    """Fetches and caches spot prices for all tracked crypto symbols in one call."""

    def __init__(self, symbols=None):
        self.symbols = symbols or CRYPTO_SYMBOLS
        self.prices: dict[str, float] = {}
        self.prev_prices: dict[str, float] = {}
        self._last_fetch = 0

    def fetch(self):
        """Pull all rates in one request, cached for 10 seconds."""
        if time.time() - self._last_fetch < 10:
            return self.prices
        try:
            r = requests.get(
                "https://api.coinbase.com/v2/exchange-rates",
                params={"currency": "USD"},
                timeout=5,
            )
            rates = (r.json() or {}).get("data", {}).get("rates", {})
            self.prev_prices = dict(self.prices)
            for sym in self.symbols:
                rate = rates.get(sym)
                if not rate:
                    continue
                try:
                    f = float(rate)
                    if f > 0:
                        self.prices[sym] = 1.0 / f
                except (ValueError, TypeError):
                    pass
            self._last_fetch = time.time()
        except Exception:
            pass
        return self.prices

    def get(self, symbol):
        return self.prices.get(symbol, 0.0)

    def direction_of(self, symbol):
        prev = self.prev_prices.get(symbol, 0.0)
        cur = self.prices.get(symbol, 0.0)
        if not prev:
            return ""
        if cur > prev:
            return "up"
        if cur < prev:
            return "down"
        return "flat"

    # ── Backward-compat BTC-specific surface ─────────────────────────
    @property
    def price(self):
        return self.prices.get("BTC", 0.0)

    @property
    def prev_price(self):
        return self.prev_prices.get("BTC", 0.0)

    @property
    def direction(self):
        return self.direction_of("BTC")


# Old name kept so external callers keep working.
BTCPrice = CryptoSpot



_loop_err_last: dict = {}


def _log_loop_error(where: str, e: Exception, every_s: float = 60.0) -> None:
    """Scan-loop errors to stderr with a traceback, at most once a minute per
    (where, message) so a 429 storm on a 5s cycle cannot flood the log."""
    import sys
    import traceback
    key = (where, str(e)[:200])
    now = time.time()
    if now - _loop_err_last.get(key, 0.0) < every_s:
        return
    _loop_err_last[key] = now
    print(f"scanner ERROR [{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now))}] "
          f"{where}: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
    traceback.print_exc(file=sys.stderr)

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


def format_spot(price):
    """Format spot price scaled to coin magnitude."""
    if price >= 1000:
        return f"${price:,.0f}"
    if price >= 100:
        return f"${price:,.1f}"
    if price >= 10:
        return f"${price:,.2f}"
    if price >= 1:
        return f"${price:,.3f}"
    return f"${price:.4f}"


_CRYPTO_RE = re.compile(
    r'^KX(BTC|ETH|SOL|XRP|DOGE|ADA|AVAX|BNB|LINK|LTC)(?:[A-Z0-9]*)?-'
)


def crypto_symbol(ticker):
    """Return BTC/ETH/SOL/... if this ticker is a crypto market, else None."""
    if not ticker:
        return None
    m = _CRYPTO_RE.match(ticker)
    return m.group(1) if m else None


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


def label_market(title, ticker, spots=None):
    """Add spot-relative label to crypto market titles.

    `spots` may be a {symbol: price} dict (preferred) or a bare float (treated
    as BTC, for backward compatibility with callers that only tracked BTC).
    """
    if not spots:
        return title
    if isinstance(spots, (int, float)):
        spots = {"BTC": float(spots)} if spots > 0 else {}
    if not spots:
        return title

    sym = crypto_symbol(ticker)
    if not sym:
        return title
    spot = spots.get(sym, 0)
    if not spot:
        return title

    strike, stype = _extract_strike(ticker)
    if strike is None:
        return title

    # Format width depends on magnitude (BTC vs DOGE)
    if spot >= 1000:
        sval, kval = f"{strike:,.0f}", f"{spot:,.0f}"
        dval = lambda d: f"{d:+,.0f}"
        near = strike * 0.002 + 50
    elif spot >= 10:
        sval, kval = f"{strike:,.2f}", f"{spot:,.2f}"
        dval = lambda d: f"{d:+,.2f}"
        near = strike * 0.002
    else:
        sval, kval = f"{strike:,.4f}", f"{spot:,.4f}"
        dval = lambda d: f"{d:+,.4f}"
        near = strike * 0.005

    diff = spot - strike
    if stype == "above":
        if diff > 0:
            return f"{sym} >${sval} [ITM {dval(diff)}]"
        return f"{sym} >${sval} [OTM {dval(diff)}]"
    if stype == "bracket":
        if abs(diff) < near:
            return f"{sym} ~${sval} [AT SPOT]"
        return f"{sym} ${sval} [spot {dval(diff)}]"

    return title


# ── Alpha Signals Table ──────────────────────────────────────────────

SIGNAL_STYLES = {
    "flow_divergence": ("bold magenta", "FLOW"),
    "arb":             ("bold red",     "ARB"),
    "momentum":        ("bold yellow",  "MNTM"),
    "odds_edge":       ("bold cyan",    "ODDS"),
}


def build_alpha_table(signals, limit=15, spots=None):
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

        display = label_market(s.title, s.ticker, spots) if spots else s.title
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

def build_top_markets_table(markets, spots=None):
    table = Table(title="Top Markets by Activity Score", expand=True, padding=(0, 2))
    table.add_column("#", min_width=3, style="dim")
    table.add_column("Market", style="cyan", min_width=38)
    table.add_column("Trades", justify="right", min_width=7)
    table.add_column("Volume", justify="right", min_width=9)
    table.add_column("Notional", justify="right", min_width=10)
    table.add_column("OI", justify="right", min_width=8)
    table.add_column("Whales", justify="right", min_width=7)
    table.add_column("Flow", justify="right", min_width=9)
    table.add_column("Score", min_width=10)

    for i, m in enumerate(markets, 1):
        score_style = "bold green" if m.score > 0.6 else "yellow" if m.score > 0.3 else "white"
        whale_style = "bold red" if m.recent_whale_count >= 5 else "yellow" if m.recent_whale_count >= 2 else "white"
        flow_style = "green" if m.buy_pressure > 0 else "red" if m.buy_pressure < 0 else "dim"
        flow_prefix = "+" if m.buy_pressure > 0 else ""

        filled = round(m.score * 8)
        bar = "█" * filled + "░" * (8 - filled)
        score_text = Text(f"{bar} {m.score:.0%}", style=score_style)

        display = label_market(m.title, m.ticker, spots) if spots else m.title
        if display == m.title:
            display = m.title if m.title != m.ticker else m.ticker
        table.add_row(
            str(i),
            trunc(display, 45),
            str(m.trade_count),
            format_contracts(m.trade_volume),
            format_dollars(m.trade_notional),
            format_contracts(m.open_interest) if m.open_interest else "-",
            Text(str(m.recent_whale_count), style=whale_style),
            Text(f"{flow_prefix}{format_contracts(m.buy_pressure)}", style=flow_style),
            score_text,
        )

    return table


# ── Whale Magnets Table ──────────────────────────────────────────────

def build_whale_magnets_table(markets, spots=None):
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
        display = label_market(m.title, m.ticker, spots) if spots else m.title
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


# ── Crypto Screen ────────────────────────────────────────────────────

def build_crypto_header(spots, prev_spots=None, symbols=None):
    """One-line spot strip across tracked coins."""
    symbols = symbols or CRYPTO_SYMBOLS
    if not spots:
        return Text.from_markup("[dim]fetching crypto spot prices...[/]")
    parts = []
    for sym in symbols:
        price = spots.get(sym)
        if not price:
            continue
        prev = (prev_spots or {}).get(sym, 0)
        if prev and price > prev:
            arrow = "[green]^[/]"
        elif prev and price < prev:
            arrow = "[red]v[/]"
        else:
            arrow = ""
        parts.append(f"[bold orange1]{sym}[/] {format_spot(price)}{arrow}")
    return Text.from_markup("  |  ".join(parts) if parts else "[dim]no spots[/]")


def build_crypto_screen_table(scanner, spots, limit=40):
    """Crypto markets table: strike, spot, distance, Kalshi price, edge flag.

    Sort: by symbol, then by closeness to strike (|spot - strike|) ascending.
    Markets near their strike are the ones where price discovery is happening.
    """
    spots = spots or {}
    rows = []
    for ticker, snap in scanner.market_snapshots.items():
        sym = crypto_symbol(ticker)
        if not sym:
            continue
        strike, stype = _extract_strike(ticker)
        if strike is None or not stype:
            continue
        spot = spots.get(sym, 0.0)
        yes_price = snap.yes_price or snap.last_price
        last = snap.last_price
        rows.append({
            "sym": sym, "ticker": ticker, "snap": snap,
            "strike": strike, "stype": stype, "spot": spot,
            "yes": yes_price, "last": last,
        })

    def sort_key(r):
        # Coins we have spot for first; then by distance, then activity.
        has_spot = 0 if r["spot"] else 1
        dist = abs(r["spot"] - r["strike"]) / r["strike"] if r["spot"] else 1e9
        return (r["sym"], has_spot, dist, -r["snap"].trade_volume)

    rows.sort(key=sort_key)
    rows = rows[:limit]

    table = Table(title="Crypto Screen — strike vs spot", expand=True, padding=(0, 1))
    table.add_column("Sym", style="bold orange1", width=4)
    table.add_column("Strike", justify="right", width=11)
    table.add_column("Spot", justify="right", width=11)
    table.add_column("Δ", justify="right", width=10)
    table.add_column("%", justify="right", width=6)
    table.add_column("T", width=2)
    table.add_column("Yes", justify="right", width=5)
    table.add_column("Last", justify="right", width=5)
    table.add_column("Vol", justify="right", width=7)
    table.add_column("Wh", justify="right", width=3)
    table.add_column("Flow", justify="right", width=7)
    table.add_column("Signal", width=10)

    for r in rows:
        snap = r["snap"]
        sym, strike, stype, spot = r["sym"], r["strike"], r["stype"], r["spot"]
        yes, last = r["yes"], r["last"]

        # Strike / spot formatting matches the coin's magnitude
        ref = spot or strike
        if ref >= 1000:
            sval, kval = f"${strike:,.0f}", (f"${spot:,.0f}" if spot else "?")
            dfmt = lambda d: f"{d:+,.0f}"
        elif ref >= 10:
            sval, kval = f"${strike:,.2f}", (f"${spot:,.2f}" if spot else "?")
            dfmt = lambda d: f"{d:+,.2f}"
        else:
            sval, kval = f"${strike:,.4f}", (f"${spot:,.4f}" if spot else "?")
            dfmt = lambda d: f"{d:+,.4f}"

        diff = (spot - strike) if spot else 0
        diff_pct = (diff / strike * 100) if (spot and strike) else 0
        diff_str = dfmt(diff) if spot else "?"
        pct_str = f"{diff_pct:+.2f}%" if spot else "?"
        diff_color = "green" if diff > 0 else ("red" if diff < 0 else "dim")

        type_label = ">" if stype == "above" else ("B" if stype == "bracket" else "?")

        # Edge detection: only for "above" (binary settle) markets
        signal = ""
        sig_style = "dim"
        actual = last or yes
        if stype == "above" and actual and spot and strike:
            # Bands are proportional to strike. AT-strike is a tight ATM zone;
            # EDGE requires both clear distance AND a disagreeing price.
            atm_band = strike * 0.002    # ~0.2%: essentially at the money
            edge_band = strike * 0.01    # ~1%: clearly one side
            if abs(diff) <= atm_band and 0.20 < actual < 0.80:
                signal, sig_style = "AT STRIKE", "bold yellow"
            elif diff > edge_band and actual < 0.60:
                signal, sig_style = "EDGE UP", "bold green"
            elif diff < -edge_band and actual > 0.40:
                signal, sig_style = "EDGE DOWN", "bold red"

        flow = snap.buy_pressure
        flow_style = "green" if flow > 0 else ("red" if flow < 0 else "dim")
        flow_prefix = "+" if flow > 0 else ""
        flow_str = (f"{flow_prefix}{format_contracts(flow)}") if flow else "-"

        table.add_row(
            sym,
            sval,
            kval,
            Text(diff_str, style=diff_color),
            Text(pct_str, style=diff_color),
            type_label,
            f"${yes:.2f}" if yes else "-",
            f"${last:.2f}" if last else "-",
            format_contracts(snap.trade_volume) if snap.trade_volume else "-",
            str(snap.recent_whale_count) if snap.recent_whale_count else "0",
            Text(flow_str, style=flow_style),
            Text(signal, style=sig_style),
        )

    if not rows:
        table.add_row("--", "no crypto markets", "--", "--", "--", "--", "--", "--", "--", "--", "--", "--")

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
    """Main dashboard loop with alpha signals + crypto screen."""
    console = Console()
    scan_count = 0
    total_trades = 0
    crypto = CryptoSpot()

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

                # Fetch all crypto spot prices in one call
                crypto.fetch()
                spots = crypto.prices
                prev_spots = crypto.prev_prices

                # Scan trades. A scan failure must not skip enrich_markets:
                # that is what emptied market_snapshots for ~3h on 2026-10-07.
                try:
                    new_whales, new_trades = scanner.scan_trades()
                    total_trades += new_trades
                except Exception as e:
                    _log_loop_error("scan_trades", e)

                # Enrich markets
                scanner.enrich_markets()

                # Alpha signals
                alpha_signals = []
                if alpha_engine:
                    alpha_signals = alpha_engine.get_top_signals(15)

                # Ranked views
                top_markets = scanner.get_top_markets(20)
                whale_magnets = scanner.get_whale_magnets(12)

                # Build layout
                layout = Layout()
                layout.split_column(
                    Layout(name="header", size=1),
                    Layout(name="spots", size=1),
                    Layout(name="body"),
                )
                layout["body"].split_row(
                    Layout(name="left", ratio=1),
                    Layout(name="right", ratio=2),
                )
                layout["left"].split_column(
                    Layout(name="alpha", ratio=2),
                )
                layout["right"].split_column(
                    Layout(name="top", ratio=5),
                    Layout(name="magnets", ratio=2),
                    Layout(name="crypto", ratio=2),
                )

                layout["header"].update(
                    build_status_bar(
                        scan_count,
                        len(scanner.market_snapshots),
                        total_trades,
                        len(scanner.whale_alerts),
                        len(alpha_signals),
                        scanner.whale_threshold,
                        btc_price=crypto,
                    )
                )
                layout["spots"].update(build_crypto_header(spots, prev_spots))

                layout["alpha"].update(
                    Panel(build_alpha_table(alpha_signals, spots=spots), border_style="magenta")
                )
                layout["top"].update(
                    Panel(build_top_markets_table(top_markets, spots=spots), border_style="green")
                )
                layout["magnets"].update(
                    Panel(build_whale_magnets_table(whale_magnets, spots=spots), border_style="red")
                )
                layout["crypto"].update(
                    Panel(build_crypto_screen_table(scanner, spots, limit=20), border_style="orange1")
                )

                live.update(layout)
                time.sleep(refresh_seconds)

            except KeyboardInterrupt:
                break
            except Exception as e:
                # The Rich panel is invisible under systemd (stdout is a log
                # file); stderr lands in logs/scanner_supervised.log.
                _log_loop_error("dashboard cycle", e)
                live.update(
                    Panel(
                        f"[red]Error: {e}[/]\n\nRetrying in {refresh_seconds}s...",
                        title="Scanner Error",
                    )
                )
                time.sleep(refresh_seconds)
