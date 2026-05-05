"""Tests for ProfitGrabber.check_exit asymmetry fixes (Phase 4 cleanup pass).

Coverage:

- Fix A: ``SL_ABSOLUTE_AGAINST`` cap (mirror of ``TP_ABSOLUTE_FAVOR``).
- Fix B: ``SL_DECAY_ENABLE`` time-decay on adaptive SL (mirror of adaptive TP decay).

Both fixes ship behind default-off env-var-gated flags, matching the
``WALKED_VWAP_EXIT_ENABLE`` pattern. With the flags off, behaviour is
bit-for-bit identical to the pre-cleanup branch.
"""

from __future__ import annotations

import time

import pytest

from active_bots.enhanced_strategy import (
    MARKET_DURATION,
    ProfitGrabber,
    adaptive_sl,
    adaptive_sl_decayed,
)


def _make_position(
    *,
    side: str = "Up",
    entry_price: float = 0.50,
    edge: float = 0.10,
    size_shares: float = 100.0,
) -> dict:
    """Build a minimal position dict matching what EnhancedStrategy stashes."""
    return {
        "side": side,
        "entry_price": entry_price,
        "size_usdc": size_shares * entry_price,
        "size_shares": size_shares,
        "strike": 110_000.0,
        "entry_time": time.time() - 60.0,
        "entry_elapsed": 60.0,
        "edge": edge,
        "fair_price": entry_price + edge,
        "market_price_up": entry_price,
    }


# ── Fix A: SL_ABSOLUTE_AGAINST cap ──────────────────────────────────────────


def test_sl_absolute_against_off_preserves_existing_sl_behavior():
    """With sl_absolute_against=None, only the adaptive SL fires.

    A 5c against move on a 0.10-edge position has adaptive_sl = 0.10.
    Below threshold → no fire. This must not change when the cap is None.
    """
    pos = _make_position(side="Up", entry_price=0.50, edge=0.10)
    pg = ProfitGrabber(
        tp_delta_min=0.10,
        tp_absolute_favor=0.30,  # high, so TP can't fire
        sl_absolute_against=None,
    )
    # delta_against = 0.05; adaptive_sl = 0.10 → no fire.
    out = pg.check_exit(
        pos,
        btc_price=110_000.0,
        sigma=0.5,
        t_zero=time.time() - 60.0,
        market_price_up=0.45,
        market_price_ts=time.time(),
    )
    assert out is None


def test_sl_absolute_against_fires_when_delta_exceeds_cap():
    """With sl_absolute_against=0.10 and delta_against=0.12, SL fires.

    Adaptive SL is still 0.10 (would fire), but the absolute cap should
    fire FIRST/independently — same precedence as tp_absolute_favor. The
    test uses a delta the adaptive curve already covers; the cap exists
    so a smaller adaptive_sl can't hide a large absolute drawdown.
    """
    pos = _make_position(side="Up", entry_price=0.50, edge=0.10)
    pg = ProfitGrabber(
        tp_delta_min=0.10,
        tp_absolute_favor=0.30,
        sl_absolute_against=0.10,
    )
    # delta_against = 0.12 ≥ 0.10 → fire
    out = pg.check_exit(
        pos,
        btc_price=110_000.0,
        sigma=0.5,
        t_zero=time.time() - 60.0,
        market_price_up=0.38,
        market_price_ts=time.time(),
    )
    assert out is not None
    assert out["action"] == "EXIT_SL"


def test_sl_absolute_against_does_not_fire_below_cap():
    """delta_against below the cap, with adaptive curve also below threshold,
    must NOT fire."""
    # High-edge position → adaptive_sl is 0.10 (mid of band).
    pos = _make_position(side="Up", entry_price=0.50, edge=0.10)
    pg = ProfitGrabber(
        tp_delta_min=0.10,
        tp_absolute_favor=0.30,
        sl_absolute_against=0.10,
    )
    # delta_against = 0.05 < 0.10 (cap) AND < 0.10 (adaptive_sl) → no fire
    out = pg.check_exit(
        pos,
        btc_price=110_000.0,
        sigma=0.5,
        t_zero=time.time() - 60.0,
        market_price_up=0.45,
        market_price_ts=time.time(),
    )
    assert out is None


