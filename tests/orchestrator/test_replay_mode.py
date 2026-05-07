"""End-to-end replay-mode tests.

Covers:
1. InMemoryEventLogger captures events without disk I/O.
2. aggregate_summary groups events by strategy with correct stats.
3. Orchestrator with replay_mode=True keeps the in-memory logger
   functional (no disk I/O attempted).
4. tick_count is exposed on the orchestrator.
"""

from __future__ import annotations

import pytest


def test_in_memory_event_logger_captures_writes():
    from polyhustle.data.replay_summary import InMemoryEventLogger
    log = InMemoryEventLogger()
    log.log("entry_filled", strategy="walked_vwap", position={"pnl": 0.0})
    log.log("exit_filled", strategy="walked_vwap", trade={"pnl": 1.5})
    assert len(log.events) == 2
    assert log.events[0]["type"] == "entry_filled"
    assert log.events[1]["type"] == "exit_filled"


def test_in_memory_event_logger_close_is_noop():
    from polyhustle.data.replay_summary import InMemoryEventLogger
    log = InMemoryEventLogger()
    log.log("x")
    log.close()  # should not raise
    log.log("y")
    assert len(log.events) == 2


def test_aggregate_summary_groups_by_strategy():
    from polyhustle.data.replay_summary import (
        InMemoryEventLogger,
        aggregate_summary,
    )
    log = InMemoryEventLogger()
    log.log("exit_filled", strategy="walked_vwap",
            trade={"pnl": 1.0, "pnl_mid": 1.5, "won": True, "exit_type": "TP"})
    log.log("exit_filled", strategy="walked_vwap",
            trade={"pnl": -2.0, "pnl_mid": -1.5, "won": False, "exit_type": "SL"})
    log.log("exit_filled", strategy="refined",
            trade={"pnl": 3.0, "won": True, "exit_type": "TP"})

    summary = aggregate_summary(log.events)
    assert "walked_vwap" in summary["by_strategy"]
    assert "refined" in summary["by_strategy"]
    walked = summary["by_strategy"]["walked_vwap"]
    assert walked["n_trades"] == 2
    assert walked["pnl_total"] == pytest.approx(-1.0)
    assert walked["pnl_mid_total"] == pytest.approx(0.0)  # 1.5 + (-1.5)
    assert walked["fill_realism_tax"] == pytest.approx(1.0)  # (1.5-1.0) + (-1.5-(-2.0))
    assert walked["win_rate"] == pytest.approx(0.5)
    assert walked["exit_types"]["TP"] == 1
    assert walked["exit_types"]["SL"] == 1


def test_aggregate_summary_omits_pnl_mid_when_absent():
    from polyhustle.data.replay_summary import (
        InMemoryEventLogger,
        aggregate_summary,
    )
    log = InMemoryEventLogger()
    log.log("exit_filled", strategy="refined",
            trade={"pnl": 3.0, "won": True, "exit_type": "TP"})
    summary = aggregate_summary(log.events)
    refined = summary["by_strategy"]["refined"]
    assert refined["pnl_mid_total"] is None
    assert refined["fill_realism_tax"] is None


def test_aggregate_summary_includes_shadow_and_resolve_events():
    from polyhustle.data.replay_summary import (
        InMemoryEventLogger,
        aggregate_summary,
    )
    log = InMemoryEventLogger()
    log.log("shadow_exit", strategy="enhanced",
            trade={"pnl": 0.5, "won": True, "exit_type": "TP"})
    log.log("resolve", strategy="walked_vwap",
            trade={"pnl": 0.0, "won": False, "exit_type": "RESOLUTION"})
    log.log("shadow_resolve", strategy="enhanced",
            trade={"pnl": 0.0, "won": False, "exit_type": "RESOLUTION"})
    summary = aggregate_summary(log.events)
    assert summary["by_strategy"]["enhanced"]["n_trades"] == 2
    assert summary["by_strategy"]["walked_vwap"]["n_trades"] == 1


@pytest.mark.asyncio
async def test_orchestrator_exposes_tick_count_and_replay_mode_flag(synthetic_session):
    from polyhustle.data.replay import ReplayDataProvider
    from polyhustle.data.replay_summary import InMemoryEventLogger
    from polyhustle.execution.paper_trader import PaperTrader
    from polyhustle.orchestrator import Orchestrator, StrategyAssignment
    from polyhustle.strategies.refined import RefinedStrategy

    # Real in-memory plumbing: provider + paper trader + refined strategy.
    provider = ReplayDataProvider(synthetic_session)
    provider.load()
    strategy = RefinedStrategy(max_risk=10.0, role="trader")
    trader = PaperTrader()
    assignment = StrategyAssignment(
        name="refined", strategy=strategy, trader=trader, role="trader",
    )
    log = InMemoryEventLogger()

    class _NoOpRisk:
        """Risk manager stub — no kill-switch / loss tracking for unit tests."""
        def check_entry(self, *a, **k): return True
        def record_trade(self, *a, **k): return None

    o = Orchestrator(
        data_provider=provider,
        assignments=[assignment],
        risk=_NoOpRisk(),
        events=log,
        replay_mode=True,
    )
    await o.run()
    assert o.replay_mode is True
    assert o.tick_count > 0


