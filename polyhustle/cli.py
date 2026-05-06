"""``python -m polyhustle.cli`` — entry point for the modular run.

Reads a JSON launch config (``--config <path>`` or
``POLYHUSTLE_CONFIG=<path>`` env), constructs an ``Orchestrator``, and
runs it. The same JSON schema covers paper / dryrun / live and Session
B's comparison mode.

Schema (validated by ``LaunchConfig``)::

    {
      "mode": "main" | "comparison" | "replay",
      "main_strategy": "refined" | "walked_vwap" | "enhanced" | "base",
      "benchmarks": ["enhanced", "base"],
      "comparison_strategies": ["refined", "walked_vwap"],
      "execution_mode": "live_real" | "live_dryrun" | "paper" | "replay",
      "replay_session": "2026-05-05T12-50-04Z",
      "params": {"max_trade_size_usdc": 5.0, ...},
      "tui_layout": "textual" | "ratatui"
    }

The strategy registry maps the JSON name strings to the concrete
classes in ``polyhustle.strategies``. Adding a new strategy means
subclassing ``Strategy``, registering it here, and adding to
``STRATEGY.md``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from polyhustle.data.live import LiveDataProvider
from polyhustle.data.paper import PaperDataProvider
from polyhustle.data.replay import ReplayDataProvider
from polyhustle.data.replay_summary import (
    InMemoryEventLogger,
    aggregate_summary,
)
from polyhustle.execution.dryrun_trader import DryrunTrader
from polyhustle.execution.paper_trader import PaperTrader
from polyhustle.orchestrator import Orchestrator, StrategyAssignment
from polyhustle.strategies.base import BaseStrategy
from polyhustle.strategies.enhanced import EnhancedStrategy
from polyhustle.strategies.refined import RefinedStrategy
from polyhustle.strategies.walked_vwap import WalkedVWAPStrategy

logger = logging.getLogger("polyhustle.cli")


# Strategy registry — JSON name → concrete class. Adding a new strategy
# means subclassing Strategy in polyhustle/strategies/, registering it
# here, and adding to STRATEGY.md.
STRATEGY_REGISTRY: dict[str, type] = {
    "base": BaseStrategy,
    "enhanced": EnhancedStrategy,
    "refined": RefinedStrategy,
    "walked_vwap": WalkedVWAPStrategy,
}


@dataclass
class LaunchConfig:
    """Parsed launch config — see module docstring for the JSON schema."""

    mode: str = "main"
    main_strategy: str = "walked_vwap"
    benchmarks: list[str] = field(default_factory=lambda: ["refined", "enhanced", "base"])
    comparison_strategies: list[str] = field(default_factory=list)
    execution_mode: str = "paper"
    replay_session: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    tui_layout: str = "textual"

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> LaunchConfig:
        return cls(
            mode=raw.get("mode", "main"),
            main_strategy=raw.get("main_strategy", "walked_vwap"),
            benchmarks=list(raw.get("benchmarks", ["refined", "enhanced", "base"])),
            comparison_strategies=list(raw.get("comparison_strategies", [])),
            execution_mode=raw.get("execution_mode", "paper"),
            replay_session=raw.get("replay_session"),
            params=dict(raw.get("params", {})),
            tui_layout=raw.get("tui_layout", "textual"),
        )

    @classmethod
    def from_path(cls, path: str | Path) -> LaunchConfig:
        return cls.from_dict(json.loads(Path(path).read_text()))


def _build_strategy(name: str, max_risk: float, role: str):
    """Look up the class in the registry and instantiate with the right role."""
    cls = STRATEGY_REGISTRY.get(name)
    if cls is None:
        raise ValueError(
            f"unknown strategy {name!r}; known: {sorted(STRATEGY_REGISTRY)}"
        )
    return cls(max_risk=max_risk, role=role)


def _build_trader(execution_mode: str):
    """Construct a fresh Trader for the given execution mode.

    For comparison mode each trader-role assignment gets its OWN
    PaperTrader instance, so each strategy has an independent simulated
    wallet. Live wiring goes through the daemon's build_executor() so
    we don't duplicate the credential / VPN / autodetect logic.
    """
    if execution_mode == "paper":
        return PaperTrader()
    if execution_mode == "replay":
        # Replay always uses paper fills — there's nothing to post against.
        return PaperTrader()
    if execution_mode == "live_dryrun":
        return DryrunTrader()
    if execution_mode == "live_real":
        # Defer to daemon_base_v1's build_executor so we don't duplicate
        # credential / autodetect / fallback logic. It returns a tuple
        # (Executor, TokenResolver, RiskManager); we wrap the Executor
        # in a Trader.
        import daemon_base_v1 as _daemon
        from active_bots.execution.executor import Executor
        from polyhustle.execution._executor_trader import _ExecutorTrader
        executor: Executor
        executor, _resolver, _risk = _daemon.build_executor()
        return _ExecutorTrader(executor)
    raise ValueError(f"unknown execution_mode {execution_mode!r}")


def _build_assignments(cfg: LaunchConfig, max_risk: float) -> list[StrategyAssignment]:
    """Translate the LaunchConfig into a list of StrategyAssignment."""
    assignments: list[StrategyAssignment] = []

    if cfg.mode == "main":
        # The main strategy is the trader; benchmarks shadow.
        assignments.append(
            StrategyAssignment(
                name=cfg.main_strategy,
                strategy=_build_strategy(cfg.main_strategy, max_risk, role="trader"),
                trader=_build_trader(cfg.execution_mode),
                role="trader",
            )
        )
        for bench in cfg.benchmarks:
            assignments.append(
                StrategyAssignment(
                    name=bench,
                    strategy=_build_strategy(bench, max_risk, role="observer"),
                    trader=PaperTrader(),
                    role="observer",
                )
            )
    elif cfg.mode == "comparison":
        # Every comparison strategy is a trader with its own paper wallet.
        for s in cfg.comparison_strategies:
            assignments.append(
                StrategyAssignment(
                    name=s,
                    strategy=_build_strategy(s, max_risk, role="trader"),
                    trader=PaperTrader(),
                    role="trader",
                )
            )
    elif cfg.mode == "replay":
        # Replay mode wires the same single-trader-plus-benchmarks shape
        # as main mode, just with a ReplayDataProvider. Session C plumbs
        # the rest.
        assignments.append(
            StrategyAssignment(
                name=cfg.main_strategy,
                strategy=_build_strategy(cfg.main_strategy, max_risk, role="trader"),
                trader=PaperTrader(),  # replay always uses paper fills
                role="trader",
            )
        )
        for bench in cfg.benchmarks:
            assignments.append(
                StrategyAssignment(
                    name=bench,
                    strategy=_build_strategy(bench, max_risk, role="observer"),
                    trader=PaperTrader(),
                    role="observer",
                )
            )
    else:
        raise ValueError(f"unknown mode {cfg.mode!r}")

    return assignments


def _build_data_provider(cfg: LaunchConfig):
    """Pick a DataProvider based on execution_mode."""
    if cfg.execution_mode == "replay":
        if not cfg.replay_session:
            raise ValueError("execution_mode=replay requires replay_session")
        return ReplayDataProvider(cfg.replay_session)
    if cfg.execution_mode == "paper":
        return PaperDataProvider()
    return LiveDataProvider()


def _apply_params_to_env(params: dict[str, Any]) -> None:
    """Push numeric/string params into os.environ.

    The strategy classes read tuneables from env (TP_DELTA_MIN,
    WALKED_VWAP_EDGE_MIN, MAX_TRADE_SIZE_USDC, …); the daemon's
    launch_daemon.sh today exports them ahead of `python daemon_base_v1.py`.
    The CLI mirrors that contract by exporting every params entry,
    so the same JSON config can drive either entry point.
    """
    for k, v in params.items():
        os.environ[k.upper()] = str(v)


async def run_async(cfg: LaunchConfig) -> int:
    """Build the Orchestrator from the config and run it. Returns exit code."""
    _apply_params_to_env(cfg.params)

    # Reuse the daemon's risk + event-logger setup so we get the same
    # state-dir layout, kill-switch path, and per-trade size resolution.
    import daemon_base_v1 as _daemon
    from active_bots.execution.event_logger import EventLogger

    _daemon.setup_logging()
    _daemon.STATE_DIR.mkdir(parents=True, exist_ok=True)

    is_replay = cfg.execution_mode == "replay"
    events: Any
    if is_replay:
        events = InMemoryEventLogger()
    else:
        events = EventLogger(_daemon.EVENTS_FILE)

    effective_max, source = _daemon.compute_effective_max_risk()
    risk_cfg = _daemon.RiskConfig(
        max_daily_loss_usdc=float(cfg.params.get("max_daily_loss_usdc", 300.0)),
        max_session_loss_usdc=float(cfg.params.get("max_session_loss_usdc", 30.0)),
        max_trade_size_usdc=effective_max,
        min_trade_size_usdc=float(cfg.params.get("min_trade_size_usdc", 10.0)),
        kill_switch_file=Path(os.environ.get("KILL_SWITCH_FILE", "daemon_state/KILL")),
    )
    risk = _daemon.RiskManager(risk_cfg)

    logger.info(
        "polyhustle.cli mode=%s execution_mode=%s main=%s "
        "benchmarks=%s max_trade_size=$%.2f (%s)",
        cfg.mode, cfg.execution_mode, cfg.main_strategy,
        cfg.benchmarks, effective_max, source,
    )

    provider = _build_data_provider(cfg)
    assignments = _build_assignments(cfg, max_risk=effective_max)
    orch = Orchestrator(
        data_provider=provider,
        assignments=assignments,
        risk=risk,
        events=events,
        replay_mode=is_replay,
    )

    t0 = time.time()
    try:
        await orch.run()
    finally:
        events.close()
    runtime = time.time() - t0

    if is_replay:
        summary = aggregate_summary(events.events)
        summary["runtime_seconds"] = round(runtime, 3)
        summary["tick_count"] = orch.tick_count
        if isinstance(provider, ReplayDataProvider):
            summary["session_id"] = provider.session_id
        out_path = (
            Path(cfg.replay_session) / "replay_summary.json"
            if cfg.replay_session and Path(cfg.replay_session).is_dir()
            else Path("replay_summary.json")
        )
        out_path.write_text(json.dumps(summary, indent=2))
        logger.info("Replay summary written to %s", out_path)

    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Parse args, load config, run."""
    parser = argparse.ArgumentParser(prog="polyhustle.cli")
    parser.add_argument(
        "--config",
        default=os.environ.get("POLYHUSTLE_CONFIG"),
        help="Path to launch config JSON (or POLYHUSTLE_CONFIG env).",
    )
    args = parser.parse_args(argv)
    if not args.config:
        parser.error("--config is required (or set POLYHUSTLE_CONFIG)")

    cfg = LaunchConfig.from_path(args.config)
    return asyncio.run(run_async(cfg))


if __name__ == "__main__":
    sys.exit(main())
