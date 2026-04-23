"""Tests for MainStrategyWidget.render_state -- pure branches, no Textual harness."""
from unittest.mock import MagicMock

import dashboard as d


def _mk_widget():
    """Bypass Textual widget init; stub the subwidget lookup."""
    w = d.MainStrategyWidget.__new__(d.MainStrategyWidget)
    # _render_pnl_chart reaches for a PlotextPlot; stub query_one to return
    # a permissive mock that allows any attribute access.
    w.query_one = MagicMock(return_value=MagicMock())
    return w


def test_main_none_state_shows_waiting():
    w = _mk_widget()
    w.render_state(None, [], [])
    # _render_headline's Static.update gets called via query_one(...).update
    assert w.query_one.call_count >= 1


def test_main_empty_strategy_blob_does_not_crash():
    """state.refined missing entirely -- every field is None / 0."""
    w = _mk_widget()
    w.render_state(
        {"slug": "x", "t_zero": 0, "market_price_up": None},
        [], [],
    )
    # Just verify no exception + some updates happened.
    assert w.query_one.call_count >= 1


def test_main_populated_refined_with_open_position():
    w = _mk_widget()
    state = {
        "slug": "btc-updown-5m-x",
        "t_zero": 1776912300,
        "market_price_up": 0.65,
        "refined": {
            "fair_price": 0.72,
            "open_position": {
                "side": "Up", "entry_price": 0.60, "size_usdc": 10.0,
                "edge": 0.12,
            },
            "closed_trades": [
                {"resolved_time": 1776912000, "side": "Up",
                 "entry_price": 0.55, "exit_price": 0.68, "pnl": 0.65,
                 "size_usdc": 5.0, "hold_time_s": 42, "exit_type": "TP"},
            ],
            "stats": {"total_trades": 1, "wins": 1, "losses": 0,
                      "total_pnl": 0.65, "total_risked": 5.0,
                      "max_drawdown": 0.0, "current_streak": 1,
                      "streak_type": "W"},
            "extra": {"tp_count": 1, "sl_count": 0, "resolution_count": 0},
        },
    }
    w.render_state(state, [], [(1.0, 0.65)])
    # Confirm the PnL chart path was exercised (query_one called for #chart-pnl).
    chart_calls = [
        c for c in w.query_one.call_args_list if "chart-pnl" in str(c.args)
    ]
    assert chart_calls, "pnl chart render was not called"
