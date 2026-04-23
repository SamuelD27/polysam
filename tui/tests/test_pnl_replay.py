"""Per-strategy persistent cumulative PnL series."""
import json
from pathlib import Path

import dashboard as d


def _write(path: Path, events: list[dict]) -> None:
    with path.open("w") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")


def test_pnl_series_sums_per_strategy(tmp_path):
    p = tmp_path / "events.jsonl"
    _write(p, [
        {"ts": 1, "type": "exit_filled", "strategy": "refined",
         "trade": {"pnl": 1.5}},
        {"ts": 2, "type": "exit_filled", "strategy": "base",
         "trade": {"pnl": -0.25}},
        {"ts": 3, "type": "resolve", "strategy": "refined",
         "trade": {"pnl": 0.5}},
    ])
    t = d.EventsTailer(p)
    t.update()
    refined = list(t.pnl_series["refined"])
    base = list(t.pnl_series["base"])
    assert [v for _, v in refined] == [1.5, 2.0]
    assert [v for _, v in base] == [-0.25]


def test_pnl_series_ignores_non_pnl_events(tmp_path):
    p = tmp_path / "events.jsonl"
    _write(p, [
        {"ts": 1, "type": "market_rollover", "slug": "x"},
        {"ts": 2, "type": "entry_filled", "strategy": "refined",
         "position": {"side": "Up", "entry_price": 0.5, "size_usdc": 10}},
        {"ts": 3, "type": "session_start", "pid": 1},
    ])
    t = d.EventsTailer(p)
    t.update()
    assert t.pnl_series == {}


def test_pnl_series_decimates_over_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(d, "PNL_SERIES_CAP", 4)
    evs = [
        {"ts": float(i), "type": "exit_filled", "strategy": "refined",
         "trade": {"pnl": 0.1}}
        for i in range(10)
    ]
    p = tmp_path / "events.jsonl"
    _write(p, evs)
    t = d.EventsTailer(p)
    t.update()
    series = list(t.pnl_series["refined"])
    assert len(series) <= 4
    assert series[0][0] == 0.0, "first point must be preserved"
    assert series[-1][0] == 9.0, "last point (running tail) must be preserved"


def test_pnl_series_handles_missing_pnl_field(tmp_path):
    p = tmp_path / "events.jsonl"
    _write(p, [
        {"ts": 1, "type": "exit_filled", "strategy": "refined",
         "trade": {}},  # no pnl field
        {"ts": 2, "type": "resolve", "strategy": "refined"},  # no trade at all
    ])
    t = d.EventsTailer(p)
    t.update()
    # Both events accumulate 0.0 pnl, so series exists with two zero points
    assert list(t.pnl_series["refined"]) == [(1.0, 0.0), (2.0, 0.0)]
