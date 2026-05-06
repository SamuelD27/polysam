# PolySnipe — Repo On-ramp

## 1. Project overview

PolySnipe is an automated trader for **Polymarket's BTC 5-minute up/down
binary** (the "Bitcoin Up or Down 5m" market). Each market re-strikes every
5 minutes and resolves on whether BTC spot is above or below the strike at
T+300s. The bot computes a fair price (`Φ(d2)`), compares against the
market price, and takes positions when the edge is large enough; an active
TP/SL grabber closes them before resolution where possible.

Production posture: paper validation by default, live trading enabled by
flipping `POLYMARKET_MODE=live`. The current production strategy class is
**`active_bots/walked_vwap_strategy.py:WalkedVWAPStrategy`** (RefinedStrategy
+ a book-aware entry gate + a default-off walked-bid exit gate).

This file is the on-ramp. The lineage doc at `STRATEGY.md` is the source of
truth for strategy mechanics; this file is the source of truth for active
operational knobs.

---

## 2. Quick start

All commands assume `conda activate polymarket-env` (Python 3.11.14).

**Prerequisites for the launcher:** `whiptail` (default on
Debian/Ubuntu/RHEL/Fedora; `sudo apt install whiptail` if missing) **or**
`dialog`. A [Nerd Font](https://www.nerdfonts.com/) in the terminal
makes the menu glyphs render — without one they show as boxes; the
menu still works.

| Goal                          | Command                                                                  |
|-------------------------------|--------------------------------------------------------------------------|
| Interactive launch            | `./launch`                                                               |
| Re-launch a saved preset      | `./launch --preset <name>`                                               |
| List saved presets            | `./launch --list-presets`                                                |
| Legacy paper daemon (cron)    | `./launch_daemon.sh paper` *(deprecated → forwards to `launch --preset _legacy_default`)* |
| Legacy live daemon (cron)     | `./launch_daemon.sh live` *(deprecated → `launch --preset _legacy_live`)* |
| Legacy live daemon — dry-run  | `./launch_daemon.sh live dry` *(deprecated → `launch --preset _legacy_dryrun`)* |
| Run primary test suite        | `pytest tests/ active_bots/tests/`                                       |
| Run a single test file        | `pytest tests/strategy/test_walked_vwap.py -v`                           |
| Lint check                    | `ruff check active_bots/ tests/ scripts/ daemon_base_v1.py`              |
| Format                        | `ruff format active_bots/ tests/`                                        |
| Status / stop / log           | `./daemon_base_v1 status`, `./daemon_base_v1 stop`, `./daemon_base_v1 log` |

Saved presets live at `~/.polymarket-hustle/presets/<name>.json`. The
`_legacy_*` names are managed by the deprecation shim — write your own
preset under a different name. Full menu walkthrough and migration
table: `docs/LAUNCHER.md`.

Observer-only mode (any non-walked strategy) happens automatically: when
the daemon launches, only the strategy with `role="trader"` posts orders.
Refined / Enhanced / Base shadow-trade for benchmarking and emit
`shadow_*` events instead of real fills.

---

## 3. Architecture map

```
polymarket-hustle/
├── active_bots/             — strategy + execution layer (production code)
│   ├── base_strategy.py     — § STRATEGY.md §1.1
│   ├── enhanced_strategy.py — § STRATEGY.md §1.2 + §1.3 (TimeBased + ProfitGrabber + SqueezeDetector)
│   ├── refined_strategy.py  — § STRATEGY.md §1.4 (squeeze-off, tighter TP)
│   ├── walked_vwap_strategy.py — § STRATEGY.md §1.5 + §3 + §4 (entry + exit gates)
│   ├── execution/           — Executor protocol + Paper/Live/DryRun/Replay; book.py; fees; risk
│   ├── pricing/             — fair price (Φ(d2)), EWMA σ
│   └── tests/               — strategy-internal tests (max_risk, shutdown_timeout)
├── daemon_base_v1.py        — 1958-line headless daemon; orchestrates 4 strategies + 2 executors
├── daemon_base_v1           — bash control script (status / start / stop / log)
├── launch                   — keyboard-driven menu launcher (whiptail/dialog → polyhustle.cli)
├── launch_daemon.sh         — DEPRECATED shim → forwards to `launch --preset _legacy_*` for cron compat
├── tests/                   — primary test suite: strategy / execution / daemon / scripts
├── scripts/                 — operational tools: consolidate_data, csv_to_parquet, onboard_check, etc.
├── scrap/                   — six-stage modular historical scraper (markets / spot / trades / books / …)
├── docs/                    — design docs: STRATEGY.md (root), REPO_AUDIT.md, session_capture_design.md, superpowers/
├── experiments/             — 37 strategy-fork dirs from the 2026-04-21 fine-tuning campaign (archive)
├── tui/                     — Textual + Rust dashboard (separate test root tui/tests/)
└── data/                    — persisted market data (gitignored)
```

Cross-reference: `STRATEGY.md` for the strategy lineage; `RUNBOOK.md` for
daemon ops; `docs/REPO_AUDIT.md` for the in-flight cleanup state; the
session-capture design + reconcile spec live under `docs/`.

---

## 4. Strategy lineage (in one breath)

```
BaseStrategy                          fair-price edge entry, hold to T+300
   └── TimeBasedStrategy              (composed) 4 time-zoned entry windows w/ per-zone edge_min
EnhancedStrategy                      TimeBased + ProfitGrabber (TP/SL) + SqueezeDetector
   └── RefinedStrategy                EnhancedStrategy w/ squeeze=off, TP_DELTA_MIN=0.08, TP_ABSOLUTE_FAVOR=0.15
        └── WalkedVWAPStrategy        RefinedStrategy + walked-VWAP entry gate
              └── WalkedExitProfitGrabber  (subclass swap)  walked-bid VWAP exit gate, default-off
```

`active_bots/walked_vwap_strategy.py:WalkedVWAPStrategy` is the production
path. It inherits all of Refined's TP/SL math and adds:

1. An **entry gate** that, after Refined emits an `ENTER`, walks the asks
   for the requested shares + bell-curve fee → `effective_VWAP`, rejects
   if the post-fee walked edge is too small. Replaces `entry_price` with
   `effective_VWAP` so downstream PnL is truthful.
2. An **exit gate** (default-off via `WALKED_VWAP_EXIT_ENABLE=0`) that, by
   subclassing `ProfitGrabber`, re-prices the realizable exit on the
   walked-bid VWAP minus fees and translates back into the YES-frame the
   parent expects. Falls back to mid in the force-window or per
   `WALKED_VWAP_EXIT_FALLBACK_MID`.

Full mechanics: `STRATEGY.md` §1.5, §3, §4.

---

## 5. The current strategy in one tick

When `daemon_base_v1.py:strategy_loop` calls `walked.on_tick(...)` with
fresh BTC price + market mid + sigma + book snapshots:

1. **Stash books** onto `self.profit_grabber._latest_books` (so the exit
   gate can read them without changing the parent's `check_exit` signature).
2. **Run parent's `on_tick`** (`RefinedStrategy.on_tick → EnhancedStrategy.on_tick`):
   1. Profit grabber check (now `WalkedExitProfitGrabber.check_exit`) on
      open positions; fires `EXIT_TP` / `EXIT_SL` or holds.
   2. Resolution check: if `elapsed >= 300s`, settle.
   3. Squeeze entry: skipped (Refined disables it).
   4. Time-based edge entry: per zone (`early` / `mid` / `sweet_spot` /
      `late_gamma`), if the edge >= zone threshold and entry_price is in
      `[MIN_ENTRY_PRICE, MAX_ENTRY_PRICE]`, return an `ENTER` action.
3. **If non-ENTER**, return as-is.
4. **If ENTER**, run the entry gate:
   - Reject if quote is stale, no book subscription, empty book,
     insufficient top-of-book, partial-fill not allowed, or walked-edge
     too small. On reject, emit `WALKED_VWAP_REJECT` with diagnostics.
   - On pass, augment the action with `effective_VWAP`, `walked_edge`,
     book diagnostics; rewrite `entry_price` to the post-fee walked VWAP;
     sync the parent's `_open_position` so subsequent `check_exit`
     operates on the realistic per-share cost.

For the full reject matrix and exit gate's bad-book matrix, see
`STRATEGY.md` §3.1 / §4.4.

---

## 6. Knob cheat-sheet (canonical)

CLAUDE.md is the single source of truth for active env-var knobs. Strategy
knobs cross-reference `STRATEGY.md §5`; if anything drifts, this table
wins and STRATEGY.md must follow.

### Strategy knobs

| Env var                          | Default | Source file:line                        | Meaning                                      |
|----------------------------------|---------|-----------------------------------------|----------------------------------------------|
| `TP_DELTA_MIN`                   | 0.05    | `enhanced_strategy.py:177`              | TP threshold floor (Refined sets to 0.08)    |
| `SL_DELTA_MIN`                   | 0.05    | `enhanced_strategy.py:180`              | Adaptive SL band lower bound                 |
| `SL_DELTA_MAX`                   | 0.20    | `enhanced_strategy.py:181`              | Adaptive SL band upper bound                 |
| `FORCE_EXIT_BEFORE_S`            | 30.0    | `enhanced_strategy.py:185`              | Force-exit window into resolution            |
| `TP_ABSOLUTE_FAVOR`              | 0.10    | `enhanced_strategy.py:190`              | Absolute TP fire (Refined sets to 0.15)      |
| `WALKED_VWAP_MARKET_STALENESS_S` | 30.0    | `walked_vwap_strategy.py:35`            | Entry-gate stale-quote threshold             |
| `WALKED_VWAP_MIN_TOP_RATIO`      | 1.0     | `walked_vwap_strategy.py:36`            | Top size must be ≥ requested * this          |
| `WALKED_VWAP_EDGE_MIN`           | 0.02    | `walked_vwap_strategy.py:37`            | Min walked edge after fee for entry          |
| `WALKED_VWAP_PARTIAL_OK`         | 0       | `walked_vwap_strategy.py:38`            | Accept partial entry fills                   |
| `WALKED_VWAP_FEE_CATEGORY`       | crypto  | `walked_vwap_strategy.py:43`            | Fee bucket (crypto/finance/geopol)           |
| `WALKED_VWAP_EXIT_ENABLE`        | 0       | `walked_vwap_strategy.py:47`            | Master switch for the walked-bid exit gate   |
| `WALKED_VWAP_EXIT_STALENESS_S`   | 30      | `walked_vwap_strategy.py:48`            | Exit-gate stale-book threshold               |
| `WALKED_VWAP_EXIT_PARTIAL_OK`    | 0       | `walked_vwap_strategy.py:49`            | Accept partial exit fills outside force-win  |
| `WALKED_VWAP_EXIT_FALLBACK_MID`  | 1       | `walked_vwap_strategy.py:54`            | Bad-book fallback policy                     |

### Phase 4 asymmetry knobs (cleanup pass May 2026)

| Env var                            | Default | Source file:line                  | Meaning                                       |
|------------------------------------|---------|-----------------------------------|-----------------------------------------------|
| `SL_ABSOLUTE_AGAINST`              | unset   | `enhanced_strategy.py:206`        | Hard SL cap (mirror of `TP_ABSOLUTE_FAVOR`). When set, SL fires if `delta_against >= this` regardless of adaptive curve. |
| `SL_DECAY_ENABLE`                  | 0       | `enhanced_strategy.py:215`        | Enable time-decay on adaptive SL (mirror of TP decay). At entry returns the un-decayed adaptive_sl; at T=0 returns floor. |
| `SL_DELTA_DECAY_FLOOR`             | 0.05    | `enhanced_strategy.py:216`        | Floor for the SL decay curve when `SL_DECAY_ENABLE=1` |
| `WALKED_VWAP_SIZE_ON_WALKED_EDGE`  | 0       | `walked_vwap_strategy.py:65`      | Recompute `size_shares` from the walked edge instead of mid edge. Reduces position size when book is thin. |

### Operational / executor knobs

| Env var                       | Default                | Source file:line                     | Meaning                                       |
|-------------------------------|------------------------|--------------------------------------|-----------------------------------------------|
| `POLYMARKET_MODE`             | paper                  | `daemon_base_v1.py:228`              | `paper` / `live`                              |
| `POLYMARKET_DRY_RUN`          | 0                      | `daemon_base_v1.py:229`              | Live mode but no order POST                   |
| `POLYMARKET_PRIVATE_KEY`      | (none)                 | `clob_client_factory.py:39`          | EOA signing key                               |
| `POLYMARKET_FUNDER`           | (none)                 | `daemon_base_v1.py:327` and others   | Magic / proxy wallet address                  |
| `POLYMARKET_API_KEY`          | (none)                 | `clob_client_factory.py:41`          | CLOB API auth                                 |
| `POLYMARKET_API_SECRET`       | (none)                 | `clob_client_factory.py:42`          | CLOB API auth                                 |
| `POLYMARKET_API_PASSPHRASE`   | (none)                 | `clob_client_factory.py:43`          | CLOB API auth                                 |
| `POLYMARKET_CHAIN_ID`         | 137                    | `clob_client_factory.py:45`          | Polygon mainnet                               |
| `POLYMARKET_SIGNATURE_TYPE`   | 1                      | `clob_client_factory.py:49`          | 0=EOA, 1=Magic, 2=browser                     |
| `KILL_SWITCH_FILE`            | `daemon_state/KILL`    | `daemon_base_v1.py:285`              | If file exists, all entries blocked           |
| `MAX_DAILY_LOSS_USDC`         | 300                    | `.env.example`                       | Daily loss circuit breaker                    |
| `MAX_TRADE_SIZE_USDC`         | 100                    | `.env.example`                       | Per-trade size ceiling                        |
| `PORTFOLIO_SIZE_USDC`         | (auto-detect)          | `daemon_base_v1.py:238`              | Override portfolio total for size scaling     |
| `MAX_BET_PCT`                 | (default in code)      | `daemon_base_v1.py:238`              | Max % of portfolio per bet                    |
| `POLYGON_RPC_URL`             | https://polygon-rpc.com| `scripts/check_v2_allowances.py:59`  | Polygon JSON-RPC for allowance checks         |

When you add a new knob, update **all three** of: (a) the source-file
docstring/constant block, (b) this table, (c) `STRATEGY.md §5` (strategy
knobs only).

---

## 7. Conventions

- **Default-off feature flags.** Every behaviour change ships behind an
  env var that defaults to "off / current behaviour." Match the pattern
  set by `WALKED_VWAP_EXIT_ENABLE`. Paper-validate before flipping.
- **Role-gating for multi-strategy sessions.** Only the strategy with
  `role="trader"` posts orders. Observers emit `shadow_*` events for
  benchmarking. Currently `walked_vwap` is the trader; everyone else is
  an observer.
- **Conventional commits.** `feat(scope): …`, `fix(scope): …`,
  `docs: …`, `style: …`, `refactor(scope): …`, `test(scope): …`,
  `chore: …`. One logical change per commit. Style/format commits stay
  separate from logic commits.
- **Decimal at the boundary.** `active_bots/execution/book.py` and
  `replay_executor.py` are pure Decimal — no floats cross the public API.
  The daemon path uses floats for speed; canonical fill arithmetic lives
  in the Decimal modules.
- **Paper-validate before live.** New strategy knobs are paper-validated
  (typically a multi-hour replay or a `POLYMARKET_DRY_RUN=1` session)
  before being flipped on a live deployment.

---

## 8. Known asymmetries / non-goals

Acknowledged gaps; each is a deliberate choice or a known follow-up. See
also `STRATEGY.md §6`.

- **Limit (GTC) orders.** All entries and exits are FAK (market-style).
  Maker rebates are unrealized. The `n4_market_maker` experiment
  abandoned this path — 5-min markets don't queue limits long enough.
- **Symmetric TP/SL.** TP has both adaptive and absolute floors
  (`TP_ABSOLUTE_FAVOR=0.15`). Until the May 2026 cleanup pass, SL had
  only the adaptive curve — no absolute cap. The Phase 4A flag
  `SL_ABSOLUTE_AGAINST` (default unset = off) makes this addressable
  without changing default behaviour.
- **SL time-decay.** TP decays toward `TP_DELTA_MIN` over the cycle.
  The Phase 4B flag `SL_DECAY_ENABLE` (default 0 = off) lets SL decay
  toward `SL_DELTA_DECAY_FLOOR` symmetrically.
- **Position-size scaling on walked edge.** Sizing was historically
  derived from mid edge even after the gate computed `effective_VWAP`.
  The Phase 4C flag `WALKED_VWAP_SIZE_ON_WALKED_EDGE` (default 0 = off)
  enables walked-edge sizing.
- **Multi-position / pyramiding.** Single position per market.
- **Cross-market hedging.** No correlation logic between markets.
- **Limit-order escalation on FAK reject.** When entry FAK no-matches
  (book moved between gate snapshot and order arrival), the strategy
  gives up on that market — no retry, no limit fallback.
- **Snapshot-vs-fill execution drag.** Even with the walked-bid exit
  gate, daemon-reported PnL is the gate-time walked VWAP, not the actual
  fill price; ~$0.25/trade residual gap to on-chain PnL has been
  observed in live sessions.

The list of items NOT touched by the May 2026 cleanup pass (waiting for
quant analysis): sweet_spot edge_min (0.08 < SL band), late_gamma zone
viability, fair-price σ source, squeeze detector, entry-zone time
windows, force-exit at T-30s.

---

## 9. Testing posture

Primary suite: `pytest tests/ active_bots/tests/`. **272 tests
collected on `refactor/modular-architecture`** (271 pass; 1 pre-existing
fragile test
`tests/execution/test_latency.py::test_fit_raises_not_fitted_on_real_events`
that depends on local `daemon_state/events.jsonl` content and is
unrelated to strategy logic). The 39-test increase from the historical
233 baseline (Sam-Dev pre-refactor) covers
`tests/data/`, `tests/orchestrator/`, the Strategy + Trader ABC suites,
and the on_tick signature-fallback warn-once behaviour added by the
refactor.

The unmerged feature branches `feat/quant-flags-phase5` and
`feat/quant-flags-phase5b` carry their own additional tests (Phase 5
parity + sizing-cap coverage) — those land in the suite when those
branches merge into Sam-Dev.

Test layout:

| Path                                    | Covers                                                                 |
|-----------------------------------------|------------------------------------------------------------------------|
| `tests/strategy/test_walked_vwap.py`    | Entry-gate full reject matrix, exit-gate matrix incl. force-window, partial-fill, NaN VWAP, fee direction, wrapper book-stash, `_open_position` sync |
| `tests/strategy/test_role_dispatch.py`  | Per-strategy role gating: only `role="trader"` posts                   |
| `tests/strategy/test_profit_grabber.py` | (Added Phase 4A) `ProfitGrabber.check_exit` direct: `SL_ABSOLUTE_AGAINST` matrix, decayed adaptive SL |
| `tests/execution/`                      | Book deltas, executor invariants (Hypothesis), fees, latency, replay   |
| `tests/daemon/test_book_subscription.py`| Book WS subscription behaviour                                         |
| `tests/scripts/test_consolidate_data.py`| ETL pipeline                                                           |
| `active_bots/tests/test_effective_max_risk.py` | Portfolio-derived sizing                                       |
| `active_bots/tests/test_shutdown_timeout.py`   | Daemon shutdown timeout                                        |

**Adding a new strategy variant.** Drop a fork dir under `experiments/`
with the modified strategy file + a `runs/` directory. Replay against
fixtures (existing pattern: see `experiments/_baseline/`). The fork's
tests are NOT collected by the primary suite — `pyproject.toml`'s
`[tool.pytest.ini_options].testpaths` scopes collection to `tests/` and
`active_bots/tests/`.

---

## 10. Dataflow

- **Live market data**: Binance WS for BTC spot, Polymarket RTDS WS for
  market mid, Polymarket CLOB WS for orderbook deltas. All consumed by
  `daemon_base_v1.py`.
- **Daemon state**: `daemon_state/state.json` (refreshed every 5s),
  `daemon_state/daemon.log` (stream), `daemon_state/events.jsonl`
  (structured per-event log), `daemon_state/book_feed/` (per-market
  WS frame archive).
- **Persisted parquet**: `~/polymarket-parquet/` for historical books and
  trades; produced by `scripts/csv_to_parquet.py` and the daemon's own
  consolidation pass (`scripts/consolidate_data.py`).
- **Analysis pipeline**: `experiments/backtest/harness.py` walks recorded
  snapshots through `replay_executor.py` for offline scoring.
  `experiments/backtest/reconcile.py` joins daemon `entry_filled` /
  `exit_filled` events against on-chain trade history (CSV / API).

---

## 11. Run logs and reports

- Per-session daemon log: `daemon_state/daemon.log`
  (RotatingFileHandler — kept across restarts, rotated by size).
- Structured events: `daemon_state/events.jsonl` (append-only, one JSON
  per line; consumers: dashboards, reconcile, latency fits, ETL).
- Reconcile reports: `reports/reconcile/<session>/` — produced by
  `experiments/backtest/reconcile.py` after a run.
- Backtest runs: `experiments/backtest/runs/<session>.parquet[.manifest.json]`.
- Session-capture and consolidation specs:
  `docs/session_capture_design.md`,
  `docs/book_walked_replay_backtester_spec.md`,
  `docs/reconcile_design.md`.

Naming convention for sessions: `<UTC>_<purpose>` — e.g.
`2026-04-24T07-10-19Z_golden`.

---

## 12. Debugging recipes

- **"The walked-VWAP gate is rejecting everything in dry-run."** Grep
  `WALKED_VWAP_REJECT` in `daemon_state/events.jsonl` and tally the
  `reject_reason` distribution. The most common failure mode is
  `empty_book` (CLOB book WS hasn't subscribed; check `book_feed/`) and
  `insufficient_walked_edge` (set `WALKED_VWAP_EDGE_MIN` looser to
  diagnose).
- **"Daemon PnL doesn't match on-chain PnL."** Run
  `experiments/backtest/reconcile.py` against the session — it joins
  `entry_filled` / `exit_filled` events against the on-chain history CSV
  pulled from the Polymarket UI. Expected residual gap is ~$0.25/trade
  (snapshot-vs-fill drag); larger gaps usually mean stale book at
  decision time.
- **"`POLYMARKET_DRY_RUN=1` session is silent."** Verify
  `DryRunExecutor.mode == "live"` (not `"paper"`) — downstream dashboards
  filter by `.mode`. Confirm the daemon is actually launched with
  `MODE=live` (not `paper`); dry-run only applies in live mode.
- **"Squeeze detector firing on unwanted markets."** Squeeze is disabled
  in `RefinedStrategy` (`enable_squeeze=False`); a squeeze entry firing
  means the daemon is dispatching to a non-Refined parent. Confirm the
  trader strategy is `walked_vwap`, not `enhanced`.
- **"`role="trader"` strategy isn't posting."** Confirm
  `self._risk.check_entry()` isn't blocking — see daemon log for "entry
  blocked" lines (kill-switch / session-loss / daily-loss). Confirm the
  CLOB client is built (look for `clob client factory` errors at
  startup).
- **"My new strategy class doesn't get picked up by the dispatcher."**
  The daemon imports a fixed list of strategy classes at
  `daemon_base_v1.py:32-39`. Adding a new one means editing that import
  block AND the strategy-loop dispatch in `strategy_loop` (around
  `:1095-1124`).

---

## 13. Extending the strategy — checklist

When adding a new entry zone, exit gate, or fee model, in order:

1. **Read** `STRATEGY.md` end-to-end. The current shape is the result of
   the 2026-04-21 fine-tuning session and the 2026-04-27 walked-VWAP
   work. Don't reintroduce a knob without checking why it landed where
   it did.
2. **Branch** from `Sam-Dev`. Never start work on `main`.
3. **Default-off feature flag.** Any behaviour change goes behind an
   env var that defaults to "off / current behaviour" — pattern:
   `WALKED_VWAP_EXIT_ENABLE`. Update the source file's knob block, this
   CLAUDE.md table, and (if a strategy knob) `STRATEGY.md §5`.
4. **Tests with the flag both off and on.** Off-branch must be
   bit-for-bit identical to today (parametrise the existing test fixture
   if possible). On-branch must exercise the new logic.
5. **Replay parity check.** Before the PR, run a baseline replay with
   all new flags off and confirm zero divergence vs. the previous
   commit's run on the same fixture.
6. **Replay divergence check.** Same fixture, each flag on in turn —
   confirm the strategy diverges at the expected step. Sanity that the
   flag actually wires in.
7. **Conventional commits, atomic changes.** `feat:` for the logic,
   `test:` for the tests, `docs:` for the doc updates. No mixed
   reformatting/logic commits.
8. **Paper-validate.** Before flipping the flag in any live deployment,
   run a `POLYMARKET_DRY_RUN=1` session and reconcile against on-chain
   PnL.

For larger work, write a plan under `docs/superpowers/plans/`
(`YYYY-MM-DD-<topic>.md`). Existing plans there are good templates.

---

## 14. Package layout (`polyhustle/`)

The modular-architecture refactor introduced a `polyhustle/` package that
splits the daemon's monolithic loop into three layers connected by typed
interfaces. Strategy class bodies still live in `active_bots/` during the
transition; `polyhustle/strategies/` are shim re-exports.

```
polyhustle/
├── __init__.py
├── data/
│   ├── provider.py        # DataProvider ABC + frozen MarketTick dataclass
│   ├── live.py            # LiveDataProvider — wraps daemon's WS feeds + DaemonState
│   ├── paper.py           # PaperDataProvider — alias of live (paper differs in trader, not data)
│   └── replay.py          # ReplayDataProvider — Session C stub
├── strategies/
│   ├── strategy_abc.py    # Strategy ABC: on_tick / reset / has_position
│   ├── base.py            # shim: re-exports active_bots.base_strategy.BaseStrategy
│   ├── enhanced.py        # shim
│   ├── refined.py         # shim
│   └── walked_vwap.py     # shim
├── execution/
│   ├── trader.py          # Trader ABC + Decision + ExecutionResult dataclasses
│   ├── _executor_trader.py # shared dispatch base — action → enter/exit/resolve
│   ├── live_trader.py     # wraps active_bots.execution.live_executor.LiveExecutor
│   ├── paper_trader.py    # wraps PaperExecutor — each instance owns its own wallet
│   ├── dryrun_trader.py   # wraps DryRunExecutor — live-shape paper fills, no POST
│   ├── reconciler.py      # shim re-export
│   └── risk_manager.py    # shim re-export
├── orchestrator.py        # the run loop — single-instrument; supports N traders with isolated wallets
└── cli.py                 # `python -m polyhustle.cli --config <json>` entry point
```

Top-level `launch` script is the user-facing entry point. As of session B
(2026-05-06) it is a keyboard-driven menu launcher (whiptail/dialog) that
writes a JSON config consumed by `polyhustle.cli`. Saved presets live at
`~/.polymarket-hustle/presets/`. The previous `./launch_daemon.sh` is now
a deprecation shim that auto-installs `_legacy_*` presets and forwards to
`launch --preset _legacy_<mode>` for cron-script compatibility. Full
reference: `docs/LAUNCHER.md`.

The legacy `daemon_base_v1.py` continues to work unchanged — it is
the import target for `polyhustle/data/live.py` and
`polyhustle/cli.py`'s live-mode trader build. Eventual retirement of
the legacy daemon happens once the new path has soaked in production.

---

## 15. Adding a new strategy

Three steps:

1. **Subclass `Strategy`** in a new file under
   `polyhustle/strategies/<name>.py`. Implement `on_tick` / `reset` /
   `has_position`. The class body can live there directly, or — if the
   strategy is a tweak on an existing one — re-export a class from
   `active_bots/` (the existing four classes use this shim pattern
   during the refactor).
2. **Register it in `polyhustle/cli.py`**. Add the entry to
   `STRATEGY_REGISTRY` so the JSON config's
   `main_strategy` / `comparison_strategies` / `benchmarks` accept the
   name.
3. **Update `STRATEGY.md`**. New section under §1, knob entries in §5,
   lineage in §8 if applicable. Don't skip this — `STRATEGY.md` is the
   source of truth for strategy mechanics; the cheat-sheet table here
   in CLAUDE.md cross-references it.

Beyond that the existing checklist from §13 applies (default-off flag,
tests both ways, replay-parity, conventional commits, paper-validate
before live).

The legacy daemon's strategy dispatch (`daemon_base_v1.py` lines 33-40
imports + `strategy_loop` lines 1095-1124) does NOT auto-update from
the registry — that remains a manual edit until the legacy entry point
is retired.

---

## 16. Documentation rule — Plain-language summary

Every NEW markdown file under `docs/` or `reports/` must start with a
`## Plain-language summary` section of 3-6 sentences explaining the
document for a non-technical reader, BEFORE the technical body. The
summary answers: what is this document, who is it for, what would
change if its conclusions were acted on?

The rule applies to:
- Every new doc file added to `docs/` or `reports/`.
- Any existing doc file Claude edits substantively (i.e. structural
  changes, not typo fixes or knob-table updates).

The rule does NOT apply to:
- Chat-style replies in conversations (technical first; plain-language
  on request).
- Existing doc files that aren't being edited — DO NOT bulk-add
  summaries to all 50+ existing docs.
- Code comments, docstrings, or commit messages.

Format example::

    # R3.0 — Sweep results

    ## Plain-language summary

    This document records the outcome of a 256-combination flag sweep
    against the R2.2 capture. We tested whether tightening or loosening
    five book-quality / risk-management knobs would have closed the
    walked_vwap strategy's $12 loss. The headline finding: the
    `MIN_TOP_OF_BOOK_SHARES_RATIO` knob is the largest lever, but it
    needs validation against a higher-vol capture before promoting any
    default. No code defaults are changed by this document; it informs
    the `tune/quant-defaults` branch.

    ## 1. Methodology
    ...

The summary lets the operator (and future readers) decide in 30
seconds whether to read the technical body. Documents that miss this
section get rejected at code review.
