"""The trade page cap: silent truncation made observable.

`scan_trades` pages the trades endpoint at most MAX_TRADE_PAGES (10) x 1000
trades. If that cap is reached while the API still has a cursor outstanding,
more trades were available -- and they are skipped PERMANENTLY, because
`last_trade_ts` is advanced to `scan_start_ts` afterwards regardless, so the
next scan starts past them. There was no counter, no log and no alarm, so the
loss was invisible.

At the measured exchange rate (~104 trades/s daily average, ~163/s at the
evening peak) the cap is 61-96 seconds of tape against a nominal 5s cycle, so
it is not believed to fire today. But api.py allows 15s per request across up
to 10 pages, so a slow sequence can reach it.

The trap this pins down is the false positive. `cursor` is NOT a safe thing to
inspect after the loop: when page 10 comes back empty the loop breaks with
`cursor` still holding page 9's non-empty value, so an
`if pages == MAX and cursor` test would cry wolf every time the tape happened
to end exactly on the cap. Three negative cases below discriminate a correct
implementation from that one.
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scanner as scanner_mod
from scanner import Scanner

NOW_ISO = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _trade(tid):
    return {"trade_id": tid, "ticker": "KXBTC15M-A", "count_fp": "10",
            "yes_price_dollars": "0.50", "taker_outcome_side": "yes",
            "created_time": NOW_ISO}


def _page(index, n_trades, cursor):
    """One API response. trade_ids stay unique across pages so dedup never
    masks what paging did."""
    return {"trades": [_trade(f"p{index}-t{i}") for i in range(n_trades)],
            "cursor": cursor}


class ScriptedAPI:
    """Serves scripted responses in order, then repeats the last one."""

    def __init__(self, pages):
        self.pages = pages
        self.calls = 0

    def get_trades(self, **kw):
        page = self.pages[min(self.calls, len(self.pages) - 1)]
        self.calls += 1
        return page


def _scanner(pages):
    return Scanner(api=ScriptedAPI(pages), whale_threshold=50)


def _full_pages(n, cursor="more"):
    return [_page(i, 5, cursor) for i in range(n)]


# --- the genuine case -------------------------------------------------------

def test_truncation_is_reported_at_all(capsys):
    """The load-bearing case, written against the literal page limit so it
    depends on no new attribute: ten full pages with a cursor still
    outstanding drops trades on the floor, and today says nothing at all."""
    s = Scanner(api=ScriptedAPI([_page(i, 5, "more") for i in range(10)]),
                whale_threshold=50)
    s.scan_trades()

    assert capsys.readouterr().err != "", \
        "trades were skipped permanently and nothing was reported"


def test_warns_when_the_cap_is_hit_with_a_cursor_outstanding(capsys):
    """Every page full AND page 10 still hands back a cursor: trades existed
    and we stopped anyway."""
    s = _scanner(_full_pages(scanner_mod.MAX_TRADE_PAGES))
    s.scan_trades()

    err = capsys.readouterr().err
    assert "WARNING" in err
    assert s.truncated_scans == 1


def test_the_warning_names_what_actually_happened(capsys):
    """A future reader seeing this in the journal must understand that trades
    were lost for good, not that a page limit was merely reached."""
    s = _scanner(_full_pages(scanner_mod.MAX_TRADE_PAGES))
    s.scan_trades()

    err = capsys.readouterr().err.lower()
    assert "skip" in err
    assert "cursor" in err


# --- the three negatives that discriminate a correct implementation ---------

def test_no_warning_when_the_tenth_page_comes_back_empty(capsys):
    """The false-positive trap. The loop breaks on `if not trades` with
    `cursor` still holding page 9's non-empty value -- but the tape ended, so
    nothing was skipped."""
    pages = _full_pages(scanner_mod.MAX_TRADE_PAGES - 1)
    pages.append({"trades": [], "cursor": "still-here"})

    s = _scanner(pages)
    s.scan_trades()

    assert capsys.readouterr().err == ""
    assert s.truncated_scans == 0


def test_no_warning_when_the_tenth_page_ends_the_data(capsys):
    """All 10 pages fetched and full, but the API says there is no more."""
    pages = _full_pages(scanner_mod.MAX_TRADE_PAGES - 1)
    pages.append(_page(9, 5, ""))

    s = _scanner(pages)
    s.scan_trades()

    assert capsys.readouterr().err == ""
    assert s.truncated_scans == 0


def test_no_warning_on_a_normal_single_page_scan(capsys):
    s = _scanner([_page(0, 5, "")])
    s.scan_trades()

    assert capsys.readouterr().err == ""
    assert s.truncated_scans == 0


# --- the counter ------------------------------------------------------------

def test_counter_starts_at_zero():
    assert Scanner(api=None).truncated_scans == 0


def test_counter_accumulates_across_scans(capsys):
    """Cumulative, so it can be surfaced later rather than only tailing logs."""
    s = _scanner(_full_pages(scanner_mod.MAX_TRADE_PAGES))
    s.scan_trades()
    s.scan_trades()

    capsys.readouterr()
    assert s.truncated_scans == 2


# --- observability only: fetch behaviour must be unchanged ------------------

def test_the_page_limit_itself_is_unchanged():
    assert scanner_mod.MAX_TRADE_PAGES == 10


def test_truncation_adds_no_retry(capsys):
    """Observability only. Detecting the cap must not make the scanner fetch
    more pages than it did before."""
    api = ScriptedAPI(_full_pages(scanner_mod.MAX_TRADE_PAGES))
    s = Scanner(api=api, whale_threshold=50)
    s.scan_trades()

    capsys.readouterr()
    assert api.calls == scanner_mod.MAX_TRADE_PAGES


def test_last_trade_ts_still_advances_when_truncated(capsys):
    """Pinned because it is precisely what makes the loss irrecoverable. The
    warning exists to expose this, not to change it."""
    s = _scanner(_full_pages(scanner_mod.MAX_TRADE_PAGES))
    s.scan_trades()

    capsys.readouterr()
    assert s.last_trade_ts is not None
