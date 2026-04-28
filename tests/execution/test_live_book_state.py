"""Unit tests for the daemon-side per-token book state machine."""
from __future__ import annotations

import pytest

from active_bots.execution.live_book_state import (
    LiveBookState,
    MarketBooks,
    walk_for_vwap,
)


def test_apply_snapshot_replaces_both_sides():
    s = LiveBookState(tick_size=0.01)
    s.apply_snapshot(
        bids=[{"price": "0.40", "size": "10"}, {"price": "0.39", "size": "5"}],
        asks=[{"price": "0.42", "size": "8"}, {"price": "0.43", "size": "20"}],
        ts_ms=1000,
    )
    assert s.bids == [(0.40, 10.0), (0.39, 5.0)]
    assert s.asks == [(0.42, 8.0), (0.43, 20.0)]
    assert s.ts_ms == 1000


def test_apply_delta_inserts_and_removes_levels():
    s = LiveBookState(tick_size=0.01)
    s.apply_snapshot(
        bids=[{"price": "0.40", "size": "10"}],
        asks=[{"price": "0.42", "size": "8"}],
        ts_ms=1000,
    )
    s.apply_delta({"side": "BUY", "price": "0.41", "size": "5"}, ts_ms=1100)
    s.apply_delta({"side": "SELL", "price": "0.42", "size": "0"}, ts_ms=1100)
    assert s.bids == [(0.41, 5.0), (0.40, 10.0)]
    assert s.asks == []  # 0.42 removed, no other asks
    assert s.ts_ms == 1100


def test_apply_delta_before_snapshot_is_dropped():
    s = LiveBookState(tick_size=0.01)
    # No snapshot yet — apply_delta is a no-op + records the dropped count
    s.apply_delta({"side": "BUY", "price": "0.40", "size": "5"}, ts_ms=900)
    assert s.bids == []
    assert s.dropped_deltas_no_baseline == 1
    assert not s.has_baseline


def test_walk_for_vwap_full_fill():
    asks = [(0.42, 8.0), (0.43, 20.0), (0.45, 100.0)]
    res = walk_for_vwap(asks, requested_shares=10.0)
    # 8 @ 0.42 + 2 @ 0.43 = 10 shares; VWAP = (8*0.42 + 2*0.43) / 10 = 0.422
    assert res.classification == "full"
    assert res.filled_shares == pytest.approx(10.0)
    assert res.vwap == pytest.approx(0.422)
    assert res.levels_consumed == 2


def test_walk_for_vwap_partial_fill():
    asks = [(0.42, 8.0)]
    res = walk_for_vwap(asks, requested_shares=10.0)
    assert res.classification == "partial"
    assert res.filled_shares == pytest.approx(8.0)
    assert res.vwap == pytest.approx(0.42)
    assert res.residual_shares == pytest.approx(2.0)
    assert res.levels_consumed == 1


def test_walk_for_vwap_empty_book():
    res = walk_for_vwap([], requested_shares=10.0)
    assert res.classification == "unfilled"
    assert res.filled_shares == 0.0
    assert res.vwap is None
    assert res.residual_shares == pytest.approx(10.0)


def test_top_of_book_size_helper():
    s = LiveBookState(tick_size=0.01)
    s.apply_snapshot(
        bids=[{"price": "0.40", "size": "10"}],
        asks=[{"price": "0.42", "size": "8"}],
        ts_ms=1000,
    )
    assert s.top_size("asks") == 8.0
    assert s.top_size("bids") == 10.0
    s.apply_snapshot(bids=[], asks=[], ts_ms=1100)
    assert s.top_size("asks") == 0.0
    assert s.top_size("bids") == 0.0


def test_market_books_wrapper_staleness():
    yes = LiveBookState(tick_size=0.01)
    yes.apply_snapshot(
        bids=[{"price": "0.40", "size": "10"}],
        asks=[{"price": "0.42", "size": "8"}],
        ts_ms=1000,
    )
    no = LiveBookState(tick_size=0.01)
    mb = MarketBooks(yes=yes, no=no)
    assert mb.yes_staleness_ms(now_ms=1500) == 500
    assert mb.no_staleness_ms(now_ms=1500) is None  # never seen a snapshot
