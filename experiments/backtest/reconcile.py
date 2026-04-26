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
import dataclasses
import json
import math
import sys
import time
from pathlib import Path
from typing import Iterable, Iterator

from active_bots.execution.latency import (
    LatencyProfile,
    _percentile_sorted as _pct,
)
from active_bots.execution.replay_executor import ExecutionRecord
from experiments.backtest.schema import (
    GoldenTraceRecord,
    PARTITION_BOUNDARY_NS,
)


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
) -> Iterator[dict]:
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
# Held-out latency fitting (Task 3)
# ---------------------------------------------------------------------------

_HELD_OUT_MIN_SAMPLES = 30


def fit_held_out_latency(
    held_out_session_ids: list[str],
    *,
    scrapes_root: Path,
) -> tuple[LatencyProfile | None, dict]:
    """Fit an empirical latency profile from sessions OTHER than the one
    being reconciled. Returns (profile_or_None, fit_source_dict).

    fit_source_dict has keys: ``sessions``, ``n_samples``,
    ``fit_window_start_ns``, ``fit_window_end_ns``, ``profile`` (or
    ``reason`` if profile is None).

    Returning None (with reason "below_threshold" or
    "no_sessions_provided") tells the caller to skip the empirical
    pass — gate will emit "n/a (no empirical pass)".
    """
    if not held_out_session_ids:
        return None, {
            "sessions": [],
            "n_samples": 0,
            "fit_window_start_ns": 0,
            "fit_window_end_ns": 0,
            "reason": "no_sessions_provided",
        }
    all_gaps_ms: list[float] = []
    fit_window_start_ns = 2**63 - 1
    fit_window_end_ns = 0
    used_sessions: list[str] = []
    for sid in held_out_session_ids:
        manifest_path = scrapes_root / sid / "manifest.json"
        if not manifest_path.is_file():
            continue
        try:
            m = json.loads(manifest_path.read_text())
        except (OSError, ValueError):
            continue
        events_path = Path(m.get("events_jsonl_path", ""))
        if not events_path.is_file():
            continue
        launch_ns = int(m.get("launch_ts_ns", 0))
        stop_ns = m.get("stop_ts_ns")
        end_ns = int(stop_ns) if stop_ns is not None else int(time.time() * 1e9)
        n_this_session = 0
        with events_path.open("r") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                ts = row.get("ts")
                if ts is None:
                    continue
                ts_ns = int(float(ts) * 1e9)
                if ts_ns < launch_ns or ts_ns > end_ns:
                    continue
                pos = row.get("position") or row.get("trade") or {}
                ack = pos.get("ack_ts") if isinstance(pos, dict) else None
                dec = pos.get("entry_time") if isinstance(pos, dict) else None
                if ack is None or dec is None:
                    continue
                try:
                    gap = (float(ack) - float(dec)) * 1000.0
                except (TypeError, ValueError):
                    continue
                if gap < 0:
                    continue
                all_gaps_ms.append(gap)
                n_this_session += 1
        if n_this_session > 0:
            used_sessions.append(sid)
            fit_window_start_ns = min(fit_window_start_ns, launch_ns)
            fit_window_end_ns = max(fit_window_end_ns, end_ns)
    n = len(all_gaps_ms)
    if n < _HELD_OUT_MIN_SAMPLES:
        return None, {
            "sessions": used_sessions,
            "n_samples": n,
            "fit_window_start_ns": fit_window_start_ns if used_sessions else 0,
            "fit_window_end_ns": fit_window_end_ns if used_sessions else 0,
            "reason": "below_threshold",
        }
    all_gaps_ms.sort()
    profile = LatencyProfile(
        p50_ms=float(_pct(all_gaps_ms, 0.50)),
        p95_ms=float(_pct(all_gaps_ms, 0.95)),
        p99_ms=float(_pct(all_gaps_ms, 0.99)),
        p999_ms=float(_pct(all_gaps_ms, 0.999)),
        source="empirical",
    )
    return profile, {
        "sessions": used_sessions,
        "n_samples": n,
        "fit_window_start_ns": fit_window_start_ns,
        "fit_window_end_ns": fit_window_end_ns,
        "profile": {
            "p50_ms": profile.p50_ms,
            "p95_ms": profile.p95_ms,
            "p99_ms": profile.p99_ms,
            "p999_ms": profile.p999_ms,
        },
    }


