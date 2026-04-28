"""Tests for LiveCurvesWidget -- deque accumulation + rollover reset."""
from collections import deque
from unittest.mock import MagicMock

import dashboard as d


def _mk_widget():
    """Bypass Textual init; pre-populate the rolling deques."""
    w = d.LiveCurvesWidget.__new__(d.LiveCurvesWidget)
    w._btc = deque(maxlen=d.PRICE_HISTORY_CAP)
    w._mkt = deque(maxlen=d.PRICE_HISTORY_CAP)
    w._fair = deque(maxlen=d.PRICE_HISTORY_CAP)
    w._last_t_zero = None
    w.query_one = MagicMock(return_value=MagicMock())
    return w


def test_live_curves_none_state_shows_waiting():
    w = _mk_widget()
    w.render_state(None)
    # query_one("#hero-prices", Static) called for the waiting message
    assert w.query_one.call_count >= 1


def test_live_curves_appends_to_deques():
    w = _mk_widget()
    w.render_state({
        "t_zero": 1000,
        "btc_price": 50000.0,
        "market_price_up": 0.55,
        "walked_vwap": {"fair_price": 0.62},
    })
    assert len(w._btc) == 1 and w._btc[0][1] == 50000.0
    assert len(w._mkt) == 1 and w._mkt[0][1] == 0.55
    assert len(w._fair) == 1 and w._fair[0][1] == 0.62


def test_live_curves_skips_invalid_btc():
    w = _mk_widget()
    w.render_state({"t_zero": 1000, "btc_price": None,
                    "market_price_up": 0.5})
    assert len(w._btc) == 0
    assert len(w._mkt) == 1


def test_live_curves_skips_zero_btc():
    """Cold-start daemon writes btc_price: 0.0 before first feed tick."""
    w = _mk_widget()
    w.render_state({"t_zero": 1000, "btc_price": 0.0})
    assert len(w._btc) == 0


def test_live_curves_clears_on_rollover():
    w = _mk_widget()
    w.render_state({"t_zero": 1000, "btc_price": 50000.0,
                    "market_price_up": 0.5,
                    "walked_vwap": {"fair_price": 0.6}})
    w.render_state({"t_zero": 1000, "btc_price": 50001.0,
                    "market_price_up": 0.51,
                    "walked_vwap": {"fair_price": 0.61}})
    assert len(w._btc) == 2
    # Market rollover -- t_zero changes
    w.render_state({"t_zero": 2000, "btc_price": 50002.0,
                    "market_price_up": 0.52,
                    "walked_vwap": {"fair_price": 0.62}})
    assert len(w._btc) == 1, "deques must reset on rollover"
    assert len(w._mkt) == 1
    assert len(w._fair) == 1


def test_fair_line_renders_yellow():
    """Fair line is always yellow regardless of fair-vs-mkt sign."""
    w = _mk_widget()
    plot_mock = MagicMock()
    w.query_one = MagicMock(return_value=plot_mock)
    w._mkt.append((1.0, 0.50))
    w._fair.append((1.0, 0.70))
    w._render_mkt_chart()
    plot_calls = plot_mock.plt.plot.call_args_list
    fair_call = next(c for c in plot_calls
                     if c.kwargs.get("label") == "fair")
    assert fair_call.kwargs.get("color") == "yellow"


def test_fair_waiting_in_title_when_no_data():
    """When _fair is empty, the chart title surfaces 'fair: waiting'
    rather than silently dropping the series."""
    w = _mk_widget()
    plot_mock = MagicMock()
    w.query_one = MagicMock(return_value=plot_mock)
    w._mkt.append((1.0, 0.50))
    # _fair deliberately empty
    w._render_mkt_chart()
    title_calls = [str(c.args[0]) for c in plot_mock.plt.title.call_args_list]
    assert any("fair: waiting" in t for t in title_calls), title_calls
