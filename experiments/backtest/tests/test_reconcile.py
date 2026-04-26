"""Tests for the golden-trace stub (spec §6.3)."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest


def _write_manifest(scrapes_root: Path, session_id: str, mode: str,
                    launch_ns: int, stop_ns: int | None,
                    events_path: str, feed_dir: str) -> Path:
    d = scrapes_root / session_id
    d.mkdir(parents=True, exist_ok=True)
    m = d / "manifest.json"
    m.write_text(json.dumps({
        "session_id": session_id,
        "launch_ts_ns": launch_ns,
        "stop_ts_ns": stop_ns,
        "stop_ts_utc": None if stop_ns is None else "2026-04-26T07:00:00Z",
        "mode": mode,
        "events_jsonl_path": events_path,
        "scrape_canonical_dir": feed_dir,
    }))
    return m


def test_discover_session_latest_picks_most_recent_live_or_dryrun(tmp_path: Path) -> None:
    from experiments.backtest.reconcile import discover_session
    scrapes_root = tmp_path / "scrapes"
    _write_manifest(scrapes_root, "2026-04-24T07-10-19Z", "live_dryrun",
                    1_777_014_619_000_000_000, 1_777_200_000_000_000_000,
                    str(tmp_path / "events_old.jsonl"), str(tmp_path / "feed_old"))
    _write_manifest(scrapes_root, "2026-04-26T07-00-00Z", "live_dryrun",
                    1_777_180_800_000_000_000, None,
                    str(tmp_path / "events_new.jsonl"), str(tmp_path / "feed_new"))
    _write_manifest(scrapes_root, "2026-04-26T08-00-00Z", "paper",
                    1_777_184_400_000_000_000, None,
                    str(tmp_path / "events_paper.jsonl"), str(tmp_path / "feed_paper"))
    sess = discover_session("latest", scrapes_root=scrapes_root)
    assert sess["session_id"] == "2026-04-26T07-00-00Z"
    assert sess["mode"] == "live_dryrun"


def test_discover_session_explicit_id_loads_named_manifest(tmp_path: Path) -> None:
    from experiments.backtest.reconcile import discover_session
    scrapes_root = tmp_path / "scrapes"
    _write_manifest(scrapes_root, "S1", "live_dryrun",
                    1_777_000_000_000_000_000, 1_777_100_000_000_000_000,
                    "/x/events.jsonl", "/x/feed")
    sess = discover_session("S1", scrapes_root=scrapes_root)
    assert sess["session_id"] == "S1"
    assert sess["effective_stop_ns"] == 1_777_100_000_000_000_000


def test_discover_session_explicit_paper_id_rejected(tmp_path: Path) -> None:
    from experiments.backtest.reconcile import discover_session
    scrapes_root = tmp_path / "scrapes"
    _write_manifest(scrapes_root, "P1", "paper",
                    1_777_000_000_000_000_000, 1_777_100_000_000_000_000,
                    "/x/events.jsonl", "/x/feed")
    with pytest.raises(ValueError, match="reconcile only operates on"):
        discover_session("P1", scrapes_root=scrapes_root)


def test_discover_session_running_session_uses_now_for_stop(tmp_path: Path) -> None:
    from experiments.backtest.reconcile import discover_session
    scrapes_root = tmp_path / "scrapes"
    _write_manifest(scrapes_root, "S1", "live_dryrun",
                    1_777_000_000_000_000_000, None,
                    "/x/events.jsonl", "/x/feed")
    before = int(time.time() * 1e9)
    sess = discover_session("S1", scrapes_root=scrapes_root)
    after = int(time.time() * 1e9)
    assert sess["stop_ts_ns"] is None
    assert before <= sess["effective_stop_ns"] <= after

from experiments.backtest.reconcile import (
    NoLiveFillsCaptured,
    count_live_fills,
    reconcile,
)


def _write_events(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def test_count_live_fills_zero_on_paper_only(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"
    _write_events(
        events,
        [
            {
                "ts": 1.0,
                "type": "entry_filled",
                "position": {
                    "slug": "btc-updown-5m-1",
                    "order_id": None,
                    "token_id": None,
                },
            },
            {"ts": 2.0, "type": "session_start"},
        ],
    )
    assert count_live_fills(events) == 0


def test_count_live_fills_requires_all_three_keys(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"
    _write_events(
        events,
        [
            {
                "ts": 1.0,
                "type": "entry_filled",
                "position": {"order_id": "abc", "ack_ts": 1.0},  # no fill_price
            },
            {
                "ts": 2.0,
                "type": "entry_filled",
                "position": {"order_id": "abc", "fill_price": 0.5},  # no ack_ts
            },
            {
                "ts": 3.0,
                "type": "entry_filled",
                "position": {"ack_ts": 2.0, "fill_price": 0.5},  # no order_id
            },
        ],
    )
    assert count_live_fills(events) == 0


def test_count_live_fills_accepts_top_level_fields(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"
    _write_events(
        events,
        [
            {
                "ts": 1.0,
                "type": "entry_filled",
                "order_id": "0xabc",
                "ack_ts": 1.00012,
                "fill_price": 0.48,
                "position": {"slug": "btc-updown-5m-1"},
            },
        ],
    )
    assert count_live_fills(events) == 1


def test_count_live_fills_accepts_filled_price_alias(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"
    _write_events(
        events,
        [
            {
                "ts": 1.0,
                "type": "entry_filled",
                "position": {
                    "order_id": "0xabc",
                    "ack_ts": 1.00012,
                    "filled_price": 0.48,  # alias
                },
            },
        ],
    )
    assert count_live_fills(events) == 1


def test_count_live_fills_accepts_entry_price_alias_for_live_fills(tmp_path: Path) -> None:
    # LiveExecutor.enter() persists the actual fill avg_price as 'entry_price';
    # the predicate must accept this. Paper mode lacks order_id/ack_ts so it
    # cannot accidentally slip through via this alias.
    events = tmp_path / "events.jsonl"
    _write_events(
        events,
        [
            {
                "ts": 1.0,
                "type": "entry_filled",
                "position": {
                    "order_id": "dry-run-1776910000001",
                    "ack_ts": 1.00035,
                    "entry_price": 0.48,  # LiveExecutor's fill_price alias
                },
            },
        ],
    )
    assert count_live_fills(events) == 1


def test_reconcile_raises_no_live_fills_on_paper(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"
    _write_events(
        events,
        [
            {
                "ts": 1.0,
                "type": "entry_filled",
                "position": {"order_id": None, "ack_ts": None},
            },
        ],
    )
    out = tmp_path / "golden.parquet"
    with pytest.raises(NoLiveFillsCaptured) as exc:
        reconcile(events_path=events, out=out, min_live_fills=100)
    msg = str(exc.value)
    # Exact predicate must be echoed so the operator sees the gate.
    assert "ack_ts NOT NULL" in msg
    assert "order_id NOT NULL" in msg
    assert "fill_price NOT NULL" in msg


def test_reconcile_under_min_fills_still_raises(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"
    _write_events(
        events,
        [
            {
                "ts": 1.0,
                "type": "entry_filled",
                "position": {
                    "order_id": "0x1",
                    "ack_ts": 1.00012,
                    "fill_price": 0.48,
                },
            },
        ],
    )
    out = tmp_path / "golden.parquet"
    # 1 live fill, default min=100 -> should still raise.
    with pytest.raises(NoLiveFillsCaptured) as exc:
        reconcile(events_path=events, out=out)
    assert "Found 1 live fills" in str(exc.value)


def test_reconcile_raises_not_implemented_when_gate_met(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"
    rows = [
        {
            "ts": float(i),
            "type": "entry_filled",
            "position": {
                "order_id": f"0x{i}",
                "ack_ts": i + 0.001,
                "fill_price": 0.5,
            },
        }
        for i in range(5)
    ]
    _write_events(events, rows)
    out = tmp_path / "golden.parquet"
    # With gate lowered to 5, we have enough — reconcile body is unimplemented.
    with pytest.raises(NotImplementedError):
        reconcile(events_path=events, out=out, min_live_fills=5)
