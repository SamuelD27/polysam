# PolySnipe — Repo on-ramp

## Plain-language summary

PolySnipe is an algorithmic trader for Polymarket's BTC 5-minute
up/down binary markets. This file is the per-session orientation a
fresh Claude Code instance reads first: what the code is, where it
lives, how to operate it safely, and which doors are locked. Strategy
mechanics, knob tuning, and historical context live in dedicated docs
(`STRATEGY.md`, `docs/LAUNCHER.md`, `docs/REPLAY.md`, `reports/`);
CLAUDE.md points at them rather than duplicating.

## 1. Project overview

Production strategy: **WalkedVWAPStrategy**
(`active_bots/walked_vwap_strategy.py`); lineage and tuning in
`STRATEGY.md`. Daemon entry: `launch_daemon.sh` (capture) or
`./launch` (interactive menu) — see §11.

## 2. Quick start

Conda env: `polymarket-env` (Python 3.11.14).

| Goal                                        | Command                                                                                       |
|---------------------------------------------|-----------------------------------------------------------------------------------------------|
| Interactive launch                          | `./launch`                                                                                    |
| Capture-producing live-dryrun (12 h shape)  | `./launch_daemon.sh live dryrun`                                                              |
| Test suite                                  | `pytest tests/ active_bots/tests/`                                                            |
| Lint check                                  | `ruff check active_bots/ tests/ scripts/ daemon_base_v1.py`                                   |
| Shell lint                                  | `shellcheck launch launch_daemon.sh`                                                          |
| Replay against a capture                    | `python -m polyhustle.cli --config <replay_config.json>` (see `docs/REPLAY.md`)               |
| Capture health check                        | `python scripts/check_book_feed.py daemon_state/book_feed/<DATE>/ --btc-tape daemon_state/scrapes/<session>/btc_ticks.jsonl` |
| Kill switch (immediate stop of all entries) | `touch daemon_state/KILL`                                                                     |

Daemon controls: `./daemon_base_v1 status / stop / log`. Presets:
`~/.polymarket-hustle/presets/`. Launcher reference: `docs/LAUNCHER.md`.

## 3. Repo layout

```
polymarket-hustle/
├── active_bots/             — strategy + execution layer (production)
│   ├── *_strategy.py        — Base / Enhanced / Refined / WalkedVWAP (STRATEGY.md §1)
│   ├── execution/           — Executor protocol; Paper/Live/DryRun/Replay; book.py; fees; risk
│   └── pricing/             — fair price (Φ(d2)), EWMA σ
├── polyhustle/              — modular runtime (data / strategies-shim / execution / orchestrator / cli)
├── daemon_base_v1.py        — legacy headless daemon (still the live-mode trader builder)
├── daemon_base_v1           — bash control script (status / stop / log)
├── launch                   — keyboard-driven menu launcher (whiptail/dialog → polyhustle.cli)
├── launch_daemon.sh         — capture-path launcher (scraper + manifest + tunnel verify)
├── tests/                   — primary test suite
├── scripts/                 — operational tools (check_book_feed.py, consolidate_data, …)
├── scrap/                   — modular historical scraper
├── docs/                    — design + reference docs (STRATEGY.md is at repo root)
├── experiments/             — historical strategy-fork archive + backtest harness
└── data/                    — persisted market data (gitignored)
```

`polyhustle/` runtime: `data/` (DataProvider ABC), `strategies/` (shims
to `active_bots/`), `execution/` (Trader ABC), `orchestrator.py`,
`cli.py`. Repo cleanup audit: `docs/REPO_AUDIT.md`.

## 4. Strategy lineage

`BaseStrategy` → `EnhancedStrategy` (= `TimeBasedStrategy` +
`ProfitGrabber` + `SqueezeDetector`) → `RefinedStrategy` →
`WalkedVWAPStrategy` (book-aware entry + exit gates). Mechanics, tick
walk-through, reject matrix, knob table, non-goals: `STRATEGY.md`
§1, §3-§6.

## 5. Conventions

- **Default-off feature flags.** Every behaviour change ships behind
  an env var that defaults to "off / current behaviour." Pattern:
  `WALKED_VWAP_EXIT_ENABLE`. Paper-validate before flipping.
- **Role-gating.** In `main` mode, only the strategy with
  `role="trader"` posts orders; observers emit `shadow_*` events.
  `comparison` mode runs N traders with isolated paper wallets.
