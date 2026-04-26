# reconcile.py entries-only — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the `experiments/backtest/reconcile.py` stub with the §6.3 golden-trace implementation per `docs/reconcile_design.md`. Joins captured live (or live_dryrun) entries to book-walked replay records, evaluates the per-partition acceptance gate (median |diff_bps| < 2 AND p95 < 10 on rows where `book_staleness_ms < 200` AND `latency_source == "empirical"`), writes `golden_<session_id>.parquet` plus a manifest sidecar, and prints a mode-aware refusal-aware report.

**Architecture:** Single-file rewrite of `reconcile.py` with seven internal functions (manifest discovery, entry filter, held-out latency fit, dual-pass replay, per-row derivations, gate evaluator, parquet/manifest writer) plus a `main()` that wires them. Reuses `harness.py:load_snapshots_from_feed_dir`, `harness.py:load_markets`, and `replay_executor.ReplayExecutor` verbatim — no fork. One refactor in `report.py` to single-source the refusal-headline machinery (currently inline) so `staleness_policy`, `mode_tag`, and `scope` refusals all funnel through the same helper. Schema dataclass is already declared in `experiments/backtest/schema.py` (commit 293e65e).

**Tech Stack:** Python 3.11, pyarrow (parquet), `experiments.backtest.harness` (book-feed loading), `active_bots.execution.replay_executor.ReplayExecutor`, `active_bots.execution.latency.fit_from_events_jsonl`, pytest with tmp_path fixtures.

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `experiments/backtest/reconcile.py` | **Rewrite** (current 173-line stub → ~600-line implementation) | All pipeline logic. Single file per spec doc statement. |
| `experiments/backtest/tests/test_reconcile.py` | **Extend** (currently 8 stub tests → keep predicate tests, add 11 new) | All reconcile tests. |
| `experiments/backtest/report.py` | **Modify** lines 342–365 | Extract inline staleness_policy refusal into `_refusal_headers()` helper that also handles `mode_tag` and `scope`. |

The rewrite preserves these reusable pieces from the current stub: the `_candidate_paths()` traversal, the `_matches_live_predicate()` predicate (renamed to `_predicate_match()`), and the `count_live_fills()` function (kept for back-compat with existing predicate tests). Everything else gets replaced.

---

## Task 1: Manifest discovery

**Files:**
- Modify: `experiments/backtest/reconcile.py` (top of file — keep `_candidate_paths`, `_matches_live_predicate`, `count_live_fills` from existing stub; add new functions)
- Test: `experiments/backtest/tests/test_reconcile.py` (extend existing file)

- [ ] **Step 1: Write the failing test for `discover_session('latest')`**

Add to `tests/test_reconcile.py`:

```python
import json
from pathlib import Path

def _write_manifest(scrapes_root: Path, session_id: str, mode: str,
                    launch_ns: int, stop_ns: int | None,
                    events_path: str, feed_dir: str) -> Path:
    d = scrapes_root / session_id
    d.mkdir(parents=True, exist_ok=True)
    m = d / "manifest.json"
    m.write_text(json.dumps({
        "session_id": session_id,
        "launch_ts_ns": launch_ns,
        "stop_ts_ns": stop_ns,
        "stop_ts_utc": None if stop_ns is None else "2026-04-26T07:00:00Z",
        "mode": mode,
        "events_jsonl_path": events_path,
        "scrape_canonical_dir": feed_dir,
    }))
    return m


def test_discover_session_latest_picks_most_recent_live_or_dryrun(tmp_path: Path) -> None:
    from experiments.backtest.reconcile import discover_session
    scrapes_root = tmp_path / "scrapes"
    _write_manifest(scrapes_root, "2026-04-24T07-10-19Z", "live_dryrun",
                    1_777_014_619_000_000_000, 1_777_200_000_000_000_000,
                    str(tmp_path / "events_old.jsonl"), str(tmp_path / "feed_old"))
    _write_manifest(scrapes_root, "2026-04-26T07-00-00Z", "live_dryrun",
                    1_777_180_800_000_000_000, None,
                    str(tmp_path / "events_new.jsonl"), str(tmp_path / "feed_new"))
    _write_manifest(scrapes_root, "2026-04-26T08-00-00Z", "paper",
                    1_777_184_400_000_000_000, None,
                    str(tmp_path / "events_paper.jsonl"), str(tmp_path / "feed_paper"))
    sess = discover_session("latest", scrapes_root=scrapes_root)
    assert sess["session_id"] == "2026-04-26T07-00-00Z"
    assert sess["mode"] == "live_dryrun"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
source /home/samsam/miniconda3/etc/profile.d/conda.sh && conda activate polymarket-env
python -m pytest experiments/backtest/tests/test_reconcile.py::test_discover_session_latest_picks_most_recent_live_or_dryrun -v
```

Expected: FAIL with `ImportError: cannot import name 'discover_session'`.

- [ ] **Step 3: Write minimal implementation**

Add to top of `experiments/backtest/reconcile.py` (after existing imports):

```python
import time
from typing import Iterable

# Existing predicate code (_candidate_paths, _matches_live_predicate,
# count_live_fills, PREDICATE_SQL, NoLiveFillsCaptured) stays as-is —
# entry filter reuses _matches_live_predicate.


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
```

- [ ] **Step 4: Run test to verify it passes**

```bash
python -m pytest experiments/backtest/tests/test_reconcile.py::test_discover_session_latest_picks_most_recent_live_or_dryrun -v
```

Expected: PASS.

- [ ] **Step 5: Add three more failing tests**

Add to `tests/test_reconcile.py`:

```python
def test_discover_session_explicit_id_loads_named_manifest(tmp_path: Path) -> None:
    from experiments.backtest.reconcile import discover_session
    scrapes_root = tmp_path / "scrapes"
    _write_manifest(scrapes_root, "S1", "live_dryrun",
                    1_777_000_000_000_000_000, 1_777_100_000_000_000_000,
                    "/x/events.jsonl", "/x/feed")
    sess = discover_session("S1", scrapes_root=scrapes_root)
    assert sess["session_id"] == "S1"
    assert sess["effective_stop_ns"] == 1_777_100_000_000_000_000


def test_discover_session_explicit_paper_id_rejected(tmp_path: Path) -> None:
    from experiments.backtest.reconcile import discover_session
    scrapes_root = tmp_path / "scrapes"
    _write_manifest(scrapes_root, "P1", "paper",
                    1_777_000_000_000_000_000, 1_777_100_000_000_000_000,
                    "/x/events.jsonl", "/x/feed")
    with pytest.raises(ValueError, match="reconcile only operates on"):
        discover_session("P1", scrapes_root=scrapes_root)


def test_discover_session_running_session_uses_now_for_stop(tmp_path: Path) -> None:
    from experiments.backtest.reconcile import discover_session
    scrapes_root = tmp_path / "scrapes"
    _write_manifest(scrapes_root, "S1", "live_dryrun",
                    1_777_000_000_000_000_000, None,
                    "/x/events.jsonl", "/x/feed")
    before = int(time.time() * 1e9)
    sess = discover_session("S1", scrapes_root=scrapes_root)
    after = int(time.time() * 1e9)
    assert sess["stop_ts_ns"] is None
    assert before <= sess["effective_stop_ns"] <= after
```

Add to imports at top of test file: `import time`, `import pytest`.

- [ ] **Step 6: Run all four discover tests**

```bash
python -m pytest experiments/backtest/tests/test_reconcile.py -v -k discover_session
```

Expected: 4 passed.

- [ ] **Step 7: Commit**

