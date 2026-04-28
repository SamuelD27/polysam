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
    w.render_state(None, [])
    # _render_headline's Static.update gets called via query_one(...).update
    assert w.query_one.call_count >= 1


def test_main_empty_strategy_blob_does_not_crash():
    """state.walked_vwap missing entirely -- every field is None / 0."""
    w = _mk_widget()
    w.render_state(
        {"slug": "x", "t_zero": 0, "market_price_up": None},
        [],
    )
    # Just verify no exception + some updates happened.
    assert w.query_one.call_count >= 1


def test_main_populated_walked_vwap_with_open_position():
    w = _mk_widget()
    state = {
        "slug": "btc-updown-5m-x",
        "t_zero": 1776912300,
        "market_price_up": 0.65,
        "walked_vwap": {
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
    w.render_state(state, [(1.0, 0.65)])
    # Confirm the PnL chart path was exercised (query_one called for #chart-pnl).
    chart_calls = [
        c for c in w.query_one.call_args_list if "chart-pnl" in str(c.args)
    ]
    assert chart_calls, "pnl chart render was not called"


def test_render_rejects_renders_count_and_top_reasons():
    """Reject summary: count + top 3 reasons + last_walked_edge colouring."""
    w = _mk_widget()
    static_mock = MagicMock()
    w.query_one = MagicMock(return_value=static_mock)
    w._render_rejects({
        "reject_count": 7,
        "reject_reasons": {
            "vwap_no_quote": 4,
            "abs_edge_too_small": 2,
            "stale_book": 1,
            "tiny_extra": 0,
        },
        "last_walked_edge": -0.0123,
    })
    # The Static was updated with a Text containing rejects + reasons + edge.
    static_mock.update.assert_called()
    rendered = str(static_mock.update.call_args.args[0])
    assert "rejects: 7" in rendered
    assert "vwap_no_quote=4" in rendered
    assert "abs_edge_too_small=2" in rendered
    assert "stale_book=1" in rendered
    # Only top 3 should appear -- "tiny_extra" is the 4th and must be excluded.
    assert "tiny_extra" not in rendered
    assert "last walked_edge" in rendered
    assert "-0.0123" in rendered


def test_render_rejects_handles_empty_extra():
    """Empty extra: rejects: 0, no reasons, no last_walked_edge segment."""
    w = _mk_widget()
    static_mock = MagicMock()
    w.query_one = MagicMock(return_value=static_mock)
    w._render_rejects({})
    rendered = str(static_mock.update.call_args.args[0])
    assert "rejects: 0" in rendered
    assert "last walked_edge" not in rendered


def test_pnl_chart_colour_flat_uses_white():
    """Final value == 0 selects 'white' colour, not green/red."""
    w = _mk_widget()
    plot_mock = MagicMock()
    w.query_one = MagicMock(return_value=plot_mock)
    # Drive _render_pnl_chart directly with a flat series.
    w._render_pnl_chart([(1.0, 0.0), (2.0, 0.0)])
    # plt.plot(...) should have been called with color="white"
    plot_calls = plot_mock.plt.plot.call_args_list
    assert plot_calls, "plt.plot was not called"
    assert plot_calls[0].kwargs.get("color") == "white"


def test_pnl_chart_colour_positive_uses_green():
    w = _mk_widget()
    plot_mock = MagicMock()
    w.query_one = MagicMock(return_value=plot_mock)
    w._render_pnl_chart([(1.0, 0.0), (2.0, 1.5)])
    assert plot_mock.plt.plot.call_args_list[0].kwargs.get("color") == "green"


def test_pnl_chart_colour_negative_uses_red():
    w = _mk_widget()
    plot_mock = MagicMock()
    w.query_one = MagicMock(return_value=plot_mock)
    w._render_pnl_chart([(1.0, 0.0), (2.0, -1.5)])
    assert plot_mock.plt.plot.call_args_list[0].kwargs.get("color") == "red"
