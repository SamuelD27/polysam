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


def test_post_fee_effective_vwap_at_peak():
    """At p=0.50, fee = (180/10000) * 0.5 * 0.5 * shares * 1.0 = 45 bps * shares."""
    s = WalkedVWAPStrategy()
    # 100 shares at fill_VWAP=0.50: fee = 0.0045 * 100 = $0.45
    eff = s._effective_vwap_after_fees(fill_vwap=0.50, filled_shares=100.0)
    # effective per-share = 0.50 + 0.45/100 = 0.5045
    assert eff == pytest.approx(0.5045)


def test_post_fee_effective_vwap_at_tail():
    """At p=0.10, fee = (180/10000) * 0.1 * 0.9 * shares * 1.0 = 16.2 bps * shares."""
    s = WalkedVWAPStrategy()
    # 100 shares at fill_VWAP=0.10: fee = 0.00162 * 100 = $0.162
    eff = s._effective_vwap_after_fees(fill_vwap=0.10, filled_shares=100.0)
    # effective per-share = 0.10 + 0.162/100 = 0.10162
    assert eff == pytest.approx(0.10162)


def test_post_fee_effective_vwap_zero_shares():
    s = WalkedVWAPStrategy()
    eff = s._effective_vwap_after_fees(fill_vwap=0.5, filled_shares=0.0)
    assert eff == 0.5  # no fee on zero shares; preserve fill_vwap


def test_post_fee_effective_vwap_at_extremes():
    """p=0 or p=1 → no fee per fees.py rules."""
    s = WalkedVWAPStrategy()
    assert s._effective_vwap_after_fees(0.0, 100.0) == 0.0
    assert s._effective_vwap_after_fees(1.0, 100.0) == 1.0


def _make_strategy_with_books(
    *,
    yes_asks=None, yes_bids=None,
    no_asks=None, no_bids=None,
    yes_ts_ms=1000, no_ts_ms=1000,
):
    yes = LiveBookState(tick_size=0.01)
    yes.apply_snapshot(
        bids=yes_bids or [], asks=yes_asks or [], ts_ms=yes_ts_ms,
    )
    no = LiveBookState(tick_size=0.01)
    no.apply_snapshot(
        bids=no_bids or [], asks=no_asks or [], ts_ms=no_ts_ms,
    )
    return WalkedVWAPStrategy(), MarketBooks(yes=yes, no=no)


def _refined_would_enter_action():
    """Synthetic ENTER action shape RefinedStrategy returns."""
    return {
        "action": "ENTER",
        "side": "Up",
        "entry_price": 0.30,
        "edge": 0.20,
        "size_usdc": 5.0,
        "size_shares": 16.6667,  # $5 at $0.30
        "fair": 0.50,
        "market": 0.30,
        "time_zone": "sweet_spot",
    }


def test_gate_rejects_stale_market_price(monkeypatch):
    s, mb = _make_strategy_with_books(
        yes_asks=[{"price": "0.30", "size": "100"}],
    )
    monkeypatch.setattr(s, "_run_parent_on_tick",
                        lambda *a, **k: _refined_would_enter_action())
    out = s.on_tick(
        btc_price=110_000, market_price_up=0.30, sigma=0.5,
        t_zero=time.time() - 130,
        market_price_ts=time.time() - 60,  # stale by 60s, threshold 30s
        books=mb,
    )
    assert out is not None
    assert out["action"] == "WALKED_VWAP_REJECT"
    assert out["reject_reason"] == "stale_market_price"


def test_gate_rejects_no_book_subscription(monkeypatch):
    s = WalkedVWAPStrategy()
    monkeypatch.setattr(s, "_run_parent_on_tick",
                        lambda *a, **k: _refined_would_enter_action())
    out = s.on_tick(
        btc_price=110_000, market_price_up=0.30, sigma=0.5,
        t_zero=time.time() - 130,
        market_price_ts=time.time(),
        books=None,
    )
    assert out["action"] == "WALKED_VWAP_REJECT"
    assert out["reject_reason"] == "no_book_subscription"