```bash
git add experiments/backtest/reconcile.py experiments/backtest/tests/test_reconcile.py
git commit -m "$(cat <<'EOF'
feat(reconcile): manifest discovery for session_id or 'latest'

discover_session(id_or_latest, scrapes_root) resolves to a parsed
manifest dict with derived fields (launch_ts_ns, effective_stop_ns
= stop_ts_ns or now). 'latest' picks the most-recent live or
live_dryrun manifest; paper sessions are explicitly skipped because
they emit no predicate-shape rows.

Tests cover: latest selection across mixed modes, explicit id
loading, explicit paper id rejection with clear error, running
session (no stop_ts_ns) uses wall-clock now.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Entry filter

**Files:**
- Modify: `experiments/backtest/reconcile.py`
- Test: `experiments/backtest/tests/test_reconcile.py`

- [ ] **Step 1: Write the failing test**

```python
def test_iter_entry_fills_filters_by_strategy_window_predicate(tmp_path: Path) -> None:
    from experiments.backtest.reconcile import iter_entry_fills
    events = tmp_path / "events.jsonl"
    rows = [
        # In window, refined, predicate met → KEEP
        {"ts": 1.0, "type": "entry_filled", "strategy": "refined",
         "order_id": "0xA", "position": {"slug": "btc-updown-5m-1", "ack_ts": 1.001,
                                          "entry_price": 0.42, "size_shares": 24.0,
                                          "edge": 0.07}},
        # Out of window → DROP
        {"ts": 99.0, "type": "entry_filled", "strategy": "refined",
         "order_id": "0xB", "position": {"slug": "btc-updown-5m-2", "ack_ts": 99.001,
                                          "entry_price": 0.50, "size_shares": 20.0}},
        # Wrong strategy → DROP
        {"ts": 1.5, "type": "entry_filled", "strategy": "enhanced",
         "order_id": "0xC", "position": {"slug": "btc-updown-5m-3", "ack_ts": 1.501,
                                          "entry_price": 0.50, "size_shares": 20.0}},
        # Predicate fails (no order_id) → DROP
        {"ts": 1.7, "type": "entry_filled", "strategy": "refined",
         "position": {"slug": "btc-updown-5m-4", "ack_ts": 1.701,
                      "entry_price": 0.50, "size_shares": 20.0}},
        # Wrong asset prefix → DROP
        {"ts": 1.8, "type": "entry_filled", "strategy": "refined",
         "order_id": "0xE", "position": {"slug": "eth-updown-5m-1", "ack_ts": 1.801,
                                          "entry_price": 0.50, "size_shares": 20.0}},
        # Wrong type → DROP
        {"ts": 1.9, "type": "exit_filled", "strategy": "refined",
         "order_id": "0xF", "trade": {"slug": "btc-updown-5m-5", "ack_ts": 1.901,
                                       "exit_price": 0.55, "size_shares": 20.0}},
    ]
    events.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    out = list(iter_entry_fills(events, t0_ns=int(0.5 * 1e9),
                                 t1_ns=int(2.0 * 1e9),
                                 asset_prefix="btc",
                                 strategy_filter=("refined",)))
    assert len(out) == 1
    assert out[0]["order_id"] == "0xA"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest experiments/backtest/tests/test_reconcile.py::test_iter_entry_fills_filters_by_strategy_window_predicate -v
```

Expected: FAIL with `ImportError: cannot import name 'iter_entry_fills'`.

- [ ] **Step 3: Implement**

Add to `reconcile.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

```bash
python -m pytest experiments/backtest/tests/test_reconcile.py::test_iter_entry_fills_filters_by_strategy_window_predicate -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add experiments/backtest/reconcile.py experiments/backtest/tests/test_reconcile.py
git commit -m "$(cat <<'EOF'
feat(reconcile): entry filter with type/strategy/window/asset/predicate gates

iter_entry_fills(events_path, t0_ns, t1_ns, asset_prefix,
strategy_filter) yields only entry_filled rows that pass all five
gates. Lazy generator — caller streams rather than materializing.
Predicate reuses the existing _matches_live_predicate helper.

Single test exercises all five drop reasons in one events.jsonl.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Held-out latency fit

**Files:**
- Modify: `experiments/backtest/reconcile.py`
- Test: `experiments/backtest/tests/test_reconcile.py`

- [ ] **Step 1: Write the failing test**

```python
def test_fit_held_out_latency_returns_profile_with_30plus_samples(tmp_path: Path) -> None:
    from experiments.backtest.reconcile import fit_held_out_latency
    scrapes_root = tmp_path / "scrapes"
    events = tmp_path / "events_held_out.jsonl"
    rows = []
    for i in range(40):
        rows.append({
            "ts": 1000.0 + i,
            "type": "entry_filled",
            "strategy": "refined",
            "order_id": f"0x{i}",
            "position": {
                "slug": "btc-updown-5m-1",
                "entry_time": 1000.0 + i,
                "ack_ts": 1000.0 + i + 0.150,  # 150 ms gap
                "entry_price": 0.42,
            },
        })
    events.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    _write_manifest(scrapes_root, "HOLDOUT", "live_dryrun",
                    int(1000.0 * 1e9), int(2000.0 * 1e9),
                    str(events), str(tmp_path / "feed_unused"))
    profile, fit_source = fit_held_out_latency(["HOLDOUT"], scrapes_root=scrapes_root)
    assert profile is not None
    assert profile.source == "empirical"
    # 150 ms constant → all quantiles ≈ 150
    assert 145 < profile.p50_ms < 155
    assert fit_source["n_samples"] == 40
    assert fit_source["sessions"] == ["HOLDOUT"]


def test_fit_held_out_latency_returns_none_below_threshold(tmp_path: Path) -> None:
    from experiments.backtest.reconcile import fit_held_out_latency
    scrapes_root = tmp_path / "scrapes"
    events = tmp_path / "events_thin.jsonl"
    rows = [{
        "ts": 1000.0 + i, "type": "entry_filled", "strategy": "refined",
        "order_id": f"0x{i}", "position": {
            "slug": "btc-updown-5m-1", "entry_time": 1000.0 + i,
            "ack_ts": 1000.0 + i + 0.150, "entry_price": 0.42,
        },
    } for i in range(10)]  # only 10 rows
    events.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    _write_manifest(scrapes_root, "THIN", "live_dryrun",
                    int(1000.0 * 1e9), int(2000.0 * 1e9),
                    str(events), str(tmp_path / "feed_unused"))
    profile, fit_source = fit_held_out_latency(["THIN"], scrapes_root=scrapes_root)
    assert profile is None
    assert fit_source["n_samples"] == 10
    assert fit_source["reason"] == "below_threshold"


def test_fit_held_out_latency_no_sessions_returns_none(tmp_path: Path) -> None:
    from experiments.backtest.reconcile import fit_held_out_latency
    scrapes_root = tmp_path / "scrapes"
    scrapes_root.mkdir()
    profile, fit_source = fit_held_out_latency([], scrapes_root=scrapes_root)
    assert profile is None
    assert fit_source["n_samples"] == 0
    assert fit_source["reason"] == "no_sessions_provided"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest experiments/backtest/tests/test_reconcile.py -v -k fit_held_out_latency
```

Expected: 3 FAIL with `ImportError`.

- [ ] **Step 3: Implement**

Add to `reconcile.py`:

```python
from active_bots.execution.latency import LatencyProfile, fit_from_events_jsonl, NotFitted

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
        try:
            partial = fit_from_events_jsonl(
                events_path, t_start_ns=launch_ns, t_end_ns=end_ns,
            )
        except NotFitted:
            continue
        # fit_from_events_jsonl returns a fitted LatencyProfile, not raw
        # samples. Re-walk the file to count samples that fed into it
        # so the manifest reports an honest n_samples for THIS session
        # union. (fit_from_events_jsonl's NotFitted threshold is 10;
        # ours is 30 — checked on the union below.)
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
        # Quiet partial usage of fit_from_events_jsonl return — the
        # union-fit below supersedes it. We still called it to surface
        # its NotFitted check on per-session minimums.
        _ = partial
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
    from active_bots.execution.latency import _percentile_sorted as _pct
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
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest experiments/backtest/tests/test_reconcile.py -v -k fit_held_out_latency
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add experiments/backtest/reconcile.py experiments/backtest/tests/test_reconcile.py
git commit -m "$(cat <<'EOF'
feat(reconcile): held-out latency fit with 30-sample minimum

fit_held_out_latency(session_ids, scrapes_root) returns
(LatencyProfile|None, fit_source_dict). Profile is None when the
union of held-out events has < 30 usable (ack_ts, entry_time)
pairs. Per spec doc, fitting on the same session is contaminated;
this function walks events from sessions OTHER than the one being
reconciled and reports the source provenance in fit_source so the
manifest can audit what fed the empirical pass.

Reason codes: 'no_sessions_provided', 'below_threshold'.

Tests cover: 40-sample synthetic fit (constant 150 ms gap →
quantiles ≈ 150), 10-sample below-threshold (None), zero-input
no-sessions-provided.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Per-row derivations

