"""End-to-end smoke test for the book-walked replay harness.

The first test builds a fully synthetic sqlite orderbook + events.jsonl in
``tmp_path`` and runs the harness through to a parquet artefact, then
checks schema and §5.2 attribution-sum invariants.

The second test activates only if the real daemon events.jsonl and the
captured btc5m sqlite share >= 30 minutes of timestamp overlap; otherwise
it is skipped cleanly. That keeps the suite green in the current repo
state (events cover 2026-04-22, orderbook snapshots cover 2026-04-09..11)
while auto-enabling once real overlap exists.
"""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

import pytest

from active_bots.execution.replay_executor import ExecutionRecord
from experiments.backtest.harness import run as harness_run

REPO_ROOT = Path(__file__).resolve().parents[3]
REAL_EVENTS = REPO_ROOT / "daemon_state" / "events.jsonl"
REAL_DB = REPO_ROOT / "data" / "btc5m.db"


# ----------------------------------------------------------------------
# Real-overlap detection (module-level; decides whether test 2 runs)
# ----------------------------------------------------------------------


def _real_overlap_window() -> Optional[tuple[str, str]]:
    """If real events.jsonl entries live inside a 30+ minute window that also
    has >= 10 orderbook snapshot rows AND at least one matching slug exists
    in the markets table, return that window as ISO strings. Otherwise None.

    A bounding-box intersection is not enough: events may span 01:10-09:44Z
    while orderbooks exist at 14:07 yesterday and 11:40 today — the rectangle
    intersects but has no actual data inside.
    """
    if not REAL_EVENTS.exists() or not REAL_DB.exists():
        return None
    try:
        ev_min, ev_max = _events_range(REAL_EVENTS)
    except Exception:
        return None
    if ev_min is None or ev_max is None:
        return None
    iso_lo = datetime.fromtimestamp(ev_min, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    iso_hi = datetime.fromtimestamp(ev_max, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    if ev_max - ev_min < 30 * 60:
        return None
    try:
        conn = sqlite3.connect(f"file:{REAL_DB}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        # Count orderbook rows whose snapshot_time falls inside the events range.
        row = conn.execute(
            "SELECT COUNT(*) FROM orderbooks WHERE snapshot_time BETWEEN ? AND ?",
            (iso_lo, iso_hi),
        ).fetchone()
        ob_rows = row[0] if row else 0
        if ob_rows < 10:
            return None
        # At least one slug must be shared between events and markets table.
        events_slugs = _events_slugs(REAL_EVENTS, ev_min, ev_max)
        if not events_slugs:
            return None
        placeholders = ",".join("?" for _ in events_slugs)
        row = conn.execute(
            f"SELECT COUNT(*) FROM markets WHERE slug IN ({placeholders})",
            tuple(events_slugs),
        ).fetchone()
        shared = row[0] if row else 0
        if shared == 0:
            return None
    finally:
        conn.close()
    return iso_lo, iso_hi


def _events_slugs(path: Path, lo: float, hi: float) -> set[str]:
    out: set[str] = set()
    with path.open("r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("type") != "entry_filled":
                continue
            ts = row.get("ts")
            if ts is None:
                continue
            ts = float(ts)
            if ts < lo or ts > hi:
                continue
            pos = row.get("position") or {}
            slug = pos.get("slug")
            if slug:
                out.add(slug)
    return out


def _events_range(path: Path) -> tuple[Optional[float], Optional[float]]:
    lo: Optional[float] = None
    hi: Optional[float] = None
    with path.open("r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("type") not in ("entry_filled", "exit_filled", "resolve"):
                continue
            ts = row.get("ts")
            if ts is None:
                continue
            ts = float(ts)
            lo = ts if lo is None or ts < lo else lo
            hi = ts if hi is None or ts > hi else hi
    return lo, hi


def _orderbooks_range(db: Path) -> tuple[Optional[str], Optional[str]]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT MIN(snapshot_time), MAX(snapshot_time) FROM orderbooks"
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None, None
    return row[0], row[1]


def _iso_to_epoch(s: str) -> float:
    return datetime.fromisoformat(s).timestamp()


_REAL_OVERLAP = _real_overlap_window()


# ----------------------------------------------------------------------
# Synthetic-fixture helpers
# ----------------------------------------------------------------------


def _build_synthetic_db(db_path: Path, *, t0_epoch: float) -> None:
    """Create a sqlite db with one market and three `yes`-side snapshots
    at t0, t0+1s, t0+2s around mid=0.50."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            CREATE TABLE markets (
                slug TEXT,
                yes_token_id TEXT,
                no_token_id TEXT,
                condition_id TEXT,
                end_date TEXT
            )
            """
        )
        end_iso = datetime.fromtimestamp(
            t0_epoch + 300.0, tz=timezone.utc
        ).isoformat()
        conn.execute(
            "INSERT INTO markets VALUES (?, ?, ?, ?, ?)",
            (
                "btc-updown-5m-9999999000",
                "yt1",
                "nt1",
                "c1",
                end_iso,
            ),
        )
        conn.execute(
            """
            CREATE TABLE orderbooks (
                condition_id TEXT,
                side TEXT,
                bids TEXT,
                asks TEXT,
                snapshot_time TEXT
            )
            """
        )
        # 5-level book around mid=0.50, tick 0.01, plenty of top size so fills go full.
        bids = [
            {"price": "0.49", "size": "500"},
            {"price": "0.48", "size": "500"},
            {"price": "0.47", "size": "500"},
            {"price": "0.46", "size": "500"},
            {"price": "0.45", "size": "500"},
        ]
        asks = [
            {"price": "0.51", "size": "500"},
            {"price": "0.52", "size": "500"},
            {"price": "0.53", "size": "500"},
            {"price": "0.54", "size": "500"},
            {"price": "0.55", "size": "500"},
        ]
        for dt_s in (0.0, 1.0, 2.0):
            snap_iso = datetime.fromtimestamp(
                t0_epoch + dt_s, tz=timezone.utc
            ).isoformat()
            conn.execute(
                "INSERT INTO orderbooks VALUES (?, ?, ?, ?, ?)",
                ("c1", "yes", json.dumps(bids), json.dumps(asks), snap_iso),
            )
        conn.commit()
    finally:
        conn.close()


def _build_synthetic_events(events_path: Path, *, t_decision: float, t_zero: float) -> None:
    """Two events: one entry_filled (inside snapshot window) and one matching
    exit_filled 60s later."""
    entry = {
        "ts": t_decision,
        "type": "entry_filled",
        "strategy": "refined",
        "position": {
            "slug": "btc-updown-5m-9999999000",
            "side": "Up",
            "size_shares": 5,
            "size_usdc": 2.5,
            "market_at_entry": 0.5,
            "fair_at_entry": 0.55,
            "edge": 0.05,
            "strike": 60000,
            "entry_time": t_decision,
            "t_zero": t_zero,
            "source": "edge",
        },
    }
    exit_ev = {
        "ts": t_decision + 60.0,
        "type": "exit_filled",
        "trade": {
            "slug": "btc-updown-5m-9999999000",
            "side": "Up",
            "entry_price": 0.5,
            "exit_price": 0.7,
            "size_shares": 5,
            "size_usdc": 2.5,
            "pnl": 1.0,
            "won": True,
            "resolved_time": t_decision + 60.0,
            "strike": 60000,
            "final_btc": 60100,
            "exit_type": "TP",
            "edge": 0.05,
        },
    }
    with events_path.open("w") as f:
        f.write(json.dumps(entry) + "\n")
        f.write(json.dumps(exit_ev) + "\n")


# ----------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------


def test_smoke_synthetic_end_to_end(tmp_path: Path) -> None:
    """Drive the harness end-to-end on a synthetic two-event scenario."""
    # Anchor to a fixed instant; decision at t0+1s (inside all 3 snapshots).
    t0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc).timestamp()
    t_decision = t0 + 1.0
    t_zero = t0  # the "tunnel" starts at t0, 300 s window

    db_path = tmp_path / "synthetic.db"
    events_path = tmp_path / "events.jsonl"
    out_path = tmp_path / "out.parquet"

    _build_synthetic_db(db_path, t0_epoch=t0)
    _build_synthetic_events(events_path, t_decision=t_decision, t_zero=t_zero)

    # Window that spans a couple of minutes around the decision.
    win_lo = datetime.fromtimestamp(t0 - 30.0, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    win_hi = datetime.fromtimestamp(t0 + 240.0, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    result = harness_run(
        events=events_path,
        scrapes=db_path,
        out=out_path,
        window=f"{win_lo}..{win_hi}",
        tick_size=Decimal("0.01"),
        latency_profile="sg_wg_prior",
        fee_category="crypto",
        mode="freeze_depleted",
        seed=7,
        input_format="sqlite",
    )
    assert result == out_path
    assert out_path.exists(), "harness did not write parquet file"

    import pyarrow.parquet as pq

    table = pq.read_table(out_path)
    assert table.num_rows > 0, "expected at least one emitted row"

    # Schema — every dataclass field must be a parquet column.
    expected_cols = set(ExecutionRecord.__dataclass_fields__.keys())
    actual_cols = set(table.column_names)
    missing = expected_cols - actual_cols
    assert not missing, f"parquet is missing columns: {sorted(missing)}"

    # Attribution-sum invariant on non-stale rows.
    df = table.to_pydict()
    n_checked = 0
    for i in range(table.num_rows):
        if df["classification"][i] == "book_stale":
            continue
        parts = (
            df["half_spread_cost"][i],
            df["latency_drift_cost"][i],
            df["book_walk_cost"][i],
            df["fees_cost"][i],
        )
        total = df["total_IS"][i]
        if any((p is None) or (isinstance(p, float) and math.isnan(p)) for p in parts):
            # Harness correctly propagates NaN; total_IS must also be NaN.
            assert total is None or (isinstance(total, float) and math.isnan(total)), (
                f"row {i}: parts contain NaN but total_IS={total!r}"
            )
            continue
        s = sum(float(p) for p in parts)
        assert abs(float(total) - s) < 1e-9, (
            f"row {i}: total_IS={total!r} != sum(parts)={s!r}"
        )
        n_checked += 1
    assert n_checked > 0, "no non-stale rows produced; smoke check never validated sum"


@pytest.mark.skipif(
    _REAL_OVERLAP is None,
    reason=(
        "real events.jsonl and btc5m.db do not share >= 30 min of overlap "
        "(or one of them is missing); skipping real-data smoke test"
    ),
)
def test_smoke_real_overlap_if_present(tmp_path: Path) -> None:
    """If real captures overlap, drive the harness over the overlap window
    and assert that at least one non-book_stale row is emitted."""
    assert _REAL_OVERLAP is not None  # narrows type for mypy; guarded by skipif
    iso_lo, iso_hi = _REAL_OVERLAP
    out_path = tmp_path / "real.parquet"

    # The real scraper runs at 30 s interval per spec §8.1.2 deferred,
    # so staleness below 500 ms is impossible. Raise the threshold
    # generously to let the test actually exercise the walk when real
    # overlap exists.
    harness_run(
        events=REAL_EVENTS,
        scrapes=REAL_DB,
        out=out_path,
        window=f"{iso_lo}..{iso_hi}",
        tick_size=Decimal("0.01"),
        latency_profile="sg_wg_prior",
        fee_category="crypto",
        mode="freeze_depleted",
        seed=7,
        staleness_hard_ms=10 * 60 * 1000,  # 10 min -- loose diagnostic mode
        input_format="sqlite",
        staleness_policy="allow_stale_diagnostic",
    )
    assert out_path.exists()

    import pyarrow.parquet as pq

    table = pq.read_table(out_path)
    non_stale = sum(
        1 for c in table.column("classification").to_pylist() if c != "book_stale"
    )
    assert non_stale > 0, (
        f"real overlap window {iso_lo}..{iso_hi} produced only book_stale rows"
    )
