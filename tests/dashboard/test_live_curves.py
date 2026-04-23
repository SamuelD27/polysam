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
        "refined": {"fair_price": 0.62},
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
                    "refined": {"fair_price": 0.6}})
    w.render_state({"t_zero": 1000, "btc_price": 50001.0,
                    "market_price_up": 0.51,
                    "refined": {"fair_price": 0.61}})
    assert len(w._btc) == 2
    # Market rollover -- t_zero changes
    w.render_state({"t_zero": 2000, "btc_price": 50002.0,
                    "market_price_up": 0.52,
                    "refined": {"fair_price": 0.62}})
    assert len(w._btc) == 1, "deques must reset on rollover"
    assert len(w._mkt) == 1
    assert len(w._fair) == 1


def test_fair_line_colour_when_fair_above_mkt():
    w = _mk_widget()
    plot_mock = MagicMock()
    w.query_one = MagicMock(return_value=plot_mock)
    w._mkt.append((1.0, 0.50))
    w._fair.append((1.0, 0.70))
    w._render_mkt_chart()
    # plt.plot called twice (mkt then fair); fair line should be green
    plot_calls = plot_mock.plt.plot.call_args_list
    assert len(plot_calls) == 2
    assert plot_calls[1].kwargs.get("color") == "green"


def test_fair_line_colour_when_fair_below_mkt():
    w = _mk_widget()
    plot_mock = MagicMock()
    w.query_one = MagicMock(return_value=plot_mock)
    w._mkt.append((1.0, 0.70))
    w._fair.append((1.0, 0.50))
    w._render_mkt_chart()
    assert w.query_one.return_value.plt.plot.call_args_list[1].kwargs.get("color") == "red"
