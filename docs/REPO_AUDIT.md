# Repo Audit — 2026-05-05

Generated as part of `cleanup/repo-organization-and-sl-asymmetry`.
Branch base: `Sam-Dev` (137 commits ahead of `origin/Sam-Dev`).

This document is the input to Phase 1 (reorganization plan) and Phase 2
(in-place cleanup) of the cleanup pass. **It is authoritative for the
state of the tree at HEAD on this branch and nowhere else.**

---

## 0. Pre-flight notes

- Working tree was dirty at start (modified: `docs/session_capture_design.md`,
  `launch_daemon.sh`, `scrap/config.py`, `scrape_all.sh`,
  `tui/python/dashboard_legacy.py`; deleted `scripts/dashboard_streamlit.py`;
  many untracked files including `STRATEGY.md`, `scrape.py`, scripts/, .env.example).
  The new branch was created without stashing because the untracked files
  (notably `STRATEGY.md`) are required for this task; they travel with the
  branch and are not lost.
- Conda env for this repo: `polymarket-env` (Python 3.11.14). All test commands
  in this doc must be run inside that env.
- Primary test suite: `pytest tests/ active_bots/tests/` — 211 passed, 1 failed
  pre-existing (`tests/execution/test_latency.py::test_fit_raises_not_fitted_on_real_events`,
  depends on the real `daemon_state/events.jsonl` file having data and is fragile
  by design). 77 collection errors come from `experiments/r5_*/active_bots/tests`
  forks and `tui/tests` (separate test root); they are not part of the primary
  suite.
- `STRATEGY.md` lineage matches the code at HEAD. No drift detected:
  - `BaseStrategy` at `active_bots/base_strategy.py:160` — pure fair-price.
  - `TimeBasedStrategy` at `active_bots/enhanced_strategy.py:49` — composed.
  - `ProfitGrabber` at `active_bots/enhanced_strategy.py:218`.
  - `SqueezeDetector` at `active_bots/enhanced_strategy.py:335`.
  - `EnhancedStrategy` at `active_bots/enhanced_strategy.py:490`.
  - `RefinedStrategy` at `active_bots/refined_strategy.py:28` — squeeze-off,
    `TP_DELTA_MIN=0.08`, `TP_ABSOLUTE_FAVOR=0.15`.
  - `WalkedVWAPStrategy` at `active_bots/walked_vwap_strategy.py:206`.
  - `WalkedExitProfitGrabber` at `active_bots/walked_vwap_strategy.py:61`.
  - Daemon dispatch at `daemon_base_v1.py:1095-1124` — correctly assigns
    `walked` → `role="trader"`; refined / enhanced / base are observers.

---

## 1. Directory inventory

| Path                        | Purpose                                                | Verdict           |
|-----------------------------|--------------------------------------------------------|-------------------|
| `active_bots/`              | Live strategy code + execution + pricing               | KEEP              |
| `active_bots/execution/`    | Executors (paper/dry-run/live/replay), book, fees, etc | KEEP              |
| `active_bots/pricing/`      | `compute_fair_price`, sigma estimators                 | KEEP              |
| `active_bots/tests/`        | Strategy-internal tests (max_risk, shutdown timeout)   | KEEP, CONSOLIDATE |
| `active_bots/plots/`        | Static PNGs from a prior backtest run                  | MOVE → `outputs/` |
| `active_bots/backtest_*.csv`| 8.6k-line backtest CSVs                                | MOVE → `outputs/` or DELETE |
| `daemon_base_v1.py`         | The 1958-line trading daemon                           | KEEP              |
| `daemon_base_v1`            | Bash control script (status/start/stop)                | KEEP, RENAME?     |
| `daemon_state/`             | Runtime state (events.jsonl, state.json, book_feed/)   | KEEP (gitignored mostly) |
| `data/`                     | Persisted market data                                  | KEEP              |
| `docs/`                     | Design docs + STRATEGY-adjacent specs + superpowers/   | KEEP              |
| `experiments/`              | 37 strategy-fork dirs from the fine-tuning sweep       | KEEP, ARCHIVE     |
| `launch_daemon.sh`          | Production launcher (paper/live, scraper, TUI)         | KEEP              |
| `orderbook_monitor.py`      | Legacy 7-day book snapshotter (top-level)              | DEAD — not imported |
| `rtds_monitor.py`           | Legacy WS trade tail (top-level)                       | DEAD — not imported |
| `web_monitor.py`            | Legacy aiohttp price chart                             | DEAD — not imported |
| `scrape.py`                 | Old monolith historical scraper                        | LIKELY DEAD — superseded by `scrap/` + `scrape_all.sh` |
| `scrape_all.sh`             | Multi-asset orchestrator for `scrap/` dataset scripts  | KEEP              |
| `scrap/`                    | Six-stage modular scraper (markets, spot, trades, …)   | KEEP              |
| `scripts/`                  | One-off operational scripts (consolidate, csv→parquet, onboard, etc) | KEEP |
| `tests/`                    | Primary test suite (`tests/strategy`, `/execution`, `/daemon`, `/scripts`) | KEEP |
| `tui/`                      | Textual + Rust TUI (`tui/python/`, `tui/rust/`)        | KEEP              |
| `STRATEGY.md`               | Lineage doc (untracked but canonical)                  | TRACK             |
| `RUNBOOK.md`                | Operational runbook                                    | KEEP              |
| `README.md`                 | 19 bytes — placeholder                                 | REWRITE           |
| `.env.example`              | Env template (untracked)                               | TRACK             |
| `Polymarket-History-2026-05-04.csv` | On-chain history CSV pulled from polymarket UI | MOVE → `data/` or DELETE |
| `TUI_1.png`, `TUI_2.svg`    | Screenshots                                            | MOVE → `docs/img/` or DELETE |
| `._*` files (×5)            | macOS resource forks                                   | DELETE            |
| `.DS_Store`, `._.DS_Store`  | macOS junk                                             | DELETE + add to .gitignore |
| `compass_artifact_*.md` (its `._` companion) | macOS resource fork only — original file is not present | DELETE the resource fork |

