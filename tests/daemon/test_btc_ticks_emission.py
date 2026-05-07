"""Tests for the daemon's H1 BTC-tick tape (``btc_ticks.jsonl``).

The daemon's ``binance_feed`` coroutine appends one JSON line per
Binance trade tick to
``$POLYMARKET_SCRAPE_SESSION_DIR/btc_ticks.jsonl``. Replay's
``ReplayDataProvider._load_btc_tape`` reads this file in preference
to the legacy ``dashboard_history.jsonl`` for any post-H1 capture.

Tests cover:
1. ``_open_btc_tick_writer`` returns a file handle when the env var
   is set; None when unset; appends (does not truncate) on resume.
2. The on-disk schema matches what ``ReplayDataProvider`` expects.
3. Replay prefers session ``btc_ticks.jsonl`` over a competing
   ``dashboard_history.jsonl`` when both exist.
"""

from __future__ import annotations

import json

import pytest


def test_open_btc_tick_writer_returns_none_when_env_unset(monkeypatch):
    """No env → None handle. Behaviour unchanged from pre-H1."""
    import daemon_base_v1

    monkeypatch.delenv("POLYMARKET_SCRAPE_SESSION_DIR", raising=False)
    fh = daemon_base_v1._open_btc_tick_writer()
    assert fh is None


def test_open_btc_tick_writer_creates_file_when_env_set(monkeypatch, tmp_path):
    """Env set → file handle, file exists, append mode."""
    import daemon_base_v1

    monkeypatch.setenv("POLYMARKET_SCRAPE_SESSION_DIR", str(tmp_path))
    fh = daemon_base_v1._open_btc_tick_writer()
    try:
        assert fh is not None
        path = tmp_path / "btc_ticks.jsonl"
        assert path.exists()
        # Write a sample line and confirm we can read it back.
        fh.write('{"ts_ns":1,"ts":1.0,"btc_price":100000.0}\n')
        fh.flush()
        contents = path.read_text()
        assert "100000.0" in contents
    finally:
        if fh is not None:
            fh.close()


def test_open_btc_tick_writer_appends_on_resume(monkeypatch, tmp_path):
    """Resume scenario: file exists with content → new ticks append, old
    ticks survive."""
    import daemon_base_v1

    path = tmp_path / "btc_ticks.jsonl"
    path.write_text('{"ts_ns":1,"ts":1.0,"btc_price":100000.0}\n')
    pre = path.read_text()

    monkeypatch.setenv("POLYMARKET_SCRAPE_SESSION_DIR", str(tmp_path))
    fh = daemon_base_v1._open_btc_tick_writer()
    try:
        assert fh is not None
        fh.write('{"ts_ns":2,"ts":2.0,"btc_price":100100.0}\n')
        fh.flush()
    finally:
        if fh is not None:
            fh.close()

    post = path.read_text()
    assert post.startswith(pre), "old contents survived (append mode)"
    assert "100100.0" in post


def test_btc_ticks_schema_matches_replay_consumer(tmp_path):
    """Round-trip: write a btc_ticks.jsonl in the daemon's schema, read
    it back via ReplayDataProvider, confirm it loads correctly.

    Regression-proof for the daemon-emit / replay-consume contract.
    """
    from polyhustle.data.replay import ReplayDataProvider

    # Build a minimal session — manifest + btc_ticks.jsonl.
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    t_lo = 1_700_000_000.0
    t_hi = 1_700_000_300.0
    manifest = {
        "session_id": "test",
        "launch_ts_ns": int(t_lo * 1e9),
        "stop_ts_ns": int(t_hi * 1e9),
        "events_jsonl_path": "",
        "daemon_log_path": "",
    }
    (session_dir / "manifest.json").write_text(json.dumps(manifest))
    btc_path = session_dir / "btc_ticks.jsonl"
    with btc_path.open("w") as f:
        for i in range(10):
            ts = t_lo + i * 30.0
            f.write(json.dumps({
                "ts_ns": int(ts * 1e9),
                "ts": ts,
                "btc_price": 100_000.0 + i * 5.0,
            }) + "\n")

    p = ReplayDataProvider(session_dir)
    p.load()
    assert len(p.btc_ticks) == 10
    assert p.btc_ticks[0].btc_price == 100_000.0
    assert p.btc_tape_source == "session_btc_ticks"


def test_replay_prefers_session_btc_ticks_over_dashboard_history(tmp_path):
    """When both sources exist, the session's per-capture btc_ticks.jsonl
    wins. The dashboard_history.jsonl (legacy) is the fallback only
    when the session file is absent.
    """
    from polyhustle.data.replay import ReplayDataProvider

    session_dir = tmp_path / "session"
    session_dir.mkdir()
    t_lo = 1_700_000_000.0
    t_hi = 1_700_000_300.0
    manifest = {
        "session_id": "test",
        "launch_ts_ns": int(t_lo * 1e9),
        "stop_ts_ns": int(t_hi * 1e9),
        "events_jsonl_path": "",
        # Point daemon_log_path at tmp_path so dashboard_history.jsonl
        # there is the legacy candidate.
        "daemon_log_path": str(tmp_path / "daemon.log"),
    }
    (session_dir / "manifest.json").write_text(json.dumps(manifest))

    # Session source: 5 ticks at btc_price=200_000.
    with (session_dir / "btc_ticks.jsonl").open("w") as f:
        for i in range(5):
            ts = t_lo + i * 30.0
            f.write(json.dumps({"ts": ts, "btc_price": 200_000.0}) + "\n")

    # Legacy source at the daemon_log sibling: 10 ticks at btc_price=100_000.
    with (tmp_path / "dashboard_history.jsonl").open("w") as f:
        for i in range(10):
            ts = t_lo + i * 20.0
            f.write(json.dumps({"ts": ts, "btc_price": 100_000.0}) + "\n")

    p = ReplayDataProvider(session_dir)
    p.load()
    # Session wins: 5 ticks loaded, all at 200_000.
    assert len(p.btc_ticks) == 5
    assert all(t.btc_price == 200_000.0 for t in p.btc_ticks)
    assert p.btc_tape_source == "session_btc_ticks"


