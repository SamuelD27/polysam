# Replay mode — operator guide

## Plain-language summary

This document is the operator handbook for the in-memory replay mode
shipped on `feat/realistic-paper-and-replay` (May 2026). Replay mode
loads a captured live-dryrun session from disk, runs the production
strategy code through the modular orchestrator as fast as the machine
will go, and writes a `replay_summary.json` with per-strategy PnL,
trade count, win rate, and the fill-realism tax. Wall-clock target is
under 60 seconds for a 12-hour capture; R2.2 (the May 5, 2026 capture)
runs in ~32 seconds on the GX10. **Important caveat:** the R2.2
capture's `book_feed/` is one-sided (a known pre-existing scraper bug,
see `MEMORY.md`), so end-to-end replay against R2.2 produces zero
entries — the infrastructure works, but the captured book data is
inadequate for fidelity replay. Future captures from a fixed scraper
will exercise the full pipeline.

## 1. Quick start

```bash
# Build a config (paths must be absolute; the CLI runs from any cwd)
cat > /tmp/replay_config.json <<'EOF'
{
  "mode": "main",
  "main_strategy": "walked_vwap",
  "benchmarks": ["refined", "enhanced", "base"],
  "execution_mode": "replay",
  "replay_session": "/home/samsam/polymarket-hustle/daemon_state/scrapes/2026-05-05T12-50-04Z",
  "params": {"max_trade_size_usdc": 5.0, "portfolio_size_usdc": 10}
}
EOF

# PORTFOLIO_SIZE_USDC=10 takes the env override path in
# daemon_base_v1.compute_effective_max_risk so the run does not need a
# Polygon RPC connection.
PORTFOLIO_SIZE_USDC=10 python -m polyhustle.cli --config /tmp/replay_config.json
```

The summary is written to `<replay_session>/replay_summary.json` (or
`./replay_summary.json` if the session path is not a directory).

## 2. `replay_summary.json` schema

```json
{
  "session_id": "2026-05-05T12-50-04Z",
  "runtime_seconds": 32.22,
  "tick_count": 27766,
  "by_strategy": {
    "walked_vwap": {
      "n_trades": 63,
      "pnl_total": -12.41,
      "pnl_mid_total": 51.41,
      "win_rate": 0.5238,
      "exit_types": {"TP": 34, "SL": 29},
      "fill_realism_tax": 63.82
    },
    "refined": {
      "n_trades": 121,
      "pnl_total": 259.71,
      "pnl_mid_total": null,
      "win_rate": 0.6364,
      "exit_types": {"TP": 80, "SL": 36, "RESOLUTION": 5},
      "fill_realism_tax": null
    }
  }
}
```

`pnl_mid_total` and `fill_realism_tax` are populated only for strategies
whose entries went through the walked-VWAP gate (so they have both a
`pnl` and a `pnl_mid`). Other strategies report `null`.

## 3. Flag sweeps

The intended workflow for the upcoming `tune/quant-defaults` sweep:

```bash
# Sweep MIN_TOP_OF_BOOK_SHARES_RATIO ∈ {0.5, 1.0, 2.0}
for ratio in 0.5 1.0 2.0; do
  WALKED_VWAP_MIN_TOP_RATIO=$ratio \
    PORTFOLIO_SIZE_USDC=10 \
    python -m polyhustle.cli --config /tmp/replay_config.json
  cp /home/samsam/polymarket-hustle/daemon_state/scrapes/2026-05-05T12-50-04Z/replay_summary.json \
     /tmp/sweep_ratio_${ratio}.json
done
```

Then aggregate the summaries with whatever Python scripting is
appropriate. The flat JSON shape is meant to be `json.load`-friendly
for pandas-flavoured analysis.

## 4. Performance

Achieved: **32 seconds** end-to-end on R2.2 (12-hour, ~144 markets,
123 MB compressed book_feed) on the GX10 with the optimisations applied
in commit `5b56ea3`:

- Filename-window filter on `book_feed/`: skip files whose 5-minute
  market window does not overlap the session window (the scraper's
  rotation can leave many out-of-window files in the dir).
- Pre-built bisect ts caches for btc / rtds / books / market lookups
  so per-tick cost is O(log n) without per-call list rebuilds.
- `_active_market` promoted from linear scan to bisect.

The slow-marker test
`tests/orchestrator/test_replay_mode.py::test_replay_r22_wall_clock_under_target`
asserts `< 60s` and is skipped when the capture is not on disk.

Memory peak during R2.2 load: ~9.5 GB RSS (well within the GX10's
128 GB unified memory). Most of the cost lives in
`_fold_into_market_books` (per-snapshot LiveBookState copies) and
`json.raw_decode`. Further gains would come from snapshot-coalescing —
out of scope while the 60s target holds.

## 5. Known limitation — R2.2 book_feed is one-sided (pre-existing scraper bug)

The R2.2 capture's `book_feed/` has a known issue:

> **Of the 144 markets, only 1-2 have YES books with both bids and
> asks populated.** Most snapshots are one-sided: YES carries only
> bids, NO carries only asks.

The walked-VWAP entry gate (and the new realistic-fills wrapper in
`PaperTrader`) require both sides on the side they walk:
- For an Up entry: YES.asks (sellers offering YES).
- For a Down entry: NO.asks (sellers offering NO).

Almost every captured snapshot is missing one of these sides, so the
replay's strategies emit ENTERs that paper_no_fill on the book walk.
The `replay_summary.json` for the R2.2 run reports
`tick_count=27766, by_strategy={}` — zero entries materialise.

This is **not a bug in the replay infrastructure**. It is a separate
preexisting issue in the daemon's scraper. Cross-reference:
- `MEMORY.md` entry "Scraper book_feed empty-bids bug" — the issue is
  documented and was deferred from the walked-VWAP work.

What still works against R2.2:
- The fill-realism tax reproduction
  (`tests/execution/test_paper_trader_fill_realism.py::test_paper_trader_reproduces_r22_attribution_within_10pct`)
  — operates directly on `events.jsonl` and confirms the captured
  walked_vwap entries' (entry_price - entry_price_mid) × shares sum to
  the documented $63.82 ±10%.
- Replay infrastructure perf assertion (32s).

What needs a re-captured session before it works against the live
strategies:
- End-to-end PnL reproduction (target: walked_vwap pnl_total ≈ -$12.41
  ±10%).
- End-to-end trade-count reproduction (target: walked_vwap n_trades ≈
  63 ±5%).

## 6. Limitations (general)

- **Latency injection in v1** stamps `paper_latency_ms` on `fill_details`
  but does NOT time-shift the book lookup. The book actually walked is
  the snapshot at `t_decision`, not `t_decision + L`. A future pass can
  swap in a `books_at(t)` callable from the data provider.
- **Partial fills are not simulated** — book-too-thin → `paper_no_fill`.
  The walked-VWAP gate's `WALKED_VWAP_PARTIAL_OK=0` default already
  enforces this on its strategies; we mirror at the wrapper layer.
- **The replay stream is 1 Hz**; finer cadence does not change strategy
  decisions at the current state contract.
- **`sigma`** is currently a placeholder (0.0) in the replay stream. The
  daemon's live EWMA-published sigma was not separately persisted in
  events.jsonl. With sigma=0 the fair-price model returns 0.0 and
  `compute_edge` reports edge=0.5, side=Down for any mid≠0.5 — strategies
  fire on every eligible tick, so this affects fidelity but not
  infrastructure correctness. A follow-up can rebuild sigma from the
  BTC tape via the same EWMA the daemon uses.
