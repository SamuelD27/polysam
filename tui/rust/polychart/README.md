# polychart — IN DEVELOPMENT

Not the default TUI. The Python dashboard at `tui/python/dashboard.py` is
the production path. Opt in with `DASHBOARD=rust ./launch_daemon.sh`.

---

## Build

```bash
cd tui && cargo build --release
```

Binary lands at `tui/target/release/polychart`. `launch_daemon.sh`
expects it there when `DASHBOARD=rust` is set.

## Run

```bash
# Opt-in via the launcher (warns + launches)
DASHBOARD=rust ./launch_daemon.sh paper

# Standalone (reads daemon_state/ relative to cwd; set POLYCHART_STATE_DIR
# to override)
./tui/target/release/polychart

# Dev utility: one-shot state.json parse + print, then exit
./tui/target/release/polychart --once
```

## Test

```bash
cd tui && cargo test              # unit + integration
cd tui && cargo clippy --all-targets -- -D warnings
cd tui && cargo fmt --check
```

## Module tree

```
src/
├── main.rs              entry, --once flag, tokio runtime
├── app.rs               terminal lifecycle, render loop, background tasks
├── state.rs             AppState + rolling deques + rollover detection
├── state_reader.rs      StateSnapshot serde model for state.json
├── events.rs            byte-offset tailer for events.jsonl + PnL series
├── theme.rs             hex palette mirrored from dashboard.tcss
├── util.rs              humanize_slug, format_chronometer, ts_hms
├── ui.rs                frame split + per-widget draw dispatch
├── http/
│   ├── clob.rs          orderbook poller (2s base, 10s backoff)
│   └── gamma.rs         slug -> token id resolver
└── widgets/
    ├── header.rs        BTC/sigma/strike + stretched progress bar + pills
    ├── live_curves.rs   hero prices + BTC chart + market/fair chart
    ├── main_strategy.rs refined stats + PnL chart + position + trades
    ├── orderbook.rs     adaptive-depth orderbook with block-char bars
    ├── baselines.rs     BASE / ENHANCED paper benchmarks
    └── orders_log.rs    tail of refined-strategy actions
```

## Why not default yet

- Rendering smoke-tested via `ratatui::backend::TestBackend` but not
  eyeball-verified against a running daemon across a full market
  rollover cycle.
- Edge cases in CLOB poller backoff, terminal resize behaviour, and
  Ctrl-C teardown under the bash launcher trap need manual QA before
  the default flips.
- The Python TUI is battle-tested through `tui/tests/` (64 tests) and
  lives-on-user-terminals daily. Don't break what works.

When polychart is ready to promote, flip `DASHBOARD=${DASHBOARD:-python}`
back to `rust` in `launch_daemon.sh`.