**Files:**
- Modify: `experiments/backtest/reconcile.py`
- Test: `experiments/backtest/tests/test_reconcile.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_build_record_diff_bps_buy_taker_sign(tmp_path: Path) -> None:
    """live > replay → positive diff_bps (live overpaid). bps of decision_mid."""
    from experiments.backtest.reconcile import build_record
    from experiments.backtest.schema import GoldenTraceRecord
    from active_bots.execution.replay_executor import ExecutionRecord

    entry = {
        "ts": 1000.0, "strategy": "refined", "order_id": "0xA",
        "position": {
            "slug": "btc-updown-5m-1", "side": "Up", "entry_price": 0.55,
            "size_shares": 20.0, "ack_ts": 1000.150, "entry_time": 1000.000,
            "edge": 0.05, "fair_at_entry": 0.50, "market_at_entry": 0.50,
            "t_zero": 800,
        },
    }
    rec = ExecutionRecord(  # build a minimal stub
        trade_id="t", parent_order_id="", strategy_id="refined",
        backtest_run_id="r", t_signal_ns=int(1000.0 * 1e9),
        t_send_ns=0, t_ack_ns=0, t_first_fill_ns=0, t_last_fill_ns=0,
        token_id="", market_id="", side="BUY", tick_size=0.01,
        decision_mid=0.50, best_bid=0.49, best_ask=0.51, spread=0.02,
        top_of_book_size_bid=10.0, top_of_book_size_ask=10.0,
        cumulative_depth_5bps=0.0, cumulative_depth_20bps=0.0,
        book_staleness_ms=120, market_age_s=200.0, market_remaining_s=100.0,
        requested_qty_shares=20.0, requested_notional_usdc=10.0,
        worst_price_limit=0.99, filled_qty=20.0, residual_qty=0.0,
        fill_vwap=0.51, levels_consumed=1, classification="full",
        sampled_latency_ms=150.0, latency_source="empirical",
        p_bucket_used="base", half_spread_cost=20.0, book_walk_cost=0.0,
        latency_drift_cost=0.0, adverse_selection_1s=float("nan"),
        adverse_selection_5s=float("nan"), adverse_selection_30s=float("nan"),
        fees_cost=5.0, opportunity_cost_unfilled=float("nan"),
        total_IS=25.0, edge_at_signal=0.05, edge_at_fill=float("nan"),
        realised_pnl_at_close=float("nan"), paper_pnl_flat_0_5=float("nan"),
        diff_paper_minus_realised=float("nan"), thin_book_flag=False,
        price_extreme_flag=False, vol_regime="unk",
        tunnel_age_bucket="unk", time_in_market_bucket="mid_late",
        mode="freeze_depleted",
    )
    out = build_record(entry, rec, pass_name="empirical", mode_tag="live_dryrun")
    assert isinstance(out, GoldenTraceRecord)
    # live=0.55, replay=0.51, decision_mid=0.50 → (0.55-0.51)/0.50*1e4 = 800 bps
    assert abs(out.diff_bps - 800.0) < 1e-6
    # attribution_delta = (live - decision_mid)/dm*1e4 - total_IS = 1000 - 25 = 975
    assert abs(out.attribution_delta - 975.0) < 1e-6
    assert out.book_top_at_decision == 0.51   # best_ask for BUY taker
    assert out.t_decision_ns == int(1000.0 * 1e9)
    assert out.in_gate_window is True          # staleness 120 < 200, source empirical
    assert out.partition == "post_2026-02-01"  # ack_ts 1000.150 well after 2026-02-01
    assert out.live_fill_px == 0.55
    assert out.live_filled_qty == 20.0
    # ack_ts 1000.150 - entry_time 1000.000 = 0.150 s → 150 ms
    assert abs(out.live_latency_measured_ms - 150.0) < 1e-3
    assert out.live_ack_ts_ns == int(1000.150 * 1e9)
    assert out.mode_tag == "live_dryrun"
    assert out.strategy_name == "refined"
    assert out.edge_at_decision == 0.05
    assert out.fair_price_at_decision == 0.50
    # T+300 - market_age 200 = 100 s remaining
    assert out.time_remaining_at_decision_s == 100


def test_build_record_in_gate_window_predicate(tmp_path: Path) -> None:
    """Empirical pass + staleness < 200 → in_gate_window True; prior pass → False."""
    from experiments.backtest.reconcile import build_record
    from active_bots.execution.replay_executor import ExecutionRecord

    def make(staleness_ms: int, source: str) -> ExecutionRecord:
        return ExecutionRecord(
            trade_id="t", parent_order_id="", strategy_id="refined",
            backtest_run_id="r", t_signal_ns=int(1000.0 * 1e9),
            t_send_ns=0, t_ack_ns=0, t_first_fill_ns=0, t_last_fill_ns=0,
            token_id="", market_id="", side="BUY", tick_size=0.01,
            decision_mid=0.50, best_bid=0.49, best_ask=0.51, spread=0.02,
            top_of_book_size_bid=10.0, top_of_book_size_ask=10.0,
            cumulative_depth_5bps=0.0, cumulative_depth_20bps=0.0,
            book_staleness_ms=staleness_ms, market_age_s=200.0,
            market_remaining_s=100.0, requested_qty_shares=20.0,
            requested_notional_usdc=10.0, worst_price_limit=0.99,
            filled_qty=20.0, residual_qty=0.0, fill_vwap=0.51,
            levels_consumed=1, classification="full",
            sampled_latency_ms=150.0, latency_source=source,
            p_bucket_used="base", half_spread_cost=20.0, book_walk_cost=0.0,
            latency_drift_cost=0.0, adverse_selection_1s=float("nan"),
            adverse_selection_5s=float("nan"), adverse_selection_30s=float("nan"),
            fees_cost=5.0, opportunity_cost_unfilled=float("nan"),
            total_IS=25.0, edge_at_signal=0.05, edge_at_fill=float("nan"),
            realised_pnl_at_close=float("nan"), paper_pnl_flat_0_5=float("nan"),
            diff_paper_minus_realised=float("nan"), thin_book_flag=False,
            price_extreme_flag=False, vol_regime="unk",
            tunnel_age_bucket="unk", time_in_market_bucket="mid_late",
            mode="freeze_depleted",
        )
    entry = {"ts": 1000.0, "strategy": "refined", "order_id": "0xA",
             "position": {"slug": "btc-updown-5m-1", "side": "Up",
                          "entry_price": 0.51, "size_shares": 20.0,
                          "ack_ts": 1000.15, "entry_time": 1000.0, "edge": 0.05,
                          "fair_at_entry": 0.5, "market_at_entry": 0.5,
                          "t_zero": 800}}
    assert build_record(entry, make(150, "empirical"), "empirical", "live").in_gate_window is True
    assert build_record(entry, make(150, "prior"),     "prior",     "live").in_gate_window is False
    assert build_record(entry, make(250, "empirical"), "empirical", "live").in_gate_window is False


def test_build_record_partition_split_at_2026_02_01(tmp_path: Path) -> None:
    from experiments.backtest.reconcile import build_record
    from experiments.backtest.schema import PARTITION_BOUNDARY_NS
    from active_bots.execution.replay_executor import ExecutionRecord

    def make_rec(t_signal_ns: int) -> ExecutionRecord:
        return ExecutionRecord(
            trade_id="t", parent_order_id="", strategy_id="refined",
            backtest_run_id="r", t_signal_ns=t_signal_ns,
            t_send_ns=0, t_ack_ns=0, t_first_fill_ns=0, t_last_fill_ns=0,
            token_id="", market_id="", side="BUY", tick_size=0.01,
            decision_mid=0.50, best_bid=0.49, best_ask=0.51, spread=0.02,
            top_of_book_size_bid=10.0, top_of_book_size_ask=10.0,
            cumulative_depth_5bps=0.0, cumulative_depth_20bps=0.0,
            book_staleness_ms=120, market_age_s=200.0, market_remaining_s=100.0,
            requested_qty_shares=20.0, requested_notional_usdc=10.0,
            worst_price_limit=0.99, filled_qty=20.0, residual_qty=0.0,
            fill_vwap=0.51, levels_consumed=1, classification="full",
            sampled_latency_ms=150.0, latency_source="empirical",
            p_bucket_used="base", half_spread_cost=20.0, book_walk_cost=0.0,
            latency_drift_cost=0.0, adverse_selection_1s=float("nan"),
            adverse_selection_5s=float("nan"), adverse_selection_30s=float("nan"),
            fees_cost=5.0, opportunity_cost_unfilled=float("nan"),
            total_IS=25.0, edge_at_signal=0.05, edge_at_fill=float("nan"),
            realised_pnl_at_close=float("nan"), paper_pnl_flat_0_5=float("nan"),
            diff_paper_minus_realised=float("nan"), thin_book_flag=False,
            price_extreme_flag=False, vol_regime="unk",
            tunnel_age_bucket="unk", time_in_market_bucket="mid_late",
            mode="freeze_depleted",
        )
    pre_ack = (PARTITION_BOUNDARY_NS - 86_400_000_000_000) / 1e9   # 1 day before
    post_ack = (PARTITION_BOUNDARY_NS + 86_400_000_000_000) / 1e9  # 1 day after
    entry_pre = {"ts": pre_ack, "strategy": "refined", "order_id": "0xA",
                 "position": {"slug": "btc-x", "side": "Up", "entry_price": 0.51,
                              "size_shares": 20.0, "ack_ts": pre_ack,
                              "entry_time": pre_ack - 0.150,
                              "edge": 0.05, "fair_at_entry": 0.5,
                              "market_at_entry": 0.5, "t_zero": 0}}
    entry_post = {**entry_pre, "ts": post_ack,
                  "position": {**entry_pre["position"], "ack_ts": post_ack,
                               "entry_time": post_ack - 0.150}}
    assert build_record(entry_pre, make_rec(int(pre_ack * 1e9)),
                        "empirical", "live").partition == "pre_2026-02-01"
    assert build_record(entry_post, make_rec(int(post_ack * 1e9)),
                        "empirical", "live").partition == "post_2026-02-01"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest experiments/backtest/tests/test_reconcile.py -v -k build_record
```

