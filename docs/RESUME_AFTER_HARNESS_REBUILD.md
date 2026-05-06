# Strategy work — resumption checkpoint

You're picking up the strategy thread after the upcoming TUI / repo /
simulation-fidelity rebuild has landed. This file is the 5-minute
hand-off: where things stopped, what's queued, and the order to read
the rest of the docs.

Generated 2026-05-06. The numbers below are pinned to that date — the
rebuild may have shifted file paths, line numbers, and the daemon's
runtime layout. Treat this file as a contract about INTENT and the
git-level state, not a live cross-reference into the source tree.

---

## 1. Where strategy work stopped

Three lines of work are open. None merged, none lost.

### 1.1 `feat/quant-flags-phase5` — 11 commits, 270 tests, ruff clean, NOT merged

Branch base: `Sam-Dev` after the Phase 4 cleanup branch fast-forwarded into it. Branch tip at hand-off: `510f759`. The 11 commits in chronological order:

```
510f759  feat(tui): show WALKED_VWAP as the main panel  (cherry-pick of 01ab6c8)
a20b7f1  docs+test(strategy): retire stale reject_reason names from active code
8bf57f2  docs(strategy): F04 SIGMA_HAIRCUT directional intent (correct the inversion)
2611611  test(phase5): cross-flag parity smoke
a30b0fa  docs(strategy): STRATEGY.md updates for Phase 5 quant flags
878573c  feat(walked-vwap): per-zone WALKED_VWAP_EDGE_MIN_<ZONE> override (F03)
9039868  feat(strategy): per-zone EDGE_MIN_OVERRIDE (F08+F09)
73703f8  feat(pricing): SIGMA_HAIRCUT env knob (F04)
61fa72d  chore(diagnostic): warn when fair_enh and fair_base are degenerate (F07)
5d7aa95  feat(walked-vwap): granular empty_book reasons (I01)
5915858  feat(exits): triggering_rule field on exit-fill events (I02)
```

Every change is gated default-off (or default-unset, default-1.0 for the haircut). The branch passes 270 tests, has 1 pre-existing latency failure unrelated to anything Phase 5 touches, and is ruff-clean against the operational scope (`active_bots/ tests/ scripts/ daemon_base_v1.py`).

What each Phase 5 change is for, in plain language:

- **`I01` — granular `empty_book` reject vocabulary.** Replaces the catch-all `empty_book` reason with `asks_empty`, `bids_empty`, `walk_returned_nan`, `top_smaller_than_request`, `book_baseline_missing`. Reconcile and dashboards can now distinguish "book never arrived" from "book arrived but bids/asks drained" from "walk produced NaN". This is a vocabulary refactor, not a behaviour change — log line keys (`walked_vwap_exit_liquidity_gap`, `walked_vwap_reject`) are unchanged.

- **`I02` — `triggering_rule` field on every exit action.** `ProfitGrabber.check_exit` now stamps a label naming which branch fired the exit: `adaptive_tp` / `tp_absolute_favor` / `adaptive_sl` / `sl_absolute_against` / `force_close_tp` / `force_close_sl`. Force-window labels take precedence so "this exit was time-forced, not threshold-triggered" is preserved. Daemon propagates the label into `exit_filled` events for every strategy.

- **`F03` — `WALKED_VWAP_EDGE_MIN_<ZONE>` per-zone overrides.** Per-zone walked-edge floors override the global `WALKED_VWAP_EDGE_MIN=0.02`. Resolves zone from the parent action's `time_zone` field. Default unset = fallback to global.

- **`F04` — `SIGMA_HAIRCUT` env knob.** Multiplicative haircut on the EWMA-published sigma, applied at the daemon's sigma-publish point so every strategy sees the same adjusted value. Default 1.0 = no-op. **Directional caveat:** the math is `d2 = ln(S/K)/(σ·√τ)`, so smaller sigma → LARGER `|d2|` → Phi(d2) AWAY from 0.5. The Phase 5 spec text was inverted on this point; the test suite asserts the actual math; STRATEGY.md §5 carries an "F04 directional intent" note explaining MASTER_REPORT §S2's actual reasoning (the over-prediction is conditional on bot-entered markets; sigma was too LARGE; haircut shrinks σ to compress the entry window; the lift mechanism is "fewer marginal entries", not better-calibrated probabilities).

- **`F07` — `FAIR_ENH_DEGENERATE_TICKS` watchdog.** Diagnostic only. Counts consecutive ticks where `|fair_enh − fair_base| < 1e-9`; emits one `logger.warning` per market cycle once the count crosses the threshold (default 300). No state change, no exit, no auto-fix. The R2.2 capture showed it firing in **144/144 markets** — the pricing-layer wiring producing a non-trivial `fair_enh` is currently flat. Investigation deferred to a separate branch.

