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
