import dashboard as d


def test_compute_stats_empty():
    s = d._compute_stats({})
    assert s["total"] == 0
    assert s["wr"] == 0
    assert s["roi"] == 0


def test_compute_stats_winrate():
    s = d._compute_stats({
        "total_trades": 4, "wins": 3, "losses": 1,
        "total_pnl": 2.0, "total_risked": 20.0,
    })
    assert s["wr"] == 75.0
    assert s["roi"] == 10.0


def test_action_from_event_entry():
    ev = {
        "ts": 1.0, "type": "entry_filled", "strategy": "refined",
        "position": {"side": "Up", "entry_price": 0.42, "size_usdc": 5.0},
    }
    a = d._action_from_event(ev)
    assert a["kind"] == "BUY"
    assert a["side"] == "Up"
    assert a["price"] == 0.42
    assert a["strategy"] == "refined"


def test_action_from_event_exit():
    ev = {
        "ts": 2.0, "type": "exit_filled", "strategy": "refined",
        "trade": {"side": "Up", "exit_price": 0.6, "size_usdc": 5.0,
                  "pnl": 0.9, "exit_type": "TP"},
    }
    a = d._action_from_event(ev)
    assert a["kind"] == "SELL"
    assert a["pnl"] == 0.9
    assert a["exit_type"] == "TP"


def test_action_from_event_ignored_type():
    assert d._action_from_event({"type": "market_rollover"}) is None


def test_pnl_color_signs():
    assert d.pnl_color(1.0).startswith("bold green")
    assert d.pnl_color(-1.0).startswith("bold red")
    assert d.pnl_color(0) == "white"


def test_fmt_secs():
    assert d.fmt_secs(None) == "-"
    assert d.fmt_secs(0) == "0:00"
    assert d.fmt_secs(75) == "1:15"


def test_strip_emoji_passes_box_drawing():
    assert d._strip_emoji("|---|") == "|---|"
    assert d._strip_emoji("│─┼") == "│─┼"


def test_strip_emoji_strips_high_unicode():
    out = d._strip_emoji("rocket \U0001f680 here")
    assert "\U0001f680" not in out


def test_read_json_missing_file(tmp_path):
    assert d.read_json(tmp_path / "nope.json") is None


def test_read_json_valid(tmp_path):
    p = tmp_path / "a.json"
    p.write_text('{"x": 1}')
    assert d.read_json(p) == {"x": 1}


def test_read_json_invalid(tmp_path):
    p = tmp_path / "a.json"
    p.write_text("not json")
    assert d.read_json(p) is None


def test_tail_lines_last_n(tmp_path):
    p = tmp_path / "log.txt"
    p.write_text("\n".join(f"line {i}" for i in range(20)) + "\n")
    out = d.tail_lines(p, n=3)
    assert out == ["line 17", "line 18", "line 19"]


def test_tail_lines_missing_file(tmp_path):
    assert d.tail_lines(tmp_path / "nope.txt", n=5) == []