- **`F08 + F09` — `EDGE_MIN_OVERRIDE_<ZONE>` per-zone overrides.** Same pattern as F03 but for `TimeBasedStrategy`'s per-zone edge_min. Unset = TIME_ZONES table default per zone (0.20 / 0.12 / 0.08 / 0.15 for early / mid / sweet_spot / late_gamma).

The Phase 5 §8 lineage section in STRATEGY.md (added in `a30b0fa`) cross-references each change_id to a placeholder rationale paragraph, with a note that the `MASTER_REPORT.md` and `decision_matrix.csv` files referenced by the spec were never committed to the repo as of hand-off — when those land, the §8 table's "Rationale" column needs backfilling from `decision_matrix.csv`.

### 1.2 `feat/quant-flags-phase5b` — 3 commits, NOT merged

Branch base: `Sam-Dev` (NOT `feat/quant-flags-phase5`). Branch tip: `f667eb4`. Three commits:

```
f667eb4  docs(strategy): POSITION_SIZE_EDGE_CAP row + Phase 5b lineage section
bf31f29  test(sizing): POSITION_SIZE_EDGE_CAP — 4 cases + robustness
2d5a380  feat(sizing): add POSITION_SIZE_EDGE_CAP env knob (default unset)
```

Adds **`POSITION_SIZE_EDGE_CAP`** to `active_bots/base_strategy.py::compute_position_size`. When set to float E, the linear-interp input is treated as `min(edge, E)` — caps high-edge size escalation without changing the entry decision (the `edge < edge_min` early-return runs against the un-capped edge). Default unset = bit-for-bit identical to pre-Phase-5b.

Why a separate `5b` branch and not part of `feat/quant-flags-phase5`:

- Phase 5 was scope-locked when it closed; this knob is a new finding that came out of the R2.2 attribution work that ran AFTER Phase 5 closed.
- Off Sam-Dev (not off Phase 5) so it can land independently if the operator wants to ship the sizing ceiling without committing to the rest of Phase 5.

The rationale lives in `reports/r2.2_walked_vwap_loss_attribution.md` §(b): the high-walked-edge SL bucket (7 trades, −$41.39 of the $59.34 total SL loss in the R2.2 capture) was a position-size × adaptive-SL-band amplification problem — same 53 % win-rate across all walked_edge buckets × full-saturation size for edge ≥ EDGE_MAX × widest-band SL for edge ≥ 0.30. The data argues against raising `WALKED_VWAP_EDGE_MIN` (low bucket is net positive); a sizing ceiling addresses the root cause without changing selection.

### 1.3 `reports/r2.2_sweep_design.md` — designed, NOT executed, NOT committed

A 256-combination joint-grid sweep specified against the R2.2 capture, currently held in `git stash@{0}` (label: `sweep-design-pending-harness-rebuild`) on the `feat/quant-flags-phase5b` branch. The doc spells out the five sweep axes (`MIN_TOP_OF_BOOK_SHARES_RATIO`, `WALKED_VWAP_PARTIAL_OK`, `SL_ABSOLUTE_AGAINST`, `POSITION_SIZE_EDGE_CAP`, `SL_DECAY_ENABLE`), what's deliberately excluded and why, the GX10 capacity estimate (≈ 64 min wall-clock with `multiprocessing.Pool(20)` at a worst-case 5-min sequential per combination), the per-combination metrics including per-walked-edge-bucket PnL splits comparable with the attribution buckets, and the strict-dominator + Sharpe-rank stop criteria. §4 of the design holds the deferred whale-flow research-branch spec.

Pop the stash, review, decide: commit on Sam-Dev as-is, or revise against whatever replay infrastructure the harness rebuild produced.

### 1.4 `tune/quant-defaults` — NOT opened

Waits on the sweep producing numbers. Its job is to flip env defaults to "on" in code based on the strict-dominator set, ship that as one commit per default change, and add the corresponding env vars to `launch_daemon.sh`'s `--preserve-env` netns list (extending the pattern from `454dd11`).

### 1.5 `research/whale-flow-validation` — NOT opened

One-paragraph spec lives in `reports/r2.2_sweep_design.md` §4. The R2.2 capture is genuinely later than `MASTER_REPORT`'s training window; if the whale identity list is frozen as of MASTER_REPORT's date, R2.2 IS a held-out test set. Acceptance criterion: OOS Sharpe ≥ 2.0 on the unchanged top-100 × thr=1000 × t=240s gate. **Analysis only — no code changes on this branch.** It's a strict prerequisite to any code implementing E01.

### 1.6 `feat/dryrun-true-sign` — NOT opened