- **Conventional commits, atomic.** `feat / fix / docs / test /
  refactor / chore (scope): …`. One logical change per commit; style
  and logic stay separate.
- **Decimal at the boundary.** `active_bots/execution/book.py` and
  `replay_executor.py` are pure-Decimal. Daemon hot path uses floats;
  canonical fill arithmetic lives in the Decimal modules.
- **Plain-language summary on new docs.** Every new markdown under
  `docs/` or `reports/` opens with `## Plain-language summary` (3-6
  sentences) before the technical body. Applies to substantive edits
  too; not to chat replies, code, commits, or files you aren't editing.

## 6. Safety rules and operational knobs

Non-negotiable per-session rules. Also in
`docs/RESUME_AFTER_HARNESS_REBUILD.md` §5 — duplication is intentional.

**Protected files.** These files require explicit operator
authorisation in chat before any edit, even for what looks like a
small change. Live trading bugs lose real USDC.

- `active_bots/execution/live_executor.py`
- `active_bots/execution/reconciler.py`
- `active_bots/execution/risk_manager.py`
- The `RefinedStrategy` squeeze path in
  `active_bots/refined_strategy.py` (`SqueezeDetector` is disabled
  via `enable_squeeze=False`; do not re-enable or modify the squeeze
  branch without operator sign-off).

**Operational rules with no override:**

- **No push to origin (`Wailydest`).** Local refs only; user pushes manually.
- **No force-push anywhere**, including local branches.
- **No running `MAX_TRADE_SIZE_USDC > 25`** without explicit go-ahead.

**Operational knobs** (strategy-level knobs are in `STRATEGY.md` §5):

| Env var               | Default              | Meaning                                            |
|-----------------------|----------------------|----------------------------------------------------|
| `POLYMARKET_MODE`     | paper                | `paper` / `live` (live needs explicit go-ahead)    |
| `KILL_SWITCH_FILE`    | `daemon_state/KILL`  | If file exists, all entries blocked                |
| `MAX_TRADE_SIZE_USDC` | 100                  | Per-trade size ceiling (auth limit: 25; see above) |
| `MAX_DAILY_LOSS_USDC` | 300                  | Daily loss circuit breaker                         |
| `PORTFOLIO_SIZE_USDC` | (auto-detect)        | Override portfolio total for size scaling          |

New operational knobs go here; new strategy knobs go to `STRATEGY.md` §5.

## 7. Capture verification — the only sanctioned procedure

How to confirm a captured session produced real, usable book data.

1. **Use `scripts/check_book_feed.py` — never ad-hoc bash one-liners.**
   The script counts populated frames across all three event classes;
   one-liners that grep top-level `bids|asks` under-report on captures
   dominated by `book` / `price_change` events.

2. **Predicate.** A record counts as populated iff one of:
   - `type == "snapshot"` AND top-level `bids` or `asks` non-empty
   - `type == "book"` AND `raw.bids` or `raw.asks` non-empty
   - `type == "price_change"` AND any `deltas[*].size != "0"`

3. **Pre-flight check during a live capture.** ~30 minutes in:

   ```bash
   python scripts/check_book_feed.py daemon_state/book_feed/<DATE>/ \
     --btc-tape daemon_state/scrapes/<session_id>/btc_ticks.jsonl
   ```

   | Median ratio | Action                                                |
   |--------------|-------------------------------------------------------|
   | ≥ 80 %       | Healthy. Continue.                                    |
   | 50 – 80 %    | Borderline. Surface and discuss before continuing.    |
   | < 50 %       | Alarm. Kill the capture; diagnose.                    |

   Exit codes mirror this: `0` healthy, `2` borderline, `1` alarm,
   `3` no `.jsonl.gz` files / unreadable.

4. **BTC tape is required.** Every post-H1 capture writes
   `daemon_state/scrapes/<session_id>/btc_ticks.jsonl`. A capture
   without one is unreplayable. The `--btc-tape` flag downgrades the
   verdict to `BOOK_HEALTHY but BTC_TAPE_<status>` if the book is
   fine but the tape is missing.

5. **Inside-netns smoke for scraper changes.** `tests/scraper/` runs
   in default netns; production capture is in polybot netns. Branches
   touching `scripts/scrape_book.py` or the book WS need a 30-min
   `./launch_daemon.sh paper` smoke meeting the 80 % bar.

## 8. Dataflow & events

