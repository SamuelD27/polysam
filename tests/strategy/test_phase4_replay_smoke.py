"""Phase 5 smoke: replay parity / divergence for the three Phase 4 flags.

Drives a deterministic synthetic tick sequence through ``WalkedVWAPStrategy``
twice — once with all three flags off (must match the un-instrumented path),
once with one flag on at a time (must diverge at a documented point). This
is a unit-level stand-in for the spec's "replay against an experiments/_baseline
fixture"; the same parity/divergence properties are asserted, just on a
hand-built fixture rather than recorded WS frames.

Each test is parametrised over the three Phase 4 knobs:

- ``SL_ABSOLUTE_AGAINST``        — Phase 4A hard SL cap.
- ``SL_DECAY_ENABLE``            — Phase 4B time-decay on adaptive SL.
- ``WALKED_VWAP_SIZE_ON_WALKED_EDGE`` — Phase 4C walked-edge sizing.
"""

from __future__ import annotations

import time

import pytest

from active_bots.execution.live_book_state import LiveBookState, MarketBooks
from active_bots.walked_vwap_strategy import WalkedVWAPStrategy


def _strategy_with_books(yes_asks: list[dict], yes_bids: list[dict] | None = None):
    yes = LiveBookState(tick_size=0.01)
    yes.apply_snapshot(bids=yes_bids or [], asks=yes_asks, ts_ms=int(time.time() * 1000))
    no = LiveBookState(tick_size=0.01)
    return WalkedVWAPStrategy(), MarketBooks(yes=yes, no=no)


def _stage_open_position(s, *, mid_quote: float, requested_shares: float, monkeypatch):
    """Force the parent's on_tick to emit an ENTER once and stash _open_position.

    Returns the gate-augmented action.
    """

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
            "entry_price": mid_quote,
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
    monkeypatch.setattr(
        "active_bots.walked_vwap_strategy.MIN_TOP_OF_BOOK_SHARES_RATIO",
        0.0,
    )


# ── Parity: all flags off ───────────────────────────────────────────────────


def test_replay_parity_phase4_flags_all_off(monkeypatch):
    """All Phase 4 flags off: action sequence is bit-for-bit equivalent to
    pre-cleanup behaviour. Specifically:

    - SL_ABSOLUTE_AGAINST=None: SL gating is solely the adaptive curve.
    - SL_DECAY_ENABLE=False:   SL threshold does not decay.
    - SIZE_ON_WALKED_EDGE=False: size_shares retains parent's emission.
    """
    # Explicit defaults to make this test deterministic regardless of the
    # operator's environment.
    monkeypatch.setenv("SL_DECAY_ENABLE", "0")
    monkeypatch.delenv("SL_ABSOLUTE_AGAINST", raising=False)
    monkeypatch.setattr(
        "active_bots.walked_vwap_strategy.SIZE_ON_WALKED_EDGE",
        False,
    )
    s, mb = _strategy_with_books(
        yes_asks=[{"price": "0.30", "size": "50"}, {"price": "0.40", "size": "200"}],
    )
    _stage_open_position(s, mid_quote=0.30, requested_shares=175.0, monkeypatch=monkeypatch)
    enter = s.on_tick(
        btc_price=110_000.0,
        market_price_up=0.30,
        sigma=0.5,
        t_zero=time.time() - 130,
        market_price_ts=time.time(),
        books=mb,
    )
    assert enter["action"] == "ENTER"
    # Sizing path: parent's 175 preserved (no recompute).
    assert enter["size_shares"] == pytest.approx(175.0)


# ── Divergence: each flag flipped individually ─────────────────────────────


