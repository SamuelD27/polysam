# `reconcile.py` design — entries-only, R2.2 phase 1

**Status:** design contract for the next implementation commit. **Reviewable
in <10 minutes.** Spec references are to
`docs/book_walked_replay_backtester_spec.md`. Schema is already declared in
`experiments/backtest/schema.py`.

## Purpose

Score the book-walked replay (`ReplayExecutor`) against captured live-shaped
fills, per spec §6.3. This commit covers **entries only**. SELL-side
reconciliation is the gating followup before any real `LIVE_MODE` capture
because the slippage hypothesis lives there (TP 4.15¢ vs SL 9.80¢ asymmetry
on N=39, concentrated in `adaptive_sl` with `entry_px ≥ 0.55`); entries-only
validates plumbing only, **not the strategy hypothesis**.

The first run target is the captured `live_dryrun` session, which produces a
trivially-passing gate (both sides are paper-quality math wearing different
metadata) and exercises every line of join / parquet / manifest / gate /
report code. The same binary then runs against real `LIVE_MODE` fills with
no code change.

## Inputs

| Input | Source | Required |
|---|---|---|
| Session manifest | `daemon_state/scrapes/<session_id>/manifest.json` (or `latest` resolves to most-recent with `mode ∈ {live, live_dryrun}`) | yes |
| `events.jsonl` | path read from manifest's `events_jsonl_path` | yes |
| `book_feed` | path read from manifest's `scrape_canonical_dir` | yes |
| Markets sqlite | `--scrapes` (slug → token_id resolver, same as harness.py) | yes |
| Held-out fit sources | `--latency-fit-from <session_id_a> [<session_id_b> ...]` — list of OTHER session manifests whose `entry_filled` rows feed `latency.fit_from_events_jsonl` | optional; if absent / insufficient, fall back to prior-only |
| Latency prior | `sg_wg_prior` — fixed for both passes | implicit |

**Why held-out fit:** fitting the empirical latency profile on the same
session you're reconciling makes the gate self-fulfilling — the profile is
calibrated to reproduce exactly the latencies under test. Held-out fit
restores the gate's discriminative power. Fitted profile and source
sessions land in the manifest under `latency_fit_source`.

## Pipeline

```
manifest discovery
  → derive (events_path, feed_dir, t0_ns, t1_ns)
entry filter
  → events.jsonl rows where:
       type == "entry_filled"
       AND strategy == "refined"           # other strategies excluded; see TODO
       AND ts in [launch_ts_ns, stop_ts_ns or now]
       AND predicate met (order_id + ack_ts + entry_price)
       AND slug starts with asset_prefix   # default "btc"
held-out latency fit
  → if --latency-fit-from sessions provided:
        empirical = fit_from_events_jsonl(union of those events.jsonl)
        require n_samples >= 30  (minimum for stable p95; spec §3.1)
        if < 30, log "insufficient held-out latency data" and skip empirical pass
  → else: skip empirical pass
dual-pass replay (per entry)
  → pass 1 ("empirical"): ReplayExecutor with held-out empirical profile  [if available]
  → pass 2 ("prior"):     ReplayExecutor with sg_wg_prior
  → both passes use the SAME book snapshot at the SAME decision_ts_ns
join (per row)
  → GoldenTraceRecord: replay (from pass), live overlay (from event),
                       derivations (computed below), trade context
                       (from event), gate inputs (from row)
derivations
  → diff_bps         = (live_fill_px − replay.fill_vwap) / decision_mid × 10_000
                       BUY taker convention: positive value = live overpaid relative
                       to the book-walked replay; bps are of decision_mid, matching
                       replay_executor._bps_of. SELL sign-flip lands in the SELL followup.
  → attribution_delta= (live_fill_px − decision_mid) × 10_000 / decision_mid − replay.total_IS
  → book_top_at_decision = replay.best_ask  (BUY taker; SELL flip in followup)
  → t_decision_ns    = replay.t_signal_ns
  → in_gate_window   = (replay.book_staleness_ms < 200)
                       AND (replay.latency_source == "empirical")
  → partition        = "pre_2026-02-01" if live_ack_ts_ns < PARTITION_BOUNDARY_NS
                       else "post_2026-02-01"
gate evaluation
  → for each partition independently (no pooling, spec §8.2):
        gate_rows = rows where in_gate_window AND classification ∈ {full, partial}
        if len(gate_rows) == 0: gate_status = "n/a (no in-window rows)"
        else:
            median_abs = median(|gate_rows.diff_bps|)
            p95_abs    = percentile(|gate_rows.diff_bps|, 95)
            gate_status = "PASS" if median_abs < 2 AND p95_abs < 10 else "FAIL"
parquet + manifest write
  → flatten via dataclasses.asdict (replay embed expands inline)
  → write golden_trace.parquet + golden_trace.parquet.manifest.json
report
  → stdout: per-partition row count, gate inputs, gate status,
            mode-aware refusals (see below)
```