@pytest.mark.asyncio
async def test_orchestrator_replay_mode_default_false():
    """replay_mode defaults to False so existing callers stay unchanged."""
    from collections.abc import AsyncIterator

    from polyhustle.data.provider import DataProvider, MarketTick
    from polyhustle.data.replay_summary import InMemoryEventLogger
    from polyhustle.orchestrator import Orchestrator

    class _EmptyProvider(DataProvider):
        async def stream(self) -> AsyncIterator[MarketTick]:
            if False:
                yield  # type: ignore[unreachable]
        async def shutdown(self) -> None:
            return None

    class _NoOpRisk:
        def record_trade(self, *a, **k): return None

    o = Orchestrator(
        data_provider=_EmptyProvider(),
        assignments=[],
        risk=_NoOpRisk(),
        events=InMemoryEventLogger(),
    )
    assert o.replay_mode is False
    await o.run()
    assert o.tick_count == 0


@pytest.mark.asyncio
async def test_orchestrator_emits_entry_rejected_on_trader_side_reject():
    """When PaperTrader rejects an ACTION_ENTER (e.g. paper_no_fill on
    a thin book), the orchestrator must emit an ``entry_rejected``
    event with ``reject_source="trader"``. Without this, every
    rejected entry is silent loss; the strategy thinks it has a
    position, the orchestrator stays at position=None, and every
    subsequent EXIT bounces with ``no_open_position`` invisibly.

    Regression-proof for the secondary gap surfaced during R2.2 H4
    validation — see reports/r4_replay_wiring_diag.md and the H1
    branch's commit body.
    """
    import time
    from collections.abc import AsyncIterator
    from dataclasses import dataclass, field

    from polyhustle.data.provider import DataProvider, MarketTick
    from polyhustle.execution.trader import (
        ACTION_ENTER,
        ExecutionResult,
        Trader,
    )
    from polyhustle.orchestrator import Orchestrator, StrategyAssignment
    from polyhustle.strategies.strategy_abc import Strategy

    # Strategy that always emits one ENTER on each tick.
    @dataclass
    class _AlwaysEnterStrategy(Strategy):
        on_tick_calls: int = 0
        _open: bool = False

        def on_tick(self, btc_price, market_price_up, sigma, t_zero,
                    market_price_ts=None, *, books=None, now=None, **kwargs):
            self.on_tick_calls += 1
            return {
                "action": ACTION_ENTER,
                "side": "Up",
                "entry_price": 0.5,
                "size_shares": 4.0,
                "size_usdc": 2.0,
                "edge": 0.5,
            }

        def reset(self, t_zero=None, strike=None):
            self._open = False

        @property
        def has_position(self) -> bool:
            return self._open

    # Trader that always rejects with paper_no_fill.
    @dataclass
    class _RejectingTrader(Trader):
        mode: str = "paper"
        calls: int = 0

        def execute(self, decision, ctx, **kw):
            self.calls += 1
            return ExecutionResult(
                action=decision.action,
                rejected=True,
                reject_reason="paper_no_fill",
            )

        def reconcile(self, now):
            return None

    @dataclass
    class _Provider(DataProvider):
        n_ticks: int = 1

        async def stream(self) -> AsyncIterator[MarketTick]:
            for _ in range(self.n_ticks):
                yield MarketTick(
                    timestamp=time.time(),
                    btc_price=100_000.0,
                    market_price_up=0.5,
                    sigma=0.5,
                    t_zero=10_000_000.0,
                    strike=100_000.0,
                    slug="btc-updown-5m-10000000",
                    books=None,
                    market_price_ts=time.time(),
                )

        async def shutdown(self):
            return None

    @dataclass
    class _CapturingLogger:
        events: list[tuple[str, dict]] = field(default_factory=list)

        def log(self, event_type, **fields):
            self.events.append((event_type, fields))

        def close(self):
            return None

    class _NoOpRisk:
        def record_trade(self, *a, **k):
            return None

    strategy = _AlwaysEnterStrategy()
    trader = _RejectingTrader()
    assignment = StrategyAssignment(
        name="walked_vwap", strategy=strategy, trader=trader, role="trader",
    )
    log = _CapturingLogger()

    o = Orchestrator(
        data_provider=_Provider(n_ticks=1),
        assignments=[assignment],
        risk=_NoOpRisk(),
        events=log,
    )
    await o.run()

    rejected = [e for e in log.events if e[0] == "entry_rejected"]
    assert len(rejected) == 1, (
        f"expected exactly one entry_rejected event, got {log.events}"
    )
    name, fields = rejected[0]
    assert fields["strategy"] == "walked_vwap"
    assert fields["reject_source"] == "trader"
    assert fields["reject_reason"] == "paper_no_fill"
    assert fields["intended_side"] == "Up"
    assert fields["intended_size_shares"] == 4.0
    # Position must remain None — strategy/orchestrator divergence
    # must not leak through this path.
    assert assignment.position is None


