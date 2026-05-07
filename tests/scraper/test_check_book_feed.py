"""Tests for ``scripts/check_book_feed.py``.

Centred on the predicate (``is_populated``) and the directory scan: the
goal is to lock in the corrected population semantics so that the
0%-vs-95% measurement gap that misled the R3 dcfe272 merge can never
recur. Pays particular attention to the fooler case from R3 — a
``book`` event with empty top-level bids/asks but populated
``raw.bids|asks`` — which the old top-level-only predicate counted as
empty and which the new predicate must count as populated.
"""

from __future__ import annotations

import gzip
import json
import subprocess
import sys
from pathlib import Path

from scripts import check_book_feed


# ── predicate cases ─────────────────────────────────────────────────────────


def test_predicate_book_event_with_populated_raw_is_populated() -> None:
    """The R3 fooler: top-level fields absent, but ``raw.bids|asks`` carry the
    snapshot. This is what the old top-level-only predicate missed."""
    rec = {
        "type": "book",
        "raw": {
            "bids": [{"price": "0.5", "size": "100"}],
            "asks": [],
        },
    }
    assert check_book_feed.is_populated(rec) is True


def test_predicate_book_event_with_empty_raw_is_unpopulated() -> None:
    rec = {"type": "book", "raw": {"bids": [], "asks": []}}
    assert check_book_feed.is_populated(rec) is False


def test_predicate_snapshot_with_top_level_bids_is_populated() -> None:
    rec = {
        "type": "snapshot",
        "reason": "periodic",
        "bids": [{"price": "0.5", "size": "10"}],
        "asks": [],
    }
    assert check_book_feed.is_populated(rec) is True


def test_predicate_empty_periodic_snapshot_is_unpopulated() -> None:
    """Post-resolution snapshots correctly count as un-populated."""
    rec = {"type": "snapshot", "reason": "periodic", "bids": [], "asks": []}
    assert check_book_feed.is_populated(rec) is False


def test_predicate_price_change_with_size_gt_zero_is_populated() -> None:
    rec = {
        "type": "price_change",
        "deltas": [
            {"price": "0.5", "size": "0", "side": "BUY"},
            {"price": "0.51", "size": "100", "side": "BUY"},
        ],
    }
    assert check_book_feed.is_populated(rec) is True


def test_predicate_price_change_all_zero_size_is_unpopulated() -> None:
    """The resolution-cancel cascade: every level removed. Correctly empty."""
    rec = {
        "type": "price_change",
        "deltas": [
            {"price": "0.5", "size": "0", "side": "BUY"},
            {"price": "0.51", "size": "0", "side": "SELL"},
        ],
    }
    assert check_book_feed.is_populated(rec) is False


def test_predicate_other_event_types_are_unpopulated() -> None:
    """``best_bid_ask``, ``last_trade_price``, ``tick_size_change`` carry
    metadata but no book depth — by design they don't count as populated."""
    for et in ("best_bid_ask", "last_trade_price", "tick_size_change", "other"):
        rec = {"type": et, "raw": {"some": "field"}}
        assert check_book_feed.is_populated(rec) is False, et


# ── scan_file + main ────────────────────────────────────────────────────────


def _write_gz(path: Path, records: list[dict]) -> None:
    with gzip.open(path, "wt") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


def test_scan_file_counts_populated_correctly(tmp_path: Path) -> None:
    """End-to-end on a synthetic file mixing every event class."""
    f = tmp_path / "btc-updown-5m-1700000000.jsonl.gz"
    _write_gz(f, [
        {"type": "book", "raw": {"bids": [{"price": "0.5", "size": "10"}], "asks": []}},
        {"type": "book", "raw": {"bids": [], "asks": []}},
        {"type": "snapshot", "reason": "initial",
         "bids": [{"price": "0.5", "size": "10"}], "asks": []},
        {"type": "snapshot", "reason": "periodic", "bids": [], "asks": []},
        {"type": "price_change",
         "deltas": [{"price": "0.5", "size": "5", "side": "BUY"}]},
        {"type": "price_change",
         "deltas": [{"price": "0.5", "size": "0", "side": "BUY"}]},
        {"type": "best_bid_ask", "raw": {"best_bid": "0.5", "best_ask": "0.51"}},
        {"type": "last_trade_price", "raw": {"price": "0.5"}},
    ])

    total, pop = check_book_feed.scan_file(f)
    assert total == 8
    assert pop == 3, f"expected 3 (book+snap+pc), got {pop}"