# ---------------------------------------------------------------------------
# Per-row derivations (Task 4)
# ---------------------------------------------------------------------------

_GATE_STALENESS_MAX_MS = 200
_MARKET_DURATION_S = 300


def build_record(
    entry: dict,
    exec_record: ExecutionRecord,
    pass_name: str,
    mode_tag: str,
) -> GoldenTraceRecord:
    """Compose one GoldenTraceRecord from an entry_filled event row, the
    ReplayExecutor output for that row's decision moment, the latency-
    pass label ("empirical" or "prior"), and the session mode_tag.

    Field semantics per docs/reconcile_design.md "Pipeline" section.
    BUY-taker only this commit; SELL sign-flip in the followup.

    Note on ``pass_name``: kept in the signature for caller-side
    legibility (the dual-pass loop labels its iterations) but
    deliberately NOT read inside the body — ``in_gate_window`` reads
    ``exec_record.latency_source`` instead, which is the authoritative
    ground truth stamped by ReplayExecutor on the record itself.
    Caller's pass_name and the record's latency_source can diverge if
    ReplayExecutor falls back; the record always wins.
    """
    pos = entry.get("position") or {}
    live_fill_px = float(pos.get("entry_price", 0.0))
    live_filled_qty = float(pos.get("size_shares", 0.0))
    ack_ts_s = float(pos.get("ack_ts", entry.get("ts", 0.0)))
    decision_ts_s = float(pos.get("entry_time", entry.get("ts", 0.0)))
    live_ack_ts_ns = int(ack_ts_s * 1e9)
    live_latency_ms = (ack_ts_s - decision_ts_s) * 1000.0

    decision_mid = float(exec_record.decision_mid)
    if decision_mid > 0:
        diff_bps = (live_fill_px - exec_record.fill_vwap) / decision_mid * 10_000.0
    else:
        diff_bps = float("nan")
    if decision_mid > 0:
        from_decision_bps = (live_fill_px - decision_mid) / decision_mid * 10_000.0
        attribution_delta = from_decision_bps - exec_record.total_IS
    else:
        attribution_delta = float("nan")
    book_top = exec_record.best_ask  # BUY taker; SELL flip in followup

    in_gate_window = (
        exec_record.book_staleness_ms < _GATE_STALENESS_MAX_MS
        and exec_record.latency_source == "empirical"
    )
    partition = (
        "post_2026-02-01" if live_ack_ts_ns >= PARTITION_BOUNDARY_NS
        else "pre_2026-02-01"
    )

    market_age_s = float(exec_record.market_age_s)
    if math.isnan(market_age_s):
        # Sentinel: NaN market_age_s upstream (typically book_stale or
        # unfilled rows from ReplayExecutor). Downstream consumers
        # (gate evaluator, R3 regime decomposition) MUST filter
        # time_remaining_at_decision_s == -1 before using it as a
        # numeric input.
        time_remaining_s = -1
    else:
        time_remaining_s = max(0, int(_MARKET_DURATION_S - market_age_s))

    return GoldenTraceRecord(
        replay=exec_record,
        live_order_id=str(entry.get("order_id", "")),
        live_ack_ts_ns=live_ack_ts_ns,
        live_fill_px=live_fill_px,
        live_filled_qty=live_filled_qty,
        live_latency_measured_ms=live_latency_ms,
        mode_tag=mode_tag,
        t_decision_ns=exec_record.t_signal_ns,
        diff_bps=diff_bps,
        attribution_delta=attribution_delta,
        book_top_at_decision=float(book_top),
        strategy_name=str(entry.get("strategy", "")),
        edge_at_decision=float(pos.get("edge", 0.0)),
        fair_price_at_decision=float(pos.get("fair_at_entry") or 0.0),
        time_remaining_at_decision_s=time_remaining_s,
        in_gate_window=in_gate_window,
        partition=partition,
    )


# ---------------------------------------------------------------------------
# Per-partition acceptance gate (Task 5)
# ---------------------------------------------------------------------------

