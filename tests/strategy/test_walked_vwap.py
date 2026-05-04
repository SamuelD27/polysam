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


def test_partial_fill_off_rejects(monkeypatch):
    """PARTIAL_OK=False: book can't fill full size → reject."""
    monkeypatch.setattr("active_bots.walked_vwap_strategy.WALKED_VWAP_PARTIAL_OK", False)
    monkeypatch.setattr("active_bots.walked_vwap_strategy.MIN_TOP_OF_BOOK_SHARES_RATIO", 0.0)
    s, mb = _make_strategy_with_books(
        yes_asks=[{"price": "0.30", "size": "5"}],  # 5 shares avail, want 16.67
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
    assert out["reject_reason"] == "partial_fill_disallowed"


def test_partial_fill_on_downsizes(monkeypatch):
    """PARTIAL_OK=True: fill what we can, downsize entry."""
    monkeypatch.setattr("active_bots.walked_vwap_strategy.WALKED_VWAP_PARTIAL_OK", True)
    monkeypatch.setattr("active_bots.walked_vwap_strategy.MIN_TOP_OF_BOOK_SHARES_RATIO", 0.0)
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
    assert out["action"] == "ENTER"
    assert out["size_shares"] == pytest.approx(5.0)
    assert out["walked_VWAP"] == pytest.approx(0.30)


# ── entry_price overwrite + entry_price_mid preservation ────────────────────

def test_gate_pass_overwrites_entry_price_with_effective_vwap(monkeypatch):
    """On gate-pass the action's entry_price is overwritten with effective_VWAP
    (book-walk + bell-curve fees) so dry-run / live PnL reflects realizable
    fills. The original mid quote is preserved as entry_price_mid."""
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
    # Mid (parent-quoted) preserved as diagnostic
    assert out["entry_price_mid"] == pytest.approx(0.30)
    # entry_price now equals the effective per-share cost (= walked_VWAP +
    # fee/share). Since walked_VWAP = 0.30 and the book has 1 level, fees push
    # the effective up by a small amount.
    assert out["entry_price"] == pytest.approx(out["effective_VWAP"])
    assert out["entry_price"] > out["entry_price_mid"]
    # size_usdc is recomputed against the new (higher) per-share cost
    assert out["size_usdc"] == pytest.approx(
        out["size_shares"] * out["effective_VWAP"]
    )


def test_paper_executor_carries_entry_price_mid_and_pnl_mid():
    """PaperExecutor stores entry_price_mid on the position, and at exit
    computes a parallel pnl_mid using the mid as the entry baseline."""
    from active_bots.execution.executor import MarketCtx
    from active_bots.execution.paper_executor import PaperExecutor

    ex = PaperExecutor()
    ctx = MarketCtx(slug="s", t_zero=1000, strike=110_000.0)
    action = {
        "action": "ENTER",
        "side": "Up",
        "entry_price": 0.32,        # post-gate, walked
        "entry_price_mid": 0.30,    # pre-gate, parent's mid quote
        "size_usdc": 32.0,
        "size_shares": 100.0,
        "edge": 0.20,
        "fair": 0.50,
        "market": 0.30,
    }
    entry = ex.enter(action, ctx, now=1776860000.0)
    assert entry is not None
    assert entry.entry_price == pytest.approx(0.32)
    assert entry.entry_price_mid == pytest.approx(0.30)
    pos = entry.to_position_dict()
    assert pos["entry_price"] == pytest.approx(0.32)
    assert pos["entry_price_mid"] == pytest.approx(0.30)

    exit_action = {
        "action": "EXIT_TP",
        "exit_price": 0.45,
        # Strategy quotes pnl using position["entry_price"] (= 0.32):
        # (0.45 - 0.32) * 100 - 0.01 * 100 = 13.0 - 1.0 = 12.0
        "pnl": 12.0,
        "hold_time_s": 60.0,
    }
    res = ex.exit(pos, exit_action, ctx, now=1776860100.0, btc_price=109_000.0)
    assert res is not None
    assert res.pnl == pytest.approx(12.0)
    # pnl_mid uses entry_price_mid (= 0.30): (0.45 - 0.30) * 100 - 1.0 = 14.0
    assert res.pnl_mid == pytest.approx(14.0)
    assert res.entry_price_mid == pytest.approx(0.30)
    trade = res.to_trade_dict()
    assert trade["pnl"] == pytest.approx(12.0)
    assert trade["pnl_mid"] == pytest.approx(14.0)
    assert trade["entry_price_mid"] == pytest.approx(0.30)


def test_paper_executor_entries_without_mid_omit_pnl_mid():
    """Backwards compat: entries that did not pass through the walked-VWAP gate
    have no entry_price_mid → no pnl_mid is computed and the trade dict
    excludes both keys."""
    from active_bots.execution.executor import MarketCtx
    from active_bots.execution.paper_executor import PaperExecutor

    ex = PaperExecutor()
    ctx = MarketCtx(slug="s", t_zero=1000, strike=110_000.0)
    action = {
        "action": "ENTER", "side": "Up", "entry_price": 0.30,
        "size_usdc": 30.0, "size_shares": 100.0, "edge": 0.20,
    }
    entry = ex.enter(action, ctx, now=1776860000.0)
    assert entry is not None
    assert entry.entry_price_mid is None
    pos = entry.to_position_dict()
    assert "entry_price_mid" not in pos

    exit_action = {
        "action": "EXIT_TP", "exit_price": 0.40, "pnl": 9.0, "hold_time_s": 60.0,
    }
    res = ex.exit(pos, exit_action, ctx, now=1776860100.0, btc_price=109_000.0)
    assert res is not None
    assert res.pnl_mid is None
    trade = res.to_trade_dict()
    assert "pnl_mid" not in trade
    assert "entry_price_mid" not in trade


# ── parent-position sync (Commit 4 fix) ─────────────────────────────────────

def test_gate_pass_syncs_parent_open_position_with_effective_vwap(monkeypatch):
    """Regression: the gate-pass branch must mutate self._open_position so
    that ProfitGrabber.check_exit (which reads position["entry_price"] on
    every subsequent tick) operates against the walked entry, not the mid.

    Without this sync, daemon-side trade.pnl is computed against the mid and
    ends up identical to trade.pnl_mid, defeating the dual-PnL design."""
    s, mb = _make_strategy_with_books(
        yes_asks=[
            {"price": "0.30", "size": "50"},
            {"price": "0.32", "size": "200"},
        ],
    )
    # Stub the parent so it returns a real ENTER action AND populates
    # self._open_position the way EnhancedStrategy.on_tick does on its real
    # entry path (line 634).
    def fake_parent(self_, *a, **k):
        action = _refined_would_enter_action()
        # Mirror EnhancedStrategy.on_tick line 634-647: the parent stores
        # the mid-quoted entry as the position before returning the action.
        s._open_position = {
            "side": action["side"],
            "entry_price": action["entry_price"],   # mid quote (0.30)
            "size_usdc": action["size_usdc"],
            "size_shares": action["size_shares"],
            "strike": 110_000.0,
            "entry_time": time.time(),
            "edge": action["edge"],
        }
        s._position_source = "edge"
        return action
    monkeypatch.setattr(
        "active_bots.walked_vwap_strategy.WalkedVWAPStrategy._run_parent_on_tick",
        fake_parent,
    )

    out = s.on_tick(
        btc_price=110_000, market_price_up=0.30, sigma=0.5,
        t_zero=time.time() - 130,
        market_price_ts=time.time(),
        books=mb,
    )
    assert out["action"] == "ENTER"
    eff = out["effective_VWAP"]
    assert eff > 0.30, "effective_VWAP should exceed mid (walked + fees)"

    # The action carries the walked entry — already covered by an earlier test.
    # The new assertion: the parent's internal stash is synced too.
    assert s._open_position is not None
    assert s._open_position["entry_price"] == pytest.approx(eff)
    assert s._open_position["size_shares"] == pytest.approx(out["size_shares"])
    assert s._open_position["size_usdc"] == pytest.approx(
        out["size_shares"] * eff
    )


def test_gate_pass_pnl_uses_walked_entry_not_mid(monkeypatch):
    """End-to-end regression: ProfitGrabber.check_exit, called against the
    synced _open_position, must produce a pnl computed against the walked
    entry. If the sync regresses, this fails because pnl will reflect mid
    arithmetic — and trade.pnl_mid (computed independently in PaperExecutor)
    will silently equal trade.pnl."""
    from active_bots.enhanced_strategy import ProfitGrabber

    # Multi-level book: top 50 @ 0.30, then 200 @ 0.40 → walked vwap on
    # 175 shares straddles both levels and lands well above mid.
    s, mb = _make_strategy_with_books(
        yes_asks=[
            {"price": "0.30", "size": "50"},
            {"price": "0.40", "size": "200"},
        ],
        # Big top-of-book overlay so the gate's MIN_TOP_OF_BOOK_SHARES_RATIO
        # check (defaults to 1.0 of requested) doesn't reject; we want the
        # walked vwap to exceed mid via the second level, not the gate to fail.
    )
    # Override MIN_TOP_OF_BOOK_SHARES_RATIO so 50-share top is enough to gate.
    monkeypatch.setattr(
        "active_bots.walked_vwap_strategy.MIN_TOP_OF_BOOK_SHARES_RATIO", 0.0,
    )

    requested_shares = 175.0
    mid_quote = 0.30

    def fake_parent(self_, *a, **k):
        action = {
            "action": "ENTER",
            "side": "Up",
            "entry_price": mid_quote,
            "edge": 0.20,
            "size_usdc": requested_shares * mid_quote,
            "size_shares": requested_shares,
            "fair": 0.50,
            "market": mid_quote,
            "time_zone": "sweet_spot",
        }
        s._open_position = {
            "side": "Up",
            "entry_price": mid_quote,           # parent stores mid
            "size_usdc": action["size_usdc"],
            "size_shares": requested_shares,
            "strike": 110_000.0,
            "entry_time": time.time(),
            "edge": 0.20,
        }
        s._position_source = "edge"
        return action

    monkeypatch.setattr(
        "active_bots.walked_vwap_strategy.WalkedVWAPStrategy._run_parent_on_tick",
        fake_parent,
    )

    enter_action = s.on_tick(
        btc_price=110_000, market_price_up=mid_quote, sigma=0.5,
        t_zero=time.time() - 130,
        market_price_ts=time.time(),
        books=mb,
    )
    assert enter_action["action"] == "ENTER"
    eff_vwap = enter_action["effective_VWAP"]
    assert eff_vwap > mid_quote + 0.04, (
        f"need a meaningful spread for this test; got eff={eff_vwap} "
        f"vs mid={mid_quote}"
    )

    # Simulate a TP-favourable market move and run ProfitGrabber against the
    # parent's _open_position. With the sync, pnl reflects (exit-eff)×shares;
    # without it, pnl reflects (exit-mid)×shares.
    pg = ProfitGrabber(tp_delta_min=0.01, tp_absolute_favor=0.0)
    exit_market_up = 0.70
    exit_action = pg.check_exit(
        s._open_position,
        btc_price=110_000.0, sigma=0.5,
        t_zero=time.time() - 200,
        market_price_up=exit_market_up,
        market_price_ts=time.time(),
    )
    assert exit_action is not None
    assert exit_action["action"] == "EXIT_TP"
    pnl_walked = exit_action["pnl"]
    # Walked-PnL formula: (exit - eff) * shares - SPREAD_COST * shares
    SPREAD_COST = 0.01
    expected_walked = (
        (exit_market_up - eff_vwap) * requested_shares
        - SPREAD_COST * requested_shares
    )
    expected_mid = (
        (exit_market_up - mid_quote) * requested_shares
        - SPREAD_COST * requested_shares
    )
    assert pnl_walked == pytest.approx(expected_walked)
    # The whole point of dual PnL: walked < mid when fees + book walk push
    # eff above mid. A regression that forgets the sync would make pnl_walked
    # equal expected_mid.
    assert pnl_walked != pytest.approx(expected_mid)
    assert (expected_mid - pnl_walked) > 1.0, (
        f"non-trivial spread expected; got walked={pnl_walked} mid={expected_mid}"
    )


# ── parent-position phantom-clear on reject (Commit 5 fix) ──────────────────

def test_gate_reject_clears_parent_phantom_open_position(monkeypatch):
    """Regression: when the gate rejects, the parent's self._open_position
    (set by EnhancedStrategy.on_tick BEFORE this gate ran) must be cleared.
    Otherwise the strategy thinks it has a position the daemon doesn't know
    about, blocks new entries this market, and emits phantom exits the daemon
    silently swallows.

    Triggered here via insufficient_top_of_book; same clear must happen on
    every reject reason since the parent's stash is set independently of the
    rejection path."""
    s, mb = _make_strategy_with_books(
        yes_asks=[{"price": "0.30", "size": "5"}],  # top size 5 < requested 16.7
    )

    def fake_parent(self_, *a, **k):
        action = _refined_would_enter_action()
        # Mirror EnhancedStrategy.on_tick storing the position BEFORE the
        # gate sees the action.
        s._open_position = {
            "side": action["side"],
            "entry_price": action["entry_price"],
            "size_usdc": action["size_usdc"],
            "size_shares": action["size_shares"],
            "strike": 110_000.0,
            "entry_time": time.time(),
            "edge": action["edge"],
        }
        s._position_source = "edge"
        return action
    monkeypatch.setattr(
        "active_bots.walked_vwap_strategy.WalkedVWAPStrategy._run_parent_on_tick",
        fake_parent,
    )

    out = s.on_tick(
        btc_price=110_000, market_price_up=0.30, sigma=0.5,
        t_zero=time.time() - 130,
        market_price_ts=time.time(),
        books=mb,
    )
    assert out["action"] == "WALKED_VWAP_REJECT"
    assert out["reject_reason"] == "insufficient_top_of_book"
    # The fix: phantom position is gone; strategy is free to consider new
    # entries on subsequent ticks.
    assert s._open_position is None
    assert s._position_source is None
    # Sanity: _resolved was never set to True on reject (only set True after
    # a real exit/resolution), so we don't need to clear it.
    assert s._resolved is False