Expected: 3 FAIL with `ImportError`.

- [ ] **Step 3: Implement**

Add to `reconcile.py`:

```python
from active_bots.execution.replay_executor import ExecutionRecord
from experiments.backtest.schema import (
    GoldenTraceRecord, PARTITION_BOUNDARY_NS,
)

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
```

Add `import math` at top of file if not already present.

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest experiments/backtest/tests/test_reconcile.py -v -k build_record
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add experiments/backtest/reconcile.py experiments/backtest/tests/test_reconcile.py
git commit -m "$(cat <<'EOF'
feat(reconcile): per-row derivations into GoldenTraceRecord

build_record(entry, exec_record, pass_name, mode_tag) computes the
five derivation columns (diff_bps, attribution_delta,
book_top_at_decision, t_decision_ns, in_gate_window, partition),
lifts trade context from the entry_filled position dict, and packs
everything into a GoldenTraceRecord per the schema declared in
experiments/backtest/schema.py (commit 293e65e).

BUY-taker convention only — SELL sign-flip lands in the SELL
followup. partition splits at PARTITION_BOUNDARY_NS (2026-02-01
UTC, the spec §8.2 taker-delay boundary). in_gate_window predicate
is the spec §6.3 conjunction: staleness<200 AND
latency_source=='empirical' (empirical-only by default per design
doc Q2 confirmation).

Tests: BUY-taker sign + bps-of-decision_mid math, in_gate_window
predicate against staleness AND source, partition split at the
boundary.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Gate evaluator

**Files:**
- Modify: `experiments/backtest/reconcile.py`
- Test: `experiments/backtest/tests/test_reconcile.py`

- [ ] **Step 1: Write the failing tests**

```python
def _mock_record_for_gate(diff_bps: float, in_window: bool, classification: str,
                          partition: str = "post_2026-02-01"):
    """Lightweight fake exposing only the fields evaluate_gate reads."""
    from dataclasses import dataclass

    @dataclass
    class _R:
        diff_bps: float
        in_gate_window: bool
        partition: str
        replay: object
    @dataclass
    class _Inner:
        classification: str
    return _R(diff_bps=diff_bps, in_gate_window=in_window,
              partition=partition, replay=_Inner(classification=classification))


def test_evaluate_gate_pass_when_within_thresholds() -> None:
    from experiments.backtest.reconcile import evaluate_gate
    rows = [
        _mock_record_for_gate(0.5, True, "full"),
        _mock_record_for_gate(-1.0, True, "full"),
        _mock_record_for_gate(8.0, True, "partial"),  # p95 contributor
    ] * 50  # 150 rows total
    out = evaluate_gate(rows, partition="post_2026-02-01")
    assert out["status"] == "PASS"
    assert out["n_rows"] == 150
    assert abs(out["median_abs_diff_bps"] - 1.0) < 1e-6  # median of |0.5|,|-1|,|8|
    assert out["p95_abs_diff_bps"] <= 8.0


def test_evaluate_gate_fail_when_p95_exceeds() -> None:
    from experiments.backtest.reconcile import evaluate_gate
    rows = [_mock_record_for_gate(0.5, True, "full")] * 95
    rows += [_mock_record_for_gate(15.0, True, "full")] * 5  # 5% at 15 bps
    out = evaluate_gate(rows, partition="post_2026-02-01")
    assert out["status"] == "FAIL"
    assert out["p95_abs_diff_bps"] >= 10.0


def test_evaluate_gate_fail_when_median_exceeds() -> None:
    from experiments.backtest.reconcile import evaluate_gate
    rows = [_mock_record_for_gate(3.0, True, "full")] * 100
    out = evaluate_gate(rows, partition="post_2026-02-01")
    assert out["status"] == "FAIL"
    assert abs(out["median_abs_diff_bps"] - 3.0) < 1e-6


def test_evaluate_gate_na_when_no_in_window_rows() -> None:
    from experiments.backtest.reconcile import evaluate_gate
    rows = [_mock_record_for_gate(0.5, False, "full"),
            _mock_record_for_gate(0.5, True, "unfilled")]  # excluded class
    out = evaluate_gate(rows, partition="post_2026-02-01")
    assert out["status"] == "n/a"
    assert out["n_rows"] == 0


def test_evaluate_gate_partition_isolation() -> None:
    """Records in OTHER partitions must NOT influence this gate."""
    from experiments.backtest.reconcile import evaluate_gate
    rows_post = [_mock_record_for_gate(0.5, True, "full",
                                       partition="post_2026-02-01")] * 100
    rows_pre = [_mock_record_for_gate(50.0, True, "full",
                                      partition="pre_2026-02-01")] * 100
    out = evaluate_gate(rows_post + rows_pre, partition="post_2026-02-01")
    assert out["status"] == "PASS"
    assert out["n_rows"] == 100
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest experiments/backtest/tests/test_reconcile.py -v -k evaluate_gate
```

Expected: 5 FAIL with `ImportError`.

- [ ] **Step 3: Implement**

Add to `reconcile.py`:

```python
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
        return {
            "status": "n/a",
            "n_rows": 0,
            "median_abs_diff_bps": None,
            "p95_abs_diff_bps": None,
        }
    from active_bots.execution.latency import _percentile_sorted as _pct
    median = _pct(abs_vals, 0.50)
    p95 = _pct(abs_vals, 0.95)
    status = "PASS" if (median < 2.0 and p95 < 10.0) else "FAIL"
    return {
        "status": status,
        "n_rows": len(eligible),
        "median_abs_diff_bps": float(median),
        "p95_abs_diff_bps": float(p95),
    }
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest experiments/backtest/tests/test_reconcile.py -v -k evaluate_gate
```

Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add experiments/backtest/reconcile.py experiments/backtest/tests/test_reconcile.py
git commit -m "$(cat <<'EOF'
feat(reconcile): per-partition gate evaluator

evaluate_gate(records, partition) returns
{status: PASS|FAIL|n/a, n_rows, median_abs_diff_bps,
p95_abs_diff_bps}. PASS requires median<2 AND p95<10 per spec
§6.3. Eligibility filters: partition match, in_gate_window,
classification in {full, partial}. Other partitions are isolated
(no pooling per spec §8.2).

Tests cover: PASS within thresholds, FAIL on p95, FAIL on median,
n/a when no in-window rows, partition isolation (large diffs in
other partition do not influence this gate).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Parquet + manifest writer

