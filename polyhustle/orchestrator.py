"""Single-instrument Orchestrator: owns the run loop, wires the three layers.

Holds:
- one ``DataProvider``;
- a list of ``StrategyAssignment`` triples (Strategy, Trader, role);
- a ``RiskManager`` (shared across all assignments — risk is wallet-level
  in production, even for the comparison-mode tomorrow);
- an ``EventLogger`` (today's daemon logger).

Per-tick flow (one MarketTick from the provider → all assignments):

1. Detect market rollover by comparing the tick's ``t_zero`` against
   the last-seen value. On rollover:
   - Resolve every open position via that assignment's Trader.
   - Reset every Strategy.
2. For each assignment, run ``strategy.on_tick(...)`` and dispatch the
   returned action through the Trader's ``execute`` method.

Role gating
-----------
Each assignment carries a role: ``"trader"`` or ``"observer"``.
- ``"trader"`` — Trader.execute is called with the canonical event names
  (``entry_filled`` / ``exit_filled`` / ``resolve``).
- ``"observer"`` — Trader.execute is still called (so the observer's
  paper wallet stays accurate for benchmarking) but events are
  emitted under the ``shadow_*`` namespace so consumers can distinguish
  benchmark fills from real-money fills.

Today's production posture: ``walked_vwap=trader``, all others observers.
The 2026-04-29 role-gating fix (commit 25d50f0) — only one trader posts
real CLOB orders — is preserved by separating the trader's wired
``Trader`` from the observers' independent ``Trader`` instances.

Comparison mode (Session B)
---------------------------
For Session B, multiple Strategies will run concurrently with
``role="trader"`` and INDEPENDENT ``PaperTrader`` instances each owning
their own simulated wallet. The Orchestrator does not constrain how
many traders are configured — wallet isolation lives inside each
Trader instance, not on the Orchestrator. Today's single-trader
production setup just happens to have one assignment with
``role="trader"`` and three with ``role="observer"``.

Multi-instrument (deferred)
---------------------------
The shape supports it: an Orchestrator that holds ``list[InstrumentRig]``
where each rig is one (DataProvider, list[StrategyAssignment]) pair.
Today only the BTC 5m instrument is wired; ``Orchestrator`` accepts a
single provider + list of assignments.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from polyhustle.data.provider import MarketTick
from polyhustle.execution.trader import ACTION_ENTER, ACTION_REJECT, Decision

if TYPE_CHECKING:
    from active_bots.execution.event_logger import EventLogger
    from active_bots.execution.executor import MarketCtx
    from active_bots.execution.risk_manager import RiskManager
    from polyhustle.data.provider import DataProvider
    from polyhustle.execution.trader import Trader
    from polyhustle.strategies.strategy_abc import Strategy


logger = logging.getLogger("polyhustle.orchestrator")


# Event-name maps preserve the role-gating contract from
# daemon_base_v1.strategy_loop. Trader-role strategies emit canonical
# event names; observers emit shadow_*-prefixed names so consumers can
# tell benchmark fills apart from real-money fills.
_TRADER_EVENT_NAMES: dict[str, str] = {
    "entry_filled": "entry_filled",
    "entry_rejected": "entry_rejected",
    "exit_filled": "exit_filled",
    "exit_rejected": "exit_rejected",
    "resolve": "resolve",
}
_OBSERVER_EVENT_NAMES: dict[str, str] = {
    "entry_filled": "shadow_entry",
    "entry_rejected": "shadow_entry_rejected",
    "exit_filled": "shadow_exit",
    "exit_rejected": "shadow_exit_rejected",
    "resolve": "shadow_resolve",
}


@dataclass
class StrategyAssignment:
    """One strategy/trader pair managed by the Orchestrator.

    Attributes:
        name: human-readable label (``"walked_vwap"``, ``"refined"``,
            etc.). Used in event payloads + log lines.
        strategy: a ``Strategy`` instance.
        trader: a ``Trader`` instance owning its own wallet/executor.
        role: ``"trader"`` (canonical event names, real-money path) or
            ``"observer"`` (shadow_* event names, benchmark path).
        position: per-instrument open position dict (or None). The
            Orchestrator owns this — strategies still maintain their
            own private ``_open_position`` for TP/SL math, but the
            Orchestrator's copy is the canonical one for resolves.
    """

    name: str
    strategy: Strategy
    trader: Trader
    role: str = "observer"
    position: dict[str, Any] | None = field(default=None)
    _last_t_zero: float | None = field(default=None, repr=False)

    @property
    def event_names(self) -> dict[str, str]:
        """Canonical or shadow_* names depending on role."""
        return _TRADER_EVENT_NAMES if self.role == "trader" else _OBSERVER_EVENT_NAMES


# A small protocol the Orchestrator needs to satisfy ``Trader.execute``'s
# ``ctx`` argument. Today the only producer is daemon_base_v1's
# ``refresh_market_ctx`` function; tests use a plain MarketCtx.
MarketCtxFactory = Callable[[MarketTick], "MarketCtx | None"]


def default_market_ctx_factory(tick: MarketTick) -> MarketCtx:
    """Build a MarketCtx from a tick using only its slug/strike/t_zero.

    Token ids (``yes_token_id`` / ``no_token_id``) and tick_size default
    to None / 0.01 — paper / dry-run executors don't need the resolver.
    Live mode passes its own factory backed by a ``TokenResolver``.
    """
    from active_bots.execution.executor import MarketCtx

    return MarketCtx(
        slug=tick.slug,
        t_zero=int(tick.t_zero),
        strike=tick.strike,
    )


class Orchestrator:
    """Single-instrument run loop.

    Args:
        data_provider: the DataProvider feeding ticks.
        assignments: list of StrategyAssignment (one per strategy).
        risk: shared RiskManager (wallet-level, per the daemon's
            current contract).
        events: EventLogger for events.jsonl emission.
        ctx_factory: optional callable mapping a MarketTick to a
            MarketCtx. Default uses paper-friendly fields only; live
            mode passes a factory that resolves token ids.

    The Orchestrator does not start the data provider — call ``run()``,
    which awaits the provider's ``stream()`` and shuts it down on exit.
    """

    def __init__(
        self,
        data_provider: DataProvider,
        assignments: list[StrategyAssignment],
        risk: RiskManager,
        events: EventLogger,
        *,
        ctx_factory: MarketCtxFactory = default_market_ctx_factory,
        replay_mode: bool = False,
    ) -> None:
        self._data = data_provider
        self._assignments = list(assignments)
        self._risk = risk
        self._events = events
        self._ctx_factory = ctx_factory
        self._stopping = asyncio.Event()
        self._last_t_zero: float | None = None
        # Replay-mode flag — exposed for the CLI's discoverability so it
        # knows whether to write replay_summary.json after run() returns.
        # The orchestrator itself does not branch on this flag; the
        # behaviour difference is delegated to the events object (an
        # InMemoryEventLogger when replay_mode=True).
        self.replay_mode = replay_mode
        # tick_count exposed for both CLI summary output and Task 7's
        # perf assertion (R2.2 < 60s). Incremented unconditionally at
        # the top of _on_tick.
        self.tick_count: int = 0

    @property
    def assignments(self) -> list[StrategyAssignment]:
        return list(self._assignments)

    async def run(self) -> None:
        """Drive the tick loop until shutdown is requested."""
        try:
            stream = self._data.stream()
            async for tick in self._iter(stream):
                if self._stopping.is_set():
                    break
                self._on_tick(tick)
        finally:
            await self._data.shutdown()

    @staticmethod
    async def _iter(stream: AsyncIterator[MarketTick]) -> AsyncIterator[MarketTick]:
        """Indirection so tests can pass a list-of-ticks via a fake provider."""
        async for t in stream:
            yield t

    def stop(self) -> None:
        """Request shutdown. The current tick finishes before the loop exits."""
        self._stopping.set()

    # ── per-tick dispatch ──────────────────────────────────────────────────

    def _on_tick(self, tick: MarketTick) -> None:
        """Process one tick: detect rollover, run each assignment."""
        self.tick_count += 1
        ctx = self._ctx_factory(tick)
        if ctx is None:
            return

        rolled = (
            self._last_t_zero is not None and tick.t_zero != self._last_t_zero
        )
        if rolled:
            self._on_rollover(tick, ctx)
        self._last_t_zero = tick.t_zero

        for assignment in self._assignments:
            self._run_assignment(assignment, tick, ctx)

    def _run_assignment(
        self,
        assignment: StrategyAssignment,
        tick: MarketTick,
        ctx: MarketCtx,
    ) -> None:
        """Drive one Strategy → Trader pipeline for the current tick.

        Always passes ``now=tick.timestamp`` so the strategy's internal
        time accounting matches the tick. In live mode ``tick.timestamp``
        IS wall-clock (modulo data-provider construction microseconds);
        in replay mode it is the historical tick's epoch — which is the
        whole point of this seam (see reports/r4_replay_wiring_diag.md).
        """
        action = assignment.strategy.on_tick(
            tick.btc_price,
            tick.market_price_up if tick.market_price_up is not None else 0.5,
            tick.sigma,
            tick.t_zero,
            tick.market_price_ts,
            books=tick.books,
            now=tick.timestamp,
        )

        if action is None:
            return

        decision = Decision.from_action(action)
        result = assignment.trader.execute(
            decision,
            ctx,
            position=assignment.position,
            btc_price=tick.btc_price,
            now=tick.timestamp,
            source=action.get("source", "edge"),
            books=tick.books,
        )

        ev = assignment.event_names

        if decision.action == ACTION_REJECT:
            self._events.log(
                ev["entry_rejected"],
                strategy=assignment.name,
                slug=tick.slug,
                reject_source="strategy",
                **{k: v for k, v in decision.meta.items()},
            )
            return

        # Trader-side rejection of an ENTER. Without surfacing this, the
        # strategy's internal _open_position (set inside on_tick before
        # the orchestrator dispatched) silently diverges from
        # assignment.position (still None because no entry filled). The
        # strategy then emits EXITs the orchestrator silently bounces
        # with no_open_position. Visibility is the floor; the deeper
        # fix (strategy.confirm_execution) is filed against
        # docs/POLYHUSTLE_CLI_ROADMAP.md as a follow-up. NOTE: do NOT
        # update assignment.position here — letting it stay None is
        # how the orchestrator records "no position opened."
        if decision.action == ACTION_ENTER and result.rejected:
            self._events.log(
                ev["entry_rejected"],
                strategy=assignment.name,
                slug=tick.slug,
                reject_source="trader",
                reject_reason=result.reject_reason or "trader_rejected",
                # Surface the action's intended fields so postmortem
                # analysis can see what the strategy meant to enter.
                intended_side=decision.side,
                intended_size_shares=decision.size_shares,
                intended_size_usdc=decision.size_usdc,
                intended_entry_price=decision.entry_price,
            )
            return

        if result.entry is not None:
            assignment.position = result.entry.to_position_dict()
            self._events.log(
                ev["entry_filled"],
                strategy=assignment.name,
                position=assignment.position,
                order_id=result.entry.order_id,
                token_id=result.entry.token_id,
                fill_details=result.entry.fill_details,
            )

        if result.exit_ is not None:
            trade = result.exit_.to_trade_dict()
            if assignment.role == "trader":
                self._risk.record_trade(
                    trade["pnl"], trade.get("resolved_time"),
                )
            event = ev["resolve"] if decision.action == "RESOLVE" else ev["exit_filled"]
            self._events.log(
                event,
                strategy=assignment.name,
                trade=trade,
                fill_details=result.exit_.fill_details,
            )
            assignment.position = None

    def _on_rollover(self, tick: MarketTick, ctx: MarketCtx) -> None:
        """Resolve any open positions and reset every strategy."""
        for assignment in self._assignments:
            if assignment.position is not None and tick.btc_price > 0:
                resolve = Decision(action="RESOLVE", meta={})
                result = assignment.trader.execute(
                    resolve,
                    ctx,
                    position=assignment.position,
                    btc_price=tick.btc_price,
                    now=tick.timestamp,
                    books=None,
                )
                if result.exit_ is not None:
                    trade = result.exit_.to_trade_dict()
                    if assignment.role == "trader":
                        self._risk.record_trade(
                            trade["pnl"], trade.get("resolved_time"),
                        )
                    self._events.log(
                        assignment.event_names["resolve"],
                        strategy=assignment.name,
                        trade=trade,
                        trigger="rollover",
                    )
                assignment.position = None
            assignment.strategy.reset(t_zero=tick.t_zero, strike=tick.strike)
