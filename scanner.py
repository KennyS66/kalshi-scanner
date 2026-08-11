"""Whale detection and volume analysis engine."""

import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass
class WhaleAlert:
    ticker: str
    contracts: float
    price: float
    side: str  # "yes" or "no"
    taker_side: str  # "bid" or "ask"
    timestamp: datetime
    notional: float = 0.0

    def __post_init__(self):
        self.notional = self.contracts * self.price


@dataclass
class MarketSnapshot:
    ticker: str
    title: str = ""
    subtitle: str = ""
    # From API market data
    volume_24h: float = 0.0
    total_volume: float = 0.0
    open_interest: float = 0.0
    yes_price: float = 0.0
    no_price: float = 0.0
    last_price: float = 0.0
    liquidity: float = 0.0
    # Derived from trade scanning
    trade_count: int = 0
    trade_volume: float = 0.0  # total contracts from trades we've seen
    trade_notional: float = 0.0
    recent_whale_count: int = 0
    recent_whale_volume: float = 0.0
    buy_pressure: float = 0.0  # net yes - no flow
    score: float = 0.0
    # Resolution metadata (populated for BTC strike markets)
    floor_strike: float | None = None
    cap_strike: float | None = None
    close_ts: float | None = None  # epoch seconds when market closes


def _fp(val):
    """Parse a string fixed-point value like '129.45' to float."""
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0.0