def test_gate_rejects_empty_book(monkeypatch):
    s, mb = _make_strategy_with_books(yes_asks=[])  # YES asks empty
    monkeypatch.setattr(s, "_run_parent_on_tick",
                        lambda *a, **k: _refined_would_enter_action())
    out = s.on_tick(
        btc_price=110_000, market_price_up=0.30, sigma=0.5,
        t_zero=time.time() - 130,
        market_price_ts=time.time(),
        books=mb,
    )
    assert out["action"] == "WALKED_VWAP_REJECT"
    assert out["reject_reason"] == "empty_book"


def test_gate_rejects_insufficient_top_of_book(monkeypatch):
    # Top of book has 5 shares; we want 16.67 → reject because
    # MIN_TOP_OF_BOOK_SHARES_RATIO=1.0 means top must >= requested.
    s, mb = _make_strategy_with_books(
        yes_asks=[{"price": "0.30", "size": "5"}],
    )
    monkeypatch.setattr(s, "_run_parent_on_tick",
                        lambda *a, **k: _refined_would_enter_action())
    out = s.on_tick(
        btc_price=110_000, market_price_up=0.30, sigma=0.5,
        t_zero=time.time() - 130,
        market_price_ts=time.time(),
        books=mb,
    )
    assert out["action"] == "WALKED_VWAP_REJECT"
    assert out["reject_reason"] == "insufficient_top_of_book"


def test_gate_rejects_insufficient_walked_edge(monkeypatch):
    # Big book at 0.49 (just below fair=0.50). Walked VWAP ≈ 0.49,
    # post-fee ≈ 0.4945, walked_edge = 0.50 - 0.4945 = 0.0055 < 0.02 → reject.
    s, mb = _make_strategy_with_books(
        yes_asks=[{"price": "0.49", "size": "100"}],
    )
    action = _refined_would_enter_action()
    action.update({"entry_price": 0.49, "fair": 0.50, "size_shares": 50.0})
    monkeypatch.setattr(s, "_run_parent_on_tick", lambda *a, **k: action)
    out = s.on_tick(
        btc_price=110_000, market_price_up=0.49, sigma=0.5,
        t_zero=time.time() - 130,
        market_price_ts=time.time(),
        books=mb,
    )
    assert out["action"] == "WALKED_VWAP_REJECT"
    assert out["reject_reason"] == "insufficient_walked_edge"


def test_gate_passes_when_all_checks_satisfy(monkeypatch):
    # Big book at 0.30, fair=0.50, walked edge ≈ 0.20 minus tiny fees → passes.
    s, mb = _make_strategy_with_books(
        yes_asks=[{"price": "0.30", "size": "1000"}],
    )
    monkeypatch.setattr(s, "_run_parent_on_tick",
                        lambda *a, **k: _refined_would_enter_action())
    out = s.on_tick(
        btc_price=110_000, market_price_up=0.30, sigma=0.5,
        t_zero=time.time() - 130,
        market_price_ts=time.time(),
        books=mb,
    )
    assert out["action"] == "ENTER"
    assert "walked_VWAP" in out
    assert out["walked_VWAP"] == pytest.approx(0.30)
    assert "walked_edge" in out
    assert out["walked_edge"] > 0.02
    assert "book_top_size_take" in out
    assert out["book_top_size_take"] == pytest.approx(1000.0)
    assert "spread_at_entry" in out
    assert "book_staleness_at_entry_ms" in out


def test_gate_rejects_nan_walked_vwap(monkeypatch):
    """Reviewer-mandated: WS feeding NaN must reject, not silently pass."""
    s = WalkedVWAPStrategy()
    yes = LiveBookState(tick_size=0.01)
    # Manually corrupt the asks list to simulate NaN-poisoned WS data
    yes.apply_snapshot(
        bids=[], asks=[{"price": "0.30", "size": "1000"}], ts_ms=1000,
    )
    yes.asks = [(float("nan"), 1000.0)]
    no = LiveBookState(tick_size=0.01)
    mb = MarketBooks(yes=yes, no=no)
    monkeypatch.setattr(s, "_run_parent_on_tick",
                        lambda *a, **k: _refined_would_enter_action())
    out = s.on_tick(
        btc_price=110_000, market_price_up=0.30, sigma=0.5,
        t_zero=time.time() - 130,
        market_price_ts=time.time(),
        books=mb,
    )
    assert out is not None
    assert out["action"] == "WALKED_VWAP_REJECT"
    # Reuses empty_book reason — no new vocabulary
    assert out["reject_reason"] == "empty_book"
