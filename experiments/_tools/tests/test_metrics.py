"""Tests for metrics parsing."""
import json
from pathlib import Path

from experiments._tools.metrics import parse_metrics


def _write_state(dir: Path, strategy_stats: dict) -> None:
    (dir / "state.json").write_text(json.dumps({
        "enhanced": {
            "stats": strategy_stats,
            "extra": {},
            "closed_trades": [],
            "open_position": None,
            "fair_price": None,
        },
        "base": {"stats": {}, "closed_trades": [], "open_position": None, "fair_price": None},
    }))


def _write_events(dir: Path, events: list[dict]) -> None:
    (dir / "events.jsonl").write_text("\n".join(json.dumps(e) for e in events))


def test_no_state_file_is_crashed(tmp_path):
    m = parse_metrics(tmp_path, strategy="enhanced")
    assert m["crashed"] is True


def test_empty_stats(tmp_path):
    _write_state(tmp_path, {"total_pnl": 0, "total_risked": 0, "total_trades": 0,
                             "wins": 0, "losses": 0, "max_drawdown": 0})
    m = parse_metrics(tmp_path, strategy="enhanced")
    assert m["crashed"] is False
    assert m["roi"] == 0.0
    assert m["trade_count"] == 0
    assert m["win_rate"] == 0.0
    assert m["composite"] == 0.0


def test_roi_and_composite(tmp_path):
    _write_state(tmp_path, {"total_pnl": 20.0, "total_risked": 100.0,
                             "total_trades": 10, "wins": 7, "losses": 3,
                             "max_drawdown": 5.0})
    m = parse_metrics(tmp_path, strategy="enhanced")
    assert m["roi"] == 0.2
    assert m["trade_count"] == 10
    assert m["win_rate"] == 0.7
    assert m["max_dd_pct"] == 0.05
    # composite = 0.2 - 0.5*0.05 + 0.2*0.7 = 0.315
    assert abs(m["composite"] - 0.315) < 1e-9


def test_per_exit_and_source_counts(tmp_path):
    _write_state(tmp_path, {"total_pnl": 1, "total_risked": 1, "total_trades": 3,
                             "wins": 2, "losses": 1, "max_drawdown": 0})
    _write_events(tmp_path, [
        {"strategy": "enhanced", "type": "entry_filled", "source": "edge"},
        {"strategy": "enhanced", "type": "entry_filled", "source": "squeeze"},
        {"strategy": "enhanced", "type": "entry_filled", "source": "edge"},
        {"strategy": "enhanced", "type": "exit_filled", "trade": {"exit_type": "TP"}},
        {"strategy": "enhanced", "type": "exit_filled", "trade": {"exit_type": "SL"}},
        {"strategy": "enhanced", "type": "resolve", "trade": {}},
        {"strategy": "base", "type": "entry_filled", "source": "base"},
    ])
    m = parse_metrics(tmp_path, strategy="enhanced")
    assert m["per_exit_type"] == {"TP": 1, "SL": 1, "RESOLUTION": 1}
    assert m["per_source"] == {"edge": 2, "squeeze": 1}


def test_malformed_event_lines_are_skipped(tmp_path):
    _write_state(tmp_path, {"total_pnl": 0, "total_risked": 0, "total_trades": 0,
                             "wins": 0, "losses": 0, "max_drawdown": 0})
    (tmp_path / "events.jsonl").write_text('not json\n{"strategy":"enhanced","type":"resolve"}\n')
    m = parse_metrics(tmp_path, strategy="enhanced")
    assert m["per_exit_type"]["RESOLUTION"] == 1
