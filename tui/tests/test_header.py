"""Tests for HeaderWidget.render_state — covers None-state and populated branches."""
from pathlib import Path
from unittest.mock import MagicMock

import dashboard as d


def test_header_none_state_shows_waiting():
    widget = d.HeaderWidget.__new__(d.HeaderWidget)
    widget.update = MagicMock()
    widget.render_state(None)
    widget.update.assert_called_once()
    arg = widget.update.call_args.args[0]
    assert "waiting" in str(arg)


def test_header_populated_state_handles_null_btc_price(monkeypatch, tmp_path):
    """The daemon may write btc_price: null on cold start. Must not crash."""
    monkeypatch.setattr(d, "KILL_FILE", tmp_path / "KILL")  # never exists
    widget = d.HeaderWidget.__new__(d.HeaderWidget)
    widget.update = MagicMock()
    widget.render_state({
        "btc_price": None,           # the bug case
        "sigma": None,
        "strike": None,
        "slug": "btc-updown-5m-x",
        "t_zero": 0,
        "connections": {"binance": True, "rtds": False},
    })
    widget.update.assert_called_once()
    rendered = str(widget.update.call_args.args[0])
    assert "BTC $" in rendered
    assert "btc-updown-5m-x" in rendered


def test_header_kill_pill_active(monkeypatch, tmp_path):
    kill = tmp_path / "KILL"
    kill.write_text("")  # exists in our fake state dir
    monkeypatch.setattr(d, "KILL_FILE", kill)
    widget = d.HeaderWidget.__new__(d.HeaderWidget)
    widget.update = MagicMock()
    widget.render_state({"btc_price": 50000, "t_zero": 0,
                         "slug": "x", "connections": {}})
    rendered = str(widget.update.call_args.args[0])
    assert "KILL" in rendered
