# tui — Terminal UI for polymarket-hustle

This directory isolates all TUI code so a Rust rewrite session can work
here without risk of colliding with daemon, strategy, executor, or
reconciler code upstream.

Two implementations live side-by-side:

- `python/dashboard.py` — current Textual TUI; the launch contract still
  points here until the Rust rewrite reaches parity.
- `rust/polychart/` — scaffolded ratatui binary; the rewrite target.

---

## Directory layout

```
tui/
├── README.md                  this file
├── Cargo.toml                 Rust workspace root
├── Cargo.lock                 committed (binary crate → reproducible builds)
├── rust-toolchain.toml        pins rustc 1.83.0 + rustfmt + clippy
├── rust/polychart/            ratatui binary
│   ├── Cargo.toml
│   └── src/main.rs            scaffold: reads state.json, prints one line
├── python/                    existing Textual dashboard (unchanged behavior)
│   ├── dashboard.py
│   └── dashboard.tcss
├── tests/                     pytest for the Python TUI (64 tests)
│   ├── conftest.py            puts tui/python/ on sys.path so `import dashboard` works
│   ├── test_*.py
└── target/                    cargo workspace output (gitignored)
```

## Toolchain

Rust 1.83.0 (stable, 2024-11-26), pinned in `tui/rust-toolchain.toml` with
rustfmt + clippy components. rustup installs the pinned channel on first
`cargo` invocation inside `tui/`.

Python TUI runs under conda env `polymarket-env` (same env the daemon
uses). Deps: `textual`, `textual_plotext`, `rich`, `requests`.

## Build & run

```bash
# Rust — scaffold binary
cd tui && cargo build --release
tui/target/release/polychart          # run from repo root (reads daemon_state/)

# Rust — dev iteration
cd tui && cargo run --release -- --once   # --once is the scaffold default

# Rust — lints
cd tui && cargo clippy --all-targets -- -D warnings
cd tui && cargo fmt --check

# Python — from repo root, with conda polymarket-env active
python3 tui/python/dashboard.py

# Python — test suite (from repo root)
pytest tui/tests                     # 64 tests, <1s
```

## Launch contract

`launch_daemon.sh` (at repo root) invokes the **Python** TUI today:

- Line 96 (`status` branch): `exec python3 "$PROJECT_DIR/tui/python/dashboard.py"`
- Line 242 (main): `python3 "$PROJECT_DIR/tui/python/dashboard.py" || true`

When the Rust MVP reaches feature parity, the plan is to add a
`DASHBOARD=rust|python` env switch and flip the default. Do **not**
wire the switch in ahead of time — the Rust binary must stand on its
own merits first.

The launcher expects:

- **cwd** = repo root (`/home/samsam/polymarket-hustle`) — binary reads
  `daemon_state/state.json` relative to cwd by default. Override with
  `POLYCHART_STATE_DIR=/path/to/daemon_state`.
- **conda** env `polymarket-env` activated (for the Python TUI; Rust
  binary has no Python dependency).
- **Ctrl-C** must exit the foreground dashboard cleanly. The bash trap
  (`stop_live_gui; stop_daemon`) only fires *after* the dashboard
  returns — if the Rust binary traps SIGINT itself, it must still
  exit the process cleanly so the script continues.

---

## Non-goals for the Rust rewrite

- **Do not modify daemon code** — `daemon_base_v1.py`, everything under
  `active_bots/**`, all strategy/executor/reconciler/risk code is
  read-only for the TUI session.
- **Do not change `state.json` schema** — the Rust consumer must match
  the shape emitted by `DaemonState.to_dict()` (see below).
- **Do not change `events.jsonl` shape** — the Rust tailer must consume
  the existing event types. New types are forward-compatible:

  ```rust
  #[derive(Deserialize)]
  #[serde(tag = "type", rename_all = "snake_case")]
  enum Event {
      EntryFilled(EntryFilled),
      ExitFilled(ExitFilled),
      Resolve(Resolve),
      MarketRollover(MarketRollover),
      // ...
      #[serde(other)]
      Unknown,  // silently ignore unknown event types
  }
  ```

- **Do not fix the visual bugs by editing `python/dashboard.py`.** The
  Python TUI is now frozen; every pending visual bug below should be
  fixed in the Rust port instead.
- **Do not touch `scripts/dashboard_streamlit.py`** — a separate web
  dashboard, independent of this work.
- **Do not write to `daemon_state/`** — the Python TUI has a test
  (`tests/test_read_only.py`) that enforces this. The Rust TUI is
  expected to follow the same rule.

---

## daemon_state/state.json schema

Top-level fields (emitted by `daemon_base_v1.DaemonState.to_dict()`):