## Outputs

```
experiments/backtest/runs/golden_<session_id>.parquet
experiments/backtest/runs/golden_<session_id>.parquet.manifest.json
```

Path matches the existing `dryrun_*.parquet` convention in
`experiments/backtest/runs/` — same directory, same `.manifest.json`
sidecar pattern as `harness.py`.

**Manifest extras** beyond the harness manifest baseline:

```json
{
  "kind": "golden_trace",
  "session_id": "...",
  "session_mode": "live" | "live_dryrun",
  "asset_prefix": "btc",
  "strategy_filter": ["refined"],
  "scope": "entries_only",
  "latency_fit_source": {
    "sessions": ["<session_id_a>", "..."],
    "n_samples": 0,
    "fit_window_start_ns": 0,
    "fit_window_end_ns": 0,
    "profile": {"p50_ms": 0.0, "p95_ms": 0.0, "p99_ms": 0.0, "p999_ms": 0.0}
  } | null,
  "rows_emitted_total": 0,
  "rows_emitted_by_partition": {"pre_2026-02-01": 0, "post_2026-02-01": 0},
  "rows_emitted_by_pass": {"empirical": 0, "prior": 0},
  "gate": {
    "pre_2026-02-01":  {"status": "PASS"|"FAIL"|"n/a", "median_abs_diff_bps": ..., "p95_abs_diff_bps": ..., "n_rows": ...},
    "post_2026-02-01": {"status": "PASS"|"FAIL"|"n/a", "median_abs_diff_bps": ..., "p95_abs_diff_bps": ..., "n_rows": ...}
  },
  "refusals": ["..."]      // strings explaining any headline refusal — see below
}
```

**Stdout summary** (one screen, mirrors `harness._print_summary` style):

```
=== golden-trace reconcile (session <id>, mode <live|live_dryrun>) ===
rows: total=N empirical=M prior=K
gate (post_2026-02-01, in-window, empirical pass):
  n_rows=R  median_|diff_bps|=X.XX  p95_|diff_bps|=Y.YY  → PASS|FAIL
gate (pre_2026-02-01): n/a (no in-window rows)
refusals:
  - PLUMBING-VALIDATION ONLY — paper-vs-live haircut not measured (mode_tag=live_dryrun)
  - ENTRIES-SIDE HAIRCUT ONLY — exit-side haircut not yet measured (scope=entries_only)
```

## Mode-aware refusals

`report.py` (the existing report-generator that `reconcile.py` invokes /
prints alongside) **MUST** print these refusal headers when conditions
hold; both refusals are recorded in the manifest's `refusals` array.

| Condition | Refusal string |
|---|---|
| `session_mode != "live"` | `"PLUMBING-VALIDATION ONLY — paper-vs-live haircut not measured (mode_tag=<X>)"` |
| `scope == "entries_only"` | `"ENTRIES-SIDE HAIRCUT ONLY — exit-side haircut not yet measured"` |

Same pattern as the existing `staleness_policy != strict` headline
suppression in `report.py` (inline at lines 342–365 today). The
implementation extracts that inline block into a named helper that
both `staleness_policy` and the new `mode_tag` / `scope` refusals
funnel through, so the suppression logic stays single-sourced.

## Failure modes

| Mode | Behaviour |
|---|---|
| Manifest missing / unreadable | exit 2, print path that was tried |
| `events.jsonl` missing | exit 2 |
| Zero qualifying entry_filled rows | exit 3, print predicate that excluded everything |
| `book_feed` directory empty for window | exit 4, print expected token IDs + window bounds |
| Held-out fit source has < 30 samples | warn, skip empirical pass, gate emits `"n/a (no empirical pass)"` |
| Held-out fit source not provided | same as above |
| Gate FAIL on post-2026-02-01 partition | exit 5 (CI-signal); parquet + manifest still written |
| Gate FAIL on pre-2026-02-01 partition | warn; do not exit non-zero — pre-Feb-2026 latency regime is no longer the production target (the 500 ms taker delay was removed); a failure there is informational, not a blocker for current trading |
| pyarrow missing | exit 6 with install hint |

