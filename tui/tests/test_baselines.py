"""Tests for BaselinesWidget + OrdersLogWidget."""
from collections import deque
from unittest.mock import MagicMock

import dashboard as d


def _mk_baselines():
    w = d.BaselinesWidget.__new__(d.BaselinesWidget)
    w.update = MagicMock()
    return w


def test_baselines_none_state_shows_waiting():
    w = _mk_baselines()
    w.render_state(None)
    arg = str(w.update.call_args.args[0])
    assert "waiting" in arg


def test_baselines_renders_three_strategies():
    w = _mk_baselines()
    w.render_state({
        "base": {"stats": {"total_trades": 4, "wins": 2, "losses": 2,
                           "total_pnl": 1.5, "total_risked": 20.0,
                           "max_drawdown": 0.3}},
        "enhanced": {"stats": {"total_trades": 6, "wins": 5, "losses": 1,
                               "total_pnl": 3.2, "total_risked": 25.0,
                               "max_drawdown": 0.1}},
        "refined": {"stats": {"total_trades": 3, "wins": 2, "losses": 1,
                              "total_pnl": 0.85, "total_risked": 12.0,
                              "max_drawdown": 0.2}},
    })
    rendered = str(w.update.call_args.args[0])
    assert "BASE" in rendered
    assert "ENHANCED" in rendered
    assert "REFINED" in rendered
    assert "+1.50" in rendered
    assert "+3.20" in rendered
    assert "+0.85" in rendered


def test_baselines_handles_missing_strategies():
    w = _mk_baselines()
    w.render_state({})  # none of base / enhanced / refined present
    rendered = str(w.update.call_args.args[0])
    assert "BASE" in rendered
    assert "ENHANCED" in rendered
    assert "REFINED" in rendered
    assert "+0.00" in rendered  # zero pnl when stats blob missing