---

## 2. Dead code candidates (not imported anywhere)

Verified with `grep -lrn "import.*<name>\|from <name>" --include="*.py" .`
under both `tests/` and runtime:

| File                       | Status        | Evidence                                                |
|----------------------------|---------------|---------------------------------------------------------|
| `rtds_monitor.py`          | DEAD          | No imports anywhere; WS-tail tool predating `scrap/`    |
| `web_monitor.py`           | DEAD          | No imports anywhere; aiohttp price chart                |
| `orderbook_monitor.py`     | DEAD          | No imports anywhere; replaced by daemon's `book_feed/`  |
| `scrape.py`                | LIKELY DEAD   | Only referenced from doc text + `scrap/config.py:251`'s schema comment; the live scrape pipeline runs through `scrape_all.sh` → `scrap/0[1-7]_*.py` |
| `active_bots/backtest_fair_price.csv` | STALE | 8640 lines, generated by an old harness; not regenerated by current backtest path |
| `active_bots/backtest_trades.csv`     | STALE | 762 lines; same provenance |
| `active_bots/plots/`       | STALE         | 6 PNGs from the backtest harness; no regenerator under the current code path |

---

## 3. Duplication

`experiments/` contains 37 fork dirs, of which all r-namespace and most
n-namespace dirs include their own `active_bots/` copy of the strategy
code at the time of the fork. Most also include their own `daemon_base_v1.py`
copy. Total fork weight: **14 MB**.

The forks are **historical** — the campaign that produced `RefinedStrategy`
(2026-04-21 fine-tuning) is closed; the canonical winner is encoded in
`active_bots/refined_strategy.py`. The fork copies are referenced only by
their own per-fork `runs/*.csv` outputs.

**Canonical version of every strategy file lives at the top of `active_bots/`.**
The fork copies are archive material and should not be edited. They are also
the source of the 77 pytest collection errors in the global suite (each fork
has a stale `tests/test_effective_max_risk.py` and `tests/test_shutdown_timeout.py`).

Recommended: configure `pytest` to only collect under `tests/` and
`active_bots/tests/` (the primary suite). Forks remain on disk for archive,
but `pytest` from the repo root no longer descends into them.

---

## 4. Docstring coverage (active_bots / execution / pricing)

Measured by `ast.get_docstring` over all public (non-`_`-prefixed) functions
and classes:

| Module group                           | Pub funcs | Doc'd | Coverage |
|----------------------------------------|-----------|-------|----------|
| `active_bots/*.py` + `execution/` + `pricing/` | 115 | 44    | **38 %** |
| Public classes                         | 45        | 19    | **42 %** |

Worst offenders (0 % docstring on public functions):

