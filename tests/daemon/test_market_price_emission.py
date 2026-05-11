"""Tests for the daemon's H2 RTDS market_price tape (``market_price.jsonl``).

The daemon's ``rtds_feed`` coroutine appends one JSON line per ACCEPTED
RTDS frame — after the rollover-grace + outcome filter — to
``$POLYMARKET_SCRAPE_SESSION_DIR/market_price.jsonl``. Replay's
``ReplayDataProvider._load_market_price`` reads this file in preference
to ``events.jsonl`` rows of type ``rtds_market_price`` /
``market_price_update`` (which the daemon does not actually emit, so the
events.jsonl source is empty in practice).

The previous inline per-frame filter inside ``rtds_feed`` and the new
tape write share one predicate via ``_process_rtds_message`` — the
'must be byte-identical or live and replay diverge again' requirement
is met structurally. These tests cover:

1. Writer open/close lifecycle mirrors ``_open_btc_tick_writer``.
2. Per-frame schema matches what ``ReplayDataProvider._load_market_price``
   expects.
3. The rollover-grace filter actually drops the in-flight frames that
   the live handler would drop — a regression here would silently
   re-introduce the H2 bug shape (replay sees frames live didn't).
"""

from __future__ import annotations

import json

# ── _open_market_price_writer lifecycle ──────────────────────────────────


def test_open_market_price_writer_returns_none_when_env_unset(monkeypatch):
    """No env → None handle. Behaviour unchanged from pre-H2."""
    import daemon_base_v1

    monkeypatch.delenv("POLYMARKET_SCRAPE_SESSION_DIR", raising=False)
    fh = daemon_base_v1._open_market_price_writer()
    assert fh is None


def test_open_market_price_writer_creates_file_when_env_set(monkeypatch, tmp_path):
    """Env set → file handle, file exists, append mode."""
    import daemon_base_v1

    monkeypatch.setenv("POLYMARKET_SCRAPE_SESSION_DIR", str(tmp_path))
    fh = daemon_base_v1._open_market_price_writer()
    try:
        assert fh is not None
        path = tmp_path / "market_price.jsonl"
        assert path.exists()
        fh.write('{"ts_ns":1,"ts":1.0,"slug":"x","outcome":"yes",'
                 '"raw_price":0.55,"market_price_up":0.55}\n')
        fh.flush()
        assert "0.55" in path.read_text()
    finally:
        if fh is not None:
            fh.close()


def test_open_market_price_writer_appends_on_resume(monkeypatch, tmp_path):
    """Resume scenario: file exists with content → new writes append."""
    import daemon_base_v1

    path = tmp_path / "market_price.jsonl"
    path.write_text('{"ts_ns":1,"ts":1.0,"slug":"x","outcome":"yes",'
                    '"raw_price":0.10,"market_price_up":0.10}\n')
    pre = path.read_text()

    monkeypatch.setenv("POLYMARKET_SCRAPE_SESSION_DIR", str(tmp_path))
    fh = daemon_base_v1._open_market_price_writer()
    try:
        assert fh is not None
        fh.write('{"ts_ns":2,"ts":2.0,"slug":"x","outcome":"no",'
                 '"raw_price":0.20,"market_price_up":0.80}\n')
        fh.flush()
    finally:
        if fh is not None:
            fh.close()

    post = path.read_text()
    assert post.startswith(pre), "old contents survived (append mode)"
    assert "0.80" in post


# ── _process_rtds_message: per-frame filter + tape write ─────────────────


def _make_payload(slug: str, outcome: str, price: str) -> str:
    """Build a raw RTDS WS frame string matching the activity/orders_matched
    topic's wire shape."""
    return json.dumps({"payload": {"slug": slug, "outcome": outcome, "price": price}})


