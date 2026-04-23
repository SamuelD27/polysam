"""Tests for the golden-trace stub (spec §6.3)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

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
