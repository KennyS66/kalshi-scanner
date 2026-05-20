"""
Alpha edge research engine for Kalshi.

Detects mispricing and edge opportunities by combining:
  1. Flow-price divergence — whale money says one thing, price says another
  2. Cross-market arbitrage — related markets priced inconsistently
  3. Whale momentum — acceleration in flow before price catches up
  4. External odds comparison — Kalshi vs sportsbook consensus
"""

import re
import time
import math
from dataclasses import dataclass, field
from collections import defaultdict

import requests


# ── Data structures ──────────────────────────────────────────────────

@dataclass
class AlphaSignal:
    """A detected edge opportunity."""
    ticker: str
    title: str
    signal_type: str        # "flow_divergence", "arb", "momentum", "odds_edge"
    direction: str          # "yes" or "no"
    strength: float         # 0-1, how strong the signal is
    edge_pct: float         # estimated edge in percentage points
    kalshi_price: float     # current Kalshi price
    fair_value: float       # estimated fair value
    detail: str             # human-readable explanation

    @property
    def edge_cents(self):
        return round((self.fair_value - self.kalshi_price) * 100, 1)


# ── Flow-Price Divergence ────────────────────────────────────────────

def detect_flow_divergence(scanner):
    """
    Find markets where whale flow strongly disagrees with current price.

    Logic: If whales are buying YES heavily but the price is low (or vice versa),
    either the whales are wrong or the price hasn't caught up yet.
    """
    signals = []
    for ticker, snap in scanner.market_snapshots.items():
        if snap.trade_volume < 100 or snap.recent_whale_count < 2:
            continue

        # Skip MVE parlays and penny markets — no real price discovery
        if "KXMVE" in ticker:
            continue

        price = snap.last_price or snap.yes_price
        if price <= 0.02 or price >= 0.98:
            continue

        # Need meaningful notional (skip low-dollar noise)
        if snap.trade_notional < 50:
            continue

        # Flow ratio: what fraction of volume is YES
        stats = scanner._ticker_stats.get(ticker, {})
        yes_vol = stats.get("yes_vol", 0)
        no_vol = stats.get("no_vol", 0)
        total = yes_vol + no_vol
        if total < 100:
            continue

        flow_ratio = yes_vol / total  # 0 = all NO, 1 = all YES

        # Implied fair value from flow (with dampening — flow isn't perfectly predictive)
        # Blend flow signal with current price (60% flow, 40% market)
        flow_fair = flow_ratio * 0.6 + price * 0.4

        # Divergence: how far the price is from flow-implied fair value
        divergence = flow_fair - price

        # Only flag meaningful divergences
        if abs(divergence) < 0.08:
            continue

        direction = "yes" if divergence > 0 else "no"
        strength = min(abs(divergence) / 0.30, 1.0)

        # Scale strength by whale conviction
        whale_ratio = snap.recent_whale_volume / snap.trade_volume if snap.trade_volume > 0 else 0
        strength = strength * (0.5 + 0.5 * min(whale_ratio, 1.0))

        signals.append(AlphaSignal(
            ticker=ticker,
            title=snap.title,
            signal_type="flow_divergence",
            direction=direction,
            strength=round(strength, 3),
            edge_pct=round(abs(divergence) * 100, 1),
            kalshi_price=price,
            fair_value=round(flow_fair, 3),
            detail=(
                f"Flow is {flow_ratio:.0%} YES but price is ${price:.2f}. "
                f"{snap.recent_whale_count} whales, {snap.trade_count} trades. "
                f"Implied fair value ${flow_fair:.2f} ({'+' if divergence > 0 else ''}{divergence*100:.1f}¢ edge)"
            ),
        ))

    return sorted(signals, key=lambda s: s.strength, reverse=True)


# ── Cross-Market Arbitrage ───────────────────────────────────────────