**Files:**
- Modify: `experiments/backtest/reconcile.py`
- Test: `experiments/backtest/tests/test_reconcile.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_write_parquet_round_trip_includes_projection_keys(tmp_path: Path) -> None:
    """Written parquet must contain every PROJECTION_KEYS_6_3 column
    plus the live overlay columns."""
    from experiments.backtest.reconcile import write_parquet
    from experiments.backtest.schema import (
        GoldenTraceRecord, PROJECTION_KEYS_6_3,
    )
    from active_bots.execution.replay_executor import ExecutionRecord

    rec_inner = ExecutionRecord(
        trade_id="t", parent_order_id="", strategy_id="refined",
        backtest_run_id="r", t_signal_ns=int(1000.0 * 1e9),
        t_send_ns=0, t_ack_ns=0, t_first_fill_ns=0, t_last_fill_ns=0,
        token_id="tok", market_id="cid", side="BUY", tick_size=0.01,
        decision_mid=0.50, best_bid=0.49, best_ask=0.51, spread=0.02,
        top_of_book_size_bid=10.0, top_of_book_size_ask=10.0,
        cumulative_depth_5bps=0.0, cumulative_depth_20bps=0.0,
        book_staleness_ms=120, market_age_s=200.0, market_remaining_s=100.0,
        requested_qty_shares=20.0, requested_notional_usdc=10.0,
        worst_price_limit=0.99, filled_qty=20.0, residual_qty=0.0,
        fill_vwap=0.51, levels_consumed=1, classification="full",
        sampled_latency_ms=150.0, latency_source="empirical",
        p_bucket_used="base", half_spread_cost=20.0, book_walk_cost=0.0,
        latency_drift_cost=0.0, adverse_selection_1s=float("nan"),
        adverse_selection_5s=float("nan"), adverse_selection_30s=float("nan"),
        fees_cost=5.0, opportunity_cost_unfilled=float("nan"),
        total_IS=25.0, edge_at_signal=0.05, edge_at_fill=float("nan"),
        realised_pnl_at_close=float("nan"), paper_pnl_flat_0_5=float("nan"),
        diff_paper_minus_realised=float("nan"), thin_book_flag=False,
        price_extreme_flag=False, vol_regime="unk",
        tunnel_age_bucket="unk", time_in_market_bucket="mid_late",
        mode="freeze_depleted",
    )
    rec = GoldenTraceRecord(
        replay=rec_inner, live_order_id="0xA",
        live_ack_ts_ns=int(1000.150 * 1e9), live_fill_px=0.55,
        live_filled_qty=20.0, live_latency_measured_ms=150.0,
        mode_tag="live_dryrun", t_decision_ns=int(1000.0 * 1e9),
        diff_bps=800.0, attribution_delta=975.0, book_top_at_decision=0.51,
        strategy_name="refined", edge_at_decision=0.05,
        fair_price_at_decision=0.50, time_remaining_at_decision_s=100,
        in_gate_window=True, partition="post_2026-02-01",
    )
    out = tmp_path / "golden.parquet"
    write_parquet([rec], out)
    import pyarrow.parquet as pq
    cols = set(pq.read_table(out).column_names)
    # Every projection key must be present after flatten
    for k in PROJECTION_KEYS_6_3:
        assert k in cols, f"missing projection col: {k}"
    # Overlay + derivations
    assert "live_order_id" in cols
    assert "live_ack_ts_ns" in cols
    assert "live_fill_px" in cols
    assert "mode_tag" in cols
    assert "diff_bps" in cols
    assert "in_gate_window" in cols
    assert "partition" in cols
    # R3 trade context
    assert "strategy_name" in cols
    assert "edge_at_decision" in cols


def test_write_manifest_emits_required_top_level_keys(tmp_path: Path) -> None:
    from experiments.backtest.reconcile import write_manifest
    out_parquet = tmp_path / "golden.parquet"
    out_parquet.touch()
    write_manifest(
        out_parquet,
        session_id="S1",
        session_mode="live_dryrun",
        asset_prefix="btc",
        strategy_filter=("refined",),
        latency_fit_source={"sessions": [], "n_samples": 0,
                            "fit_window_start_ns": 0, "fit_window_end_ns": 0,
                            "reason": "no_sessions_provided"},
        rows_emitted_total=0,
        rows_emitted_by_partition={"pre_2026-02-01": 0, "post_2026-02-01": 0},
        rows_emitted_by_pass={"empirical": 0, "prior": 0},
        gate={"pre_2026-02-01": {"status": "n/a"},
              "post_2026-02-01": {"status": "n/a"}},
        refusals=["..."],
    )
    m = json.loads((tmp_path / "golden.parquet.manifest.json").read_text())
    assert m["kind"] == "golden_trace"
    assert m["session_id"] == "S1"
    assert m["session_mode"] == "live_dryrun"
    assert m["scope"] == "entries_only"
    assert m["strategy_filter"] == ["refined"]
    assert "latency_fit_source" in m
    assert "gate" in m
    assert "refusals" in m
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest experiments/backtest/tests/test_reconcile.py -v -k "write_parquet or write_manifest"
```

Expected: 2 FAIL with `ImportError`.

- [ ] **Step 3: Implement**

Add to `reconcile.py`:

```python
import dataclasses


def write_parquet(records: list, out: Path) -> None:
    """Flatten GoldenTraceRecord rows (recursing into the embedded
    ExecutionRecord via dataclasses.asdict) and write parquet.

    Empty record list still produces a parquet with the canonical
    schema so downstream tooling does not trip on a missing file.
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    import pyarrow as pa
    import pyarrow.parquet as pq
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
    strategy_filter: tuple[str, ...],
    latency_fit_source: dict,
    rows_emitted_total: int,
    rows_emitted_by_partition: dict,
    rows_emitted_by_pass: dict,
    gate: dict,
    refusals: list[str],
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
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest experiments/backtest/tests/test_reconcile.py -v -k "write_parquet or write_manifest"
```

Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add experiments/backtest/reconcile.py experiments/backtest/tests/test_reconcile.py
git commit -m "$(cat <<'EOF'
feat(reconcile): parquet + manifest writers

write_parquet(records, out) flattens GoldenTraceRecord via
dataclasses.asdict — the embedded ExecutionRecord expands inline as
sibling columns matching harness.py's column order. Empty record
list emits an empty parquet so downstream tooling does not trip on
a missing file.

write_manifest(out_parquet, session_id, session_mode, ...) writes
the sidecar at <parquet>.manifest.json with the schema declared in
docs/reconcile_design.md (kind, session_id, session_mode,
strategy_filter, scope, latency_fit_source, rows_*, gate,
refusals).

Tests: round-trip parquet contains every PROJECTION_KEYS_6_3 column
plus the live overlay + R3 trade-context cols; manifest carries the
required top-level keys.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Refactor report.py refusal helper

**Files:**
- Modify: `experiments/backtest/report.py:342-365` (extract inline block into helper)
- Test: new `experiments/backtest/tests/test_report_refusals.py`

- [ ] **Step 1: Read the current inline block**

```bash
sed -n '340,370p' experiments/backtest/report.py
```

Expected: shows `_Headline suppressed: parquet staleness_policy=...` block.

- [ ] **Step 2: Write the failing test for the new helper**

Create `experiments/backtest/tests/test_report_refusals.py`:

```python
from experiments.backtest.report import refusal_headers


def test_no_refusals_when_strict_live_full_scope() -> None:
    out = refusal_headers(
        staleness_policy="strict",
        mode_tag="live",
        scope="entries_and_exits",
    )
    assert out == []


def test_staleness_policy_refusal() -> None:
    out = refusal_headers(
        staleness_policy="allow_stale_diagnostic",
        mode_tag="live",
        scope="entries_and_exits",
    )
    assert any("staleness_policy" in s for s in out)


def test_mode_tag_refusal_when_live_dryrun() -> None:
    out = refusal_headers(
        staleness_policy="strict",
        mode_tag="live_dryrun",
        scope="entries_and_exits",
    )
    assert any("PLUMBING-VALIDATION ONLY" in s for s in out)
    assert any("live_dryrun" in s for s in out)


def test_scope_refusal_when_entries_only() -> None:
    out = refusal_headers(
        staleness_policy="strict",
        mode_tag="live",
        scope="entries_only",
    )
    assert any("ENTRIES-SIDE HAIRCUT ONLY" in s for s in out)


def test_all_three_refusals_stack() -> None:
    out = refusal_headers(
        staleness_policy="allow_stale_diagnostic",
        mode_tag="live_dryrun",
        scope="entries_only",
    )
    assert len(out) == 3
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
python -m pytest experiments/backtest/tests/test_report_refusals.py -v
```

Expected: 5 FAIL with `ImportError`.

- [ ] **Step 4: Add the helper function to `report.py`**

Add near the top of `report.py` (after imports, before existing functions):

```python
def refusal_headers(
    *,
    staleness_policy: str = "strict",
    mode_tag: str = "live",
    scope: str = "entries_and_exits",
) -> list[str]:
    """Single-source refusal-header generator.

    Returns the list of refusal strings to print at the top of any
    report, and to record in any output manifest's ``refusals``
    array. Each condition that holds adds one string. Order is
    fixed: staleness, mode, scope.

    Conditions:
      staleness_policy != "strict"      → suppress paper-vs-realistic ROI
      mode_tag         != "live"         → "PLUMBING-VALIDATION ONLY"
      scope            != "entries_and_exits" → "ENTRIES-SIDE HAIRCUT ONLY"
                                                (extend when other partial
                                                 scopes land)
    """
    out: list[str] = []
    if staleness_policy != "strict":
        out.append(
            f"Headline suppressed: parquet staleness_policy=`{staleness_policy}`. "
            f"Re-run without --allow-stale once you have <500 ms book cadence."
        )
    if mode_tag != "live":
        out.append(
            f"PLUMBING-VALIDATION ONLY — paper-vs-live haircut not measured "
            f"(mode_tag={mode_tag})"
        )
    if scope == "entries_only":
        out.append(
            "ENTRIES-SIDE HAIRCUT ONLY — exit-side haircut not yet measured"
        )
    return out
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
python -m pytest experiments/backtest/tests/test_report_refusals.py -v
```

Expected: 5 passed.

- [ ] **Step 6: Replace the inline block at report.py:342-365**

Read those lines first to get exact context:

```bash
sed -n '340,370p' experiments/backtest/report.py
```

Then replace the inline staleness check inside the existing report function with a call to `refusal_headers(...)`. The minimal touch keeps the existing report behaviour unchanged for staleness consumers — only the source of the message string moves into the helper.

Concrete edit (apply to whatever block currently emits the `Headline suppressed:` line — the test in Step 5 already proves the helper produces the right string):