- `active_bots/execution/dry_run_executor.py` (0/5)
- `active_bots/execution/event_logger.py` (0/6)
- `active_bots/execution/risk_manager.py` (0/5)
- `active_bots/execution/replay_executor.py` (0/7)
- `active_bots/pricing/fair_price.py` (0/2)
- `active_bots/pricing/variance.py` (0/5)
- `active_bots/refined_strategy.py` (0/1; class has docstring, `__init__` doesn't)
- `active_bots/walked_vwap_strategy.py` (0/4 public methods; module + classes have docstrings)

Phase 2 target: ≥ 95 % public docstrings across these modules. `__init__`
docstrings are exempt where the class docstring already documents the
parameters.

---

## 5. Type-hint coverage

Same scope. A function counts as "fully typed" iff `returns is not None`
AND every non-self parameter has an annotation:

| Module group                           | Pub funcs | Typed | Coverage |
|----------------------------------------|-----------|-------|----------|
| `active_bots/*.py` + `execution/` + `pricing/` | 115 | 92    | **80 %** |

The 23 untyped public functions are mostly trivial accessors and `__init__`
methods. Phase 2 target: ≥ 95 % full type coverage.

---

## 6. Test coverage

### What's tested

- `tests/strategy/test_walked_vwap.py` (1107 lines) — entry gate full reject
  matrix, exit gate matrix incl. force-window, partial-fill, NaN VWAP,
  fee-direction, wrapper book-stash, `_open_position` sync.
- `tests/strategy/test_role_dispatch.py` (125 lines) — per-strategy role
  gating (only `role="trader"` posts; observers shadow).
- `tests/execution/` — book deltas, dry-run/live/paper/replay executor
  invariants, fees, latency, hypothesis-driven invariants on `book.py`.
- `tests/daemon/test_book_subscription.py` — book WS subscription behaviour.
- `tests/scripts/test_consolidate_data.py` — the ETL pipeline.
- `active_bots/tests/test_effective_max_risk.py` — portfolio-derived sizing.
- `active_bots/tests/test_shutdown_timeout.py` — daemon shutdown timeout.

### High-risk untested paths

- `ProfitGrabber.check_exit` — has heavy dedicated coverage via
  `WalkedExitProfitGrabber.check_exit` tests (which delegate to the parent
  via `super().check_exit`), but no direct test file targets the parent
  class in isolation. Phase 4A/4B touches this path; tests in Phase 4
  fix the gap.
- `WalkedExitProfitGrabber._compute_translated_market_price_up` —
  covered indirectly through the matrix tests but the helper is not
  unit-tested in isolation.
- `WalkedVWAPStrategy._gate` reject branches — comprehensively covered.
- `compute_fair_price` boundary conditions (S==K, sigma~0, expired) —
  not unit-tested.
- `SqueezeDetector` — squeeze is disabled in the champion config so no
  tests; restoring tests would be required if it ever re-enables.

### Pytest setup gap

- Repo has **no `pyproject.toml` or `pytest.ini`**. There is a `.hypothesis/`
  cache directory but no top-level config. As a result, `pytest` from the
  repo root collects under `experiments/` and `tui/tests/` and produces
  77 collection errors from stale forks. Phase 2 will add a minimal
  `pyproject.toml` with `[tool.pytest.ini_options].testpaths` set to
  `tests/` + `active_bots/tests/`.

---

## 7. Knob inventory

Cross-referenced against `STRATEGY.md` §5 (the cheat-sheet table) and
`launch_daemon.sh` env exports. Format: `env var name | source file:line | doc'd in §5? | notes`.

### Strategy knobs (all in §5 already)

| Env var                          | Source file                                  | §5? | Notes |
|----------------------------------|----------------------------------------------|-----|-------|
| `TP_DELTA_MIN`                   | `active_bots/enhanced_strategy.py:174`       | yes | RefinedStrategy overrides default to 0.08 |
| `SL_DELTA_MIN`                   | `active_bots/enhanced_strategy.py:177`       | yes | |
| `SL_DELTA_MAX`                   | `active_bots/enhanced_strategy.py:178`       | yes | |
| `FORCE_EXIT_BEFORE_S`            | `active_bots/enhanced_strategy.py:182`       | yes | |
| `TP_ABSOLUTE_FAVOR`              | `active_bots/enhanced_strategy.py:187`       | yes | RefinedStrategy overrides default to 0.15 |
| `WALKED_VWAP_MARKET_STALENESS_S` | `active_bots/walked_vwap_strategy.py:36`     | yes | |
| `WALKED_VWAP_MIN_TOP_RATIO`      | `active_bots/walked_vwap_strategy.py:39`     | yes | |
| `WALKED_VWAP_EDGE_MIN`           | `active_bots/walked_vwap_strategy.py:41`     | yes | |
| `WALKED_VWAP_PARTIAL_OK`         | `active_bots/walked_vwap_strategy.py:43`     | yes | |
| `WALKED_VWAP_FEE_CATEGORY`       | `active_bots/walked_vwap_strategy.py:45`     | yes | |
| `WALKED_VWAP_EXIT_ENABLE`        | `active_bots/walked_vwap_strategy.py:50`     | yes | |
| `WALKED_VWAP_EXIT_STALENESS_S`   | `active_bots/walked_vwap_strategy.py:52`     | yes | |
| `WALKED_VWAP_EXIT_PARTIAL_OK`    | `active_bots/walked_vwap_strategy.py:54`     | yes | |
| `WALKED_VWAP_EXIT_FALLBACK_MID`  | `active_bots/walked_vwap_strategy.py:57`     | yes | |

### Operational / executor knobs (not in §5 — operational, not strategy)

| Env var                          | Source file                                  | Purpose                              |
|----------------------------------|----------------------------------------------|--------------------------------------|
| `POLYMARKET_MODE`                | `daemon_base_v1.py:228`, `.env.example`      | `paper` / `live`                     |
| `POLYMARKET_DRY_RUN`             | `daemon_base_v1.py:229`, `.env.example`      | Live mode but no order POST          |
| `POLYMARKET_PRIVATE_KEY`         | `clob_client_factory.py:39`, `.env.example`  | EOA signing key                      |
| `POLYMARKET_FUNDER`              | multiple, `.env.example`                     | Magic/proxy wallet address           |
| `POLYMARKET_API_KEY`             | `clob_client_factory.py:41`                  | CLOB API auth                        |
| `POLYMARKET_API_SECRET`          | `clob_client_factory.py:42`                  | CLOB API auth                        |
| `POLYMARKET_API_PASSPHRASE`      | `clob_client_factory.py:43`                  | CLOB API auth                        |
| `POLYMARKET_CHAIN_ID`            | `clob_client_factory.py:45`                  | Polygon = 137                        |
| `POLYMARKET_SIGNATURE_TYPE`      | `clob_client_factory.py:49`                  | 0=EOA, 1=Magic, 2=browser            |
| `KILL_SWITCH_FILE`               | `daemon_base_v1.py:285`, `.env.example`      | Path that, if present, halts trading |
| `MAX_DAILY_LOSS_USDC`            | `.env.example`                               | Risk guard                           |
| `MAX_TRADE_SIZE_USDC`            | `.env.example`                               | Risk guard                           |
| `POLYGON_RPC_URL`                | `scripts/check_v2_allowances.py:59`          | Polygon JSON-RPC                     |

`PORTFOLIO_SIZE_USDC` and `MAX_BET_PCT` are referenced in
`active_bots/tests/test_effective_max_risk.py` and per the
`project_dry_run_executor_asymmetry` memory; the production code path that
honours them is in the daemon and was added during the dry-run executor
asymmetry fix. Not in `.env.example` yet.

**Conclusions**:
- All strategy knobs in source ↔ STRATEGY.md §5: in sync.
- Operational knobs are not in §5 by design (§5 is strategy-only).
- `.env.example` should list `PORTFOLIO_SIZE_USDC`, `MAX_BET_PCT` for
  discoverability. Phase 2 adds these.

---

## 8. Reorganization plan (PROPOSED — needs green light)

The following are **renames / moves** that the task spec requires explicit
confirmation for. They are NOT executed yet.

1. **Quarantine the legacy top-level monitors.**
   - Move `rtds_monitor.py`, `web_monitor.py`, `orderbook_monitor.py`,
     `scrape.py` → `legacy/`. Add a `legacy/README.md` explaining why
     they're parked and what replaced them.
   - Or: delete outright. They are dead, not imported, and superseded
     by `scrap/` + the daemon's own `book_feed/`.

2. **Move stale backtest artifacts off `active_bots/`.**
   - `active_bots/backtest_fair_price.csv` → `outputs/legacy_backtest/`
   - `active_bots/backtest_trades.csv` → `outputs/legacy_backtest/`
   - `active_bots/plots/` → `outputs/legacy_backtest/plots/`
   - These are output artifacts, not source code, and they pollute
     `active_bots/` for greppability.

3. **Track the canonical strategy doc and env template.**
   - `STRATEGY.md` is currently untracked. `git add STRATEGY.md`.
   - `.env.example` is currently untracked. `git add .env.example`.

4. **Remove macOS junk.**
   - Delete every `._*` file at repo root and `.DS_Store` / `._.DS_Store`.
   - Add `.DS_Store` and `._*` to `.gitignore`.

5. **Move screenshots to `docs/img/`.**
   - `TUI_1.png`, `TUI_2.svg` → `docs/img/`.
   - `Polymarket-History-2026-05-04.csv` → `data/onchain_history/`.

6. **Quarantine experiment fork tests from default pytest collection.**
   - Add `pyproject.toml` `[tool.pytest.ini_options].testpaths = ["tests", "active_bots/tests"]`.
   - This is in-place config (not a move) and is part of Phase 2.

7. **Active_bots layout: leave alone.**
   - The current shape (`active_bots/{base,enhanced,refined,walked_vwap}_strategy.py`
     + `execution/` + `pricing/`) reflects the lineage cleanly. No moves.

8. **`daemon_base_v1` (bash) and `daemon_base_v1.py` (Python) co-exist
   confusingly.** Optional rename: `daemon_base_v1` (bash) → `daemonctl.sh`.
   Low priority; flagged for the user's call.

9. **Untracked plan/spec files in `docs/superpowers/plans/`.**
   - `2026-04-23-dashboard-textual-rewrite.md` and
     `2026-04-27-walked-vwap-strategy.md` — `git add`.

**Items 6 (pytest config) and 4 (macOS junk delete + gitignore) are minor
and can be folded into Phase 2 in-place cleanup with no surprise. Items
1, 2, 3, 5, 8, 9 require user confirmation.**

---

## 9. Phase 2 in-place cleanup plan (NO confirmation needed per spec)

Per the prompt's section "Phase 2 — In-place Cleanup (no approval needed)",
the following will execute without further user input:

1. Add docstrings to ≥ 95 % of public functions/classes in `active_bots/`,
   `active_bots/execution/`, `active_bots/pricing/`. Format: short
   one-liner, then `Args:` / `Returns:` / `Raises:` if applicable.
   Reference `STRATEGY.md §N` where the function implements documented
   logic.
2. Add type annotations to ≥ 95 % of public signatures in the same modules.
3. Set up `pyproject.toml` with `ruff` config (`line-length = 100`,
   `target-version = py311` to match the conda env), and a
   `[tool.pytest.ini_options]` block scoping `testpaths` to the primary
   suites.
4. Run `ruff format` once across `active_bots/`, `tests/`, `scripts/`,
   `daemon_base_v1.py`. Commit separately from logic.
5. Run `ruff check --fix` for mechanical violations only. Commit separately.
6. Add inline comments where non-obvious (bell-curve fee, `Φ(d₂)`,
   walked-VWAP arithmetic) — STRATEGY.md cross-references.
7. Promote magic numbers in `enhanced_strategy.py`, `walked_vwap_strategy.py`,
   `base_strategy.py` to class constants with origin comments.
8. Delete macOS junk files (`.DS_Store`, `._*`) and add to `.gitignore`.

Each chunk lands in its own conventional commit (`docs:`, `style:`,
`refactor:`, `chore:`).

---

## 10. Phase 4 fixes — prerequisites verified

The three asymmetry fixes from the prompt's Phase 4 are wireable as
specified:

- **Fix A — `SL_ABSOLUTE_AGAINST` cap.** `ProfitGrabber.__init__` already
  accepts a `tp_absolute_favor` parameter; mirror with `sl_absolute_against`
  (default `None` = disabled). `check_exit` SL branch at
  `enhanced_strategy.py:303-304` is the single edit point.
- **Fix B — adaptive_sl decay.** `adaptive_sl` at `enhanced_strategy.py:207-215`
  takes `(edge, sl_delta_min, sl_delta_max)`. Mirror `adaptive_tp`'s
  signature with `time_remaining`, gate the call site at
  `enhanced_strategy.py:295` behind `SL_DECAY_ENABLE`.
- **Fix C — walked-edge size recompute.** `walked_vwap_strategy.py:_gate`
  on pass currently overwrites `entry_price` but leaves `size_shares`
  un-touched. Insert a recompute call between line 357 and line 358 that
  uses `compute_position_size(walked_edge, eff_vwap, …)`.

All three fixes preserve bit-for-bit current behaviour with their flags
unset, mirroring the `WALKED_VWAP_EXIT_ENABLE` precedent.