Follow-up to the modular-architecture refactor. `polyhustle.execution.dryrun_trader.DryrunTrader` (added in 2026-05-06's `refactor/modular-architecture` branch) emits a runtime `RuntimeWarning` at `__init__` flagging that it produces live-shape fills *without actually signing*. Today's `DryRunExecutor` and `LiveExecutor.dry_run=True` both short-circuit before py-clob-client's `create_and_post_market_order`, so the signing pathway is not exercised in any current code path.

The follow-up branch's job is to add a separable sign-without-post call path so DryrunTrader actually exercises the signing code (catching credential / EIP-712 / order-encoding regressions before they hit a live POST). It needs **explicit protected-file authorisation** to edit `active_bots/execution/live_executor.py` because the cleanest implementation reuses the existing `_post_market_order` plumbing with a flag that returns the signed args without calling `create_and_post_market_order`.

Until that branch lands, the runtime warning is the operator-visible signal that DryrunTrader is paper-fills-with-live-metadata, not a true sign-test.

---

## 2. The R2.2 capture

The single most important on-disk artefact at hand-off. Path layout:

- `daemon_state/scrapes/2026-05-05T12-50-04Z/manifest.json` — session manifest (mode `live_dryrun`, 12 h, daemon `git_sha=510f759`).
- `daemon_state/events.jsonl` — append-only across all sessions; the R2.2 window is `1777985404.384 ≤ ts ≤ 1778028677.488` (UTC `2026-05-05 12:50:04 → 2026-05-06 00:51:17`).
- `daemon_state/daemon.log` — append-only, **timestamps are local SGT (UTC+8)**, easy to misparse. Convert before filtering.
- `daemon_state/dashboard_history.jsonl` — 5,547 BTC ticks. **Coverage is partial: starts at 17:08 UTC, missing the first 4 h 17 min of the session.** Per-market σ computable for 94 of 143 fully-observed markets only.
- `daemon_state/book_feed/{2026-05-05,2026-05-06}/*.jsonl.gz` — 296 files, 123.4 MB total. Dense (~675 events/sec on a representative slug). This is what the replay backtester reads.

Window characterisation: 144 `market_rollover` events, **143 fully-observed** (started AND resolved within window), 1 partial (final market started at 00:50:00). Capture-mechanic health: zero Binance disconnects, zero CLOB book disconnects, 7 RTDS reconnect events (clean recoveries), zero I01-empty book rejects in the entry gate.

Two open issues against this capture:

- **F07 watchdog fired in 144/144 markets.** The pricing-layer wiring producing a non-trivial `fair_enh` is flat across the entire session. The watchdog was added to surface this; the investigation belongs to a separate diagnostic branch. It does NOT block the upcoming sweep because no trader-role strategy reads `fair_enh` (`walked_vwap` reads base fair via `RefinedStrategy → EnhancedStrategy → compute_fair_price`, the same fair `base` and `enhanced` see). When the wiring is fixed, the loss attribution should be re-validated.
- **Capture is regime-narrow.** Median realised σ_ann is **0.247** with **93 % of markets in the user-nominal calm bucket** (σ < 0.50). Up/Down split is healthy at 44.2 / 55.8. `late_gamma` zone produced **zero** gate-passing entries. **`tune/quant-defaults` must NOT promote any default to "on" for live trading without a second 12-hour capture in median σ ≥ 0.50.** The sweep against this capture answers a calm-regime-conditional question.

---

## 3. Walked_vwap loss attribution — headline numbers

Restated here so you don't have to re-read the report to re-orient. Source: `reports/r2.2_walked_vwap_loss_attribution.md`.

`walked_vwap` was the only strategy that lost money in the 12 h capture: **−$12.41** across 63 entries / 63 exits. Refined (observer): **+$259.71**. Enhanced: **+$203.96**. Base: **+$49.28**. All four ran on identical signals; only the gate's selection and pricing differ.

The −$12.41 understates the situation. Decomposed three ways:

- **Strategy signal at honest pricing: +$51.41.** If walked had used the parent's mid-quoted entries (no walked-VWAP rewrite) on its 63 accepted entries, the strategy would have been net +$51.41. The signal works.
- **Fill-realism tax: −$63.82** (sum of `pnl_mid − pnl` across all 63 trades). This is the irreducible cost of post-fee book-walked entries vs. mid-quoted entries. Real money the bot pays at the CLOB. NOT flag-tunable.
- **Gate over-rejection cost: ~+$114** (estimated; floor +$50, ceiling +$150). If walked had taken the 58 rejected entries at refined's mid-quoted outcomes minus an estimated fill tax, the recovery is ~$114. THIS is what the sweep can attempt to claw back.

Single largest line item inside the over-rejection bucket: **15 `top_smaller_than_request` rejects, refined observer won 100 % of them, +$140.58** (more than half of refined's session profit). One reject reason × one knob (`MIN_TOP_OF_BOOK_SHARES_RATIO`) accounts for the majority of the recoverable ground.

The actual −$12.41 loss concentration: **7 high-walked-edge SL exits = −$41.39** (mean per-trade −$5.91 in the high bucket vs −$0.34 / −$1.04 in low/mid). Mechanism is **position-sizing × SL-band amplification**, NOT threshold-too-late. Same 53 % win-rate across all three walked_edge buckets × `compute_position_size` saturating at `MAX_RISK` for edge ≥ `EDGE_MAX` × `adaptive_sl` saturating at `SL_DELTA_MAX` for edge ≥ 0.30. High-edge entries land at full size with the widest SL band; when the 47 % loss tail fires, losses scale.

This is exactly why Phase 5b's `POSITION_SIZE_EDGE_CAP` was added: cap the linear-interp input so high-edge sizing doesn't escalate.

---

## 4. What to do when work resumes — pointers, not commitments

The harness rebuild may have moved files Phase 5 / Phase 5b touch. Before doing anything else:

1. **Verify the feature branches still apply on the rebuilt Sam-Dev.** `git rebase Sam-Dev feat/quant-flags-phase5` and `git rebase Sam-Dev feat/quant-flags-phase5b`. If conflicts come up because the rebuild moved files, resolve and re-run the test suites on each branch tip. Both branches were 100 % test-clean at hand-off; if a rebase introduces test failures, that's the rebuild's contract being broken — surface it before proceeding.

2. **Run the sweep per `reports/r2.2_sweep_design.md`** against whatever replay infrastructure the rebuild has produced. The design doc spells out per-combination metrics, baseline reproducibility checks (the baseline combination's `pnl_total` should match the live capture's −$12.41 within 1 %), and the strict-dominator + Sharpe-rank acceptance criteria. If the rebuild has changed the parquet output format or the metrics names, update the design doc before running.

3. **Open `tune/quant-defaults`** from the sweep's strict-dominator set. Each default change is its own commit. Pair each with a `launch_daemon.sh` `--preserve-env` extension if the env var is one the netns currently strips. Paper-validate before promoting to live.

4. **Whale-flow validation runs in parallel** as `research/whale-flow-validation`. Gated on the harness exposing whale-flow data plumbing — until then the spec stays in `reports/r2.2_sweep_design.md` §4.

5. **Eventually merge `feat/quant-flags-phase5` into Sam-Dev.** The branch is held back until the sweep validates which Phase 5 flags actually pay off; merge order should be sweep → tune-defaults → Phase 5 merge or simultaneous, depending on the rebuild's branch-management posture.

---

## 5. Protected list

Restated so the next session can't miss it. **Explicit user authorisation required before any edit** to:

- `active_bots/execution/live_executor.py`
- `active_bots/execution/reconciler.py`
- `active_bots/execution/risk_manager.py`
- The `RefinedStrategy` squeeze path (`SqueezeDetector` is disabled in production via `enable_squeeze=False` in `RefinedStrategy.__init__`; do not re-enable or modify its class without going through the operator).

Operational rules with no override:

- **No push to origin (`Wailydest`).** Local refs only unless explicitly told otherwise. The user pushes manually.
- **No force-push anywhere**, including local branches.
- **No running `MAX_TRADE_SIZE_USDC > 25`** without explicit go-ahead. The R2.2 capture's `effective_max_risk` was auto-detected from the funder; live runs need a separate green-light.

---

## 6. Reading order — first 5 minutes of the new session

In this exact order:

1. **`docs/RESUME_AFTER_HARNESS_REBUILD.md`** — this file.
2. **`reports/r2.2_capture_analysis.md`** — capture inventory, regime characterisation, sweep viability assessment (option (ii) regime-narrow but sufficient).
3. **`reports/r2.2_walked_vwap_loss_attribution.md`** — the three-way loss decomposition (signal value / fill-realism tax / gate over-rejection) plus per-bucket SL/TP analysis. Drives the sweep design.
4. **`reports/r2.2_sweep_design.md`** — currently held in `git stash@{0}` ("sweep-design-pending-harness-rebuild"). Pop and read.
5. **`STRATEGY.md`** — current state of strategy lineage and active knobs. §5 is the canonical knob cheat-sheet, §8 (when populated) is the lineage table.
6. **`CLAUDE.md`** — current state of operational guidance, conventions, knob table for the active branch.

When the rebuild lands, this file should be amended with the rebuild's new docs added to (6) — at minimum: the new harness's design doc, any replay-infrastructure changes, the new launcher / TUI contract if it changed.

---

*Generated 2026-05-06 on Sam-Dev. Hand-off snapshot only — does not capture changes made after this commit.*