def _parse_game_key(ticker):
    """Extract the game identifier from a Kalshi ticker."""
    # Pattern: KXNBA{TYPE}-26MAY18SASOKC-{VARIANT}
    m = re.match(r'KX(\w+?)(GAME|SPREAD|TOTAL|TEAMTOTAL|1H\w+|2H\w+)-(\w+)-(.+)', ticker)
    if m:
        return m.group(3), m.group(1), m.group(2), m.group(4)
    return None, None, None, None


def detect_cross_market_arb(scanner):
    """
    Find inconsistencies between related markets for the same event.

    Example: If moneyline implies Team A wins 40% of the time, but spread
    market implies they cover (win by X) 50% of the time — something's off.
    """
    signals = []

    # Group markets by game
    games = defaultdict(dict)
    for ticker, snap in scanner.market_snapshots.items():
        game_key, sport, market_type, variant = _parse_game_key(ticker)
        if game_key and market_type:
            games[game_key][(market_type, variant)] = snap

    for game_key, markets in games.items():
        # Check moneyline vs spread consistency
        game_markets = {k: v for k, v in markets.items() if k[0] == "GAME"}
        spread_markets = {k: v for k, v in markets.items() if k[0] == "SPREAD"}

        if not game_markets or not spread_markets:
            continue

        for (gtype, gvar), game_snap in game_markets.items():
            game_price = game_snap.last_price or game_snap.yes_price
            if game_price <= 0 or game_price >= 1:
                continue

            # Find the tightest spread for this team
            for (stype, svar), spread_snap in spread_markets.items():
                spread_price = spread_snap.last_price or spread_snap.yes_price
                if spread_price <= 0 or spread_price >= 1:
                    continue

                # Extract spread number from variant (e.g., "OKC6" -> 6, "SAS1" -> 1)
                spread_num = re.search(r'(\d+)', svar)
                if not spread_num:
                    continue
                spread_pts = int(spread_num.group(1))

                # Logical constraint: covering a spread should be <= winning outright
                # (you can win but not cover, but you can't cover without winning)
                team_in_spread = re.match(r'[A-Z]+', svar)
                team_in_game = gvar

                if team_in_spread and team_in_spread.group(0) in gvar:
                    # Same team — spread should be <= moneyline
                    if spread_price > game_price + 0.05:
                        edge = spread_price - game_price
                        signals.append(AlphaSignal(
                            ticker=spread_snap.ticker,
                            title=spread_snap.title,
                            signal_type="arb",
                            direction="no",
                            strength=min(edge / 0.15, 1.0),
                            edge_pct=round(edge * 100, 1),
                            kalshi_price=spread_price,
                            fair_value=round(game_price * 0.9, 3),
                            detail=(
                                f"Spread -{spread_pts} priced at ${spread_price:.2f} but "
                                f"moneyline only ${game_price:.2f}. Covering a spread "
                                f"can't be more likely than winning outright."
                            ),
                        ))

        # Check total consistency (over X and over X+3 shouldn't be inverted)
        total_markets = {k: v for k, v in markets.items() if k[0] == "TOTAL"}
        totals_sorted = sorted(
            [(k, v) for k, v in total_markets.items()],
            key=lambda x: int(re.search(r'(\d+)', x[0][1]).group(1)) if re.search(r'(\d+)', x[0][1]) else 0,
        )

        for i in range(len(totals_sorted) - 1):
            (_, var1), snap1 = totals_sorted[i]
            (_, var2), snap2 = totals_sorted[i + 1]
            p1 = snap1.last_price or snap1.yes_price
            p2 = snap2.last_price or snap2.yes_price

            num1 = int(re.search(r'(\d+)', var1).group(1)) if re.search(r'(\d+)', var1) else 0
            num2 = int(re.search(r'(\d+)', var2).group(1)) if re.search(r'(\d+)', var2) else 0

            # Over higher total should be priced lower
            if num2 > num1 and p2 > p1 + 0.03 and p1 > 0 and p2 > 0:
                signals.append(AlphaSignal(
                    ticker=snap2.ticker,
                    title=snap2.title,
                    signal_type="arb",
                    direction="no",
                    strength=min((p2 - p1) / 0.10, 1.0),
                    edge_pct=round((p2 - p1) * 100, 1),
                    kalshi_price=p2,
                    fair_value=round(p1 * 0.95, 3),
                    detail=(
                        f"Over {num2} at ${p2:.2f} > Over {num1} at ${p1:.2f}. "
                        f"Higher total should have lower probability."
                    ),
                ))

    return sorted(signals, key=lambda s: s.strength, reverse=True)