def test_process_rtds_message_writes_90_of_100_with_rollover_grace_filter(
    tmp_path, monkeypatch,
):
    """100 frames feed into _process_rtds_message — 90 current-cycle (Up)
    accepted, 10 prev-cycle frames dropped via the rollover-grace
    expired branch. The on-disk file has exactly 90 lines and each line
    parses against the schema ReplayDataProvider expects.

    Reproduces the user's commit-3 acceptance shape: 'feed 100 frames
    including 10 rollover-grace frames (should be dropped), verify 90
    lines with expected schema'.
    """
    import daemon_base_v1

    monkeypatch.setenv("POLYMARKET_SCRAPE_SESSION_DIR", str(tmp_path))
    fh = daemon_base_v1._open_market_price_writer()
    assert fh is not None

    t_zero = 1_000_000_000
    # Push wall-clock far past the 5s grace window so prev-cycle frames
    # hit the grace-expired branch and are dropped (instead of being
    # accepted as in-flight). _process_rtds_message reads
    # daemon_base_v1.time.time() — monkeypatch that, not the global
    # time module.
    monkeypatch.setattr(
        daemon_base_v1.time, "time", lambda: float(t_zero + 1000.0),
    )

    state = daemon_base_v1.DaemonState()
    state.t_zero = t_zero
    state.market_price_up = None

    cur_slug = f"btc-updown-5m-{t_zero}"
    prev_slug = f"btc-updown-5m-{t_zero - daemon_base_v1.MARKET_DURATION}"

    try:
        # Phase 1: 10 prev-cycle frames. Grace window expired (we're
        # 1000s past t_zero, RTDS_ROLLOVER_GRACE_S=5.0), so each is
        # dropped via the (time.time() - state.t_zero) > grace branch.
        for _ in range(10):
            daemon_base_v1._process_rtds_message(
                state,
                _make_payload(prev_slug, "Up", "0.99"),
                market_price_fh=fh,
            )
        # Phase 2: 90 current-cycle frames, alternating Up / Down so we
        # exercise both outcome branches' normalisation.
        for i in range(90):
            outcome = "Up" if i % 2 == 0 else "Down"
            raw_price = 0.50 + (i % 10) * 0.01  # 0.50..0.59
            daemon_base_v1._process_rtds_message(
                state,
                _make_payload(cur_slug, outcome, f"{raw_price:.2f}"),
                market_price_fh=fh,
            )
    finally:
        fh.close()

    path = tmp_path / "market_price.jsonl"
    lines = path.read_text().splitlines()
    assert len(lines) == 90, (
        f"expected 90 written lines (100 frames - 10 rollover-grace "
        f"drops), got {len(lines)}"
    )

    # Schema check on every line.
    for line in lines:
        row = json.loads(line)
        assert set(row.keys()) == {
            "ts_ns", "ts", "slug", "outcome", "raw_price", "market_price_up",
        }
        assert row["slug"] == cur_slug
        assert row["outcome"] in ("yes", "no")
        assert isinstance(row["raw_price"], float)
        assert isinstance(row["market_price_up"], float)
        # market_price_up should be in [0, 1] given the construction.
        assert 0.0 <= row["market_price_up"] <= 1.0
        # Up outcome → market_price_up == raw_price; Down → 1 - raw_price.
        if row["outcome"] == "yes":
            assert abs(row["market_price_up"] - row["raw_price"]) < 1e-9
        else:
            assert abs(row["market_price_up"] - (1.0 - row["raw_price"])) < 1e-9


def test_process_rtds_message_filter_byte_identical_with_and_without_fh(
    monkeypatch,
):
    """The filter predicate is shared between the live state update and
    the tape write. Passing market_price_fh=None must produce the SAME
    state mutations as passing a real fh — the only difference is the
    on-disk side-effect.

    Regression-proof for the 'live and replay diverge if filters drift'
    requirement: by construction the predicate is shared, but this
    test catches anyone who tries to put extra logic on one branch.
    """
    import daemon_base_v1

    t_zero = 1_000_000_000
    monkeypatch.setattr(
        daemon_base_v1.time, "time", lambda: float(t_zero + 10.0),
    )

    # Run twice — once with no fh, once with a fh — and compare the
    # final state. The filter outcome must match exactly.
    cur_slug = f"btc-updown-5m-{t_zero}"
    frames = [
        _make_payload(cur_slug, "Up", "0.60"),
        _make_payload(cur_slug, "Down", "0.30"),  # → 1-0.30 = 0.70
        _make_payload("totally-bogus", "Up", "0.99"),  # SLUG_PREFIX miss
        _make_payload(cur_slug, "Sideways", "0.50"),  # unknown outcome
        _make_payload(cur_slug, "Up", "0.42"),
    ]

    state_no_fh = daemon_base_v1.DaemonState()
    state_no_fh.t_zero = t_zero
    state_no_fh.market_price_up = None
    for raw in frames:
        daemon_base_v1._process_rtds_message(
            state_no_fh, raw, market_price_fh=None,
        )

    state_with_fh = daemon_base_v1.DaemonState()
    state_with_fh.t_zero = t_zero
    state_with_fh.market_price_up = None

    class _Sink:
        """Minimal fh stub — captures writes without touching disk."""
        def __init__(self): self.buf = []
        def write(self, s): self.buf.append(s)

    sink = _Sink()
    for raw in frames:
        daemon_base_v1._process_rtds_message(
            state_with_fh, raw, market_price_fh=sink,
        )

    assert state_no_fh.market_price_up == state_with_fh.market_price_up
    assert state_no_fh.market_price_ts == state_with_fh.market_price_ts
    # 3 of 5 frames accepted (Up, Down, Up). Bogus-prefix and
    # unknown-outcome dropped before the write.
    assert len(sink.buf) == 3


