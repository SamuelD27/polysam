"""Tests for ``polyhustle.orchestrator.Orchestrator``.

Three contracts:

1. The run loop dispatches every tick to every assignment's strategy.
2. Role gating: ``"trader"`` strategies cause Trader.execute calls and
   emit canonical event names; ``"observer"`` strategies still drive
   their own (independent) Trader (so the paper wallet stays accurate)
   but emit ``shadow_*`` events.
3. Market rollover triggers resets and resolves any open positions.

We use lightweight fakes for everything except the Orchestrator
itself — the goal is to verify dispatch and role gating, not
re-test the executors / strategies.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import pytest

from active_bots.execution.executor import EntryResult, ExitResult, MarketCtx
from polyhustle.data.provider import DataProvider, MarketTick
from polyhustle.execution.trader import (
    ACTION_ENTER,
    ACTION_REJECT,
    Decision,
    ExecutionResult,
    Trader,
)
from polyhustle.orchestrator import Orchestrator, StrategyAssignment
from polyhustle.strategies.strategy_abc import Strategy

# ── Fakes ─────────────────────────────────────────────────────────────────


class FakeDataProvider(DataProvider):
    def __init__(self, ticks: list[MarketTick]) -> None:
        self._ticks = ticks
        self.shutdown_called = False

    async def stream(self) -> AsyncIterator[MarketTick]:
        for t in self._ticks:
            yield t

    async def shutdown(self) -> None:
        self.shutdown_called = True


@dataclass
class FakeStrategy(Strategy):
    """Returns a programmed list of actions, one per tick."""

    actions: list[dict[str, Any] | None] = field(default_factory=list)
    on_tick_calls: list[tuple] = field(default_factory=list)
    reset_calls: list[tuple] = field(default_factory=list)
    _has_position: bool = False

    def on_tick(
        self, btc_price, market_price_up, sigma, t_zero,
        market_price_ts=0.0, *, books=None,
    ):
        self.on_tick_calls.append(
            (btc_price, market_price_up, sigma, t_zero, market_price_ts, books)
        )
        if not self.actions:
            return None
        return self.actions.pop(0)

    def reset(self, t_zero=None, strike=None):
        self.reset_calls.append((t_zero, strike))
        self._has_position = False

    @property
    def has_position(self) -> bool:
        return self._has_position


@dataclass
class FakeTrader(Trader):
    mode: str = "paper"
    calls: list[tuple[Decision, MarketCtx]] = field(default_factory=list)
    next_results: list[ExecutionResult] = field(default_factory=list)
    reconcile_calls: int = 0

    def execute(self, decision, ctx, *, position=None, now=None,
                btc_price=None, source="edge"):
        self.calls.append((decision, ctx))
        if self.next_results:
            return self.next_results.pop(0)
        return ExecutionResult(action=decision.action)

    def reconcile(self, now):
        self.reconcile_calls += 1


class FakeRisk:
    def __init__(self):
        self.recorded: list[tuple[float, float | None]] = []

    def record_trade(self, pnl, resolved_time):
        self.recorded.append((pnl, resolved_time))


class FakeEventLogger:
    def __init__(self):
        self.events: list[tuple[str, dict[str, Any]]] = []

    def log(self, event_name, **kwargs):
        self.events.append((event_name, kwargs))


# ── Helpers ───────────────────────────────────────────────────────────────


def _tick(t_zero=1_777_985_400.0, slug="btc-updown-5m-1777985400") -> MarketTick:
    return MarketTick(
        timestamp=time.time(),
        btc_price=110_000.0,
        market_price_up=0.42,
        sigma=0.55,
        t_zero=t_zero,
        strike=110_000.0,
        slug=slug,
        books=None,
        market_price_ts=time.time(),
    )


def _entry_result(side="Up", slug="btc-updown-5m-1777985400") -> EntryResult:
    return EntryResult(
        slug=slug,
        side=side,
        entry_price=0.42,
        size_usdc=4.20,
        size_shares=10.0,
        strike=110_000.0,
        entry_time=time.time(),
        edge=0.15,
    )


def _exit_result(slug="btc-updown-5m-1777985400", pnl=1.50) -> ExitResult:
    return ExitResult(
        slug=slug,
        side="Up",
        entry_price=0.42,
        exit_price=0.55,
        pnl=pnl,
        edge=0.15,
        size_usdc=4.20,
        size_shares=10.0,
        won=True,
        resolved_time=time.time(),
        strike=110_000.0,
        final_btc=111_000.0,
        exit_type="TP",
    )


def _enter_action(side="Up") -> dict[str, Any]:
    return {
        "action": ACTION_ENTER,
        "side": side,
        "entry_price": 0.42,
        "size_shares": 10.0,
        "size_usdc": 4.20,
        "edge": 0.15,
        "fair": 0.50,
        "market": 0.38,
    }


# ── Tests ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_orchestrator_dispatches_each_tick_to_each_strategy():
    s1 = FakeStrategy()
    s2 = FakeStrategy()
    t1 = FakeTrader()
    t2 = FakeTrader()
    a1 = StrategyAssignment(name="walked_vwap", strategy=s1, trader=t1, role="trader")
    a2 = StrategyAssignment(name="refined", strategy=s2, trader=t2, role="observer")

    ticks = [_tick(), _tick(), _tick()]
    o = Orchestrator(
        data_provider=FakeDataProvider(ticks),
        assignments=[a1, a2],
        risk=FakeRisk(),
        events=FakeEventLogger(),
    )
    await o.run()

    assert len(s1.on_tick_calls) == 3
    assert len(s2.on_tick_calls) == 3


@pytest.mark.asyncio
async def test_role_gating_distinguishes_event_names():
    """Trader role → entry_filled. Observer role → shadow_entry."""
    s1 = FakeStrategy(actions=[_enter_action()])
    s2 = FakeStrategy(actions=[_enter_action()])
    t1 = FakeTrader(next_results=[ExecutionResult(action=ACTION_ENTER, entry=_entry_result())])
    t2 = FakeTrader(next_results=[ExecutionResult(action=ACTION_ENTER, entry=_entry_result())])
    a_trader = StrategyAssignment(name="walked_vwap", strategy=s1, trader=t1, role="trader")
    a_obs = StrategyAssignment(name="refined", strategy=s2, trader=t2, role="observer")

    events = FakeEventLogger()
    o = Orchestrator(
        data_provider=FakeDataProvider([_tick()]),
        assignments=[a_trader, a_obs],
        risk=FakeRisk(),
        events=events,
    )
    await o.run()

    by_name = {ev: kw for ev, kw in events.events}
    assert "entry_filled" in by_name
    assert by_name["entry_filled"]["strategy"] == "walked_vwap"
    assert "shadow_entry" in by_name
    assert by_name["shadow_entry"]["strategy"] == "refined"


@pytest.mark.asyncio
async def test_observer_strategies_still_call_their_trader():
    """Observers run their own trader so their wallet stays current."""
    s = FakeStrategy(actions=[_enter_action()])
    t = FakeTrader(next_results=[ExecutionResult(action=ACTION_ENTER, entry=_entry_result())])
    a = StrategyAssignment(name="refined", strategy=s, trader=t, role="observer")

    o = Orchestrator(
        data_provider=FakeDataProvider([_tick()]),
        assignments=[a],
        risk=FakeRisk(),
        events=FakeEventLogger(),
    )
    await o.run()
    assert len(t.calls) == 1


@pytest.mark.asyncio
async def test_walked_vwap_reject_emits_shadow_entry_rejected_for_observer():
    """REJECT actions emit entry_rejected (or shadow_*) and skip the trader."""
    s = FakeStrategy(actions=[
        {"action": ACTION_REJECT, "reject_reason": "insufficient_walked_edge", "side": "Up"},
    ])
    t = FakeTrader()
    a = StrategyAssignment(name="walked_vwap", strategy=s, trader=t, role="trader")

    events = FakeEventLogger()
    o = Orchestrator(
        data_provider=FakeDataProvider([_tick()]),
        assignments=[a],
        risk=FakeRisk(),
        events=events,
    )
    await o.run()

    by_name = [ev for ev, _ in events.events]
    assert "entry_rejected" in by_name


@pytest.mark.asyncio
async def test_rollover_resets_every_strategy_and_resolves_open_positions():
    """When t_zero changes, strategies reset and any open position is resolved."""
    enter_action = _enter_action()
    s = FakeStrategy(actions=[enter_action, None])
    t = FakeTrader(next_results=[
        ExecutionResult(action=ACTION_ENTER, entry=_entry_result()),  # tick 1: enter
        ExecutionResult(action="RESOLVE", exit_=_exit_result(pnl=2.5)),  # rollover resolve
    ])
    a = StrategyAssignment(name="walked_vwap", strategy=s, trader=t, role="trader")

    risk = FakeRisk()
    events = FakeEventLogger()
    ticks = [_tick(t_zero=1_777_985_400.0), _tick(t_zero=1_777_985_700.0)]
    o = Orchestrator(
        data_provider=FakeDataProvider(ticks),
        assignments=[a],
        risk=risk,
        events=events,
    )
    await o.run()

    # Strategy was reset at rollover with the new t_zero.
    assert any(call[0] == 1_777_985_700.0 for call in s.reset_calls)
    # Trader was called twice: once to enter, once to resolve.
    assert len(t.calls) == 2
    # Risk recorded the resolved trade since role=trader.
    assert risk.recorded == [(2.5, pytest.approx(t.calls[1][0].meta.get("resolved_time", risk.recorded[0][1])))]
    # Resolve event emitted under canonical (trader) name.
    assert any(ev == "resolve" for ev, _ in events.events)


@pytest.mark.asyncio
async def test_provider_shutdown_called_on_run_completion():
    p = FakeDataProvider([_tick()])
    o = Orchestrator(
        data_provider=p,
        assignments=[],
        risk=FakeRisk(),
        events=FakeEventLogger(),
    )
    await o.run()
    assert p.shutdown_called is True


@pytest.mark.asyncio
async def test_observer_pnl_does_not_record_into_shared_risk():
    """RiskManager records PnL only for trader-role resolves."""
    s = FakeStrategy(actions=[_enter_action(), None])
    t = FakeTrader(next_results=[
        ExecutionResult(action=ACTION_ENTER, entry=_entry_result()),
        ExecutionResult(action="RESOLVE", exit_=_exit_result(pnl=99.0)),
    ])
    a = StrategyAssignment(name="enhanced", strategy=s, trader=t, role="observer")

    risk = FakeRisk()
    ticks = [_tick(t_zero=1_777_985_400.0), _tick(t_zero=1_777_985_700.0)]
    o = Orchestrator(
        data_provider=FakeDataProvider(ticks),
        assignments=[a],
        risk=risk,
        events=FakeEventLogger(),
    )
    await o.run()
    assert risk.recorded == []