@pytest.mark.asyncio
async def test_orchestrator_strategy_side_reject_carries_strategy_source():
    """ACTION_REJECT (the WALKED_VWAP_REJECT path) must tag the event
    with reject_source="strategy" so postmortem can distinguish
    strategy gate rejects from trader rejects.
    """
    import time
    from collections.abc import AsyncIterator
    from dataclasses import dataclass, field

    from polyhustle.data.provider import DataProvider, MarketTick
    from polyhustle.execution.trader import (
        ACTION_REJECT,
        ExecutionResult,
        Trader,
    )
    from polyhustle.orchestrator import Orchestrator, StrategyAssignment
    from polyhustle.strategies.strategy_abc import Strategy

    @dataclass
    class _RejectingStrategy(Strategy):
        _open: bool = False

        def on_tick(self, btc_price, market_price_up, sigma, t_zero,
                    market_price_ts=None, *, books=None, now=None, **kwargs):
            return {
                "action": ACTION_REJECT,
                "side": "Down",
                "reject_reason": "empty_book",
            }

        def reset(self, t_zero=None, strike=None):
            self._open = False

        @property
        def has_position(self) -> bool:
            return self._open

    @dataclass
    class _PassthroughTrader(Trader):
        mode: str = "paper"

        def execute(self, decision, ctx, **kw):
            return ExecutionResult(
                action=decision.action,
                rejected=True,
                reject_reason=decision.meta.get("reject_reason", "walked_vwap_reject"),
            )

        def reconcile(self, now):
            return None

    @dataclass
    class _Provider(DataProvider):
        async def stream(self) -> AsyncIterator[MarketTick]:
            yield MarketTick(
                timestamp=time.time(),
                btc_price=100_000.0,
                market_price_up=0.5,
                sigma=0.5,
                t_zero=10_000_000.0,
                strike=100_000.0,
                slug="btc-updown-5m-10000000",
                books=None,
                market_price_ts=time.time(),
            )

        async def shutdown(self):
            return None

    @dataclass
    class _CapturingLogger:
        events: list[tuple[str, dict]] = field(default_factory=list)

        def log(self, event_type, **fields):
            self.events.append((event_type, fields))

        def close(self):
            return None

    class _NoOpRisk:
        def record_trade(self, *a, **k):
            return None

    strategy = _RejectingStrategy()
    trader = _PassthroughTrader()
    assignment = StrategyAssignment(
        name="walked_vwap", strategy=strategy, trader=trader, role="trader",
    )
    log = _CapturingLogger()

    o = Orchestrator(
        data_provider=_Provider(),
        assignments=[assignment],
        risk=_NoOpRisk(),
        events=log,
    )
    await o.run()

    rejected = [e for e in log.events if e[0] == "entry_rejected"]
    assert len(rejected) == 1
    _, fields = rejected[0]
    assert fields["reject_source"] == "strategy"
    assert fields["reject_reason"] == "empty_book"


@pytest.mark.slow
def test_replay_r22_wall_clock_under_target(tmp_path):
    """R2.2 capture replays in under the achieved-target wall clock.

    Target is 60 s. Achieved on the GX10 box (2026-05-06) was ~32 s
    end-to-end including process spin-up — see docs/REPLAY.md for the
    full breakdown and the optimisations that got us there.

    Skipped when the capture isn't on disk (worktrees that don't
    symlink daemon_state through to the parent repo).
    """
    import asyncio
    import os
    import time
    from pathlib import Path

    from polyhustle.cli import LaunchConfig, run_async

    capture = Path(
        "/home/samsam/polymarket-hustle/daemon_state/scrapes/2026-05-05T12-50-04Z"
    )
    if not (capture / "manifest.json").exists():
        pytest.skip("R2.2 capture not on disk")

    # Replicate the CLI-time env override (compute_effective_max_risk
    # respects PORTFOLIO_SIZE_USDC and skips the Polygon RPC dial).
    os.environ.setdefault("PORTFOLIO_SIZE_USDC", "10")

    cfg = LaunchConfig(
        mode="main",
        main_strategy="walked_vwap",
        benchmarks=["refined", "enhanced", "base"],
        execution_mode="replay",
        replay_session=str(capture),
        params={"max_trade_size_usdc": 5.0, "portfolio_size_usdc": 10},
    )
    t0 = time.time()
    asyncio.run(run_async(cfg))
    dt = time.time() - t0

    # ACHIEVED_TARGET_S — see docs/REPLAY.md. Bump if the perf pass
    # ever falls back to <60min; the assertion message points operators
    # at the right place to investigate.
    ACHIEVED_TARGET_S = 60.0
    assert dt < ACHIEVED_TARGET_S, (
        f"R2.2 replay took {dt:.1f}s; target {ACHIEVED_TARGET_S}s. "
        f"If this regression is real, profile + adjust; if the target "
        f"is loose, update both the assertion and docs/REPLAY.md."
    )
