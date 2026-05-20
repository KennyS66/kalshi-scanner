"""
Whale-triggered market research.

For every market with recent whale activity, aggregate every available alpha
signal (flow divergence, momentum, cross-market arb, sportsbook odds) plus the
whale flow itself into a single verdict:

    UP    = trade YES  (price expected to rise toward 1.00)
    DOWN  = trade NO   (price expected to fall toward 0.00)
    NEUTRAL = signals conflict or are too weak to act on

Reuses Scanner (whale detection + market snapshots) and AlphaEngine (per-signal
direction calls) from the existing scanner.
"""

import math
from collections import defaultdict
from dataclasses import dataclass, field

from rich.table import Table
from rich.text import Text


@dataclass
class WhaleVerdict:
    ticker: str
    title: str
    price: float
    whale_count: int
    whale_volume: float
    whale_notional: float
    net_whale_side: str        # "yes" / "no" / "mixed"
    whale_conviction: float    # 0..1 — how one-sided the whale flow is
    verdict: str               # "UP" / "DOWN" / "NEUTRAL"
    confidence: float          # 0..1 — net of all signals
    fair_value: float
    edge_cents: float
    signal_count: int
    signal_types: list = field(default_factory=list)
    reasoning: str = ""


def _whale_flow_by_ticker(scanner):
    """Aggregate whale YES vs NO contracts per ticker from the recent alert list."""
    flow = defaultdict(lambda: {"yes": 0.0, "no": 0.0, "notional": 0.0})
    for w in scanner.whale_alerts:
        bucket = flow[w.ticker]
        if w.side == "yes":
            bucket["yes"] += w.contracts
        elif w.side == "no":
            bucket["no"] += w.contracts
        bucket["notional"] += w.notional
    return flow


def research_whale_markets(scanner, alpha_engine, min_whales=1, min_notional=100):
    """Run research on every market with recent whale activity and produce verdicts."""
    # All alpha signals, grouped by ticker. AlphaEngine.scan() already dedupes
    # (ticker, signal_type) keeping the strongest of each type.
    sigs_by_ticker = defaultdict(list)
    for s in alpha_engine.scan():
        sigs_by_ticker[s.ticker].append(s)

    whale_flow = _whale_flow_by_ticker(scanner)
    verdicts = []

    for ticker, snap in scanner.market_snapshots.items():
        if snap.recent_whale_count < min_whales:
            continue
        if snap.trade_notional < min_notional:
            continue
        if "KXMVE" in ticker:  # parlays — no real price discovery
            continue

        price = snap.last_price or snap.yes_price
        if price <= 0.02 or price >= 0.98:
            continue

        flow = whale_flow.get(ticker, {"yes": 0.0, "no": 0.0, "notional": 0.0})
        whale_total = flow["yes"] + flow["no"]
        if whale_total == 0:
            continue

        if flow["yes"] > flow["no"]:
            net_side = "yes"
            conviction = flow["yes"] / whale_total
        elif flow["no"] > flow["yes"]:
            net_side = "no"
            conviction = flow["no"] / whale_total
        else:
            net_side = "mixed"
            conviction = 0.5

        # Sum signal strength toward each side
        my_sigs = sigs_by_ticker.get(ticker, [])
        yes_score = sum(s.strength for s in my_sigs if s.direction == "yes")
        no_score = sum(s.strength for s in my_sigs if s.direction == "no")

        # Whale flow contributes its own weighted vote: conviction × size factor
        size_factor = min(math.log1p(snap.recent_whale_volume) / 5.0, 1.0)
        whale_weight = conviction * size_factor
        if net_side == "yes":
            yes_score += whale_weight
        elif net_side == "no":
            no_score += whale_weight

        total_score = yes_score + no_score
        if total_score == 0:
            verdict, confidence = "NEUTRAL", 0.0
            fair_value = price
        elif yes_score > no_score:
            verdict = "UP"
            confidence = (yes_score - no_score) / total_score
            yes_fairs = [s.fair_value for s in my_sigs
                         if s.direction == "yes" and s.fair_value > 0]
            fair_value = (sum(yes_fairs) / len(yes_fairs)) if yes_fairs \
                         else min(price + 0.10 * confidence, 0.95)
        else:
            verdict = "DOWN"
            confidence = (no_score - yes_score) / total_score
            no_fairs = [s.fair_value for s in my_sigs
                        if s.direction == "no" and s.fair_value > 0]
            fair_value = (sum(no_fairs) / len(no_fairs)) if no_fairs \
                         else max(price - 0.10 * confidence, 0.05)

        if confidence < 0.15 and verdict != "NEUTRAL":
            verdict = "NEUTRAL"

        sig_types = sorted({s.signal_type for s in my_sigs})
        sig_summary = ", ".join(sig_types) if sig_types else "whale flow only"
        reasoning = (
            f"{snap.recent_whale_count} whales, {snap.recent_whale_volume:.0f} contracts "
            f"({conviction:.0%} {net_side.upper()}). "
            f"Signals: {sig_summary}. "
            f"YES score {yes_score:.2f} vs NO score {no_score:.2f}."
        )

        verdicts.append(WhaleVerdict(
            ticker=ticker,
            title=snap.title or ticker,
            price=price,
            whale_count=snap.recent_whale_count,
            whale_volume=snap.recent_whale_volume,
            whale_notional=flow["notional"],
            net_whale_side=net_side,
            whale_conviction=round(conviction, 3),
            verdict=verdict,
            confidence=round(confidence, 3),
            fair_value=round(fair_value, 3),
            edge_cents=round((fair_value - price) * 100, 1),
            signal_count=len(my_sigs),
            signal_types=sig_types,
            reasoning=reasoning,
        ))

    verdicts.sort(key=lambda v: (v.verdict == "NEUTRAL", -v.confidence))
    return verdicts