def evaluate_gate(records: list, *, partition: str) -> dict:
    """Score the §6.3 acceptance gate on records of the given partition.

    Filters in two stages:
      * record.partition == partition
      * record.in_gate_window AND record.replay.classification in
        {"full", "partial"}

    Returns a dict with keys: status ("PASS"|"FAIL"|"n/a"),
    n_rows, median_abs_diff_bps, p95_abs_diff_bps. status="n/a" when
    n_rows == 0 (no in-window rows for this partition).
    """
    eligible = [
        r for r in records
        if getattr(r, "partition", None) == partition
        and getattr(r, "in_gate_window", False)
        and getattr(getattr(r, "replay", None), "classification", "")
            in ("full", "partial")
    ]
    if not eligible:
        return {
            "status": "n/a",
            "n_rows": 0,
            "median_abs_diff_bps": None,
            "p95_abs_diff_bps": None,
        }
    abs_vals = sorted(abs(r.diff_bps) for r in eligible
                      if not math.isnan(r.diff_bps))
    if not abs_vals:
        # Eligible rows exist but all have NaN diff_bps (e.g., when
        # build_record produced NaN due to fill_px==0 or missing
        # decision_mid). Report the eligible count honestly so
        # downstream readers can see "we filtered N rows but couldn't
        # gate them" vs. "no rows passed the filter".
        return {
            "status": "n/a",
            "n_rows": len(eligible),
            "median_abs_diff_bps": None,
            "p95_abs_diff_bps": None,
        }
    median = _pct(abs_vals, 0.50)
    # p95 uses the "higher" (ceiling) method so that 5 rows out of 100 at an
    # extreme value correctly land above the threshold — linear interpolation
    # would dilute the tail signal at this sample size (spec §6.3 intent).
    p95_idx = min(len(abs_vals) - 1, math.ceil(0.95 * (len(abs_vals) - 1)))
    p95 = abs_vals[p95_idx]
    status = "PASS" if (median < 2.0 and p95 < 10.0) else "FAIL"
    return {
        "status": status,
        "n_rows": len(eligible),
        "median_abs_diff_bps": float(median),
        "p95_abs_diff_bps": float(p95),
    }


# ---------------------------------------------------------------------------
# Parquet + manifest writers (Task 6)
# ---------------------------------------------------------------------------

def write_parquet(records: list, out: Path) -> None:
    """Flatten GoldenTraceRecord rows (recursing into the embedded
    ExecutionRecord via dataclasses.asdict) and write parquet.

    Empty record list still produces a parquet with the canonical
    schema so downstream tooling does not trip on a missing file.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq
    out.parent.mkdir(parents=True, exist_ok=True)
    if not records:
        # Flatten an instantiable zero-record to derive the schema.
        # Cheaper than building a synthetic; same result.
        pq.write_table(pa.table({}), out)
        return
    flat_rows = [_flatten_record(r) for r in records]
    fields = list(flat_rows[0].keys())
    table_data = {k: [r[k] for r in flat_rows] for k in fields}
    pq.write_table(pa.table(table_data), out)


def _flatten_record(rec) -> dict:
    """Flatten one GoldenTraceRecord — replay embed expands into
    sibling columns; non-replay fields keep their names."""
    d = dataclasses.asdict(rec)
    replay = d.pop("replay", None) or {}
    out = dict(replay)  # ExecutionRecord cols first (matches harness order)
    out.update(d)       # then overlay/derivations/context/gate cols
    return out


def write_manifest(
    out_parquet: Path,
    *,
    session_id: str,
    session_mode: str,
    asset_prefix: str,
    strategy_filter: tuple,
    latency_fit_source: dict,
    rows_emitted_total: int,
    rows_emitted_by_partition: dict,
    rows_emitted_by_pass: dict,
    gate: dict,
    refusals: list,
) -> Path:
    """Write the manifest sidecar at out_parquet.with_suffix('.parquet.manifest.json')."""
    manifest = {
        "kind": "golden_trace",
        "session_id": session_id,
        "session_mode": session_mode,
        "asset_prefix": asset_prefix,
        "strategy_filter": list(strategy_filter),
        "scope": "entries_only",
        "latency_fit_source": latency_fit_source,
        "rows_emitted_total": rows_emitted_total,
        "rows_emitted_by_partition": rows_emitted_by_partition,
        "rows_emitted_by_pass": rows_emitted_by_pass,
        "gate": gate,
        "refusals": refusals,
    }
    sidecar = out_parquet.with_suffix(out_parquet.suffix + ".manifest.json")
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(json.dumps(manifest, indent=2) + "\n")
    return sidecar


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
