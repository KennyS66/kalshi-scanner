"""Whale detection and volume analysis engine."""

import sys
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


# Fetch-list bounds, per 5s enrich cycle. PRIORITY_CAP is ~10x the measured
# live count of 15M tickers (14 on 2026-08-10) so it never binds in normal
# operation; it exists to stop an unbounded fetch list if that assumption
# ever breaks. REST_CAP is the long-standing top-by-volume limit.
PRIORITY_CAP = 150
REST_CAP = 200

# whale_alerts bounds. The 4h window is long-standing; the count cap is the
# backstop it never had -- it reached 665,889 alerts on 2026-08-11, 90% of
# all live objects in the process. 150k holds ~54min at the observed burst
# rate (~166k/hour), which is >5 half-lives of the 10-minute EWMA that
# consumes it, so the decayed signals are unaffected. Worst case ~60MB on a
# 31GB box. A quiet evening sits near 18k, so it does not bind normally.
WHALE_WINDOW_S = 4 * 3600
MAX_WHALE_ALERTS = 150_000

# `_seen_trade_ids` retention floor. That dict maps trade_id -> epoch first
# seen and exists for exactly one purpose: skip trades a previous scan already
# processed. It therefore only has to cover the furthest back scan_trades can
# ever re-fetch, and that is bounded by
# `min_ts = self.last_trade_ts or (now - lookback_minutes*60)` -- the API is
# never asked for trades older than `lookback_minutes` (60 by default), and
# normally only back to the previous scan's start, ~5s ago.
#
# It was pruned with a hardcoded 86400 (24h), ~24x beyond anything reachable.
# Measured on the live scanner 2026-08-14, pid 10068 (steady state, confirmed
# twice 45s apart): 9,013,616 ids at 159 bytes each = 1,373 MB, which is 84%
# of the process's entire 1,631 MB RSS. health_check.py had been reporting
# DEGRADED on footprint:scanner against its 1500 MB limit.
#
# The same dict caused the CPU, not a second bug: rebuilding one that size
# costs ~2.9s and the rebuild below runs every scan cycle (nominal 5s), i.e.
# ~58% of a core against 61% observed.
#
# Do NOT restore 86400 believing it was safety margin -- it buys nothing
# scan_trades can reach. It hid for so long because gc_objects sat flat at
# ~225k across the whole growth curve: gc.get_objects() only tracks
# containers, so nine million str keys are invisible to it and every
# object-counting diagnostic showed a healthy process.
#
# The floor keeps a small `--lookback` from shrinking the window to something
# fragile: at 15 minutes the scan loop can stall for 180 nominal cycles and
# dedup still holds. Should a gap ever exceed the retention anyway, the
# failure is re-processing (duplicate whale alerts, double-counted ticker
# stats), not corruption -- and the old 24h value had the identical failure
# mode, just at a 24h threshold.
SEEN_ID_RETENTION_FLOOR_S = 15 * 60