def test_process_rtds_message_rejects_after_market_price_set_during_grace(
    monkeypatch,
):
    """Once state.market_price_up is set for the new cycle, prev-cycle
    in-flight frames must be rejected even WITHIN the grace window.
    This is the `state.market_price_up is not None` clause in the
    rollover-grace branch — without it, late prints from the just-
    ended market would corrupt the new cycle's price."""
    import daemon_base_v1

    t_zero = 1_000_000_000
    # Within grace window — only 2s past t_zero.
    monkeypatch.setattr(
        daemon_base_v1.time, "time", lambda: float(t_zero + 2.0),
    )

    state = daemon_base_v1.DaemonState()
    state.t_zero = t_zero
    # Already have a price for the new cycle.
    state.market_price_up = 0.42

    cur_slug = f"btc-updown-5m-{t_zero}"
    prev_slug = f"btc-updown-5m-{t_zero - daemon_base_v1.MARKET_DURATION}"

    class _Sink:
        def __init__(self): self.buf = []
        def write(self, s): self.buf.append(s)

    sink = _Sink()
    # Prev-cycle frame within grace BUT state.market_price_up is set →
    # rejected.
    daemon_base_v1._process_rtds_message(
        state, _make_payload(prev_slug, "Up", "0.99"),
        market_price_fh=sink,
    )
    assert state.market_price_up == 0.42, "prev-cycle frame leaked through"
    assert sink.buf == []
    # Current-cycle frame → accepted, state advances.
    daemon_base_v1._process_rtds_message(
        state, _make_payload(cur_slug, "Up", "0.55"),
        market_price_fh=sink,
    )
    assert state.market_price_up == 0.55
    assert len(sink.buf) == 1


# ── Schema contract with ReplayDataProvider ──────────────────────────────


def test_market_price_schema_matches_replay_consumer(tmp_path, monkeypatch):
    """Round-trip: write a market_price.jsonl in the daemon's schema, read
    it back via ReplayDataProvider, confirm rtds_by_slug populates and
    market_price_source == "market_price.jsonl"."""
    import daemon_base_v1
    from polyhustle.data.replay import ReplayDataProvider

    session_dir = tmp_path / "session"
    session_dir.mkdir()
    t_lo = 1_700_000_000.0
    t_hi = 1_700_000_300.0
    manifest = {
        "session_id": "h2-rt",
        "launch_ts_ns": int(t_lo * 1e9),
        "stop_ts_ns": int(t_hi * 1e9),
        "events_jsonl_path": "",
        "daemon_log_path": "",
    }
    (session_dir / "manifest.json").write_text(json.dumps(manifest))

    # Use _process_rtds_message itself to write the tape, so the schema
    # on disk is whatever the production write actually emits — not a
    # parallel hand-rolled JSON shape that could drift.
    slug = "btc-updown-5m-1700000000"
    monkeypatch.setenv("POLYMARKET_SCRAPE_SESSION_DIR", str(session_dir))
    fh = daemon_base_v1._open_market_price_writer()
    assert fh is not None
    state = daemon_base_v1.DaemonState()
    state.t_zero = 1_700_000_000

    # Force time.time() into the [t_lo, t_hi] window so the replay-side
    # window-filter accepts the rows.
    monkeypatch.setattr(daemon_base_v1.time, "time", lambda: t_lo + 1.0)

    try:
        for i in range(5):
            outcome = "Up" if i % 2 == 0 else "Down"
            daemon_base_v1._process_rtds_message(
                state,
                _make_payload(slug, outcome, f"{0.50 + i * 0.01:.2f}"),
                market_price_fh=fh,
            )
    finally:
        fh.close()

    p = ReplayDataProvider(session_dir)
    p.load()
    assert p.market_price_source == "market_price.jsonl"
    assert slug in p.rtds_by_slug
    assert len(p.rtds_by_slug[slug]) == 5