# ── Whale Momentum ───────────────────────────────────────────────────

def detect_whale_momentum(scanner):
    """
    Find markets where whale activity is accelerating.

    If the most recent whale trades are clustered in time and one-directional,
    price likely hasn't caught up yet.
    """
    signals = []

    # Group recent whale alerts by ticker
    whale_by_ticker = defaultdict(list)
    for w in scanner.whale_alerts[:200]:
        whale_by_ticker[w.ticker].append(w)

    for ticker, whales in whale_by_ticker.items():
        if len(whales) < 3:
            continue

        # Skip MVE parlays
        if "KXMVE" in ticker:
            continue

        snap = scanner.market_snapshots.get(ticker)
        if not snap:
            continue

        price = snap.last_price or snap.yes_price
        if price <= 0.02 or price >= 0.98:
            continue

        if snap.trade_notional < 50:
            continue

        # Check directional consistency of recent whales
        yes_whales = [w for w in whales if w.side == "yes"]
        no_whales = [w for w in whales if w.side == "no"]

        total_whale_contracts = sum(w.contracts for w in whales)
        yes_whale_contracts = sum(w.contracts for w in yes_whales)
        no_whale_contracts = sum(w.contracts for w in no_whales)

        if total_whale_contracts == 0:
            continue

        # Directional ratio
        dominant_side = "yes" if yes_whale_contracts > no_whale_contracts else "no"
        dominant_vol = max(yes_whale_contracts, no_whale_contracts)
        ratio = dominant_vol / total_whale_contracts

        # Need strong directional consensus (>70%)
        if ratio < 0.70:
            continue

        # Check time clustering — are whales arriving recently?
        timestamps = sorted([w.timestamp.timestamp() for w in whales if w.timestamp])
        if len(timestamps) < 3:
            continue

        # Recency score: what fraction of whale volume is in the last 15 min
        now = time.time()
        recent_vol = sum(w.contracts for w in whales if w.timestamp and (now - w.timestamp.timestamp()) < 900)
        recency = recent_vol / total_whale_contracts if total_whale_contracts > 0 else 0

        # Momentum strength
        strength = ratio * 0.5 + recency * 0.3 + min(len(whales) / 20, 1.0) * 0.2

        if strength < 0.4:
            continue

        # Estimated edge based on momentum
        if dominant_side == "yes":
            edge = (ratio - 0.5) * 0.3  # conservative estimate
            fair = min(price + edge, 0.95)
        else:
            edge = (ratio - 0.5) * 0.3
            fair = max(price - edge, 0.05)

        signals.append(AlphaSignal(
            ticker=ticker,
            title=snap.title,
            signal_type="momentum",
            direction=dominant_side,
            strength=round(strength, 3),
            edge_pct=round(edge * 100, 1),
            kalshi_price=price,
            fair_value=round(fair, 3),
            detail=(
                f"{len(whales)} whales, {ratio:.0%} {dominant_side.upper()}. "
                f"{dominant_vol:.0f} contracts one way vs {total_whale_contracts - dominant_vol:.0f} the other. "
                f"{'Recent surge — ' if recency > 0.5 else ''}"
                f"{recency:.0%} of whale vol in last 15 min."
            ),
        ))

    return sorted(signals, key=lambda s: s.strength, reverse=True)


# ── External Odds Comparison ─────────────────────────────────────────