def test_sl_absolute_against_edge_case_equal_threshold():
    """delta_against exactly equal to the cap fires SL (>= comparator).

    Mirrors the >= semantics of tp_absolute_favor at enhanced_strategy.py:300.
    Uses the IEEE-754-exact difference for the cap so we test the boundary
    condition (>= rather than >) without false-failing on float noise.
    """
    entry_price = 0.50
    market_price_up = 0.40
    pos = _make_position(side="Up", entry_price=entry_price, edge=0.10)
    cap = entry_price - market_price_up  # exact IEEE difference
    pg = ProfitGrabber(
        tp_delta_min=0.10,
        tp_absolute_favor=0.30,
        sl_absolute_against=cap,
    )
    out = pg.check_exit(
        pos,
        btc_price=110_000.0,
        sigma=0.5,
        t_zero=time.time() - 60.0,
        market_price_up=market_price_up,
        market_price_ts=time.time(),
    )
    assert out is not None
    assert out["action"] == "EXIT_SL"


def test_sl_absolute_against_independent_of_adaptive_curve():
    """A small adaptive_sl (low-edge entry) should NOT block a large
    delta_against from triggering the absolute cap."""
    # Tiny edge → adaptive_sl floors at SL_DELTA_MIN = 0.05.
    pos = _make_position(side="Up", entry_price=0.50, edge=0.0)
    pg = ProfitGrabber(
        tp_delta_min=0.10,
        tp_absolute_favor=0.30,
        sl_absolute_against=0.15,
    )
    # delta_against = 0.20 > 0.15 (cap) AND > 0.05 (adaptive) → fires.
    # The test point is that the cap, not adaptive, is the gating threshold.
    out = pg.check_exit(
        pos,
        btc_price=110_000.0,
        sigma=0.5,
        t_zero=time.time() - 60.0,
        market_price_up=0.30,
        market_price_ts=time.time(),
    )
    assert out is not None
    assert out["action"] == "EXIT_SL"


# ── Fix B: adaptive_sl_decayed (time-decay on adaptive SL) ──────────────────


@pytest.mark.parametrize(
    "time_remaining,expected_min,expected_max",
    [
        # At full cycle: returns the un-decayed adaptive_sl ceiling.
        (MARKET_DURATION, 0.10, 0.10),
        # At half cycle: midpoint between floor and ceiling.
        (MARKET_DURATION / 2, 0.075 - 0.001, 0.075 + 0.001),
        # Near resolution: at the floor.
        (0.0, 0.05, 0.05),
    ],
)
def test_adaptive_sl_decayed_curve_shape(
    time_remaining: float, expected_min: float, expected_max: float
):
    """Linear decay from ceiling at entry to floor at expiry."""
    edge = 0.10  # adaptive_sl = clamp(0.5*0.10 + 0.05, 0.05, 0.20) = 0.10
    decayed = adaptive_sl_decayed(edge, time_remaining, sl_delta_decay_floor=0.05)
    assert expected_min <= decayed <= expected_max


def test_adaptive_sl_decayed_at_entry_equals_undecayed():
    """At t=MARKET_DURATION (full cycle remaining), the decayed SL must
    equal the un-decayed adaptive_sl. This is the no-op-at-entry property
    that gives the operator a safe initial behaviour identical to today's."""
    edge = 0.20  # adaptive_sl = clamp(0.15, 0.05, 0.20) = 0.15
    expected = adaptive_sl(edge)
    decayed = adaptive_sl_decayed(edge, MARKET_DURATION, sl_delta_decay_floor=0.05)
    assert decayed == pytest.approx(expected)


def test_adaptive_sl_decayed_floor_overrides_negative_remaining():
    """Negative time_remaining (past expiry) clamps to the floor."""
    edge = 0.10
    decayed = adaptive_sl_decayed(edge, -100.0, sl_delta_decay_floor=0.05)
    assert decayed == pytest.approx(0.05)


