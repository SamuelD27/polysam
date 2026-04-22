# Book-walked replay backtester

Pure-Python stdlib-first replay harness for Polymarket FAK (IOC) BTC 5-min
markets. Implements the spec
`Book-Walked_Replay_Backtester_for_Polymarket_FAK_BTC_5-Minute_Markets__Design_Specification_and_Paper-to-Live.md`
(§1.3 freeze-last-book, §2.1 FAK semantics, §2.2 ROUNDING_CONFIG,
§2.3 bell-curve fees, §2.4 book-walk + #3221 overfill, §3 latency injection,
§4.2 freeze_depleted + snap_back, §5.2 IS decomposition, §6.2 invariants,
§7 per-trade schema).

## Files

```
active_bots/execution/
    book.py               immutable L2 Book + walk_book + freeze_last_book
    fees.py               Polymarket intl bell-curve + US flat-theta (unused)
    latency.py            LatencyProfile + SG_WG_PRIOR + log-normal sampler
    invariants.py         §6.2 assertions (stateless callables)
    replay_executor.py    ReplayExecutor.post_fak -> §7 ExecutionRecord

experiments/backtest/
    harness.py            CLI: events.jsonl + sqlite -> parquet
    attribution.py        regime-conditional median-bps markdown table
    report.py             single-page paper-vs-realised report
    tests/test_smoke.py   end-to-end synthetic fixture test + skipif real-data test
    runs/                 parquet outputs (gitignored)
```

## Typical run

```bash
conda activate polymarket-env
cd /home/samsam/polymarket-hustle

python -m experiments.backtest.harness \
    --events daemon_state/events.jsonl \
    --scrapes data/btc5m.db \
    --out experiments/backtest/runs/dryrun_01.parquet \
    --window '2026-04-22T20:00Z..2026-04-22T22:00Z' \
    --tick-size 0.01 \
    --latency-profile sg_wg_prior \
    --fee-category crypto \
    --mode both

python -m experiments.backtest.report \
    --in experiments/backtest/runs/dryrun_01.freeze_depleted.parquet
```

`--mode both` writes two parquets (`.freeze_depleted.parquet` +
`.snap_back.parquet`) so §4.2 bounds are both visible.

## Preconditions for a useful dry-run

The harness needs **time-overlap** between two on-disk sources:

1. `daemon_state/events.jsonl` containing `entry_filled` + `exit_filled`
   rows in the window (daemon was running in paper or live mode during
   that wall-clock period).
2. `data/btc5m.db` containing `orderbooks` and `markets` rows for the
   same 5-min market slugs that the entries reference.

As of 2026-04-22 this repo has:

- events.jsonl covering 2026-04-22T01:10..09:44Z (8.5 h)
- orderbook_monitor.py restarted 2026-04-22T19:27Z with `--interval 30`

These windows do NOT overlap. A successful Phase-4 dry-run requires
restarting the paper daemon concurrently with the scraper so both sources
grow into the same wall-clock window. Suggested workflow:

```bash
# 1. scraper is already running (PID captured in data/orderbook_monitor.log)
# 2. restart the paper daemon
bash launch_daemon.sh   # or the usual entry point

# 3. let both run for >=2 h
# 4. run the harness over the overlap window
python -m experiments.backtest.harness ...
```

## Snapshot cadence warning (spec §8.1.2)

The scraper runs at 30-second interval. Spec §1.2 requires >=10 Hz
snapshots for a sub-1s decision horizon. At 30 s cadence, the
`book_staleness_ms` flag on most fills will be in the hundreds of
milliseconds to tens of seconds, frequently exceeding the
`staleness_hard_ms=500` threshold. Those fills are classified
`book_stale` and have NaN cost attribution. This is intentional: the
spec says the right fix is WebSocket `book` / `price_change` delta
capture (§1.2, day 5 of §8 priority list), which is out of scope for
this backtester MVP.

## Tick size

`--tick-size` is required with no implicit default. The CLI accepts one
of `0.1 / 0.01 / 0.001 / 0.0001` per spec §2.2 `ROUNDING_CONFIG`. The
harness warns if any observed best_bid < 0.02 or best_ask > 0.98 with
`--tick-size 0.01` (would imply the real market ran 0.001/0.0001 tick
near extremes and the walk cannot represent that honestly).

## Running the tests

```bash
conda activate polymarket-env
cd /home/samsam/polymarket-hustle
python -m pytest -q tests/execution/ experiments/backtest/tests/
```

Expected: 54 passed, 1 skipped (the skip is
`test_smoke_real_overlap_if_present` auto-enabling once real overlap
exists).

## Deferred (not MVP scope)

- §1.2 WebSocket `book` / `price_change` delta capture wired into
  scrape.py.
- §3.1 empirical latency fit from `ack_ts` (events.jsonl does not log
  ack timestamps today; `fit_from_events_jsonl` raises `NotFitted` and
  harness falls back to `SG_WG_PRIOR`).
- §5.2 `adverse_selection_{1,5,30}s` — requires post-fill snapshots at
  those offsets; with 30-s scrape cadence only the 30 s column is
  populatable and only rarely. Columns are emitted as NaN in the
  meantime.
- §6.3 golden-trace reconciliation vs live fills (no live fills on
  record today — daemon is paper-mode).
- §6.4 CPCV / deflated Sharpe reporting.
- Fit a 4-point quantile-inversion latency sampler (current one fits
  log-normal from p50/p95 only, so p99/p999 in the profile are
  reference values rather than fit targets — slightly optimistic in
  the tail).