class OddsAPI:
    """Compare Kalshi prices against sportsbook consensus."""

    BASE = "https://api.the-odds-api.com/v4"

    def __init__(self, api_key=None):
        self.api_key = api_key
        self.session = requests.Session()
        self._cache = {}
        self._cache_ts = {}

    def _get(self, path, params=None):
        if not self.api_key:
            return None
        url = f"{self.BASE}{path}"
        all_params = {"apiKey": self.api_key}
        if params:
            all_params.update(params)

        cache_key = f"{path}:{sorted(all_params.items())}"
        if cache_key in self._cache and time.time() - self._cache_ts.get(cache_key, 0) < 300:
            return self._cache[cache_key]

        resp = self.session.get(url, params=all_params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        self._cache[cache_key] = data
        self._cache_ts[cache_key] = time.time()
        return data

    def get_odds(self, sport, markets="h2h,spreads,totals"):
        return self._get(f"/sports/{sport}/odds", {
            "regions": "us",
            "markets": markets,
            "oddsFormat": "decimal",
        })

    def get_sports(self):
        return self._get("/sports")


def american_to_implied(odds):
    """Convert American odds to implied probability."""
    if odds > 0:
        return 100 / (odds + 100)
    else:
        return abs(odds) / (abs(odds) + 100)


def decimal_to_implied(odds):
    """Convert decimal odds to implied probability."""
    if odds <= 0:
        return 0
    return 1 / odds


def detect_odds_edge(scanner, odds_api):
    """
    Compare Kalshi prices against sportsbook consensus to find mispricing.

    Sportsbooks have decades of line-setting expertise and sharp money.
    When Kalshi diverges significantly from the book consensus, it's often
    Kalshi that's wrong.
    """
    if not odds_api or not odds_api.api_key:
        return []

    signals = []

    # Map Kalshi sport prefixes to odds-api sport keys
    sport_map = {
        "KXNBA": "basketball_nba",
        "KXMLB": "baseball_mlb",
        "KXNHL": "icehockey_nhl",
        "KXNFL": "americanfootball_nfl",
    }

    for prefix, sport_key in sport_map.items():
        # Find Kalshi markets for this sport
        kalshi_games = {}
        for ticker, snap in scanner.market_snapshots.items():
            if ticker.startswith(prefix) and "GAME" in ticker:
                kalshi_games[ticker] = snap

        if not kalshi_games:
            continue

        try:
            odds_data = odds_api.get_odds(sport_key)
        except Exception:
            continue

        if not odds_data:
            continue

        for event in odds_data:
            home = event.get("home_team", "")
            away = event.get("away_team", "")
            bookmakers = event.get("bookmakers", [])

            if not bookmakers:
                continue

            # Average moneyline across bookmakers
            h2h_probs = defaultdict(list)
            for book in bookmakers:
                for market in book.get("markets", []):
                    if market["key"] == "h2h":
                        for outcome in market.get("outcomes", []):
                            prob = decimal_to_implied(outcome.get("price", 0))
                            h2h_probs[outcome["name"]].append(prob)

            # Compute consensus probability (average, then normalize to remove vig)
            consensus = {}
            for team, probs in h2h_probs.items():
                consensus[team] = sum(probs) / len(probs)

            total_prob = sum(consensus.values())
            if total_prob > 0:
                consensus = {t: p / total_prob for t, p in consensus.items()}

            # Try to match against Kalshi tickers
            for ticker, snap in kalshi_games.items():
                kalshi_price = snap.last_price or snap.yes_price
                if kalshi_price <= 0 or kalshi_price >= 1:
                    continue

                # Match by team name substring
                matched_team = None
                for team, fair_prob in consensus.items():
                    team_parts = team.lower().split()
                    title_lower = snap.title.lower()
                    if any(part in title_lower for part in team_parts if len(part) > 3):
                        matched_team = team
                        break

                if not matched_team:
                    continue

                fair_prob = consensus[matched_team]
                edge = fair_prob - kalshi_price

                if abs(edge) < 0.04:
                    continue

                direction = "yes" if edge > 0 else "no"
                strength = min(abs(edge) / 0.15, 1.0)

                book_count = len(bookmakers)
                signals.append(AlphaSignal(
                    ticker=ticker,
                    title=snap.title,
                    signal_type="odds_edge",
                    direction=direction,
                    strength=round(strength, 3),
                    edge_pct=round(abs(edge) * 100, 1),
                    kalshi_price=kalshi_price,
                    fair_value=round(fair_prob, 3),
                    detail=(
                        f"Kalshi ${kalshi_price:.2f} vs book consensus ${fair_prob:.2f} "
                        f"({book_count} books). "
                        f"{'Kalshi underpriced' if edge > 0 else 'Kalshi overpriced'} "
                        f"by {abs(edge)*100:.1f}¢."
                    ),
                ))

    return sorted(signals, key=lambda s: s.strength, reverse=True)


# ── BTC Strike Monotonicity Arb ─────────────────────────────────────

def _btc_strike(ticker: str):
    """Return (strike_int, direction_char) from KXBTCD-T77000-... or None."""
    m = re.search(r'-([TB])(\d{4,6})', ticker.upper())
    if not m:
        return None, None
    return int(m.group(2)), m.group(1)  # e.g. (77000, 'T')


def detect_btc_strike_arb(scanner):
    """
    P(BTC > higher_strike) must be <= P(BTC > lower_strike).
    Any inversion is a tradeable arb.
    """
    signals = []

    # Build {(series_key, direction): [(strike, price, snap), ...]}
    buckets: dict = {}
    for ticker, snap in scanner.market_snapshots.items():
        if not ticker.upper().startswith("KXBTC"):
            continue
        strike, direction = _btc_strike(ticker)
        if strike is None:
            continue
        price = snap.last_price or snap.yes_price
        if not price or price <= 0.01 or price >= 0.99:
            continue
        # Group by date suffix so we only compare same-expiry markets
        parts = ticker.split("-")
        date_key = parts[-1] if len(parts) >= 3 else "all"
        key = (date_key, direction)
        buckets.setdefault(key, []).append((strike, price, snap))

    for (date_key, direction), entries in buckets.items():
        if len(entries) < 2:
            continue
        entries.sort(key=lambda x: x[0])  # ascending strike

        for i in range(len(entries) - 1):
            s_lo, p_lo, snap_lo = entries[i]
            s_hi, p_hi, snap_hi = entries[i + 1]

            if direction == "T":
                # P(above higher) should be < P(above lower)
                if p_hi > p_lo + 0.03:
                    edge = p_hi - p_lo
                    signals.append(AlphaSignal(
                        ticker=snap_hi.ticker,
                        title=snap_hi.title or snap_hi.ticker,
                        signal_type="btc_strike_arb",
                        direction="no",
                        strength=min(edge / 0.15, 1.0),
                        edge_pct=round(edge * 100, 1),
                        kalshi_price=p_hi,
                        fair_value=round(p_lo * 0.97, 3),
                        detail=(
                            f"P(BTC>{s_hi:,}) = {p_hi:.2f} > P(BTC>{s_lo:,}) = {p_lo:.2f}. "
                            f"Higher strike must be cheaper. Sell {snap_hi.ticker}."
                        ),
                    ))
            else:
                # P(below lower) should be < P(below higher)
                if p_lo > p_hi + 0.03:
                    edge = p_lo - p_hi
                    signals.append(AlphaSignal(
                        ticker=snap_lo.ticker,
                        title=snap_lo.title or snap_lo.ticker,
                        signal_type="btc_strike_arb",
                        direction="no",
                        strength=min(edge / 0.15, 1.0),
                        edge_pct=round(edge * 100, 1),
                        kalshi_price=p_lo,
                        fair_value=round(p_hi * 0.97, 3),
                        detail=(
                            f"P(BTC<{s_lo:,}) = {p_lo:.2f} > P(BTC<{s_hi:,}) = {p_hi:.2f}. "
                            f"Lower threshold must be cheaper. Sell {snap_lo.ticker}."
                        ),
                    ))

    return sorted(signals, key=lambda s: s.strength, reverse=True)


# ── BTC Ladder Sweep ─────────────────────────────────────────────────

def detect_btc_ladder_sweep(scanner):
    """
    If whales are buying the same direction across 3+ BTC 15m markets,
    it's a coordinated directional bet — high conviction signal.
    """
    signals = []

    yes_markets = []
    no_markets = []

    for ticker, snap in scanner.market_snapshots.items():
        if "KXBTC15M" not in ticker.upper() and not ("KXBTC" in ticker.upper() and "15M" in ticker.upper()):
            continue
        if snap.recent_whale_count == 0:
            continue
        price = snap.last_price or snap.yes_price
        if not price or price <= 0.01 or price >= 0.99:
            continue
        if snap.buy_pressure > 0:
            yes_markets.append(snap)
        elif snap.buy_pressure < 0:
            no_markets.append(snap)

    for direction, mkt_list in (("yes", yes_markets), ("no", no_markets)):
        if len(mkt_list) < 3:
            continue

        total_whales = sum(s.recent_whale_count for s in mkt_list)
        total_notional = sum(s.trade_notional for s in mkt_list)
        top = max(mkt_list, key=lambda s: s.recent_whale_count)
        price = top.last_price or top.yes_price or 0.5

        strength = min(len(mkt_list) / 8, 1.0) * 0.6 + min(total_whales / 20, 1.0) * 0.4

        signals.append(AlphaSignal(
            ticker=top.ticker,
            title=f"BTC 15m ladder sweep ({len(mkt_list)} markets)",
            signal_type="btc_ladder_sweep",
            direction=direction,
            strength=round(strength, 3),
            edge_pct=round(strength * 20, 1),
            kalshi_price=price,
            fair_value=round(price + (0.05 if direction == "yes" else -0.05), 3),
            detail=(
                f"{len(mkt_list)} BTC 15m markets with {direction.upper()} whale flow. "
                f"{total_whales} total whale prints, ${total_notional:,.0f} notional. "
                f"Coordinated directional positioning."
            ),
        ))

    return sorted(signals, key=lambda s: s.strength, reverse=True)


# ── Main Alpha Engine ────────────────────────────────────────────────

class AlphaEngine:
    """Combines all alpha signals into ranked opportunities."""

    def __init__(self, scanner, odds_api_key=None):
        self.scanner = scanner
        self.odds_api = OddsAPI(api_key=odds_api_key) if odds_api_key else None

    def scan(self):
        """Run all alpha detectors and return ranked signals."""
        all_signals = []

        # 1. Flow-price divergence
        all_signals.extend(detect_flow_divergence(self.scanner))

        # 2. Cross-market arbitrage
        all_signals.extend(detect_cross_market_arb(self.scanner))

        # 3. Whale momentum
        all_signals.extend(detect_whale_momentum(self.scanner))

        # 4. External odds comparison
        if self.odds_api and self.odds_api.api_key:
            all_signals.extend(detect_odds_edge(self.scanner, self.odds_api))

        # 5. BTC strike monotonicity arb
        all_signals.extend(detect_btc_strike_arb(self.scanner))

        # 6. BTC 15m ladder sweep
        all_signals.extend(detect_btc_ladder_sweep(self.scanner))

        # Deduplicate — keep strongest signal per ticker
        best_by_ticker = {}
        for sig in all_signals:
            key = (sig.ticker, sig.signal_type)
            if key not in best_by_ticker or sig.strength > best_by_ticker[key].strength:
                best_by_ticker[key] = sig

        return sorted(best_by_ticker.values(), key=lambda s: s.strength, reverse=True)

    def get_top_signals(self, n=20):
        return self.scan()[:n]

    def get_signals_by_type(self, signal_type):
        return [s for s in self.scan() if s.signal_type == signal_type]