**Sources.** Binance WS (BTC spot), Polymarket RTDS WS (mid), CLOB WS
(book deltas), all consumed by `daemon_base_v1.py`.

**`daemon_state/`:**

- `state.json` (5 s refresh), `daemon.log` (RotatingFileHandler, SGT timestamps).
- `events.jsonl` — append-only structured event log; canonical for
  dashboards, reconcile, latency fits, ETL.
- `book_feed/<DATE>/<slug>.jsonl.gz` — per-slug WS frame archive.
- `scrapes/<session_id>/` — `manifest.json` + `btc_ticks.jsonl`.

**Persisted parquet.** `~/polymarket-parquet/` for historical books
and trades; produced by `scripts/csv_to_parquet.py` and
`scripts/consolidate_data.py`.

**Analysis.** `experiments/backtest/harness.py` runs offline replays;
`experiments/backtest/reconcile.py` joins fill events against on-chain
history. Specs: `docs/session_capture_design.md`,
`docs/book_walked_replay_backtester_spec.md`, `docs/reconcile_design.md`.

### `entry_rejected.reject_source` (post-H4)

Every `entry_rejected` event in `events.jsonl` carries a
`reject_source` discriminator that R4+ replay reports lean on:

| Value        | Meaning                                                                          |
|--------------|----------------------------------------------------------------------------------|
| `"strategy"` | Gate fired (e.g., `WALKED_VWAP_REJECT` for `empty_book`); `reject_reason` carries the gate code. |
| `"trader"`   | Trader rejected `ACTION_ENTER` post-commit (e.g., `paper_no_fill`). `_open_position` and orchestrator `position` may diverge — follow-up in `docs/POLYHUSTLE_CLI_ROADMAP.md`. |

A non-zero `reject_source=trader` count is informative, not an alarm.

## 9. Testing

`pytest tests/ active_bots/tests/`. Test counts drift; verify with the
command, not from this file.

| Path                              | Covers                                                  |
|-----------------------------------|---------------------------------------------------------|
| `tests/strategy/`                 | Walked-VWAP gates, profit-grabber, role dispatch        |
| `tests/execution/`                | Book deltas, executor invariants, fees, latency, replay |
| `tests/{daemon,scraper,scripts}/` | Book WS, resubscribe contract, ETL                      |
| `active_bots/tests/`              | Portfolio-derived sizing, shutdown timeout              |

`pyproject.toml` scopes collection — strategy-fork tests under
`experiments/` are deliberately not collected.

## 10. Extending — feature-flag checklist

For any behaviour change (new zone, gate, fee model, …):

1. **Branch from Sam-Dev.** Never on `main`.
2. **Default-off feature flag.** Pattern: `WALKED_VWAP_EXIT_ENABLE`.
   Update the source file's knob block and (if a strategy knob)
   `STRATEGY.md` §5.
3. **Tests with the flag both off and on.** Off-branch must be
   bit-for-bit identical to today.
4. **Replay parity.** Flags off → zero divergence vs. prior commit;
   each flag on in turn → diverges at the expected step.
5. **Paper-validate before live.** `POLYMARKET_DRY_RUN=1` session +
   reconcile against on-chain PnL.

Larger work: plan under `docs/superpowers/plans/<YYYY-MM-DD>-<topic>.md`.

**New strategy class:** subclass `Strategy` in `polyhustle/strategies/`
(or shim to `active_bots/`), register in
`polyhustle/cli.py:STRATEGY_REGISTRY`, document in `STRATEGY.md` §1 + §5.

## 11. `launch` vs `launch_daemon.sh`

Two entry points coexist while `polyhustle.cli` grows wrapper-layer
features (scraper, manifest, tunnel verify). Bright-line rule:

| You're doing                                                                  | Use                                                  |
|-------------------------------------------------------------------------------|------------------------------------------------------|
| Capture-producing session (analysis, sweep input, reconcile-comparable)       | `./launch_daemon.sh paper` / `live` / `live dryrun`  |
| Interactive non-capture work (preset re-launch, comparison mode, replay walk) | `./launch`                                           |

`launch_daemon.sh` writes a session manifest, spawns the L2 book
scraper, and (live) verifies the polybot tunnel; `launch` does none of
that yet (gaps in `docs/POLYHUSTLE_CLI_ROADMAP.md`). Until they close,
**capture-producing work must use `launch_daemon.sh`** or the session
is invisible to replay/reconcile.
