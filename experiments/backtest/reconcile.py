"""Golden-trace validation against real live fills (spec §6.3).

This module is a **stub** until the first live-mode daemon session captures
enough fills for a defensible reconciliation. When invoked against a paper-
only ``events.jsonl`` it raises ``NoLiveFillsCaptured`` with the exact SQL-
style predicate we are waiting on. Once live fills exist, a follow-up
commit will flesh out the full §6.3 workflow:

- Partition captured live fills by pre- vs post-Feb-2026 (the 500 ms taker-
  delay removal — spec §8.2 footnote). Do not pool.
- For each live fill, replay the same decision timestamp through
  ``ReplayExecutor`` with the measured-quantile latency sampler.
- Per-trade record: ``t_decision, live_fill_px, paper_fill_px, diff_bps,
  live_filled_qty, paper_filled_qty, book_top_at_decision,
  book_staleness_ms, paper_latency_sample, live_latency_measured,
  attribution_delta``.
- Acceptance gate: on rows where ``book_staleness_ms < 200`` and the
  latency sampler uses measured live quantiles, require
  ``|median diff_bps| < 2`` and ``p95 diff_bps < 10`` vs the live fill
  price.
- Output: ``golden_trace.parquet`` with the above columns.

Running this now, against a paper-mode ``events.jsonl``, intentionally
refuses to produce a misleading empty/zero-row parquet.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Iterable


# ---------------------------------------------------------------------------
# Session manifest discovery (Task 1)
# ---------------------------------------------------------------------------

_LIVE_MODES: tuple[str, ...] = ("live", "live_dryrun")


def discover_session(
    session_id_or_latest: str,
    *,
    scrapes_root: Path,
) -> dict:
    """Resolve a session_id (or "latest") to its parsed manifest dict.

    "latest" picks the most-recent manifest under ``scrapes_root`` whose
    ``mode`` is in {"live", "live_dryrun"} (paper sessions are skipped
    because they emit no predicate-shape rows). Returns the manifest dict
    augmented with ``manifest_path``, ``launch_ts_ns``, ``stop_ts_ns``,
    ``effective_stop_ns`` (= stop_ts_ns or now-ns if still running).
    """
    if not scrapes_root.exists():
        raise FileNotFoundError(f"scrapes root not found: {scrapes_root}")
    if session_id_or_latest == "latest":
        candidates: list[tuple[int, Path, dict]] = []
        for child in scrapes_root.iterdir():
            mp = child / "manifest.json"
            if not mp.is_file():
                continue
            try:
                m = json.loads(mp.read_text())
            except (OSError, ValueError):
                continue
            if m.get("mode") not in _LIVE_MODES:
                continue
            launch_ns = m.get("launch_ts_ns")
            if launch_ns is None:
                continue
            candidates.append((int(launch_ns), mp, m))
        if not candidates:
            raise FileNotFoundError(
                f"no manifest under {scrapes_root} has mode in {_LIVE_MODES}"
            )
        candidates.sort(key=lambda t: t[0], reverse=True)
        _, manifest_path, m = candidates[0]
    else:
        manifest_path = scrapes_root / session_id_or_latest / "manifest.json"
        if not manifest_path.is_file():
            raise FileNotFoundError(f"manifest not found: {manifest_path}")
        m = json.loads(manifest_path.read_text())
        if m.get("mode") not in _LIVE_MODES:
            raise ValueError(
                f"session {session_id_or_latest} has mode={m.get('mode')!r}; "
                f"reconcile only operates on {_LIVE_MODES}"
            )
    launch_ns = int(m["launch_ts_ns"])
    stop_ns = m.get("stop_ts_ns")
    stop_ns_int = int(stop_ns) if stop_ns is not None else None
    effective_stop_ns = stop_ns_int if stop_ns_int is not None else int(time.time() * 1e9)
    return {
        **m,
        "manifest_path": str(manifest_path),
        "launch_ts_ns": launch_ns,
        "stop_ts_ns": stop_ns_int,
        "effective_stop_ns": effective_stop_ns,
    }


# ---------------------------------------------------------------------------
# Entry fill iterator (Task 2)
# ---------------------------------------------------------------------------

def iter_entry_fills(
    events_path: Path,
    *,
    t0_ns: int,
    t1_ns: int,
    asset_prefix: str = "btc",
    strategy_filter: tuple[str, ...] = ("refined",),
) -> Iterable[dict]:
    """Yield entry_filled events that satisfy ALL of:

      * type == "entry_filled"
      * strategy in strategy_filter
      * t0_ns <= ts_ns <= t1_ns
      * position.slug starts with asset_prefix
      * predicate met: order_id + ack_ts + entry_price all non-null
        somewhere in the row (top-level or nested in position)

    Rows are not loaded into memory all at once — caller may consume
    lazily. JSON-decode failures are skipped silently (same policy as
    harness.py:load_events).
    """
    if not events_path.exists():
        return
    with events_path.open("r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("type") != "entry_filled":
                continue
            if row.get("strategy") not in strategy_filter:
                continue
            ts = row.get("ts")
            if ts is None:
                continue
            ts_ns = int(float(ts) * 1e9)
            if ts_ns < t0_ns or ts_ns > t1_ns:
                continue
            pos = row.get("position") or {}
            slug = str(pos.get("slug", ""))
            if not slug.startswith(asset_prefix):
                continue
            if not _matches_live_predicate(row):
                continue
            yield row


# ---------------------------------------------------------------------------
# Existing stub code (Tasks 2+ reuse these helpers)
# ---------------------------------------------------------------------------

class NoLiveFillsCaptured(RuntimeError):
    """Raised when ``events.jsonl`` contains no rows that satisfy the
    golden-trace predicate. The predicate and the first live-capture
    command are named verbatim in the message so the operator sees the
    exact gate and the exact path to unblock it."""


PREDICATE_SQL = (
    "WHERE ack_ts NOT NULL AND order_id NOT NULL AND fill_price NOT NULL"
)


def _candidate_paths(row: dict) -> Iterable[dict]:
    """Yield every subdict a live-fill row might hang the live-only fields
    off. Events.jsonl puts entry fields under ``position`` and exit fields
    under ``trade``; the order_id / ack_ts / fill_price could live at the
    top level or in either subdict depending on the executor."""
    yield row
    pos = row.get("position")
    if isinstance(pos, dict):
        yield pos
    trd = row.get("trade")
    if isinstance(trd, dict):
        yield trd


def _matches_live_predicate(row: dict) -> bool:
    """Return True iff the row has non-null ``ack_ts`` AND ``order_id`` AND
    ``fill_price`` somewhere in its top-level or nested dicts."""
    has_ack = False
    has_order_id = False
    has_fill_price = False
    for d in _candidate_paths(row):
        if d.get("ack_ts") not in (None, ""):
            has_ack = True
        if d.get("order_id") not in (None, ""):
            has_order_id = True
        # Live executors commonly name the fill price 'fill_price' or
        # 'filled_price'; our LiveExecutor.enter() persists it as
        # 'entry_price' (the actual avg fill, not the strategy's pre-call
        # estimate), so accept that alias too. Paper mode also stamps
        # entry_price, but reconcile's gate requires BOTH order_id AND
        # ack_ts AND fill_price — paper lacks order_id/ack_ts, so this
        # alias cannot accidentally let paper rows through.
        for key in ("fill_price", "filled_price", "executed_price", "entry_price"):
            if d.get(key) not in (None, ""):
                has_fill_price = True
                break
    return has_ack and has_order_id and has_fill_price


def count_live_fills(events_path: Path) -> int:
    """Scan events.jsonl and return the number of rows satisfying the
    live-fill predicate. 0 is a valid answer."""
    if not events_path.exists():
        return 0
    n = 0
    with events_path.open("r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if _matches_live_predicate(row):
                n += 1
    return n


def reconcile(
    events_path: Path,
    *,
    out: Path,
    min_live_fills: int = 100,
) -> Path:
    """Produce golden_trace.parquet per spec §6.3. Currently STUBBED: will
    raise ``NoLiveFillsCaptured`` on any paper-mode events.jsonl because the
    downstream diff-bps math requires real live fills to compare against.

    Target minimum for a defensible first run is ``min_live_fills`` = 100
    per Lo 2002 / §6.4 MinTRL considerations.
    """
    n_live = count_live_fills(events_path)
    if n_live < min_live_fills:
        raise NoLiveFillsCaptured(
            f"Found {n_live} live fills in {events_path}; need >= {min_live_fills}.\n"
            f"\n"
            f"Golden-trace predicate (spec §6.3):\n"
            f"  SELECT * FROM events {PREDICATE_SQL};\n"
            f"\n"
            f"Current events.jsonl is paper-mode — order_id / ack_ts / "
            f"fill_price are null on every row. To capture live fills:\n"
            f"\n"
            f"  MAX_TRADE_SIZE_USDC=1 LIVE_MODE=1 bash launch_daemon.sh\n"
            f"\n"
            f"Run concurrent with scripts/scrape_book.py for ~3 h on 2 BTC 5m\n"
            f"markets, targeting ~{min_live_fills} fills to hit the Lo 2002 /\n"
            f"§6.4 MinTRL floor. Re-run this command after that window.\n"
        )
    # TODO: per-fill replay + diff_bps + acceptance gate. Unreachable until
    # live fills are captured; implement in a follow-up commit.
    raise NotImplementedError(
        f"Found {n_live} live fills — reconcile.py body pending (§6.3). "
        "This is the next commit after the first live-capture session."
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="experiments.backtest.reconcile")
    p.add_argument("--events", required=True, type=Path)
    p.add_argument(
        "--out", required=True, type=Path,
        help="Target golden_trace.parquet path.",
    )
    p.add_argument(
        "--min-live-fills", type=int, default=100,
        help="Minimum live-fill count to proceed past the gate (spec §6.4).",
    )
    args = p.parse_args(argv)
    try:
        reconcile(
            events_path=args.events,
            out=args.out,
            min_live_fills=args.min_live_fills,
        )
    except NoLiveFillsCaptured as e:
        print(str(e), file=sys.stderr)
        return 2
    except NotImplementedError as e:
        print(str(e), file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
