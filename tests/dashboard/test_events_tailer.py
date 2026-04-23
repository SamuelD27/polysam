"""Byte-offset tailer for events.jsonl with rotation handling."""
import json
from pathlib import Path

import dashboard as d


def _write(path: Path, events: list[dict]) -> None:
    with path.open("w") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")


def _append(path: Path, events: list[dict]) -> None:
    with path.open("a") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")


def test_tailer_picks_up_initial_events(tmp_path):
    p = tmp_path / "events.jsonl"
    _write(p, [
        {"ts": 1, "type": "entry_filled", "strategy": "refined",
         "position": {"side": "Up", "entry_price": 0.5, "size_usdc": 10}},
    ])
    t = d.EventsTailer(p)
    t.update()
    assert len(t.refined_actions) == 1
    assert len(t.unified_actions) == 1


def test_tailer_picks_up_appended_events(tmp_path):
    p = tmp_path / "events.jsonl"
    _write(p, [
        {"ts": 1, "type": "entry_filled", "strategy": "refined",
         "position": {"side": "Up", "entry_price": 0.5, "size_usdc": 10}},
    ])
    t = d.EventsTailer(p)
    t.update()
    _append(p, [
        {"ts": 2, "type": "exit_filled", "strategy": "refined",
         "trade": {"side": "Up", "exit_price": 0.6, "size_usdc": 10,
                   "pnl": 1.0, "exit_type": "TP"}},
    ])
    t.update()
    assert len(t.refined_actions) == 2


def test_tailer_idempotent_when_no_new_events(tmp_path):
    p = tmp_path / "events.jsonl"
    _write(p, [
        {"ts": 1, "type": "exit_filled", "strategy": "refined",
         "trade": {"pnl": 1.0}},
    ])
    t = d.EventsTailer(p)
    t.update()
    t.update()
    t.update()
    assert len(t.pnl_series["refined"]) == 1


def test_tailer_resets_on_truncation(tmp_path):
    p = tmp_path / "events.jsonl"
    _write(p, [
        {"ts": 1, "type": "exit_filled", "strategy": "refined",
         "trade": {"pnl": 1.0}},
    ])
    t = d.EventsTailer(p)
    t.update()
    assert len(t.pnl_series["refined"]) == 1

    # truncate (simulate the daemon rotating events.jsonl)
    p.write_text("")
    t.update()
    assert t._pos == 0
    assert t.pnl_series == {}, "PnL series must clear on truncation"


def test_tailer_handles_missing_file(tmp_path):
    t = d.EventsTailer(tmp_path / "nope.jsonl")
    t.update()
    assert len(t.events) == 0
    assert t.pnl_series == {}


def test_tailer_skips_malformed_lines(tmp_path):
    p = tmp_path / "events.jsonl"
    p.write_text(
        '{"ts": 1, "type": "exit_filled", "strategy": "refined", "trade": {"pnl": 0.5}}\n'
        "not-json-line\n"
        '{"ts": 2, "type": "exit_filled", "strategy": "refined", "trade": {"pnl": 0.5}}\n'
    )
    t = d.EventsTailer(p)
    t.update()
    assert len(t.pnl_series["refined"]) == 2