def test_scan_file_tolerates_truncated_gzip(tmp_path: Path) -> None:
    """A capture inspected before SIGTERM has a mid-write file. Truncating
    the gzip stream must not crash the scan; it should count whatever
    parseable records existed."""
    f = tmp_path / "btc-updown-5m-1700000000.jsonl.gz"
    _write_gz(f, [
        {"type": "book", "raw": {"bids": [{"price": "0.5", "size": "10"}], "asks": []}},
        {"type": "snapshot", "reason": "periodic", "bids": [], "asks": []},
    ])
    # Truncate the gzip mid-stream.
    raw = f.read_bytes()
    f.write_bytes(raw[: len(raw) // 2])

    total, pop = check_book_feed.scan_file(f)
    # The exact count depends on where the truncation lands — what matters
    # is that the scan returns without raising.
    assert isinstance(total, int)
    assert isinstance(pop, int)
    assert pop <= total


def test_main_healthy_exits_zero(tmp_path: Path) -> None:
    """Median ratio >= 80% returns exit code 0 (healthy)."""
    for i in range(3):
        f = tmp_path / f"btc-updown-5m-170000000{i}.jsonl.gz"
        _write_gz(f, [
            {"type": "book",
             "raw": {"bids": [{"price": "0.5", "size": "10"}], "asks": []}}
        ] * 9 + [
            {"type": "snapshot", "reason": "periodic", "bids": [], "asks": []},
        ])

    rc = subprocess.run(
        [sys.executable, "scripts/check_book_feed.py", str(tmp_path)],
        capture_output=True, text=True,
    )
    assert rc.returncode == 0, f"stdout={rc.stdout} stderr={rc.stderr}"
    assert "HEALTHY" in rc.stdout


def test_main_alarm_exits_one(tmp_path: Path) -> None:
    """Median ratio < 50% returns exit code 1 (alarm)."""
    for i in range(3):
        f = tmp_path / f"btc-updown-5m-170000000{i}.jsonl.gz"
        _write_gz(f, [
            {"type": "book",
             "raw": {"bids": [{"price": "0.5", "size": "10"}], "asks": []}}
        ] + [
            {"type": "snapshot", "reason": "periodic", "bids": [], "asks": []},
        ] * 9)

    rc = subprocess.run(
        [sys.executable, "scripts/check_book_feed.py", str(tmp_path)],
        capture_output=True, text=True,
    )
    assert rc.returncode == 1, f"stdout={rc.stdout} stderr={rc.stderr}"
    assert "ALARM" in rc.stdout


def test_main_borderline_exits_two(tmp_path: Path) -> None:
    """Median ratio in [50%, 80%) returns exit code 2 (surface and discuss)."""
    for i in range(3):
        f = tmp_path / f"btc-updown-5m-170000000{i}.jsonl.gz"
        _write_gz(f, [
            {"type": "book",
             "raw": {"bids": [{"price": "0.5", "size": "10"}], "asks": []}}
        ] * 6 + [
            {"type": "snapshot", "reason": "periodic", "bids": [], "asks": []},
        ] * 4)

    rc = subprocess.run(
        [sys.executable, "scripts/check_book_feed.py", str(tmp_path)],
        capture_output=True, text=True,
    )
    assert rc.returncode == 2, f"stdout={rc.stdout} stderr={rc.stderr}"
    assert "BORDERLINE" in rc.stdout


def test_main_no_files_exits_three(tmp_path: Path) -> None:
    rc = subprocess.run(
        [sys.executable, "scripts/check_book_feed.py", str(tmp_path)],
        capture_output=True, text=True,
    )
    assert rc.returncode == 3