def test_replay_falls_back_to_dashboard_history_when_session_absent(tmp_path):
    """Pre-H1 captures (no btc_ticks.jsonl) still replay via the legacy
    dashboard_history.jsonl path. Backwards compat for R2.2."""
    from polyhustle.data.replay import ReplayDataProvider

    session_dir = tmp_path / "session"
    session_dir.mkdir()
    t_lo = 1_700_000_000.0
    t_hi = 1_700_000_300.0
    manifest = {
        "session_id": "test",
        "launch_ts_ns": int(t_lo * 1e9),
        "stop_ts_ns": int(t_hi * 1e9),
        "events_jsonl_path": "",
        "daemon_log_path": str(tmp_path / "daemon.log"),
    }
    (session_dir / "manifest.json").write_text(json.dumps(manifest))

    # Only the legacy source exists.
    with (tmp_path / "dashboard_history.jsonl").open("w") as f:
        for i in range(8):
            ts = t_lo + i * 30.0
            f.write(json.dumps({"ts": ts, "btc_price": 100_000.0}) + "\n")

    p = ReplayDataProvider(session_dir)
    p.load()
    assert len(p.btc_ticks) == 8
    assert p.btc_tape_source == "dashboard_history_log_sibling"


def test_replay_warns_when_no_btc_tape_source_exists(tmp_path, caplog):
    """No btc_ticks.jsonl + no dashboard_history.jsonl → empty tape +
    a clear warning. ReplayDataProvider doesn't crash; downstream
    callers see zero ticks and can decide what to do."""
    from polyhustle.data.replay import ReplayDataProvider

    session_dir = tmp_path / "session"
    session_dir.mkdir()
    t_lo = 1_700_000_000.0
    t_hi = 1_700_000_300.0
    manifest = {
        "session_id": "no-tape-test",
        "launch_ts_ns": int(t_lo * 1e9),
        "stop_ts_ns": int(t_hi * 1e9),
        "events_jsonl_path": "",
        "daemon_log_path": "/nonexistent/daemon.log",
    }
    (session_dir / "manifest.json").write_text(json.dumps(manifest))

    p = ReplayDataProvider(session_dir)
    with caplog.at_level("WARNING", logger="polyhustle.data.replay"):
        p.load()
    assert len(p.btc_ticks) == 0
    assert p.btc_tape_source is None
    assert any(
        "no ticks loaded" in r.getMessage() and "no-tape-test" in r.getMessage()
        for r in caplog.records
    )


@pytest.mark.parametrize("n_ticks", [1, 100])
def test_writer_round_trip_at_realistic_tick_volume(tmp_path, monkeypatch, n_ticks):
    """Writer + reader handle realistic line counts. 100 ticks ≈ 100 s
    of daemon runtime; 12 h session = ~43k lines and the writer is
    line-buffered (buffering=1) so this scales.

    Uses an integer t0 to avoid the ``.6f``-formatting + float-precision
    corner case that surfaces only in synthetic tests where
    ``launch_ts_ns`` and the first tick's ``ts`` are derived from the
    same wall-clock instant. In real captures the launcher's
    ``launch_ts_ns`` is set milliseconds BEFORE any BTC tick, so
    ``t_lo < first_tick_ts`` always — even with ``.6f`` truncation.
    """
    import daemon_base_v1
    from polyhustle.data.replay import ReplayDataProvider

    session_dir = tmp_path / "session"
    session_dir.mkdir()
    monkeypatch.setenv("POLYMARKET_SCRAPE_SESSION_DIR", str(session_dir))

    # Integer t0 → .6f format preserves it exactly → no precision drift.
    t0 = 1_700_000_000
    fh = daemon_base_v1._open_btc_tick_writer()
    assert fh is not None
    try:
        for i in range(n_ticks):
            ts = t0 + i * 1.0
            price = 100_000.0 + i
            fh.write(
                f'{{"ts_ns":{int(ts * 1e9)},"ts":{ts:.6f},'
                f'"btc_price":{price}}}\n'
            )
    finally:
        fh.close()

    # Build a minimal manifest covering the synthetic window.
    manifest = {
        "session_id": "rt-test",
        "launch_ts_ns": int(t0 * 1e9),
        "stop_ts_ns": int((t0 + n_ticks + 10) * 1e9),
        "events_jsonl_path": "",
        "daemon_log_path": "",
    }
    (session_dir / "manifest.json").write_text(json.dumps(manifest))

    p = ReplayDataProvider(session_dir)
    p.load()
    assert len(p.btc_ticks) == n_ticks