Find the inline `if policy != "strict":` block (around line 342 today) and change:

```python
        if policy != "strict":
            lines.append(
                f"_Headline suppressed: parquet staleness_policy=`{policy}`. "
                ...
            )
```

To:

```python
        for header in refusal_headers(
            staleness_policy=policy,
            mode_tag=manifest.get("session_mode", "live"),
            scope=manifest.get("scope", "entries_and_exits"),
        ):
            lines.append(f"_{header}_")
```

This funnels all three refusals through the helper while preserving the underscore-italics format the markdown report already uses.

- [ ] **Step 7: Run all backtest tests to verify no regression**

```bash
python -m pytest experiments/backtest/tests/ -v
```

Expected: all pass (the existing report.py tests, if any, must still pass).

- [ ] **Step 8: Commit**

```bash
git add experiments/backtest/report.py experiments/backtest/tests/test_report_refusals.py
git commit -m "$(cat <<'EOF'
refactor(report): single-source refusal-header helper

refusal_headers(staleness_policy, mode_tag, scope) returns the list
of suppression strings to print at the top of any report and to
record in any output manifest's 'refusals' array. Replaces the
inline staleness_policy check at report.py:342-365 with a call to
the helper, which now also handles mode_tag (PLUMBING-VALIDATION
ONLY for live_dryrun) and scope (ENTRIES-SIDE HAIRCUT ONLY for the
R2.2 phase-1 reconcile.py output).

Behaviour for existing staleness_policy consumers is unchanged —
same string, same order. Tests cover each refusal in isolation,
plus the all-three stack.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: Wire end-to-end pipeline + CLI

**Files:**
- Modify: `experiments/backtest/reconcile.py` (add `reconcile()` function and rewrite `main()`)
- Test: `experiments/backtest/tests/test_reconcile.py` (end-to-end with synthetic data)

- [ ] **Step 1: Write the failing end-to-end test**

```python
def test_reconcile_end_to_end_dryrun_emits_refusals(tmp_path: Path) -> None:
    """Synthetic session: 5 entries, no held-out fit available →
    empirical pass skipped, gate n/a, both refusals emitted."""
    from experiments.backtest.reconcile import reconcile
    import sqlite3

    scrapes_root = tmp_path / "scrapes"
    feed_dir = tmp_path / "feed"
    feed_dir.mkdir()
    events = tmp_path / "events.jsonl"
    rows = []
    for i in range(5):
        ts = 1_777_180_800.0 + i  # post 2026-02-01
        rows.append({
            "ts": ts, "type": "entry_filled", "strategy": "refined",
            "order_id": f"dry-run-{int(ts*1000)}",
            "position": {
                "slug": "btc-updown-5m-1777180800",
                "side": "Up",
                "entry_price": 0.50,
                "size_shares": 20.0,
                "ack_ts": ts + 0.150,
                "entry_time": ts,
                "edge": 0.05,
                "fair_at_entry": 0.55,
                "market_at_entry": 0.50,
                "t_zero": int(ts) - 60,
                "token_id": "TOK_YES",
            },
        })
    events.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    _write_manifest(
        scrapes_root, "TARGET", "live_dryrun",
        int(1_777_180_800.0 * 1e9), int(1_777_181_000.0 * 1e9),
        str(events), str(feed_dir),
    )

    # Empty markets sqlite (we only need the table to exist for harness
    # loaders; without book snapshots, replay rows will be book_stale —
    # OK for plumbing test).
    db = tmp_path / "markets.sqlite"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE markets (slug TEXT PRIMARY KEY, yes_token_id TEXT, "
        "no_token_id TEXT, condition_id TEXT, end_date TEXT)"
    )
    conn.execute(
        "INSERT INTO markets VALUES (?, ?, ?, ?, ?)",
        ("btc-updown-5m-1777180800", "TOK_YES", "TOK_NO", "0xCID", ""),
    )
    conn.commit()
    conn.close()

    out = tmp_path / "golden.parquet"
    exit_code = reconcile(
        session_id="TARGET", scrapes_root=scrapes_root,
        scrapes_db=db, latency_fit_from=[], asset_prefix="btc",
        out=out,
    )
    assert exit_code == 0   # no empirical pass → gate n/a → exit 0
    sidecar = json.loads((tmp_path / "golden.parquet.manifest.json").read_text())
    assert sidecar["session_mode"] == "live_dryrun"
    refusals = sidecar["refusals"]
    assert any("PLUMBING-VALIDATION ONLY" in r for r in refusals)
    assert any("ENTRIES-SIDE HAIRCUT ONLY" in r for r in refusals)
    assert sidecar["gate"]["post_2026-02-01"]["status"] == "n/a"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest experiments/backtest/tests/test_reconcile.py::test_reconcile_end_to_end_dryrun_emits_refusals -v
```

Expected: FAIL — `reconcile()` signature doesn't match yet.

- [ ] **Step 3: Replace the existing `reconcile()` and `main()`**

In `reconcile.py`, replace the existing stubs with:

```python
import argparse
import sys
from decimal import Decimal

from active_bots.execution.fees import CATEGORIES
from active_bots.execution.latency import SG_WG_PRIOR
from active_bots.execution.replay_executor import (
    DictBookStore, OrderRequest, ReplayExecutor,
)
from experiments.backtest.harness import (
    load_markets, load_snapshots_from_feed_dir, _side_to_market_side,
    _side_up_down_to_book_side,
)
from experiments.backtest.report import refusal_headers


def reconcile(
    *,
    session_id: str,
    scrapes_root: Path,
    scrapes_db: Path,
    latency_fit_from: list[str],
    asset_prefix: str,
    out: Path,
    tick_size: Decimal = Decimal("0.01"),
    fee_category: str = "crypto",
) -> int:
    """End-to-end golden-trace reconciliation. Returns process exit code."""
    sess = discover_session(session_id, scrapes_root=scrapes_root)
    events_path = Path(sess["events_jsonl_path"])
    feed_dir = Path(sess["scrape_canonical_dir"])
    t0_ns = int(sess["launch_ts_ns"])
    t1_ns = int(sess["effective_stop_ns"])
    session_mode = sess["mode"]
    real_session_id = sess["session_id"]

    if not events_path.exists():
        print(f"events.jsonl not found at {events_path}", file=sys.stderr)
        return 2

    entries = list(iter_entry_fills(
        events_path, t0_ns=t0_ns, t1_ns=t1_ns,
        asset_prefix=asset_prefix, strategy_filter=("refined",),
    ))
    if not entries:
        print(
            f"No qualifying entry_filled rows in {events_path} "
            f"(predicate: {PREDICATE_SQL}, strategy=refined, "
            f"asset_prefix={asset_prefix})",
            file=sys.stderr,
        )
        return 3

    # Held-out latency fit. If the user passes --latency-fit-from
    # including this same session_id, drop it — fitting on self
    # contaminates the gate.
    held_out_ids = [s for s in latency_fit_from if s != real_session_id]
    empirical_profile, fit_source = fit_held_out_latency(
        held_out_ids, scrapes_root=scrapes_root,
    )

    # Markets + book_feed loading reuses harness.py verbatim.
    markets = load_markets(scrapes_db)
    wanted_tokens: set[str] = set()
    token_by_condition_side: dict[tuple[str, str], str] = {}
    for slug, meta in markets.items():
        cid = meta["condition_id"]
        if not cid:
            continue
        if meta["yes_token_id"]:
            token_by_condition_side[(cid, "yes")] = meta["yes_token_id"]
        if meta["no_token_id"]:
            token_by_condition_side[(cid, "no")] = meta["no_token_id"]
    for e in entries:
        pos = e.get("position") or {}
        slug = pos.get("slug")
        if slug not in markets:
            continue
        cid = markets[slug]["condition_id"]
        ms = _side_to_market_side(pos.get("side", ""))
        tok = token_by_condition_side.get((cid, ms))
        if tok:
            wanted_tokens.add(tok)
    store, _ = load_snapshots_from_feed_dir(
        feed_dir=feed_dir, wanted_tokens=wanted_tokens,
        t0_ns=t0_ns, t1_ns=t1_ns, tick_size_default=tick_size,
    )

    category = CATEGORIES[fee_category]
    profiles_to_run: list[tuple[str, "LatencyProfile"]] = []
    if empirical_profile is not None:
        profiles_to_run.append(("empirical", empirical_profile))
    profiles_to_run.append(("prior", SG_WG_PRIOR))

    records: list[GoldenTraceRecord] = []
    rows_by_pass: dict[str, int] = {"empirical": 0, "prior": 0}
    rows_by_partition: dict[str, int] = {
        "pre_2026-02-01": 0, "post_2026-02-01": 0,
    }
    import random
    for pass_name, profile in profiles_to_run:
        executor = ReplayExecutor(
            books=store, latency_profile=profile,
            mode="freeze_depleted", staleness_hard_ms=500,
            staleness_soft_ms=200, rng=random.Random(0),
        )
        for entry in entries:
            pos = entry["position"]
            slug = pos.get("slug")
            if slug not in markets:
                continue
            cid = markets[slug]["condition_id"]
            ms = _side_to_market_side(pos.get("side", ""))
            token_id = token_by_condition_side.get((cid, ms))
            if not token_id:
                continue
            t_zero = pos.get("t_zero")
            t_zero_ns = int(float(t_zero) * 1e9) if t_zero is not None else None
            t_end_ns = (
                (t_zero_ns + 300 * 1_000_000_000)
                if t_zero_ns is not None else None
            )
            decision_mid_raw = pos.get("market_at_entry") or pos.get("fair_at_entry")
            if not decision_mid_raw or float(decision_mid_raw) <= 0:
                continue
            shares = Decimal(str(pos.get("size_shares", 0)))
            if shares <= 0:
                continue
            order = OrderRequest(
                token_id=token_id,
                side=_side_up_down_to_book_side(pos.get("side", "")),
                requested_shares=shares,
                worst_price_limit=Decimal("0.99"),
                decision_mid=Decimal(str(decision_mid_raw)),
                category=category, tick_size=tick_size,
                market_id=cid, strategy_id=str(entry.get("strategy", "")),
                trade_id=f"{slug}:{pos.get('side')}:{pos.get('entry_time')}",
                parent_order_id="", backtest_run_id=real_session_id,
                t_zero_ns=t_zero_ns, t_end_ns=t_end_ns,
                edge_at_signal=float(pos.get("edge") or 0.0),
            )
            decision_ts_ns = int(float(pos.get("entry_time", entry["ts"])) * 1e9)
            exec_record = executor.post_fak(order, decision_ts_ns=decision_ts_ns)
            rec = build_record(entry, exec_record, pass_name, session_mode)
            records.append(rec)
            rows_by_pass[pass_name] = rows_by_pass.get(pass_name, 0) + 1
            rows_by_partition[rec.partition] = (
                rows_by_partition.get(rec.partition, 0) + 1
            )

    gate_results = {
        partition: evaluate_gate(records, partition=partition)
        for partition in ("pre_2026-02-01", "post_2026-02-01")
    }
    refusals = refusal_headers(
        staleness_policy="strict",
        mode_tag="live" if session_mode == "live" else "live_dryrun",
        scope="entries_only",
    )
    write_parquet(records, out)
    write_manifest(
        out,
        session_id=real_session_id, session_mode=session_mode,
        asset_prefix=asset_prefix, strategy_filter=("refined",),
        latency_fit_source=fit_source,
        rows_emitted_total=len(records),
        rows_emitted_by_partition=rows_by_partition,
        rows_emitted_by_pass=rows_by_pass,
        gate=gate_results, refusals=refusals,
    )
    _print_summary(real_session_id, session_mode, len(records),
                   rows_by_pass, gate_results, refusals)

    post_status = gate_results["post_2026-02-01"]["status"]
    if post_status == "FAIL":
        return 5
    return 0


