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


# ── Math helpers ─────────────────────────────────────────────────────

def _erfinv(z: float) -> float:
    """Rational approximation to erfinv(z); accurate to ~5e-4 for |z| < 0.99."""
    a = 0.147
    z = max(-1 + 1e-9, min(1 - 1e-9, z))
    ln = math.log(1.0 - z * z)
    c = 2.0 / (math.pi * a) + ln / 2.0
    return math.copysign(math.sqrt(math.sqrt(c * c - ln / a) - c), z)


def _normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2)))


def _normal_ppf(p: float) -> float:
    p = max(1e-6, min(1 - 1e-6, p))
    return math.sqrt(2) * _erfinv(2 * p - 1)


def _lin_reg(xs, ys):
    """Simple OLS; returns (slope, intercept) or (None, None)."""
    n = len(xs)
    if n < 2:
        return None, None
    xm = sum(xs) / n
    ym = sum(ys) / n
    num = sum((x - xm) * (y - ym) for x, y in zip(xs, ys))
    den = sum((x - xm) ** 2 for x in xs)
    if den == 0:
        return None, None
    slope = num / den
    return slope, ym - slope * xm


def _ewma_whale_flow(whale_alerts, ticker: str, half_life_min: float = 10.0):
    """Return (yes_w, no_w) — time-decayed, aggression-boosted whale flow."""
    now = time.time()
    decay = math.log(2) / (half_life_min * 60)
    yes_w = no_w = 0.0
    for a in whale_alerts:
        if a.ticker != ticker:
            continue
        age_s = max(0.0, now - (a.timestamp.timestamp() if a.timestamp else now))
        w = math.exp(-decay * age_s) * a.contracts
        aggr = 1.4 if a.taker_side == "ask" else 1.0
        if a.side == "yes":
            yes_w += w * aggr
        else:
            no_w += w * aggr
    return yes_w, no_w


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
    for ticker, snap in list(scanner.market_snapshots.items()):
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

        # Flow ratio: what fraction of volume is YES (flat lookback)
        stats = scanner._ticker_stats.get(ticker, {})
        yes_vol = stats.get("yes_vol", 0)
        no_vol = stats.get("no_vol", 0)
        total = yes_vol + no_vol
        if total < 100:
            continue

        flat_ratio = yes_vol / total  # 0 = all NO, 1 = all YES

        # EWMA whale flow (time-decayed + aggression-boosted) — more reactive
        ewma_yes, ewma_no = _ewma_whale_flow(scanner.whale_alerts, ticker)
        ewma_total = ewma_yes + ewma_no
        if ewma_total > 0:
            # Blend: 50% flat (volume-rich) + 50% EWMA (time/conviction-aware)
            flow_ratio = flat_ratio * 0.5 + (ewma_yes / ewma_total) * 0.5
        else:
            flow_ratio = flat_ratio

        # Implied fair value from flow (with dampening — flow isn't perfectly predictive)
        flow_fair = flow_ratio * 0.7 + price * 0.3

        # Divergence: how far the price is from flow-implied fair value
        divergence = flow_fair - price

        # Only flag meaningful divergences
        if abs(divergence) < 0.08:
            continue

        direction = "yes" if divergence > 0 else "no"
        strength = min(abs(divergence) / 0.30, 1.0)

        # Scale strength by whale conviction
        whale_ratio = snap.recent_whale_volume / snap.trade_volume if snap.trade_volume > 0 else 0
        # Extra boost when EWMA ratio strongly agrees with direction
        ewma_agree = 1.0
        if ewma_total > 0:
            ewma_ratio = ewma_yes / ewma_total
            agreement = abs(ewma_ratio - 0.5) * 2  # 0 = neutral, 1 = one-sided
            same_direction = (ewma_ratio > 0.5) == (divergence > 0)
            ewma_agree = 1.0 + 0.3 * agreement if same_direction else 1.0 - 0.2 * agreement
        strength = min(strength * (0.5 + 0.5 * min(whale_ratio, 1.0)) * ewma_agree, 1.0)

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
    for ticker, snap in list(scanner.market_snapshots.items()):
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
        # Require meaningful notional — 3 tiny whales shouldn't trigger momentum
        if sum(w.notional for w in whales) < 500:
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

        # EWMA-weighted + aggression-boosted directional flow
        ewma_yes, ewma_no = _ewma_whale_flow(whales, ticker)
        ewma_total = ewma_yes + ewma_no
        if ewma_total == 0:
            continue

        ewma_ratio = ewma_yes / ewma_total  # 0 = all NO, 1 = all YES
        dominant_side = "yes" if ewma_ratio > 0.5 else "no"
        # Directional consensus strength (0 = split, 1 = one-sided)
        ratio = abs(ewma_ratio - 0.5) * 2

        # Need strong directional consensus (>70% one-sided → ratio > 0.40)
        if ratio < 0.40:
            continue

        # Flat counts for display
        total_whale_contracts = sum(w.contracts for w in whales)
        yes_whale_contracts = sum(w.contracts for w in whales if w.side == "yes")
        no_whale_contracts = total_whale_contracts - yes_whale_contracts

        # EWMA recency: ratio of EWMA weight vs flat weight; high = recent whales dominate
        flat_yes = sum(w.contracts for w in whales if w.side == "yes")
        flat_total = sum(w.contracts for w in whales)
        flat_ratio_raw = flat_yes / flat_total if flat_total > 0 else 0.5
        # Recency amplification: EWMA diverges from flat when recent flow is strong
        recency = min(abs(ewma_ratio - flat_ratio_raw) * 5, 1.0)

        # Momentum strength: directional consensus (50%) + recency (30%) + whale count (20%)
        strength = ratio * 0.5 + recency * 0.3 + min(len(whales) / 20, 1.0) * 0.2

        if strength < 0.35:
            continue

        # Estimated edge based on momentum
        edge = ratio * 0.15  # conservative; ratio=1 → 15¢ edge
        if dominant_side == "yes":
            fair = min(price + edge, 0.95)
        else:
            fair = max(price - edge, 0.05)

        dominant_vol = yes_whale_contracts if dominant_side == "yes" else no_whale_contracts
        signals.append(AlphaSignal(
            ticker=ticker,
            title=snap.title,
            signal_type="momentum",
            direction=dominant_side,
            strength=round(min(strength, 1.0), 3),
            edge_pct=round(edge * 100, 1),
            kalshi_price=price,
            fair_value=round(fair, 3),
            detail=(
                f"{len(whales)} whales, EWMA {ewma_ratio:.0%} YES. "
                f"{dominant_vol:.0f} contracts {dominant_side.upper()} vs "
                f"{total_whale_contracts - dominant_vol:.0f} other. "
                f"{'Recency surge — ' if recency > 0.5 else ''}"
                f"aggression-weighted flow {ratio:.0%} directional."
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
        for ticker, snap in list(scanner.market_snapshots.items()):
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
    for ticker, snap in list(scanner.market_snapshots.items()):
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

_ET_MONTHS = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
              "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}


def _ticker_mins_left(ticker: str) -> float | None:
    """Parse ET-based expiry from a BTC 15m ticker; return minutes until expiry."""
    m = re.search(r'(\d{2})([A-Z]{3})(\d{2})(\d{2})(\d{2})', ticker.upper())
    if not m:
        return None
    try:
        from zoneinfo import ZoneInfo
        import datetime as _dt
        ET = ZoneInfo("America/New_York")
        exp = _dt.datetime(2000 + int(m.group(1)), _ET_MONTHS[m.group(2)],
                           int(m.group(3)), int(m.group(4)), int(m.group(5)), tzinfo=ET)
        return (exp.timestamp() - time.time()) / 60
    except Exception:
        return None


def detect_btc_ladder_sweep(scanner):
    """
    If whales are buying the same direction across 3+ BTC 15m markets,
    it's a coordinated directional bet — high conviction signal.
    Strength is boosted when time-to-expiry is short (final minutes are high-info).
    """
    signals = []

    yes_markets = []
    no_markets = []

    for ticker, snap in list(scanner.market_snapshots.items()):
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

        base_strength = min(len(mkt_list) / 8, 1.0) * 0.6 + min(total_whales / 20, 1.0) * 0.4

        # Boost when close to expiry — whales in final minutes = much higher conviction
        mins_left = _ticker_mins_left(top.ticker)
        expiry_boost = 1.0
        expiry_note = ""
        if mins_left is not None and 0 < mins_left <= 15:
            if mins_left < 2:
                expiry_boost = 1.6
                expiry_note = f"FINAL {mins_left:.1f} min — "
            elif mins_left < 5:
                expiry_boost = 1.3
                expiry_note = f"{mins_left:.1f} min left — "

        strength = min(base_strength * expiry_boost, 1.0)

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
                f"{expiry_note}{len(mkt_list)} BTC 15m markets with {direction.upper()} whale flow. "
                f"{total_whales} total whale prints, ${total_notional:,.0f} notional. "
                f"Coordinated directional positioning."
            ),
        ))

    return sorted(signals, key=lambda s: s.strength, reverse=True)


