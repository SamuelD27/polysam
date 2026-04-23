"""Enforce that the dashboard process never writes to daemon_state/."""
import os
import pathlib
import tempfile
from unittest.mock import patch

import dashboard as d


# Paths that are legitimate for the dashboard to open read-only.
READ_MODES = ("r", "rb")


def _guard_open(real_open, state_dir: pathlib.Path):
    """Return a patched open() that asserts read-only access under state_dir."""

    def guarded(file, mode="r", *args, **kwargs):
        try:
            path = pathlib.Path(file).resolve()
        except (TypeError, ValueError):
            path = None
        if path is not None and state_dir.resolve() in path.parents:
            assert mode in READ_MODES, (
                f"non-read open on {path} mode={mode!r}"
            )
        return real_open(file, mode, *args, **kwargs)

    return guarded


def test_read_json_is_read_only(tmp_path, monkeypatch):
    """read_json must open state.json in read mode only."""
    state_dir = tmp_path / "daemon_state"
    state_dir.mkdir()
    state_file = state_dir / "state.json"
    state_file.write_text('{"btc_price": 50000}')

    monkeypatch.setattr(d, "STATE_DIR", state_dir)
    monkeypatch.setattr(d, "STATE_FILE", state_file)

    real_open = open
    with patch("builtins.open", side_effect=_guard_open(real_open, state_dir)):
        assert d.read_json(state_file) == {"btc_price": 50000}


def test_events_tailer_is_read_only(tmp_path, monkeypatch):
    """EventsTailer.update must only open events.jsonl for reading."""
    state_dir = tmp_path / "daemon_state"
    state_dir.mkdir()
    events_file = state_dir / "events.jsonl"
    events_file.write_text(
        '{"ts": 1, "type": "exit_filled", "strategy": "refined",'
        ' "trade": {"pnl": 1.5}}\n'
    )

    monkeypatch.setattr(d, "STATE_DIR", state_dir)
    monkeypatch.setattr(d, "EVENTS_FILE", events_file)

    tailer = d.EventsTailer(events_file)
    real_open = open
    with patch("builtins.open", side_effect=_guard_open(real_open, state_dir)):
        tailer.update()
    assert "refined" in tailer.pnl_series


def test_tail_lines_is_read_only(tmp_path, monkeypatch):
    state_dir = tmp_path / "daemon_state"
    state_dir.mkdir()
    log_file = state_dir / "daemon.log"
    log_file.write_text("line 1\nline 2\nline 3\n")
    monkeypatch.setattr(d, "LOG_FILE", log_file)

    real_open = open
    with patch("builtins.open", side_effect=_guard_open(real_open, state_dir)):
        assert d.tail_lines(log_file, n=2) == ["line 2", "line 3"]


def test_no_files_created_in_daemon_state_during_full_tick_cycle(tmp_path, monkeypatch):
    """Simulate a few tick cycles and verify the state directory listing
    is unchanged -- no caches, pidfiles, KILL file, nothing."""
    state_dir = tmp_path / "daemon_state"
    state_dir.mkdir()
    (state_dir / "state.json").write_text('{"btc_price": 50000, "t_zero": 0}')
    (state_dir / "events.jsonl").write_text("")
    (state_dir / "daemon.log").write_text("")

    monkeypatch.setattr(d, "STATE_DIR", state_dir)
    monkeypatch.setattr(d, "STATE_FILE", state_dir / "state.json")
    monkeypatch.setattr(d, "EVENTS_FILE", state_dir / "events.jsonl")
    monkeypatch.setattr(d, "LOG_FILE", state_dir / "daemon.log")
    monkeypatch.setattr(d, "KILL_FILE", state_dir / "KILL")

    before = sorted(p.name for p in state_dir.iterdir())

    # Exercise the read paths that a tick would call.
    d.read_json(d.STATE_FILE)
    d.tail_lines(d.LOG_FILE, n=5)
    tailer = d.EventsTailer(d.EVENTS_FILE)
    tailer.update()
    # KILL_FILE existence check
    assert not d.KILL_FILE.exists()

    after = sorted(p.name for p in state_dir.iterdir())
    assert before == after, (
        f"daemon_state/ contents changed during tick: {set(after) - set(before)}"
    )


def test_kill_file_is_only_stat_checked():
    """A scan of dashboard.py source text confirms KILL_FILE is only used
    via .exists() -- never created, written, removed."""
    src = pathlib.Path(d.__file__).read_text()
    # Permit exactly: KILL_FILE.exists()
    # Forbid: KILL_FILE.touch(), KILL_FILE.write*, KILL_FILE.unlink, open(KILL_FILE...
    for forbidden in (".touch(", ".write_text", ".write_bytes",
                      ".unlink(", ".write("):
        assert f"KILL_FILE{forbidden}" not in src, (
            f"KILL_FILE must not be mutated: found {forbidden}"
        )


def test_production_module_has_no_forbidden_file_ops():
    """Scan dashboard.py source for any file-write calls at module scope
    that operate on daemon_state paths. This is a belt-and-suspenders
    sibling to the per-function tests."""
    src = pathlib.Path(d.__file__).read_text()
    forbidden_patterns = [
        "STATE_FILE.write",
        "STATE_FILE.touch",
        "EVENTS_FILE.write",
        "EVENTS_FILE.touch",
        "EVENTS_FILE.unlink",
        "LOG_FILE.write",
        "STATE_DIR.mkdir",
    ]
    for pat in forbidden_patterns:
        assert pat not in src, f"forbidden write operation found: {pat}"
