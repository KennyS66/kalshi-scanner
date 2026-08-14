import gzip
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from wsrig.tape import Tape, read_tape


def test_write_then_read_roundtrips(tmp_path):
    t = Tape(tmp_path)
    t.write({"k": "spot", "p": 63416.5})
    t.close()
    got = list(read_tape(tmp_path))
    assert len(got) == 1
    assert got[0]["k"] == "spot"
    assert got[0]["p"] == 63416.5


def test_write_stamps_both_clocks(tmp_path):
    t = Tape(tmp_path)
    t.write({"k": "spot", "p": 1.0})
    t.close()
    rec = next(iter(read_tape(tmp_path)))
    assert isinstance(rec["tw"], float) and rec["tw"] > 1_700_000_000
    assert isinstance(rec["tm"], float)


def test_write_does_not_overwrite_caller_timestamps(tmp_path):
    """A replayed or back-dated record must keep its own clocks."""
    t = Tape(tmp_path)
    t.write({"k": "spot", "p": 1.0, "tw": 123.0, "tm": 456.0})
    t.close()
    rec = next(iter(read_tape(tmp_path)))
    assert rec["tw"] == 123.0 and rec["tm"] == 456.0


def test_rotates_by_hour(tmp_path):
    t = Tape(tmp_path)
    t._hour_key = lambda: "A"
    t.write({"k": "x", "n": 1})
    t._hour_key = lambda: "B"
    t.write({"k": "x", "n": 2})
    t.close()
    files = sorted(p.name for p in tmp_path.glob("*.jsonl.gz"))
    assert len(files) == 2, files
    assert [r["n"] for r in read_tape(tmp_path)] == [1, 2]


def test_files_are_valid_gzip_after_close(tmp_path):
    """A truncated gzip member loses the whole hour. Close must finalise."""
    t = Tape(tmp_path)
    for i in range(100):
        t.write({"k": "x", "n": i})
    t.close()
    f = next(tmp_path.glob("*.jsonl.gz"))
    with gzip.open(f, "rt") as fh:
        assert len([json.loads(l) for l in fh if l.strip()]) == 100


def test_read_tape_orders_files_chronologically(tmp_path):
    for name, n in (("tape-20260813-09.jsonl.gz", 2),
                    ("tape-20260813-08.jsonl.gz", 1)):
        with gzip.open(tmp_path / name, "wt") as fh:
            fh.write(json.dumps({"n": n}) + "\n")
    assert [r["n"] for r in read_tape(tmp_path)] == [1, 2]


def test_read_tape_skips_a_corrupt_trailing_line(tmp_path):
    """A crash mid-write leaves a partial line; it must not kill the read."""
    with gzip.open(tmp_path / "tape-20260813-08.jsonl.gz", "wt") as fh:
        fh.write(json.dumps({"n": 1}) + "\n")
        fh.write('{"n": 2')          # truncated
    assert [r["n"] for r in read_tape(tmp_path)] == [1]