def build_verdict_table(verdicts, limit=25):
    table = Table(title="WHALE RESEARCH — UP / DOWN VERDICTS", expand=True)
    table.add_column("Ticker", style="cyan", no_wrap=True, max_width=32)
    table.add_column("Title", max_width=38, overflow="ellipsis")
    table.add_column("Price", justify="right")
    table.add_column("Whales", justify="right")
    table.add_column("Flow", justify="center")
    table.add_column("Verdict", justify="center")
    table.add_column("Conf", justify="right")
    table.add_column("Fair", justify="right")
    table.add_column("Edge", justify="right")
    table.add_column("Signals", max_width=20, overflow="ellipsis")

    for v in verdicts[:limit]:
        if v.verdict == "UP":
            verdict_text = Text("UP", style="bold green")
        elif v.verdict == "DOWN":
            verdict_text = Text("DOWN", style="bold red")
        else:
            verdict_text = Text("—", style="dim")

        if v.net_whale_side == "yes":
            flow_text = Text(f"YES {v.whale_conviction:.0%}", style="green")
        elif v.net_whale_side == "no":
            flow_text = Text(f"NO {v.whale_conviction:.0%}", style="red")
        else:
            flow_text = Text("mixed", style="yellow")

        edge_style = "green" if v.edge_cents > 0 else ("red" if v.edge_cents < 0 else "dim")
        edge_text = Text(f"{v.edge_cents:+.1f}¢", style=edge_style)

        table.add_row(
            v.ticker,
            v.title,
            f"${v.price:.2f}",
            f"{v.whale_count} ({v.whale_volume:.0f})",
            flow_text,
            verdict_text,
            f"{v.confidence:.0%}",
            f"${v.fair_value:.2f}",
            edge_text,
            ", ".join(v.signal_types) or "—",
        )

    return table


def verdicts_to_json(verdicts):
    return [
        {
            "ticker": v.ticker,
            "title": v.title,
            "price": v.price,
            "whale_count": v.whale_count,
            "whale_volume": v.whale_volume,
            "whale_notional": round(v.whale_notional, 2),
            "net_whale_side": v.net_whale_side,
            "whale_conviction": v.whale_conviction,
            "verdict": v.verdict,
            "confidence": v.confidence,
            "fair_value": v.fair_value,
            "edge_cents": v.edge_cents,
            "signal_count": v.signal_count,
            "signal_types": v.signal_types,
            "reasoning": v.reasoning,
        }
        for v in verdicts
    ]