Exit codes mirror `harness.py`'s pattern. Non-fatal warnings go to stderr.

## CLI

```
python -m experiments.backtest.reconcile \
    --session <session_id|latest> \
    --scrapes <markets.sqlite> \
    [--latency-fit-from <session_id> [<session_id> ...]] \
    [--asset-prefix btc] \
    [--out experiments/backtest/runs/golden_<session_id>.parquet]
```

`--out` defaults to the path shown above so the standard invocation is just
`--session latest --scrapes data/btc5m.db [--latency-fit-from <id>]`.

## Test plan

`experiments/backtest/tests/test_reconcile.py` already has gate-stub tests.
Extend with:

| Test | Asserts |
|---|---|
| `test_manifest_discovery_latest` | `--session latest` resolves to the most-recent `live`/`live_dryrun` manifest |
| `test_entry_filter_excludes_non_refined` | `enhanced` and `base` rows are dropped |
| `test_entry_filter_excludes_paper_predicate_failures` | rows missing `order_id`/`ack_ts`/`entry_price` are dropped |
| `test_partition_boundary_split` | a synthetic event before and after `PARTITION_BOUNDARY_NS` lands in the right partition |
| `test_gate_predicate_empirical_only` | `prior` pass rows have `in_gate_window=False` even with `book_staleness_ms<200` |
| `test_gate_pass_when_diff_within_thresholds` | synthetic rows with median |diff|=0.5 / p95=8 → PASS |
| `test_gate_fail_when_p95_exceeds` | synthetic rows with median|diff|=0.5 / p95=12 → FAIL, exit code 5 |
| `test_gate_na_when_no_empirical_pass` | no `--latency-fit-from` → gate status `"n/a"`, exit code 0 |
| `test_held_out_fit_below_threshold` | < 30 samples → empirical pass skipped, refusal recorded |
| `test_dryrun_emits_refusal` | `mode_tag=live_dryrun` → both refusals in manifest + stdout |
| `test_diff_bps_sign_convention` | live_fill_px > replay.fill_vwap → positive diff_bps for BUY taker |
| `test_parquet_round_trip` | written parquet's columns ⊇ `PROJECTION_KEYS_6_3` ∪ overlay keys |

Existing stub tests (`test_count_live_fills_*`, `test_reconcile_raises_*`)
stay; their stub-only behaviour is replaced by real implementation but the
predicate semantics they encode are still asserted.

## File-level TODO header

The implementation commit places this at the top of `reconcile.py` (the
"load-bearing TODO" already specified):

```python
# TODO(R2.2-followup): SELL-side extension — entries-only does not
# validate the R2.2 hypothesis. The slippage analysis on N=39
# (commit <hash>, see docs/SESSION_2026-04-22_STRATEGY_TUNING.md
# or the slippage report) found exit-side book-walk asymmetry of
# 2.36× (TP 4.15¢ vs SL 9.80¢, concentrated in adaptive_sl with
# entry_px >= 0.55). The hypothesis under test is exit-side, not
# entry-side; entry-side measurement here is plumbing validation
# only. SELL extension MUST land before any real LIVE_MODE capture
# session — see docs/reconcile_design.md "Purpose" section and
# docs/book_walked_replay_backtester_spec.md §6.3.
#
# Followup also reconsiders whether book_staleness_ms needs to
# split into book_staleness_at_decision and book_staleness_at_fill.
# Exit decisions fire on state.market_price_up (last-trade) which
# can lag the real book; the gap between strategy decision and
# CLOB post is meaningfully larger on exits than on entries.
```

The `<hash>` placeholder is filled by the implementation commit pointing
at the slippage-analysis commit on Sam-Dev.

## Out of scope (this commit)

- SELL-side reconciliation (followup before real LIVE_MODE capture)
- `enhanced` and `base` strategies (added when SELL-side lands; same join
  logic, different filter)
- CPCV / deflated Sharpe (R4)
- Regime-conditional decomposition by strategy / edge bucket /
  time-in-market (R3, after R2.2 produces a non-empty parquet)
- Multi-asset (eth/sol/xrp/doge) — `--asset-prefix btc` hardcoded for now;
  the same flag already exists in `harness.py`