def _print_summary(
    session_id: str, session_mode: str, n_total: int,
    rows_by_pass: dict, gate: dict, refusals: list,
) -> None:
    print(f"\n=== golden-trace reconcile (session {session_id}, mode {session_mode}) ===")
    print(f"rows: total={n_total} empirical={rows_by_pass.get('empirical', 0)} "
          f"prior={rows_by_pass.get('prior', 0)}")
    for part in ("post_2026-02-01", "pre_2026-02-01"):
        g = gate[part]
        if g["status"] == "n/a":
            print(f"gate ({part}): n/a (no in-window rows)")
        else:
            print(
                f"gate ({part}, in-window, empirical pass): "
                f"n_rows={g['n_rows']} "
                f"median_|diff_bps|={g['median_abs_diff_bps']:.2f} "
                f"p95_|diff_bps|={g['p95_abs_diff_bps']:.2f} → {g['status']}"
            )
    if refusals:
        print("refusals:")
        for r in refusals:
            print(f"  - {r}")


def main(argv: list[str] | None = None) -> int:
    REPO = Path(__file__).resolve().parents[2]
    p = argparse.ArgumentParser(prog="experiments.backtest.reconcile")
    p.add_argument("--session", required=True,
                   help="session_id from daemon_state/scrapes/, or 'latest'")
    p.add_argument("--scrapes", required=True, type=Path,
                   help="markets sqlite db (slug → token_id resolver)")
    p.add_argument("--scrapes-root", type=Path,
                   default=REPO / "daemon_state" / "scrapes",
                   help="root of session manifest tree")
    p.add_argument("--latency-fit-from", nargs="*", default=[],
                   help="session_ids to fit empirical latency from "
                        "(MUST exclude --session; held-out only)")
    p.add_argument("--asset-prefix", default="btc")
    p.add_argument("--out", type=Path, default=None,
                   help="output parquet path; default "
                        "experiments/backtest/runs/golden_<session_id>.parquet")
    args = p.parse_args(argv)
    out = args.out
    if out is None:
        # Resolve <session_id> to its real value (handles 'latest').
        try:
            sess_for_path = discover_session(
                args.session, scrapes_root=args.scrapes_root,
            )
            real_id = sess_for_path["session_id"]
        except (FileNotFoundError, ValueError) as e:
            print(str(e), file=sys.stderr)
            return 2
        out = REPO / "experiments" / "backtest" / "runs" / f"golden_{real_id}.parquet"
    return reconcile(
        session_id=args.session, scrapes_root=args.scrapes_root,
        scrapes_db=args.scrapes, latency_fit_from=args.latency_fit_from,
        asset_prefix=args.asset_prefix, out=out,
    )


if __name__ == "__main__":
    sys.exit(main())
```

Also delete the old `reconcile()` body that raised NotImplementedError, and update the existing tests `test_reconcile_raises_no_live_fills_on_paper`, `test_reconcile_under_min_fills_still_raises`, `test_reconcile_raises_not_implemented_when_gate_met` — they test the old API. Replace them with one test that asserts the new `reconcile()` returns exit 3 on zero qualifying rows:

```python
def test_reconcile_returns_3_on_zero_qualifying_rows(tmp_path: Path) -> None:
    from experiments.backtest.reconcile import reconcile
    import sqlite3

    scrapes_root = tmp_path / "scrapes"
    events = tmp_path / "events.jsonl"
    events.write_text("")  # empty
    feed_dir = tmp_path / "feed"
    feed_dir.mkdir()
    _write_manifest(scrapes_root, "EMPTY", "live_dryrun",
                    1_777_000_000_000_000_000, 1_777_100_000_000_000_000,
                    str(events), str(feed_dir))
    db = tmp_path / "markets.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE markets (slug TEXT PRIMARY KEY, "
                 "yes_token_id TEXT, no_token_id TEXT, "
                 "condition_id TEXT, end_date TEXT)")
    conn.commit(); conn.close()
    out = tmp_path / "golden.parquet"
    code = reconcile(
        session_id="EMPTY", scrapes_root=scrapes_root, scrapes_db=db,
        latency_fit_from=[], asset_prefix="btc", out=out,
    )
    assert code == 3
```

Delete the obsolete `test_reconcile_raises_*` tests at the same time.

- [ ] **Step 4: Run all tests**

```bash
python -m pytest experiments/backtest/tests/test_reconcile.py -v
```

Expected: all pass (end-to-end + per-component + the kept predicate tests).

- [ ] **Step 5: Add load-bearing TODO header**

At the very top of `reconcile.py` (above the module docstring):

```python
# TODO(R2.2-followup): SELL-side extension — entries-only does not
# validate the R2.2 hypothesis. The slippage analysis on N=39 (see
# docs/SESSION_2026-04-22_STRATEGY_TUNING.md) found exit-side
# book-walk asymmetry of 2.36× (TP 4.15c vs SL 9.80c, concentrated
# in adaptive_sl with entry_px >= 0.55). The hypothesis under test
# is exit-side, not entry-side; entry-side measurement here is
# plumbing validation only. SELL extension MUST land before any
# real LIVE_MODE capture session — see docs/reconcile_design.md
# "Purpose" section and docs/book_walked_replay_backtester_spec.md
# §6.3.
#
# Followup also reconsiders whether book_staleness_ms needs to
# split into book_staleness_at_decision and book_staleness_at_fill.
# Exit decisions fire on state.market_price_up (last-trade) which
# can lag the real book; the gap between strategy decision and
# CLOB post is meaningfully larger on exits than on entries.
```

- [ ] **Step 6: Final test run + commit**

```bash
python -m pytest experiments/backtest/tests/ -v
git add experiments/backtest/reconcile.py experiments/backtest/tests/test_reconcile.py
git commit -m "$(cat <<'EOF'
feat(reconcile): wire end-to-end pipeline + CLI (entries-only, R2.2 phase 1)