| field               | type           | notes |
|---------------------|----------------|-------|
| `btc_price`         | f64            | spot BTC in USD |
| `btc_ts`            | f64            | unix seconds of last Binance trade |
| `sigma`             | f64 \| null    | EWMA-annualized vol; null during warmup |
| `t_zero`            | i64 \| null    | unix seconds of current 5-min market start |
| `strike`            | f64 \| null    | BTC strike for current market |
| `slug`              | string \| null | `btc-updown-5m-<t_zero>` |
| `market_price_up`   | f64 \| null    | YES-side probability, 0..1 |
| `market_price_ts`   | f64            | unix seconds of last RTDS price tick |
| `rtds_last_msg_ts`  | f64            | last RTDS frame (for staleness render) |
| `connections`       | `{binance:bool, rtds:bool}` | |
| `base`              | StrategyBlob   | paper benchmark |
| `enhanced`          | StrategyBlob + EnhancedExtra | paper benchmark |
| `refined`           | StrategyBlob + RefinedExtra  | session champion; live-capable |
| `last_update`       | f64            | unix seconds of state write |

`StrategyBlob` = `{fair_price: f64?, open_position: Position?, closed_trades: Trade[], stats: Stats}`.

`Position`: `slug, side("Up"|"Down"), entry_price, edge, size_usdc, size_shares, strike, entry_time, source("base"|"edge"|"squeeze"), time_zone(null|"early"|"mid"|"late"), spike_score, t_zero, ack_ts`. Base adds `fair_at_entry, market_at_entry`; live adds `order_id, token_id`.

`Trade`: `slug, side, entry_price, exit_price, pnl, edge, size_usdc, size_shares, won(bool), resolved_time, strike, final_btc, exit_type("TP"|"SL"|"RESOLUTION"), source, time_zone, hold_time_s`.

`Stats`: `total_pnl, total_trades, wins, losses, max_drawdown, current_drawdown, high_water_mark, current_streak, streak_type("W"|"L"|null), start_time, total_risked`.

`EnhancedExtra`: `squeeze_active, spike_score, last_exit_type, last_time_zone, last_source, tp_count, sl_count, resolution_count, edge_trades, squeeze_trades`.
`RefinedExtra` = `EnhancedExtra` minus `squeeze_active, spike_score, squeeze_trades`.

---

## daemon_state/events.jsonl shape

One JSON object per line, line-buffered writes. Every record has `ts: f64` and `type: str`. Types currently emitted (see `active_bots/execution/event_logger.py` and `daemon_base_v1.py`):

| type              | payload fields (in addition to `ts, type`) |
|-------------------|--------------------------------------------|
| `session_start`   | `pid` |
| `startup`         | `mode, max_trade_size, max_daily_loss, dry_run` |
| `shutdown`        | — |
| `market_rollover` | `slug, t_zero, strike, yes_token_id?, no_token_id?` |
| `entry_signal`    | `strategy, source, side, entry_price, edge, size_usdc, time_zone?, spike_score?, offset, slug` (+ `fair, market` for base) |
| `entry_filled`    | `strategy, source?, position, order_id?, token_id?` |
| `entry_rejected`  | `strategy, source?, side, slug` |
| `exit_filled`     | `strategy, trade` |
| `exit_rejected`   | `strategy, action, position` |
| `resolve`         | `strategy, trade, trigger?("rollover")` |

The Python TUI only consumes `entry_filled`, `exit_filled`, `resolve` (for PnL series + orders log). The rest is forward-compatibility surface.

**Tailing semantics:** byte-offset tailer, resets on truncation
(`size < _pos`), defers any trailing partial line (daemon mid-write)
until the next tick. See `tests/test_events_tailer.py` for the
exact invariants the Rust tailer must preserve.

---

## External HTTP surface (Rust port reimplements)

### CLOB orderbook (for the orderbook panel)
```
GET https://clob.polymarket.com/book?token_id=<id>
→ { "bids": [{price, size}, ...], "asks": [{price, size}, ...] }
```
- 2s base cadence, back off to 10s after 3 consecutive failures, recover
  on first success.
- Price/size come as strings *or* numbers in the wild — handle both with
  `serde`'s `#[serde(deserialize_with)]` or a custom visitor.
- HTTPS only. Geo-blocked for SG IPs — in live mode the daemon runs
  inside a WireGuard netns; the TUI runs on the host and hits CLOB
  directly (no netns wrap).

### Gamma (for slug → token id resolution)
```
GET https://gamma-api.polymarket.com/events?slug=<slug>
→ [{markets: [{clobTokenIds, orderPriceMinTickSize|minimumTickSize, ...}]}]

GET https://gamma-api.polymarket.com/markets?slug=<slug>
→ [{clobTokenIds, ...}]      (fallback)
```
- `clobTokenIds` may be a JSON-encoded string OR a raw array — Rust
  parser must accept both. The Python impl:

  ```python
  raw = market.get("clobTokenIds", "[]")
  tokens = json.loads(raw) if isinstance(raw, str) else raw
  ```

