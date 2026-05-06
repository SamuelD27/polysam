"""Tests for ``polyhustle.cli``.

LaunchConfig parsing + assignment construction. The full ``run_async``
path opens real websockets (LiveDataProvider) so we don't drive it
end-to-end here — the smoke test in the verification step covers the
import-and-construct path; here we cover the deterministic logic.
"""

from __future__ import annotations

from polyhustle.cli import (
    STRATEGY_REGISTRY,
    LaunchConfig,
    _build_assignments,
    _build_data_provider,
    _build_trader,
)
from polyhustle.data.live import LiveDataProvider
from polyhustle.execution.dryrun_trader import DryrunTrader
from polyhustle.execution.paper_trader import PaperTrader


def test_strategy_registry_covers_four_classes():
    assert set(STRATEGY_REGISTRY) == {"base", "enhanced", "refined", "walked_vwap"}


def test_launch_config_from_dict_round_trips():
    raw = {
        "mode": "comparison",
        "comparison_strategies": ["refined", "walked_vwap"],
        "execution_mode": "paper",
        "params": {"max_trade_size_usdc": 5.0},
    }
    cfg = LaunchConfig.from_dict(raw)
    assert cfg.mode == "comparison"
    assert cfg.comparison_strategies == ["refined", "walked_vwap"]
    assert cfg.execution_mode == "paper"
    assert cfg.params == {"max_trade_size_usdc": 5.0}


def test_main_mode_builds_one_trader_plus_observers():
    cfg = LaunchConfig.from_dict({
        "mode": "main",
        "main_strategy": "walked_vwap",
        "benchmarks": ["refined", "enhanced", "base"],
        "execution_mode": "paper",
    })
    assignments = _build_assignments(cfg, max_risk=10.0)
    by_role = {a.name: a.role for a in assignments}
    assert by_role == {
        "walked_vwap": "trader",
        "refined": "observer",
        "enhanced": "observer",
        "base": "observer",
    }


def test_comparison_mode_makes_every_strategy_a_trader_with_own_wallet():
    cfg = LaunchConfig.from_dict({
        "mode": "comparison",
        "comparison_strategies": ["refined", "walked_vwap"],
        "execution_mode": "paper",
    })
    assignments = _build_assignments(cfg, max_risk=10.0)
    assert all(a.role == "trader" for a in assignments)
    # Each trader is its own PaperTrader instance — wallet isolation by construction.
    trader_ids = {id(a.trader) for a in assignments}
    assert len(trader_ids) == len(assignments)


def test_build_trader_paper_returns_paper_trader():
    assert isinstance(_build_trader("paper"), PaperTrader)


def test_build_trader_live_dryrun_returns_dryrun_trader():
    assert isinstance(_build_trader("live_dryrun"), DryrunTrader)


def test_build_data_provider_paper_uses_live_websockets():
    cfg = LaunchConfig.from_dict({"mode": "main", "execution_mode": "paper"})
    p = _build_data_provider(cfg)
    # PaperDataProvider is an alias of LiveDataProvider — same data path.
    assert isinstance(p, LiveDataProvider)
