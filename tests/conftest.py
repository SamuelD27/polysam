"""Shared pytest fixtures.

The ``synthetic_session`` fixture builds a tiny on-disk capture matching
the production ``daemon_state/scrapes/<SESSION>/`` layout so the
``ReplayDataProvider`` can be exercised by fast unit tests without
shipping a multi-MB R2.2 capture into the repo.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest


@pytest.fixture
def synthetic_session(tmp_path: Path) -> Path:
    """Build a tiny on-disk session under tmp_path/scrapes/SESSION/.

    Layout matches daemon_state/scrapes/<SESSION>/:
      manifest.json
      book_feed/<YYYY-MM-DD>/<slug>.jsonl.gz

    Returns the session directory path.
    """
    session_id = "2026-05-06T00-00-00Z"
    session_dir = tmp_path / "scrapes" / session_id
    book_feed_dir = session_dir / "book_feed" / "2026-05-06"
    book_feed_dir.mkdir(parents=True, exist_ok=True)

    t_start = 1_777_900_000.0
    t_end = t_start + 600.0  # 10 minutes

    manifest = {
        "session_id": session_id,
        "launch_ts_utc": "2026-05-06T00:00:00Z",
        "launch_ts_ns": int(t_start * 1e9),
        "stop_ts_ns": int(t_end * 1e9),
        "mode": "live_dryrun",
        "scrape_output_dir": str(session_dir),
        "events_jsonl_path": str(tmp_path / "events.jsonl"),
        "daemon_log_path": str(tmp_path / "daemon.log"),
        "git_sha": "synthetic",
        "git_branch": "synthetic",
    }
    (session_dir / "manifest.json").write_text(json.dumps(manifest))

    # Two markets, each spanning 5 minutes, with snapshots + a delta.
    slugs = [
        ("btc-updown-5m-1777900000", t_start + 0.0,
         "asset-yes-A", "asset-no-A"),
        ("btc-updown-5m-1777900300", t_start + 300.0,
         "asset-yes-B", "asset-no-B"),
    ]
    for slug, t_zero, yes_id, no_id in slugs:
        path = book_feed_dir / f"{slug}.jsonl.gz"
        with gzip.open(path, "wt") as f:
            ts0 = int((t_zero + 1.0) * 1e9)
            f.write(json.dumps({
                "type": "snapshot", "reason": "initial",
                "ts_ns": ts0,
                "asset_id": yes_id, "slug": slug, "side": "yes",
                "canonical_tick": "0.01", "effective_tick": "0.01",
                "bids": [{"price": "0.42", "size": "100"}],
                "asks": [{"price": "0.43", "size": "100"},
                         {"price": "0.44", "size": "200"}],
            }) + "\n")
            f.write(json.dumps({
                "type": "snapshot", "reason": "initial",
                "ts_ns": ts0 + 1_000,
                "asset_id": no_id, "slug": slug, "side": "no",
                "canonical_tick": "0.01", "effective_tick": "0.01",
                "bids": [{"price": "0.57", "size": "100"}],
                "asks": [{"price": "0.58", "size": "100"}],
            }) + "\n")
            # one delta later: replace the YES top-of-book.
            f.write(json.dumps({
                "type": "book", "ts_ns": ts0 + 5_000_000_000,
                "asset_id": yes_id, "slug": slug, "side": "yes",
                "raw": {
                    "asset_id": yes_id, "slug": slug,
                    "bids": [{"price": "0.41", "size": "150"}],
                    "asks": [{"price": "0.43", "size": "100"}],
                    "tick_size": "0.01",
                },
            }) + "\n")

    # Tiny events.jsonl with a market_rollover marker per slug + RTDS.
    events_path = tmp_path / "events.jsonl"
    with events_path.open("w") as f:
        for slug, t_zero, *_ in slugs:
            f.write(json.dumps({
                "ts": t_zero, "type": "market_rollover",
                "slug": slug, "t_zero": t_zero,
                "strike": 110_000.0,
            }) + "\n")
        f.write(json.dumps({
            "ts": t_start + 2.0, "type": "rtds_market_price",
            "slug": slugs[0][0], "market_price_up": 0.42,
        }) + "\n")
        f.write(json.dumps({
            "ts": t_start + 60.0, "type": "rtds_market_price",
            "slug": slugs[0][0], "market_price_up": 0.45,
        }) + "\n")

    # BTC tape lives at the canonical location resolved by ReplayDataProvider:
    # Path(manifest["daemon_log_path"]).parent / "dashboard_history.jsonl",
    # which is tmp_path / "dashboard_history.jsonl" (sibling of events.jsonl).
    btc_path = tmp_path / "dashboard_history.jsonl"
    with btc_path.open("w") as f:
        for i in range(10):
            f.write(json.dumps({
                "ts": t_start + i * 30,
                "btc_price": 110_000.0 + i * 5.0,
            }) + "\n")

    return session_dir