def _parse_iso_ts(s):
    """Parse 2026-05-23T19:45:00Z → epoch seconds, or None."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


class Scanner:
    """Scans Kalshi for whale trades and volume anomalies."""

    def __init__(self, api, whale_threshold=50, lookback_minutes=60):
        self.api = api
        self.whale_threshold = whale_threshold
        self.lookback_minutes = lookback_minutes
        self.whale_alerts: list[WhaleAlert] = []
        self.market_snapshots: dict[str, MarketSnapshot] = {}
        self.last_trade_ts = None
        self._seen_trade_ids: dict[str, float] = {}  # trade_id → epoch when first seen
        # Aggregated from all trades we've seen
        # `last_ts` is when we last saw a TRADE for this ticker. It exists so
        # prune_closed can expire stats entries that never had a snapshot --
        # without it they are unreachable and immortal (see prune_closed).
        self._ticker_stats: dict[str, dict] = defaultdict(lambda: {
            "count": 0, "volume": 0.0, "notional": 0.0,
            "yes_vol": 0.0, "no_vol": 0.0,
            "whale_count": 0, "whale_volume": 0.0,
            "last_ts": 0.0,
        })

    def scan_trades(self):
        """Fetch recent trades, detect whales, and build per-ticker stats."""
        cutoff = int(time.time()) - (self.lookback_minutes * 60)
        min_ts = self.last_trade_ts or cutoff

        scan_start_ts = int(time.time())  # capture before API calls so we don't miss trades during slow fetches
        now_ts = float(scan_start_ts)
        new_whales = []
        cursor = None
        new_trade_count = 0

        for _ in range(10):  # max pages
            data = self.api.get_trades(limit=1000, cursor=cursor, min_ts=min_ts)
            trades = data.get("trades", [])
            if not trades:
                break

            for t in trades:
                trade_id = t.get("trade_id", "")
                if trade_id in self._seen_trade_ids:
                    continue
                self._seen_trade_ids[trade_id] = now_ts
                new_trade_count += 1

                ticker = t.get("ticker", "")
                contracts = _fp(t.get("count_fp"))
                yes_price = _fp(t.get("yes_price_dollars"))
                side = t.get("taker_outcome_side", t.get("taker_side", "?"))

                # Aggregate per-ticker stats
                stats = self._ticker_stats[ticker]
                stats["last_ts"] = now_ts
                stats["count"] += 1
                stats["volume"] += contracts
                stats["notional"] += contracts * yes_price
                if side == "yes":
                    stats["yes_vol"] += contracts
                else:
                    stats["no_vol"] += contracts

                # Whale detection
                if contracts >= self.whale_threshold:
                    ts_str = t.get("created_time", "")
                    try:
                        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    except (ValueError, AttributeError):
                        ts = datetime.now(timezone.utc)

                    alert = WhaleAlert(
                        ticker=ticker,
                        contracts=contracts,
                        price=yes_price,
                        side=side,
                        taker_side=t.get("taker_book_side", "?"),
                        timestamp=ts,
                    )
                    new_whales.append(alert)
                    stats["whale_count"] += 1
                    stats["whale_volume"] += contracts

            cursor = data.get("cursor", "")
            if not cursor:
                break

        self.last_trade_ts = scan_start_ts
        # Merge new alerts and prune anything older than 4 hours
        _whale_cutoff = time.time() - 4 * 3600
        self.whale_alerts = [
            a for a in (new_whales + self.whale_alerts)
            if a.timestamp and a.timestamp.timestamp() >= _whale_cutoff
        ]

        # Prune seen IDs older than 24 hours
        _id_cutoff = now_ts - 86400
        self._seen_trade_ids = {
            tid: ts for tid, ts in self._seen_trade_ids.items()
            if ts >= _id_cutoff
        }

        return new_whales, new_trade_count

    def prune_closed(self, grace_s: float = 900.0,
                     stats_ttl_s: float = 7200.0) -> int:
        """Forget markets that closed more than `grace_s` ago, and trade stats
        for tickers not seen in `stats_ttl_s`. Returns the count forgotten.

        Without this both `market_snapshots` and `_ticker_stats` grow forever.
        That is not merely a memory leak: `enrich_markets` derives its fetch
        list from `_ticker_stats`, and its `priority` set (every "15M" ticker)
        is UNCAPPED -- so every 15-minute market ever seen, across the whole
        strike ladder, was re-fetched from the Kalshi API on every 5s cycle.

        Measured on 2026-08-09, same box, same code: a freshly started process
        sat at 134MB / 11% of a core, while one 5 days old was at 2462MB / 102%.
        The saturated GIL starved uvicorn's event loop in this same process and
        pushed /api/crypto/signal's p90 to 4.22s against the 4s timeout in
        swing_bot.fetch_signal -- roughly 100 silent feed_down events a day.

        `grace_s` defaults to 15 minutes, comfortably past the 120s window
        /api/crypto/signal allows for just-expired markets, so pruning can
        never race that lookup. Snapshots whose close_ts is unknown (built by
        the batch-fetch fallback path) are always kept; they get a real
        close_ts on the next successful scan.
        """
        now = time.time()
        dead = [t for t, snap in self.market_snapshots.items()
                if snap.close_ts is not None and now - snap.close_ts > grace_s]
        for t in dead:
            self.market_snapshots.pop(t, None)
            self._ticker_stats.pop(t, None)

        # Stats entries that never had a snapshot. scan_trades creates one for
        # EVERY ticker that prints a trade (defaultdict), so the loop above --
        # which can only see tickers that have a snapshot -- never reaches
        # them. They were immortal: 2669 stats against 288 snapshots on
        # 2026-08-10, each one an uncapped API fetch every 5s cycle.
        #
        # A live snapshot always wins over trade staleness: a market that is
        # open but quiet must not be forgotten just because nobody traded it.
        stale = [t for t, st in self._ticker_stats.items()
                 if t not in self.market_snapshots
                 and now - st.get("last_ts", 0.0) > stats_ttl_s]
        for t in stale:
            self._ticker_stats.pop(t, None)

        return len(dead) + len(stale)

    def enrich_markets(self):
        """Fetch market metadata for tickers we've seen in trades."""
        self.prune_closed()
        all_tickers = list(self._ticker_stats.keys())
        # Always include crypto 15m tickers so whale data loads immediately at market open
        priority = {t for t in all_tickers if "15M" in t and t.startswith("KX")}
        rest = sorted(
            [t for t in all_tickers if t not in priority],
            key=lambda t: self._ticker_stats[t]["volume"],
            reverse=True,
        )[:200]
        active_tickers = list(priority) + rest

        # Batch fetch in groups of 100 (API limit for tickers param)
        for i in range(0, len(active_tickers), 100):
            batch = active_tickers[i:i+100]
            try:
                data = self.api.get_markets_by_tickers(batch)
                for m in data.get("markets", []):
                    ticker = m.get("ticker", "")
                    stats = self._ticker_stats.get(ticker, {})

                    close_ts = _parse_iso_ts(m.get("close_time"))
                    self.market_snapshots[ticker] = MarketSnapshot(
                        ticker=ticker,
                        title=m.get("title", ticker),
                        subtitle=m.get("subtitle", ""),
                        volume_24h=_fp(m.get("volume_24h_fp")),
                        total_volume=_fp(m.get("volume_fp")),
                        open_interest=_fp(m.get("open_interest_fp")),
                        yes_price=_fp(m.get("yes_ask_dollars")) or _fp(m.get("last_price_dollars")),
                        no_price=_fp(m.get("no_ask_dollars")),
                        last_price=_fp(m.get("last_price_dollars")),
                        liquidity=_fp(m.get("liquidity_dollars")),
                        trade_count=stats.get("count", 0),
                        trade_volume=stats.get("volume", 0),
                        trade_notional=stats.get("notional", 0),
                        recent_whale_count=stats.get("whale_count", 0),
                        recent_whale_volume=stats.get("whale_volume", 0),
                        buy_pressure=stats.get("yes_vol", 0) - stats.get("no_vol", 0),
                        floor_strike=float(m["floor_strike"]) if m.get("floor_strike") else None,
                        cap_strike=float(m["cap_strike"]) if m.get("cap_strike") else None,
                        close_ts=close_ts,
                    )
            except Exception:
                # If batch fetch fails, build snapshots from trade data alone
                for ticker in batch:
                    if ticker not in self.market_snapshots:
                        stats = self._ticker_stats.get(ticker, {})
                        self.market_snapshots[ticker] = MarketSnapshot(
                            ticker=ticker,
                            title=ticker,
                            trade_count=stats.get("count", 0),
                            trade_volume=stats.get("volume", 0),
                            trade_notional=stats.get("notional", 0),
                            recent_whale_count=stats.get("whale_count", 0),
                            recent_whale_volume=stats.get("whale_volume", 0),
                            buy_pressure=stats.get("yes_vol", 0) - stats.get("no_vol", 0),
                        )

    def score_markets(self):
        """Score markets by composite activity."""
        if not self.market_snapshots:
            return []

        all_snaps = list(self.market_snapshots.values())

        max_trade_vol = max((s.trade_volume for s in all_snaps), default=1) or 1
        max_trade_not = max((s.trade_notional for s in all_snaps), default=1) or 1
        max_whale_vol = max((s.recent_whale_volume for s in all_snaps), default=1) or 1
        max_whale_count = max((s.recent_whale_count for s in all_snaps), default=1) or 1
        max_oi = max((s.open_interest for s in all_snaps), default=1) or 1
        max_trades = max((s.trade_count for s in all_snaps), default=1) or 1

        for s in all_snaps:
            s.score = (
                (s.trade_volume / max_trade_vol) * 0.25
                + (s.trade_notional / max_trade_not) * 0.15
                + (s.recent_whale_volume / max_whale_vol) * 0.25
                + (s.recent_whale_count / max_whale_count) * 0.10
                + (s.open_interest / max_oi) * 0.10
                + (s.trade_count / max_trades) * 0.15
            )

        return sorted(all_snaps, key=lambda s: s.score, reverse=True)

    def get_top_markets(self, n=20):
        return self.score_markets()[:n]

    def get_volume_leaders(self, n=15):
        snaps = list(self.market_snapshots.values())
        return sorted(snaps, key=lambda s: s.trade_volume, reverse=True)[:n]

    def get_whale_magnets(self, n=15):
        """Markets attracting the most whale activity."""
        snaps = list(self.market_snapshots.values())
        return sorted(snaps, key=lambda s: s.recent_whale_volume, reverse=True)[:n]
