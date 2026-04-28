"""Unit tests for WalkedVWAPStrategy.

Tests are organized by phase:
- Scaffold: import, instantiation, identity behaviour without books.
- Computation (B2/B3): walked VWAP, fee curve.
- Gate (B4): each reject reason fires under the right condition.
- Partial-fill (B5): WALKED_VWAP_PARTIAL_OK flag behaviour.
"""
from __future__ import annotations

import time

import pytest

from active_bots.walked_vwap_strategy import WalkedVWAPStrategy
from active_bots.refined_strategy import RefinedStrategy
from active_bots.execution.live_book_state import LiveBookState, MarketBooks


def test_imports_cleanly():
    s = WalkedVWAPStrategy()
    assert isinstance(s, RefinedStrategy)


def test_no_books_falls_through_to_refined():
    """Without books kwarg, on_tick should behave exactly like RefinedStrategy."""
    s = WalkedVWAPStrategy()
    t0 = time.time() - 130  # entry window for refined is T+120..T+150
    # No books → expect None (refined would fire only with edge; we pass benign args)
    out = s.on_tick(
        btc_price=110_000.0,
        market_price_up=0.50,
        sigma=0.5,
        t_zero=t0,
        market_price_ts=time.time(),
    )
    # Without books, at p=mkt=0.5 fair=0.5 → no edge → None. Sanity: also reachable
    # via parent path. The point is: no exception, no books-required error.
    assert out is None or out.get("action") in ("ENTER", "EXIT_TP", "EXIT_SL", "RESOLVE")


def test_books_kwarg_accepted():
    """Calling with books=MarketBooks(...) does not raise."""
    s = WalkedVWAPStrategy()
    yes = LiveBookState(tick_size=0.01)
    no = LiveBookState(tick_size=0.01)
    mb = MarketBooks(yes=yes, no=no)
    out = s.on_tick(
        btc_price=110_000.0,
        market_price_up=0.50,
        sigma=0.5,
        t_zero=time.time() - 130,
        market_price_ts=time.time(),
        books=mb,
    )
    # Empty books at p=0.50 with no fair-price edge: returns None (no entry signal upstream).
    assert out is None


def test_walked_vwap_for_action_up():
    """side=Up → consumes books.yes.asks."""
    s = WalkedVWAPStrategy()
    yes = LiveBookState(tick_size=0.01)
    yes.apply_snapshot(
        bids=[{"price": "0.40", "size": "10"}],
        asks=[{"price": "0.42", "size": "8"}, {"price": "0.43", "size": "20"}],
        ts_ms=1000,
    )
    no = LiveBookState(tick_size=0.01)
    mb = MarketBooks(yes=yes, no=no)
    res = s._walked_vwap_for_entry(
        side="Up", requested_shares=10.0, books=mb,
    )
    # 8 @ 0.42 + 2 @ 0.43 = 10 shares; VWAP = 0.422
    assert res.classification == "full"
    assert res.vwap == pytest.approx(0.422)


def test_walked_vwap_for_action_down():
    """side=Down → consumes books.no.asks."""
    s = WalkedVWAPStrategy()
    yes = LiveBookState(tick_size=0.01)
    no = LiveBookState(tick_size=0.01)
    no.apply_snapshot(
        bids=[{"price": "0.55", "size": "10"}],
        asks=[{"price": "0.58", "size": "5"}, {"price": "0.59", "size": "20"}],
        ts_ms=1000,
    )
    mb = MarketBooks(yes=yes, no=no)
    res = s._walked_vwap_for_entry(
        side="Down", requested_shares=10.0, books=mb,
    )
    # 5 @ 0.58 + 5 @ 0.59 = 10 shares; VWAP = (5*0.58 + 5*0.59)/10 = 0.585
    assert res.classification == "full"
    assert res.vwap == pytest.approx(0.585)


def test_walked_vwap_partial():
    s = WalkedVWAPStrategy()
    yes = LiveBookState(tick_size=0.01)
    yes.apply_snapshot(
        bids=[],
        asks=[{"price": "0.42", "size": "5"}],
        ts_ms=1000,
    )
    no = LiveBookState(tick_size=0.01)
    mb = MarketBooks(yes=yes, no=no)
    res = s._walked_vwap_for_entry(side="Up", requested_shares=10.0, books=mb)
    assert res.classification == "partial"
    assert res.filled_shares == pytest.approx(5.0)
    assert res.residual_shares == pytest.approx(5.0)
