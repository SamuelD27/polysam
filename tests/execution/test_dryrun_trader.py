"""Tests for ``polyhustle.execution.dryrun_trader.DryrunTrader``.

Contracts:

1. Implements the Trader ABC and reports ``mode == "live"`` (matches
   the underlying ``DryRunExecutor``; downstream dashboards filter by
   mode and live_dryrun must surface as live, not paper).
2. Entry results carry a synthetic ``dry-run-<ms>`` order_id so
   reconcile.py's non-null predicate passes.
3. Token id is resolved from MarketCtx.
"""

from __future__ import annotations

from active_bots.execution.executor import MarketCtx
from polyhustle.execution.dryrun_trader import DryrunTrader
from polyhustle.execution.trader import ACTION_ENTER, Decision, Trader


def test_dryrun_trader_implements_abc():
    t = DryrunTrader()
    assert isinstance(t, Trader)
    assert t.mode == "live"


def test_dryrun_trader_stamps_synthetic_order_id():
    t = DryrunTrader()
    ctx = MarketCtx(
        slug="btc-updown-5m-1",
        t_zero=1,
        strike=110_000.0,
        yes_token_id="yes-token-abc",
        no_token_id="no-token-def",
    )
    decision = Decision(
        action=ACTION_ENTER,
        side="Up",
        entry_price=0.42,
        size_shares=10.0,
        size_usdc=4.20,
        edge=0.15,
        meta={},
    )
    res = t.execute(decision, ctx, source="edge", now=100.0)
    assert res.entry is not None
    assert res.entry.order_id is not None
    assert res.entry.order_id.startswith("dry-run-")
    # token_id resolved from MarketCtx for the side
    assert res.entry.token_id == "yes-token-abc"