# ── Ask-Sum Arb ──────────────────────────────────────────────────────

def detect_ask_sum_arb(scanner):
    """
    If yes_ask + no_ask < 1.00, buying both guarantees $1 in profit — a real arb.
    Also flags illiquid markets (sum > 1.10) where other signals should be discounted.
    """
    signals = []
    for ticker, snap in list(scanner.market_snapshots.items()):
        if "KXMVE" in ticker:
            continue
        yes_ask = snap.yes_price
        no_ask = snap.no_price
        if not yes_ask or not no_ask:
            continue
        ask_sum = yes_ask + no_ask

        if ask_sum < 0.97 and ask_sum > 0.05:
            edge = round((1.0 - ask_sum) * 100, 1)
            direction = "yes" if yes_ask <= no_ask else "no"
            signals.append(AlphaSignal(
                ticker=ticker,
                title=snap.title or ticker,
                signal_type="ask_arb",
                direction=direction,
                strength=min((0.97 - ask_sum) / 0.10, 1.0),
                edge_pct=edge,
                kalshi_price=yes_ask,
                fair_value=round(1.0 - no_ask, 3),
                detail=(
                    f"YES ask {yes_ask:.3f} + NO ask {no_ask:.3f} = {ask_sum:.3f}. "
                    f"Buy both → guaranteed {edge}¢ profit per contract."
                ),
            ))

    return sorted(signals, key=lambda s: s.strength, reverse=True)