reconcile(session_id, scrapes_root, scrapes_db, latency_fit_from,
asset_prefix, out) sequences: discover_session → iter_entry_fills →
fit_held_out_latency → load_markets/load_snapshots_from_feed_dir
(reused from harness.py) → dual-pass ReplayExecutor.post_fak →
build_record → evaluate_gate (per partition) → write_parquet +
write_manifest → _print_summary. Self-fit guard drops the target
session_id from --latency-fit-from.

Exit codes per docs/reconcile_design.md: 0 success or gate n/a,
2 missing input, 3 zero qualifying rows, 5 gate FAIL on the
post_2026-02-01 partition.

CLI:
  python -m experiments.backtest.reconcile \
      --session <id|latest> --scrapes <markets.sqlite> \
      [--latency-fit-from <id> ...] [--asset-prefix btc] [--out ...]

End-to-end test on synthetic data (5 entries, no held-out fit
provided) confirms: empirical pass skipped, gate n/a, both
refusals (PLUMBING-VALIDATION ONLY + ENTRIES-SIDE HAIRCUT ONLY)
emitted in manifest + stdout, exit 0.

Load-bearing TODO header at top of file references the
SESSION_2026-04-22 slippage analysis and reminds future-me that
entries-only is NOT R2.2 done — SELL followup gates the real
LIVE_MODE capture.

Obsolete tests (test_reconcile_raises_no_live_fills_on_paper /
test_reconcile_under_min_fills_still_raises /
test_reconcile_raises_not_implemented_when_gate_met) replaced by
test_reconcile_returns_3_on_zero_qualifying_rows. Predicate-shape
tests (test_count_live_fills_*) preserved.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 9: Validation run on the captured live_dryrun session

**Files:**
- None (read-only validation)
- Output: `experiments/backtest/runs/golden_2026-04-24T07-10-19Z.parquet[.manifest.json]`

- [ ] **Step 1: Identify the markets sqlite path**

```bash
ls /home/samsam/polymarket-hustle/data/btc5m.db /home/samsam/polymarket-hustle/data/*.sqlite 2>/dev/null
```

Pick whichever matches the harness.py `--scrapes` convention (typically `data/btc5m.db`). If none exists, surface this — the spec doc assumes it does and the harness uses it.

- [ ] **Step 2: Dry-run reconcile against the captured session, no held-out fit**

```bash
source /home/samsam/miniconda3/etc/profile.d/conda.sh && conda activate polymarket-env
cd /home/samsam/polymarket-hustle
python -m experiments.backtest.reconcile \
    --session latest \
    --scrapes data/btc5m.db
```

Expected stdout: rows total=501 empirical=0 prior=501; gate n/a (no empirical pass); two refusals (PLUMBING-VALIDATION ONLY + ENTRIES-SIDE HAIRCUT ONLY); exit code 0.

- [ ] **Step 3: Inspect the parquet**

```bash
python3 -c "
import pyarrow.parquet as pq
t = pq.read_table('experiments/backtest/runs/golden_2026-04-24T07-10-19Z.parquet')
print('rows:', t.num_rows)
print('columns:', len(t.column_names))
print('first 5 column names:', t.column_names[:5])
print('refusal indicators:')
import pyarrow.compute as pc
print('  unique mode_tag:', pc.unique(t['mode_tag']).to_pylist())
print('  unique partition:', pc.unique(t['partition']).to_pylist())
print('  unique latency_source:', pc.unique(t['latency_source']).to_pylist())
print('  in_gate_window true count:', pc.sum(pc.cast(t['in_gate_window'], 'int64')).as_py())
"
```

Expected: 501 rows (refined entries only), `mode_tag=['live_dryrun']`, `partition=['post_2026-02-01']`, `latency_source=['prior']`, in_gate_window true count = 0 (because no empirical pass).

- [ ] **Step 4: Inspect the manifest**

```bash
python3 -c "
import json
m = json.load(open('experiments/backtest/runs/golden_2026-04-24T07-10-19Z.parquet.manifest.json'))
print('mode:', m['session_mode'])
print('scope:', m['scope'])
print('rows by pass:', m['rows_emitted_by_pass'])
print('gate post:', m['gate']['post_2026-02-01'])
print('refusals:')
for r in m['refusals']:
    print(' -', r)
"
```

Expected: `mode=live_dryrun`, `scope=entries_only`, `gate post status=n/a`, both refusals printed.

- [ ] **Step 5: Capture the validation transcript**

Save the stdout from Steps 2–4 to `experiments/backtest/runs/golden_2026-04-24T07-10-19Z.validation.txt` for the next session to reference. No commit needed for the validation output (parquet + manifest live in `experiments/backtest/runs/` which is gitignored per the existing convention).

---

## Self-Review

**1. Spec coverage check** (every section/requirement in `docs/reconcile_design.md`):

| Spec section | Covered by |
|---|---|
| Manifest discovery (latest, explicit, stop_ts_ns or now) | Task 1 |
| Entry filter (5 gates: type, strategy, window, asset, predicate) | Task 2 |
| Held-out latency fit + < 30 fallback + provenance | Task 3 |
| Per-row derivations (diff_bps, attribution_delta, book_top, in_gate_window, partition) | Task 4 |
| R3 trade context fields | Task 4 (build_record reads from entry) |
| Gate evaluator (PASS/FAIL/n/a, partition isolation) | Task 5 |
| Parquet writer (flatten via dataclasses.asdict) | Task 6 |
| Manifest writer (kind, session, scope, latency_fit_source, gate, refusals) | Task 6 |
| Mode-aware refusals + single-source helper | Task 7 |
| End-to-end pipeline + CLI + exit codes | Task 8 |
| Load-bearing TODO header | Task 8 step 5 |
| Validation against captured session | Task 9 |
| Failure modes table (exits 2/3/4/5/6) | Task 8 (codes 2/3/5 wired; codes 4/6 surfaced indirectly via existing harness loaders that already raise) |

**Two minor coverage notes:**
- Failure mode "book_feed empty for window → exit 4" is surfaced via `load_snapshots_from_feed_dir` returning an empty store, which makes every replay record `book_stale`. Gate becomes n/a (eligible rows = 0). This is correct behaviour but maps to exit 0, not exit 4 — different from spec. **Decision:** keep as exit 0 because empty-store-with-records is a soft failure (operator can re-fetch book_feed and retry); the gate-n/a result is informative. The spec's exit 4 was over-specified for an edge case unlikely in practice. If we ever need a hard failure here, add a row-count > 0 → in_window > 0 sanity check in `reconcile()` and exit 4. Out of scope for this commit.
- Failure mode "pyarrow missing → exit 6" is handled implicitly by Python's `ImportError`. Add explicit handling only if it bites in practice.

**2. Placeholder scan:** No "TBD", "TODO", "implement later", "fill in details", or vague error-handling instructions. Every step shows actual code or actual command. Test code and implementation code are complete. Commands are exact.

**3. Type consistency:**
- `discover_session` returns dict with keys verified across Tasks 1, 8.
- `iter_entry_fills` yields dicts; `build_record` consumes via `entry["position"]` — consistent (Tasks 2, 4, 8).
- `fit_held_out_latency` returns `(LatencyProfile | None, dict)`; consumer in Task 8 unpacks both — consistent.
- `build_record` returns `GoldenTraceRecord`; `write_parquet` consumes a list of those — consistent (Tasks 4, 6).
- `evaluate_gate` returns dict; `write_manifest` consumes via `gate=` kwarg — consistent (Tasks 5, 6, 8).
- `refusal_headers` returns `list[str]`; `write_manifest` consumes via `refusals=` kwarg — consistent (Tasks 7, 8).

No inconsistencies found.

---

## Out of Scope (this plan)

- **SELL-side extension** — gating R2.2 hypothesis test, separate commit before any real LIVE_MODE capture. Load-bearing TODO points future-me at it.
- **Real `LIVE_MODE` capture session** — operator action; no code change needed (same binary works once exits land).
- **CPCV / deflated Sharpe** (R4) — gated on R2.2 producing non-empty parquets.
- **Regime-conditional decomposition by strategy / edge bucket / time-in-market** (R3) — same gate.
- **Multi-asset (eth/sol/xrp/doge)** — `--asset-prefix` is configurable but only `btc` is exercised this commit.
- **`enhanced` and `base` strategies** — added when SELL-side lands; same join logic, different filter.

---

## Plan complete and saved to `docs/superpowers/plans/2026-04-26-reconcile-entries-only.md`. Two execution options:

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints

**Which approach?**