# Trade paging bound: 10 x 1000 = 10,000 trades per scan. At the measured
# exchange rate (~104 trades/s daily average, ~163/s at the evening peak) that
# is 61-96 seconds of tape against a nominal 5s cycle, so it is not believed to
# bind today -- but api.py allows 15s per request across up to 10 pages, so a
# slow sequence can reach it. When it does, the untaken pages are skipped
# PERMANENTLY: `last_trade_ts` advances to `scan_start_ts` regardless, so the
# next scan starts past them. That was entirely silent -- no counter, no log,
# no alarm. See the for/else in scan_trades for how it is now detected.
MAX_TRADE_PAGES = 10


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
        # Cumulative count of scans that hit MAX_TRADE_PAGES with a cursor
        # still outstanding, i.e. cycles that dropped trades for good.
        self.truncated_scans = 0
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

    def _seen_id_retention_s(self) -> float:
        """How long a trade_id stays in `_seen_trade_ids`.

        The re-fetch horizon, floored. See SEEN_ID_RETENTION_FLOOR_S for why
        this tracks `lookback_minutes` rather than the 86400 it replaced.
        """
        return max(self.lookback_minutes * 60, SEEN_ID_RETENTION_FLOOR_S)

    def scan_trades(self):
        """Fetch recent trades, detect whales, and build per-ticker stats."""
        cutoff = int(time.time()) - (self.lookback_minutes * 60)
        min_ts = self.last_trade_ts or cutoff

        scan_start_ts = int(time.time())  # capture before API calls so we don't miss trades during slow fetches
        now_ts = float(scan_start_ts)
        new_whales = []
        cursor = None
        new_trade_count = 0

        truncated = False

        for _ in range(MAX_TRADE_PAGES):
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
        else:
            # for/else runs only when the loop finished every page WITHOUT
            # breaking -- i.e. page MAX_TRADE_PAGES came back with trades AND
            # still handed us a cursor. That is exactly the case where more
            # trades existed and we stopped anyway.
            #
            # Note this deliberately never inspects `cursor` after the loop,
            # which is how it avoids the false positive: when a page comes back
            # empty the loop breaks with `cursor` still holding the PREVIOUS
            # page's non-empty value, so an `if pages == MAX and cursor` test
            # would cry wolf every time the tape ended exactly on the cap.
            truncated = True

        self.last_trade_ts = scan_start_ts
        self.whale_alerts = self._merge_whale_alerts(new_whales)

        if truncated:
            # Observability only -- the cap and the fetch behaviour are
            # unchanged. last_trade_ts has just advanced past the trades we
            # never fetched, so this loss is not recoverable on a later scan.
            self.truncated_scans += 1
            print(f"scanner WARNING: trade page cap hit -- fetched "
                  f"{MAX_TRADE_PAGES} pages x 1000 and the API still had more "
                  f"(cursor outstanding). Trades this cycle were SKIPPED and "
                  f"are irrecoverable: last_trade_ts advanced to "
                  f"{scan_start_ts} regardless, so the next scan starts past "
                  f"them. processed={new_trade_count} min_ts={min_ts} "
                  f"truncated_scans={self.truncated_scans}",
                  file=sys.stderr, flush=True)

        # Prune seen IDs past the re-fetch horizon (see SEEN_ID_RETENTION_FLOOR_S)
        _id_cutoff = now_ts - self._seen_id_retention_s()
        self._seen_trade_ids = {
            tid: ts for tid, ts in self._seen_trade_ids.items()
            if ts >= _id_cutoff
        }

        return new_whales, new_trade_count

    def _merge_whale_alerts(self, new_whales, now: float | None = None,
                            window_s: float = WHALE_WINDOW_S,
                            max_alerts: int | None = None):
        """Newest-first, aged out past `window_s`, then capped by count.

        The count cap is the part that was missing. A 4-hour window alone
        let this reach 665,889 alerts on 2026-08-11 -- 90% of every live
        Python object in the process, at RSS 1553MB against a fresh 86MB.
        It is also rescanned in full, per ticker, per cycle, by
        `_ewma_whale_flow` (alpha.py) and the flow block in web.py.

        Order matters as much as the bound: `new_whales` goes first, so the
        list stays newest-first, and the cap therefore drops the OLDEST.
        Every consumer takes the head (`rows[:80]`, `[:200]`, `[:50]`,
        `[:limit]`) and every analytical one decays at a 8-10 minute half
        life, so capping the other end would satisfy the bound while
        quietly destroying the signals.
        """
        now = time.time() if now is None else now
        if max_alerts is None:
            max_alerts = MAX_WHALE_ALERTS
        cutoff = now - window_s
        merged = [a for a in (list(new_whales) + self.whale_alerts)
                  if a.timestamp and a.timestamp.timestamp() >= cutoff]
        return merged[:max_alerts]

    def prune_closed(self, grace_s: float = 900.0,
                     stats_ttl_s: float = 3600.0) -> int:
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
        # Measured arrival rate on 2026-08-10: ~42,600 new stats entries per
        # HOUR, because every Kalshi market that prints a trade lands here
        # while only crypto 15M tickers and the top 200 by volume ever get
        # enriched into a snapshot. Over the 31h run that reached 1858MB that
        # is ~1.3M immortal dicts. stats_ttl_s bounds it to roughly one hour's
        # arrivals, and bounds `priority` in enrich_markets with it.
        #
        # Note this makes the "top 200 by volume" ranking a rolling 1h window
        # rather than a lifetime accumulation -- fresher, and the only
        # behaviour change here.
        #
        # A live snapshot always wins over trade staleness: a market that is
        # open but quiet must not be forgotten just because nobody traded it.
        stale = [t for t, st in self._ticker_stats.items()
                 if t not in self.market_snapshots
                 and now - st.get("last_ts", 0.0) > stats_ttl_s]
        for t in stale:
            self._ticker_stats.pop(t, None)

        return len(dead) + len(stale)

    def fetch_list(self, priority_cap: int = PRIORITY_CAP,
                   rest_cap: int = REST_CAP) -> list[str]:
        """Tickers to enrich this cycle: 15M markets first, then top volume.

        `rest` was always capped. `priority` -- every "KX*15M*" ticker -- was
        not, so it grew with the number of distinct 15M tickers ever seen.
        Measured 2026-08-10: ~14 live at any moment across 8 series, rolling
        every 15 minutes, so a 31h process accumulated on the order of a
        thousand and issued ~12 batch fetches per 5s cycle instead of 2.

        The cap is a safety net, not a routine limiter -- at 14 live it does
        not bind. What it must never do is drop a market that is still open:
        /api/crypto/signal reads those and losing one is a feed_down for the
        swing bot. So open markets are exempt and the cap bounds only the
        stale remainder, which means the returned list can exceed
        `priority_cap` when genuinely many markets are open. That is correct;
        an arbitrary limit must not silently blind the scanner.
        """
        now = time.time()
        all_tickers = list(self._ticker_stats.keys())
        candidates = [t for t in all_tickers if "15M" in t and t.startswith("KX")]
        priority = candidates

        if len(priority) > priority_cap:
            def _open(t):
                snap = self.market_snapshots.get(t)
                # close_ts None means the batch-fetch fallback built this
                # snapshot and we do not know yet -- prune_closed refuses to
                # guess those dead, so neither does the cap.
                return snap is not None and (snap.close_ts is None
                                             or snap.close_ts > now)

            open_now = [t for t in priority if _open(t)]
            # An open market trades continuously and a rolled one stops, so
            # recency of the last trade ranks what still matters.
            rest_pri = sorted((t for t in priority if not _open(t)),
                              key=lambda t: self._ticker_stats[t]["last_ts"],
                              reverse=True)
            priority = open_now + rest_pri[:max(0, priority_cap - len(open_now))]

        # Exclude every 15M CANDIDATE from `rest`, not just the ones kept.
        # Excluding only the kept set would let capped-out tickers fall
        # straight back in through the volume ranking, and the cap would
        # bound nothing.
        excluded = set(candidates)
        rest = sorted(
            [t for t in all_tickers if t not in excluded],
            key=lambda t: self._ticker_stats[t]["volume"],
            reverse=True,
        )[:rest_cap]
        return priority + rest

    def enrich_markets(self):
        """Fetch market metadata for tickers we've seen in trades."""
        self.prune_closed()
        active_tickers = self.fetch_list()

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