- Cache forever per slug (5-min markets are time-unique).
- Blocking lookup is acceptable — slug resolution happens once at market
  rollover, not on every tick. Suggested Rust impl: ~40 lines,
  `reqwest` blocking or `tokio::task::spawn_blocking`.

---

## Color palette (match the Python TUI; see `python/dashboard.py` + `python/dashboard.tcss`)

| element                         | color                        |
|---------------------------------|------------------------------|
| screen background               | `#0f172a` (slate-950)        |
| header border                   | heavy `#3b82f6` (blue-500)   |
| refined/main panel border       | round `#06b6d4` (cyan-500)   |
| other panel borders             | round `#475569` (slate-600)  |
| BTC label                       | bold `#22d3ee` (cyan-400)    |
| slug                            | yellow                       |
| progress bar chars              | `█` filled, `░` empty; `#94a3b8` |
| `t+X` / conns cluster           | `#22d3ee` / `#94a3b8`        |
| sigma                           | magenta                      |
| strike                          | `#94a3b8`                    |
| UP price (hero)                 | bold `#4ade80` (green-400)   |
| DOWN price (hero)               | bold `#f87171` (red-400)     |
| PnL positive / negative / zero  | bold green / bold red / white |
| BUY / SELL / RES (orders log)   | bold bright_green / bold bright_yellow / bold cyan |
| KILL pill                       | bold white on `#d946ef` (fuchsia-500) |
| ok pill                         | dim green                    |
| connection ● (up/down)          | green / red                  |
| chart: BTC / market / fair      | cyan / white / yellow        |
| chart: PnL pos / neg / flat     | green / red / white          |

Plotext theme in the Python impl is `"pro"` — no direct equivalent in
ratatui; pick a close-enough chart background and match the palette
above.

---

## Pending TUI bugs — fix during the Rust rewrite, NOT in Python

These are live visual bugs in the current Textual dashboard. They were
intentionally left unfixed because the Rust port is about to replace
the whole rendering layer.

1. **Orderbook depth bars render as literal `#`** — should be Unicode
   block characters (`▇` or `█`) scaled to size. Source: `python/dashboard.py`
   `OrderbookWidget.bar()`.

2. **Progress bar time format** — currently `t+111s / 300s`; should be
   a chronometer `MM:SS / 05:00` (e.g. `01:51 / 05:00`). Source:
   `HeaderWidget.render_state`.

3. **Market title shows raw slug** — `btc-updown-5m-1776921900` should
   render as humanized `BTC 5min HH:MM-HH:MM` computed from `t_zero`.
   Source: `MainStrategyWidget._render_headline`.

4. **Right-side dead space on multiple panels.** Progress bar, baselines
   panel header, orderbook panel header, and main-strategy panel header
   all have significant unused horizontal space on the right. The
   progress bar specifically should stretch to fill the available width
   with the `t+X` / conns / kill cluster right-justified. The
   live-data panel (`LiveCurvesWidget`) is the only panel that uses
   its width correctly — use it as the model.

5. **Refined PnL chart is cramped** — `#chart-pnl { height: 5 }` in
   `python/dashboard.tcss`. The main-strategy panel under-uses
   vertical space; the PnL chart should be taller and the panel width
   should be fuller.

6. **Connection pills / kill pill colors** — confirmed palette: green/red
   for connection dots, **bold magenta** for the kill-state pill (matches
   the `#d946ef` fuchsia background in the current impl). Keep consistent
   across the header and any status row in the Rust port.

---

## Handoff invariants — Rust session must preserve these

- **Read-only on `daemon_state/`.** Never write, touch, unlink, or
  `mkdir` inside that directory. Mirror the Python test in
  `tests/test_read_only.py` as a Rust integration test.
- **Tolerate mid-write state.json.** The daemon writes via `.tmp` +
  `rename`, but events.jsonl is appended line-by-line. The tailer must
  handle partial trailing lines exactly as the Python impl does (see
  `test_events_tailer.py::test_tailer_defers_partial_trailing_line`).
- **Reset on rollover.** Rolling deques for BTC / market / fair must
  clear whenever `t_zero` changes. The PnL series is cumulative and
  does NOT reset on rollover — session-wide.
- **Ctrl-C exits cleanly.** The bash launcher's trap fires
  `stop_live_gui; stop_daemon` after the dashboard returns. If you
  install a SIGINT handler, still exit the process (don't swallow).
- **No network on paper mode startup.** The dashboard must render
  offline (no CLOB / Gamma calls) if `state.json` is stale or the
  daemon is down. External HTTP is per-tick best-effort, never
  blocking on startup.