def test_adaptive_sl_decayed_respects_explicit_floor():
    """Floor parameter shapes the asymptote at t=0."""
    edge = 0.10
    decayed = adaptive_sl_decayed(edge, 0.0, sl_delta_decay_floor=0.03)
    assert decayed == pytest.approx(0.03)


# ── Fix B integration: SL_DECAY_ENABLE flag wires through check_exit ───────


def test_check_exit_sl_decay_off_uses_undecayed_curve():
    """With SL_DECAY_ENABLE=0 (default), check_exit must use adaptive_sl
    unchanged — no time-remaining argument enters the SL threshold path.

    Concrete: after 270s (30s left), a 0.10-edge position has adaptive_sl
    = 0.10. delta_against = 0.06 < 0.10 → no fire. With decay ON the
    threshold would have shrunk to ~0.055 and SL would fire.
    """
    pos = _make_position(side="Up", entry_price=0.50, edge=0.10)
    pg = ProfitGrabber(
        tp_delta_min=0.10,
        tp_absolute_favor=0.30,
        sl_decay_enable=False,  # default; explicit for the test
    )
    # 270s elapsed → 30s remaining. But force-window is 30s, so we'd be
    # IN the force window. Use 240s elapsed → 60s remaining (outside force).
    t_zero = time.time() - 240.0
    pos["entry_time"] = t_zero + 60.0
    out = pg.check_exit(
        pos,
        btc_price=110_000.0,
        sigma=0.5,
        t_zero=t_zero,
        market_price_up=0.44,  # delta_against = 0.06
        market_price_ts=time.time(),
    )
    assert out is None


def test_check_exit_sl_decay_on_fires_when_threshold_decayed():
    """With SL_DECAY_ENABLE=1, near resolution the SL threshold has decayed
    below today's static value — a delta that wouldn't fire under the
    un-decayed curve does fire under the decayed one."""
    pos = _make_position(side="Up", entry_price=0.50, edge=0.10)
    pg = ProfitGrabber(
        tp_delta_min=0.10,
        tp_absolute_favor=0.30,
        sl_decay_enable=True,
        sl_delta_decay_floor=0.05,
        force_exit_before_s=0.0,  # disable force-window so we test the decay path
    )
    # 240s elapsed → 60s remaining (out of 300). Decay frac = 60/300 = 0.2.
    # threshold = 0.05 + (0.10 - 0.05) * 0.2 = 0.06. delta_against = 0.07 fires.
    t_zero = time.time() - 240.0
    pos["entry_time"] = t_zero + 60.0
    out = pg.check_exit(
        pos,
        btc_price=110_000.0,
        sigma=0.5,
        t_zero=t_zero,
        market_price_up=0.43,  # delta_against = 0.07
        market_price_ts=time.time(),
    )
    assert out is not None
    assert out["action"] == "EXIT_SL"


def test_check_exit_sl_decay_on_at_entry_matches_undecayed():
    """At entry (full cycle remaining), the decay flag is a no-op: the
    threshold is the un-decayed adaptive_sl. Bit-for-bit equivalent to OFF."""
    pos = _make_position(side="Up", entry_price=0.50, edge=0.10)
    pg = ProfitGrabber(
        tp_delta_min=0.10,
        tp_absolute_favor=0.30,
        sl_decay_enable=True,
        sl_delta_decay_floor=0.05,
        force_exit_before_s=0.0,
    )
    # ~0s elapsed → time_remaining ≈ MARKET_DURATION. delta_against = 0.06
    # < 0.10 → no fire under the un-decayed curve.
    t_zero = time.time() - 1.0
    pos["entry_time"] = t_zero + 0.5
    out = pg.check_exit(
        pos,
        btc_price=110_000.0,
        sigma=0.5,
        t_zero=t_zero,
        market_price_up=0.44,
        market_price_ts=time.time(),
    )
    assert out is None
