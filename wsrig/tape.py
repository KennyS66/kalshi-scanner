"""Append-only gzipped record tape, rotated hourly.

Every record carries three clocks, because the entire question this rig
exists to answer is a latency question:

    tw  wall clock   (time.time())     -- joins across processes and to settlement
    tm  monotonic    (time.monotonic()) -- immune to NTP steps; the basis for deltas
    tx  exchange ts  (from the message) -- reveals feed-side lag

Reading is deliberately forgiving: a crash leaves a partial final line, and
losing an hour of capture to one truncated record would be absurd.
"""
from __future__ import annotations

import gzip
import json
import logging
import time
from collections.abc import Iterator
from pathlib import Path

log = logging.getLogger("wsrig.tape")

FLUSH_EVERY = 200          # records; bounds loss on a hard kill


def safe_write(tape, rec: dict) -> None:
    """Write a record from inside an error handler, without raising a new error.

    A tape write fails (disk full, bad fd) exactly when a feed is already in
    trouble — and an OSError raised from inside an `except` block escapes the
    handler and kills the feed task outright. Losing one diagnostic record is
    always cheaper than losing the feed.
    """
    try:
        tape.write(rec)
    except Exception:                      # noqa: BLE001 — nothing may escape here
        log.exception("tape write failed for a %r record", rec.get("k"))


class Tape:
    def __init__(self, dir: Path, hour_fmt: str = "%Y%m%d-%H"):
        self.dir = Path(dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._hour_fmt = hour_fmt
        self._fh = None
        self._open_key = None
        self._since_flush = 0

    def _hour_key(self) -> str:
        return time.strftime(self._hour_fmt, time.gmtime())

    def _ensure_open(self) -> None:
        key = self._hour_key()
        if key != self._open_key:
            self.close()
            path = self.dir / f"tape-{key}.jsonl.gz"
            self._fh = gzip.open(path, "at", compresslevel=6)
            self._open_key = key

    def write(self, rec: dict) -> None:
        rec.setdefault("tw", time.time())
        rec.setdefault("tm", time.monotonic())
        self._ensure_open()
        self._fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
        self._since_flush += 1
        if self._since_flush >= FLUSH_EVERY:
            self._fh.flush()
            self._since_flush = 0

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None
            self._open_key = None
            self._since_flush = 0


def read_tape(dir: Path) -> Iterator[dict]:
    """Yield every record, files in chronological name order."""
    for path in sorted(Path(dir).glob("tape-*.jsonl.gz")):
        try:
            with gzip.open(path, "rt") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except ValueError:
                        continue          # partial final line after a crash
        except (OSError, EOFError):
            continue                      # truncated gzip member
