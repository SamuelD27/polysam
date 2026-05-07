"""Per-strategy unit tests for the canonical ``now=`` contract.

These tests pin down the H4 fix from
``reports/r4_replay_wiring_diag.md``: every strategy must compute
``elapsed`` from the threaded ``now=`` kwarg, not from
``time.time()``. The test passes a ``t_zero`` and ``now`` whose delta
falls cleanly inside the entry window, regardless of when the test
runs on the clock — if any strategy regresses to wall-clock, the
elapsed will be ~1.7 billion seconds (epoch arithmetic against a
mid-2000s arbitrary t_zero) and the entry-zone gate fires None
instead of producing a Decision.

Companion to the historical fail mode where R2.2 replay yielded
27,766 ticks but zero ENTER actions because every elapsed computed
against wall-clock landed 40+ hours past the entry window.
"""

from __future__ import annotations

import pytest

from active_bots.base_strategy import BaseStrategy
from active_bots.enhanced_strategy import EnhancedStrategy
from active_bots.refined_strategy import RefinedStrategy
from active_bots.walked_vwap_strategy import WalkedVWAPStrategy

# Arbitrary t_zero far from wall-clock so any regression to time.time()
# pushes elapsed wildly out of the entry window.
T_ZERO = 10_000_000.0
NOW_IN_WINDOW = T_ZERO + 135.0  # 135 s into the cycle — inside every strategy's mid zone
NOW_PAST = T_ZERO + 9_999.0     # well past resolution — no zone matches

# sigma=0 collapses fair_price to deterministic 1.0/0.0/0.5 around the
# strike (see active_bots/base_strategy.py:55). With spot > strike +
# market = 0.5, edge = 0.5 → far above every zone's edge_min. Keeps
# the test free of any vol-regime assumption.
SIGMA = 0.0
SPOT = 100_000.0
STRIKE = 99_900.0  # spot > strike → fair_price=1.0 → edge=0.5 vs market=0.5
MARKET_PRICE_UP = 0.5


def _seed(strategy):
    """Seed strategy state for ``t_zero=T_ZERO``, ``strike=STRIKE``.

    Calling ``on_tick`` first would auto-reset and set strike to the
    btc_price from the call (collapsing fair_price to 0.5 because
    spot==strike). We seed directly so the test can pass spot != strike
    and reliably produce edge > zone_edge_min on the first on_tick call.
    """
    strategy.reset(t_zero=T_ZERO, strike=STRIKE)


@pytest.mark.parametrize(
    "factory",
    [
        pytest.param(lambda: BaseStrategy(), id="base"),
        pytest.param(lambda: EnhancedStrategy(enable_squeeze=False), id="enhanced"),
        pytest.param(lambda: RefinedStrategy(), id="refined"),
        pytest.param(lambda: WalkedVWAPStrategy(), id="walked_vwap"),
    ],
)
def test_on_tick_with_now_in_window_produces_action(factory):
    """now=T+135 → elapsed=135 → entry-zone gate fires; on_tick returns
    a non-None dict.

    For non-walked strategies the action is an ``ENTER`` (or
    equivalent). For ``WalkedVWAPStrategy`` the parent emits ENTER and
    the gate rejects on missing books → ``WALKED_VWAP_REJECT``. Both
    are non-None — the test asserts the entry-zone gate fired, not the
    specific action.
    """
    strategy = factory()
    _seed(strategy)
    action = strategy.on_tick(
        SPOT, MARKET_PRICE_UP, SIGMA, T_ZERO,
        market_price_ts=NOW_IN_WINDOW,
        books=None,
        now=NOW_IN_WINDOW,
    )
    assert action is not None, (
        "Strategy fell through to None despite now= inside entry window. "
        "Likely regression to time.time() — see "
        "reports/r4_replay_wiring_diag.md."
    )
    assert action.get("action") in (
        "ENTER", "WALKED_VWAP_REJECT",
    ), f"Unexpected action: {action}"


@pytest.mark.parametrize(
    "factory",
    [
        pytest.param(lambda: BaseStrategy(), id="base"),
        pytest.param(lambda: EnhancedStrategy(enable_squeeze=False), id="enhanced"),
        pytest.param(lambda: RefinedStrategy(), id="refined"),
        pytest.param(lambda: WalkedVWAPStrategy(), id="walked_vwap"),
    ],
)
def test_on_tick_past_resolution_without_position_returns_none(factory):
    """now=T+9999 → elapsed=9999 → past every entry zone AND past
    resolution. With no open position, on_tick has nothing to do and
    must return None.
    """
    strategy = factory()
    _seed(strategy)
    action = strategy.on_tick(
        SPOT, MARKET_PRICE_UP, SIGMA, T_ZERO,
        market_price_ts=NOW_PAST,
        books=None,
        now=NOW_PAST,
    )
    assert action is None, (
        f"Past-resolution call without an open position must return None; "
        f"got {action}"
    )


def test_on_tick_default_now_falls_back_to_wall_clock():
    """Omitting ``now=`` exercises the live-mode fallback: ``time.time()``
    is consulted internally. With a t_zero ~16 hours in the past, the
    elapsed computed against wall-clock falls outside the entry window
    so the strategy returns None — confirming the fallback path runs
    rather than crashing.
    """
    import time
    # 16 hours before wall-clock — far past every entry zone but inside
    # the same epoch family so time.time() arithmetic is sound.
    t_zero_far_past = time.time() - 16 * 3600
    strategy = BaseStrategy()
    action = strategy.on_tick(
        SPOT, MARKET_PRICE_UP, SIGMA, t_zero_far_past,
    )
    assert action is None