# ── BTC Implied Distribution ─────────────────────────────────────────

def detect_btc_implied_distribution(scanner):
    """
    Fit a log-normal/normal CDF to the full BTC T-type strike ladder per expiry.
    Strikes that deviate from the fitted curve by >3¢ are mispriced.

    P(BTC > K) = 1 - N((K - mu) / sigma)
    => normppf(1 - price) = (K - mu) / sigma  → linear in K
    Fit via OLS on (strike, normppf(1 - price)); outliers = mispricings.
    """
    signals = []

    # Group T-type BTC strikes by expiry suffix (last date component of ticker)
    buckets: dict = {}
    for ticker, snap in list(scanner.market_snapshots.items()):
        t = ticker.upper()
        if not t.startswith("KXBTC"):
            continue
        m = re.search(r'-T(\d{4,7})-(\S+)', t)
        if not m:
            continue
        strike = int(m.group(1))
        expiry_key = m.group(2)
        price = snap.last_price or snap.yes_price
        if not price or price <= 0.03 or price >= 0.97:
            continue
        buckets.setdefault(expiry_key, []).append((strike, price, snap))

    for expiry_key, entries in buckets.items():
        if len(entries) < 5:
            continue

        entries.sort(key=lambda x: x[0])
        strikes = [e[0] for e in entries]
        prices = [e[1] for e in entries]

        # y = normppf(1 - price) = (K - mu) / sigma
        try:
            ys = [_normal_ppf(1.0 - p) for p in prices]
        except Exception:
            continue

        def _fit_curve(xs, ys_vals):
            s, i = _lin_reg(xs, ys_vals)
            if s is None or s <= 0:
                return None, None
            sig = 1.0 / s
            return sig, -i * sig  # sigma, mu

        sigma, mu = _fit_curve(strikes, ys)
        if sigma is None:
            continue

        # Iterative outlier rejection: drop points > 2.5σ residual, refit once
        residuals = [
            abs((1.0 - _normal_cdf((sk - mu) / sigma)) - pr)
            for sk, pr, _ in entries
        ]
        med_res = sorted(residuals)[len(residuals) // 2]
        clean = [(sk, pr, sn, r) for (sk, pr, sn), r in zip(entries, residuals)
                 if r < max(med_res * 4, 0.06)]
        if len(clean) >= 5:
            try:
                clean_ys = [_normal_ppf(1.0 - pr) for _, pr, _, _ in clean]
                sigma2, mu2 = _fit_curve([sk for sk, _, _, _ in clean], clean_ys)
                if sigma2 is not None:
                    sigma, mu = sigma2, mu2
            except Exception:
                pass

        # Expiry-aware threshold: tighter near expiry where noise is lower
        now_epoch = time.time()
        for strike, price, snap in entries:
            fitted_price = 1.0 - _normal_cdf((strike - mu) / sigma)
            deviation = price - fitted_price
            # Tighten threshold when close to expiry
            mins_left = None
            if snap.close_ts:
                mins_left = max(0.0, (snap.close_ts - now_epoch) / 60)
            threshold = 0.02 if (mins_left is not None and mins_left < 10) else 0.03
            if abs(deviation) < threshold:
                continue
            # Mispriced relative to the curve
            direction = "no" if deviation > 0 else "yes"  # overpriced → sell (no), underpriced → buy (yes)
            edge = abs(deviation)
            signals.append(AlphaSignal(
                ticker=snap.ticker,
                title=snap.title or snap.ticker,
                signal_type="implied_curve",
                direction=direction,
                strength=min(edge / 0.12, 1.0),
                edge_pct=round(edge * 100, 1),
                kalshi_price=price,
                fair_value=round(fitted_price, 3),
                detail=(
                    f"Strike {strike:,} actual {price:.3f} vs curve-implied {fitted_price:.3f}. "
                    f"Implied BTC median ~${mu:,.0f} ± ${sigma:,.0f}. "
                    f"{'Overpriced — sell' if direction == 'no' else 'Underpriced — buy'}."
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

        # 6. BTC 15m ladder sweep (time-to-expiry boosted)
        all_signals.extend(detect_btc_ladder_sweep(self.scanner))

        # 7. Ask-sum arb (yes_ask + no_ask < 1.00)
        all_signals.extend(detect_ask_sum_arb(self.scanner))

        # 8. BTC implied distribution curve — outlier strikes
        all_signals.extend(detect_btc_implied_distribution(self.scanner))

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
