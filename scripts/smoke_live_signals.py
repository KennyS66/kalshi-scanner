"""Smoke test for GET /api/live_signals malformed-line handling.

Not a pytest file by design -- this endpoint is a thin read-only display of
data/bot/live_signals.jsonl (already exercised at the writer level in
bot_broker's own test suite, and the line-skipping behavior it now reuses,
_read_jsonl_tail, already has pytest coverage in tests/test_bot_web.py via
test_read_jsonl_tail_skips_torn_lines). This script proves the endpoint-level
behavior end to end with FastAPI's TestClient, matching how Task 8 smoke-
tested the endpoint originally.

Run with:  python scripts/smoke_live_signals.py
"""
import json
import sys
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

import web


def main() -> int:
    failures = []

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        orig_bot_dir = web._BOT_DIR
        web._BOT_DIR = tmp_dir
        try:
            client = TestClient(web.app)

            # --- Case 1: missing file -> {"signals": []}, unchanged behavior
            r = client.get("/api/live_signals")
            if r.json() != {"signals": []}:
                failures.append(f"missing-file case: expected empty list, got {r.json()}")

            # --- Case 2: one malformed line among several valid ones ->
            # all VALID rows returned, not an empty list.
            lines = [
                json.dumps({"ts": 1, "ticker": "AAA", "signal": "buy"}),
                "{not valid json at all",
                json.dumps({"ts": 2, "ticker": "BBB", "signal": "sell"}),
                "",  # blank line, should also just be skipped
                json.dumps({"ts": 3, "ticker": "CCC", "signal": "hold"}),
            ]
            (tmp_dir / "live_signals.jsonl").write_text("\n".join(lines))

            r = client.get("/api/live_signals")
            body = r.json()
            signals = body.get("signals")
            expected = [
                {"ts": 1, "ticker": "AAA", "signal": "buy"},
                {"ts": 2, "ticker": "BBB", "signal": "sell"},
                {"ts": 3, "ticker": "CCC", "signal": "hold"},
            ]
            if signals != expected:
                failures.append(
                    "malformed-line case: expected 3 valid rows preserved, "
                    f"got {signals!r}"
                )
            if r.status_code != 200:
                failures.append(f"malformed-line case: expected 200, got {r.status_code}")

        finally:
            web._BOT_DIR = orig_bot_dir

    if failures:
        print("SMOKE TEST FAILED:")
        for f in failures:
            print(" -", f)
        return 1

    print("SMOKE TEST PASSED: /api/live_signals keeps valid rows and skips "
          "malformed/blank lines individually; missing file still returns "
          "{'signals': []}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