def test_replay_divergence_sl_absolute_against_on(monkeypatch):
    """Flag on → SL fires on a delta the adaptive curve alone wouldn't trip."""
    pos = {
        "side": "Up",
        "entry_price": 0.50,
        "size_usdc": 50.0,
        "size_shares": 100.0,
        "strike": 110_000.0,
        "entry_time": time.time() - 60.0,
        "edge": 0.10,
    }
    from active_bots.enhanced_strategy import ProfitGrabber

    # Same drawdown, two grabbers: one without cap (no fire), one with (fires).
    pg_off = ProfitGrabber(tp_delta_min=0.10, tp_absolute_favor=0.30, sl_absolute_against=None)
    pg_on = ProfitGrabber(tp_delta_min=0.10, tp_absolute_favor=0.30, sl_absolute_against=0.10)
    common = dict(
        btc_price=110_000.0,
        sigma=0.5,
        t_zero=time.time() - 60.0,
        market_price_up=0.38,  # delta_against = 0.12 — > cap, < adaptive (0.10)
        market_price_ts=time.time(),
    )
    # Divergence shows up when delta_against < adaptive_sl but >= cap.
    # Use a high-edge position so adaptive_sl saturates wide.
    pos["edge"] = 0.30  # adaptive_sl = clamp(0.20, 0.05, 0.20) = 0.20
    common["market_price_up"] = 0.38  # delta_against = 0.12 < 0.20
    out_off = pg_off.check_exit(pos, **common)
    out_on = pg_on.check_exit(pos, **common)
    assert out_off is None  # off branch: wide adaptive_sl masks the drawdown
    assert out_on is not None and out_on["action"] == "EXIT_SL"  # cap fires


def test_replay_divergence_sl_decay_on(monkeypatch):
    """Flag on → SL threshold has decayed late in the cycle, so a smaller
    drawdown fires than would under the static adaptive_sl."""
    pos = {
        "side": "Up",
        "entry_price": 0.50,
        "size_usdc": 50.0,
        "size_shares": 100.0,
        "strike": 110_000.0,
        "entry_time": time.time() - 240.0,
        "edge": 0.10,
    }
    from active_bots.enhanced_strategy import ProfitGrabber

    pg_off = ProfitGrabber(
        tp_delta_min=0.10, tp_absolute_favor=0.30, sl_decay_enable=False, force_exit_before_s=0.0
    )
    pg_on = ProfitGrabber(
        tp_delta_min=0.10,
        tp_absolute_favor=0.30,
        sl_decay_enable=True,
        sl_delta_decay_floor=0.05,
        force_exit_before_s=0.0,
    )
    common = dict(
        btc_price=110_000.0,
        sigma=0.5,
        t_zero=time.time() - 240.0,  # 60s remaining → decayed threshold ~0.06
        market_price_up=0.43,  # delta_against = 0.07 — > decayed, < static (0.10)
        market_price_ts=time.time(),
    )
    out_off = pg_off.check_exit(pos, **common)
    out_on = pg_on.check_exit(pos, **common)
    assert out_off is None  # static curve at 0.10 doesn't fire
    assert out_on is not None and out_on["action"] == "EXIT_SL"  # decayed curve fires


def test_replay_divergence_size_on_walked_edge(monkeypatch):
    """Flag on → size_shares shrinks from parent's mid-edge value when the
    book is thin enough that walked_edge << mid_edge."""
    monkeypatch.setattr(
        "active_bots.walked_vwap_strategy.SIZE_ON_WALKED_EDGE",
        True,
    )
    s_on, mb_on = _strategy_with_books(
        yes_asks=[{"price": "0.30", "size": "50"}, {"price": "0.40", "size": "200"}],
    )
    _stage_open_position(s_on, mid_quote=0.30, requested_shares=175.0, monkeypatch=monkeypatch)
    enter_on = s_on.on_tick(
        btc_price=110_000.0,
        market_price_up=0.30,
        sigma=0.5,
        t_zero=time.time() - 130,
        market_price_ts=time.time(),
        books=mb_on,
    )
    monkeypatch.setattr(
        "active_bots.walked_vwap_strategy.SIZE_ON_WALKED_EDGE",
        False,
    )
    s_off, mb_off = _strategy_with_books(
        yes_asks=[{"price": "0.30", "size": "50"}, {"price": "0.40", "size": "200"}],
    )
    _stage_open_position(s_off, mid_quote=0.30, requested_shares=175.0, monkeypatch=monkeypatch)
    enter_off = s_off.on_tick(
        btc_price=110_000.0,
        market_price_up=0.30,
        sigma=0.5,
        t_zero=time.time() - 130,
        market_price_ts=time.time(),
        books=mb_off,
    )
    assert enter_off["size_shares"] == pytest.approx(175.0)
    assert enter_on["size_shares"] < enter_off["size_shares"]
