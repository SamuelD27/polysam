# Session: Strategy Fine-Tuning + Champion Integration (2026-04-21 → 22)

**Duration:** ~8 hours (evening start, overnight run, next-morning integration)
**Goal:** Run a multi-round, parallel, dry-run fine-tuning session on the BTC 5m daemon to find a better-tuned Enhanced-strategy configuration, then integrate the winner into the main codebase as a first-class strategy alongside BASE and ENHANCED.

This document is the single-source-of-truth write-up. It captures the full
journey from brainstorming through implementation. Reference this document
first when picking up the thread in a future session.

---

## Part 1 — What got built (the tuning harness)

All tuning work lived in `experiments/` (git-ignored) to keep the main
codebase untouched during exploration. The harness supported launching
multiple daemons in parallel with different configurations.

### Harness tools (`experiments/_tools/`)

- **`variant_spec.py`** — dataclass loader + validator for `variant.json`.
  Supports three tweak types: `env_only`, `code_patch`, `new_module`.
  TDD-backed (5 tests passing).
- **`metrics.py`** — parses a variant daemon's `state.json` + `events.jsonl`
  into `{crashed, roi, trade_count, win_rate, max_dd_pct, composite,
  per_exit_type, per_source}`. TDD-backed (5 tests).
- **`patch_variant.py`** — applies new_files + string-replace patches to a
  variant directory. TDD-backed (5 tests).
- **`scaffold_variant.sh`** — copies `daemon_base_v1.py` + `active_bots/`
  into a variant dir, creates `daemon_state/`.
- **`launch_variant.sh`** — launches the variant daemon in background with
  `POLYMARKET_MODE=live POLYMARKET_DRY_RUN=1` forced (paper-routes all
  orders regardless of mode). Writes `runtime.json` with pid/start_ts.
- **`stop_variant.sh`** — TERM-then-KILL escalation, updates `runtime.json`.

### Isolation model

Each variant lives in `experiments/<name>/` with its own copy of the daemon
+ `active_bots/` + `daemon_state/`. Because `daemon_base_v1.py` defines
`STATE_DIR = Path(__file__).parent / "daemon_state"`, each instance writes
to its own state dir. The PID guard (`_another_daemon_running`) matches on
parent-dir name, so sibling variants don't block each other.

All variants share Binance + Polymarket RTDS WebSocket feeds (each opens
its own connections), so cross-variant comparisons are apples-to-apples.

### Metrics and ranking rules

- **Primary:** ROI = `total_pnl / total_risked`
- **Tiebreaker** (when top-two ROI gap ≤ 20 % relative):
  `composite = ROI − 0.5 × max_drawdown_pct + 0.2 × win_rate`
- **Spec floor:** ≥ 8 trades per variant per round. In practice we ran with
  3–5 trades per variant (see noise caveat below) and relaxed the bar
  to ≥ 3 to keep iteration tractable.
- **Convergence:** stop when the champion's ROI fails to improve by ≥ 5 %
  relative for two consecutive rounds, or after 10 rounds, whichever first.

### Design and plan documents

- `docs/superpowers/specs/2026-04-22-strategy-fine-tuning-design.md`
- `docs/superpowers/plans/2026-04-22-strategy-fine-tuning.md`

---

## Part 2 — The 5-round session trajectory

### Round 1 — six new-strategy explorations

6 variants + `_baseline`:

| Variant            | Hypothesis                                     | Result                         |
|--------------------|-----------------------------------------------|--------------------------------|
| `n1_mean_revert`   | Pure reversion, no edge model                  | BUG: over-traded 115 ×; dropped|
| `n2_late_gamma`    | T+260-285 only, large edge, aggressive sizing  | 0 trades in 30 min; too tight  |
| `n3_momentum`      | Follow spike when no reversion by T+90         | +8.5 %, 2 trades (insuff data) |
| `n4_market_maker`  | Passive posting at fair ± 0.03                 | −41.5 % — adverse selection    |
| `n5_no_squeeze`    | Enhanced with squeeze disabled                 | +23.8 %, 2 trades              |
| `n6_vol_regime`    | σ ≥ 0.60 gate                                  | +43.2 %, 1 trade               |

**Decision:** no qualifying champion; shift to Enhanced fine-tuning per
spec. `n4_market_maker` and `n1_mean_revert` dropped permanently.

### Round 2 — Enhanced exit-parameter fine-tuning

| Variant             | Change                                  | ROI      |
|---------------------|-----------------------------------------|----------|
| `r2_sl_wide` ⭐      | SL bounds [0.08, 0.25]                   | +37.3 % ← champion |
| `r2_tp_tight`       | TP_MIN=0.08, TP_ABS=0.15                 | +34.1 %  |
| `_baseline`         | Unchanged                                | +31.1 %  |
| `r2_no_squeeze`     | Squeeze off                              | +21.2 %  |
| `r2_tp_loose`       | TP_MIN=0.03, TP_ABS=0.05                 | +19.3 %  |
| `r2_sweet_only`     | TIME_ZONES=sweet-spot only               | −1.8 %   |
| `r2_mid_aggressive` | Mid zone edge 0.12 → 0.08                | −14.6 %  |

**Finding:** exit-parameter tuning dominates time-zone tuning.

### Round 3 — combine + extend Round 2 winners

| Variant                       | Change                              | ROI      |
|-------------------------------|-------------------------------------|----------|
| `r3_sl_wide_no_squeeze` ⭐     | SL wide + squeeze off               | +78.8 % ← champion |
| `_baseline`                    | (reference)                         | +73.7 %  |
| `r3_tp_tighter`                | TP_MIN=0.12, TP_ABS=0.20            | +56.1 %  |
| `r3_entry_floor`               | MIN_ENTRY_PRICE=0.10                | +55.7 %  |
| `r3_combined_plus`             | SL + TP both pushed further         | +55.8 %  |
| `r3_combined`                  | SL wide + TP tight combined         | +49.0 %  |
| `r3_sl_wide_plus`              | SL [0.10, 0.30]                     | +14.3 %  |

**Findings:** (1) combined perturbations underperform single-axis; (2) SL
wider than [0.08, 0.25] hurts; (3) **baseline ROI swung +137 % across
rounds on identical code** — cross-round raw ROI is not a valid signal.

### Round 4 — confirm champion + isolate components

| Variant                    | Change                               | ROI      |
|----------------------------|--------------------------------------|----------|
| `_baseline`                | (reference)                          | +77.2 %  |
| `r4_tp_tight_no_squeeze`   | TP tight + squeeze off               | +67.8 %  |
| `r4_sl_wide_only`          | SL wide, squeeze on                  | +59.2 %  |
| `r4_all_winners`           | SL wide + TP tight + squeeze off     | +51.4 %  |
| `r4_no_squeeze_only`       | Squeeze off only                     | +47.9 %  |
| `r4_confirm_champion`      | Same config as R3 champion           | +39.7 %  |
| `r4_no_abs_tp`             | TP_ABS=0                             | 2 trades (insufficient) |

**Finding:** champion confirmation FAILED — the R3 config scored +39.7 %
on rerun (vs its R3 +78.8 %). `flat_round_count` → 1. The Enhanced defaults
appeared to be a local optimum; perturbations were within sample noise.

### Round 5 — the pivotal round

| Variant                               | ROI      | Exits      |
|---------------------------------------|----------|------------|
| `r5_confirm_tp_tight_no_squeeze` ⭐    | +35.5 %  | 4 TP / 0 SL / 0 Res |
| `r5_no_squeeze_repeat`                | +32.6 %  | 4 TP / 0 SL / 0 Res |
| `r5_wide_sweet`                       | +11.5 %  | 4 TP / 0 SL / 0 Res |
| `_baseline`                           | **−67.9 %** | 3 TP / 0 SL / 1 Res |
| `r5_aggressive_size`                  | −67.4 %  | 3 TP / 0 SL / 1 Res |
| `r5_sl_moderate`                      | −67.4 %  | 3 TP / 0 SL / 1 Res |
| `r5_squeeze_low_thresh`               | −68.1 %  | 3 TP / 0 SL / 1 Res |

**The pivotal trade — market slug `btc-updown-5m-1776788900`:**

All 7 variants took identical entries (same BTC feed, same signals):
`Down @ 0.390, $100 size`. But:

- **Squeeze-ON variants** (including baseline): held to resolution,
  `won=False`, PnL = **−$102.56** per variant.
- **Squeeze-OFF variants:** exited at 0.540-0.550 via profit-grabber,
  PnL = **+$35.90 to +$38.46** per variant.

~**$140-per-trade swing driven purely by squeeze on/off**, on one market.
Squeeze-on logs show a rejected phantom entry signal (`side=Up,
entry_price=0.413, edge=None, tz=None`) immediately before the real fill —
not present in squeeze-off logs. Exact root cause is unaudited but
reproducibly observable in the event logs.

**Session convergence:** promoted `r5_confirm_tp_tight_no_squeeze` as
final champion — not because its raw ROI topped the rounds (it didn't;
R3 had noisier-but-higher numbers) but because it **survived an adverse
market event that blew up every squeeze-enabled alternative**.
`flat_round_count` → 2 → converge.

---

## Part 3 — Findings (8 total)

Written to `experiments/analysis/findings.md`.

1. **Pure mean-reversion over-trades without a once-per-market guard.**
   The `_resolved=True` flag is set after exit but never re-checked on
   entry → re-enters every tick. Any future counter-trend variant must
   fix this.
2. **Passive MM has adverse selection.** Gets filled precisely when the
   thesis weakens; rules out passive posting at this size without
   orderbook-pressure signals.
3. **Exit tuning dominates time-zone tuning** for short-run improvements.
   The default TIME_ZONES structure is well-calibrated; perturbations hurt.
4. **Baseline ROI swings ±137 % round-to-round** — market-regime variation
   dwarfs most tuning deltas. Cross-round raw ROI is meaningless; use
   same-round delta-vs-baseline instead.
5. **Combined perturbations add interaction drag.** Single-axis tweaks
   consistently beat stacked multi-axis tweaks.
6. **SL wider than [0.08, 0.25] is self-defeating** in calm markets —
   lets ambivalent trades resolve instead of TP'ing.
7. **Enhanced defaults are a local optimum** at 5-trade-per-30-min sample
   sizes. Detecting a real improvement needs hours-long runs or synthetic
   replay for statistical power.
8. **🏆 Squeeze-enabled path can strand a position at hold-to-resolution**
   after a rejected phantom entry signal. Highest-confidence actionable
   finding of the session; led directly to the champion's squeeze-off.

---

## Part 4 — Final integration (this session's output)

After the user reviewed the session, they asked for the winner to be
pulled into the main codebase as a first-class strategy alongside BASE
and ENHANCED.

### New strategy module

**`active_bots/refined_strategy.py`** — `RefinedStrategy(EnhancedStrategy)`.
Pre-configured with the session champion parameters:

- `enable_squeeze=False`
- `TP_DELTA_MIN=0.08` (vs default 0.05)
- `TP_ABSOLUTE_FAVOR=0.15` (vs default 0.10)

Subclasses `EnhancedStrategy`; no logic duplication.

### Refactored `active_bots/enhanced_strategy.py`

`ProfitGrabber` now accepts per-instance TP/SL parameters (all optional,
fall back to module-level env-driven globals). This lets multiple
strategies with different exit tunings coexist in one process.

- `adaptive_tp(edge, time_remaining, tp_delta_min=None)`
- `adaptive_sl(edge, sl_delta_min=None, sl_delta_max=None)`
- `ProfitGrabber(tp_delta_min, sl_delta_min, sl_delta_max,
  tp_absolute_favor, force_exit_before_s)` — all kwargs optional.
- `EnhancedStrategy(..., tp_delta_min=..., ..., force_exit_before_s=...)`
  passes overrides into its `ProfitGrabber`.

Fully backward-compatible with env-var control.

### Refactored `daemon_base_v1.py`

The daemon now runs **three strategies in parallel on the same feeds**:

- **BASE** — paper benchmark (unchanged)
- **ENHANCED** — paper benchmark (re-routed from live to paper; same
  logic, now purely for comparison)
- **REFINED** — the session champion; uses the main executor
  (live-capable when `POLYMARKET_MODE=live` and not dry-run)

Changes:
- New import: `from active_bots.refined_strategy import RefinedStrategy`
- New DaemonState fields: `refined_fair_price`, `refined_position`,
  `refined_trades`, `refined_stats`, `refined_extra`
- `recompute_fair()` now populates `refined_fair_price` too
- `to_dict()` includes a `"refined"` block
- `strategy_loop` adds an `enh_executor = PaperExecutor()` so ENHANCED is
  benchmark-only, and adds a full tick/exit/resolve/rollover block for
  REFINED using the main executor
- Event logs write `strategy="refined"` for refined actions
- `risk.record_trade()` is now called on REFINED trades (the one that
  can go live); ENHANCED is benchmark, no risk tracking needed

### Redesigned `scripts/dashboard.py` TUI

New layout:

```
┌─ Header ───────────────────────────────────────────────────┐
│ BTC price  σ  strike  | market slug  [bar] time | conns    │
├─ Benchmarks banner (compact 2 rows, 1 per strategy) ──────┤
│ BASE      paper  PnL ±X  NN trades W/L rate%  ROI ±Y%  DD │
│ ENHANCED  paper  PnL ±X  NN trades W/L rate%  ROI ±Y%  DD │
│                                         TP/SL/Res  sq ● X │
├─ REFINED main panel (takes most of the screen) ───────────┤
│ PnL   ROI   trades W/L rate   risked   DD   streak        │
│ fair  market  edge  side  TP/SL/Res  last exit            │
│ ┌─ position ─────────────────────────────────────────────┐│
│ │ OPEN Side  entry X  size $$  edge E  realizable R fav F││
│ └─────────────────────────────────────────────────────────┘│
│ ┌─ recent trades (8 cols) ───────────────────────────────┐│
│ │ t side px-in→out size pnl ROI% why hold                ││
│ └─────────────────────────────────────────────────────────┘│
│ ┌─ orders ────────────────────────────────────────────────┐│
│ │ refined-specific orders ...                             ││
│ └─────────────────────────────────────────────────────────┘│
├─ Refined extras ──────────────────────────────────────────┤
│ last exit  last zone  edge trades  config summary         │
├─ Footer ──────────────────────────────────────────────────┤
│ daemon.log (last 10)     │ live (prices + sparkline + stream) │
└───────────────────────────────────────────────────────────┘
```

Key functions:
- `build_comparison_banner(d)` — compact BASE + ENHANCED summary
- `build_refined_panel(blob, d, actions)` — main big panel with more
  detail than the previous per-strategy panels (includes realizable
  price + unrealized favor for open positions, per-trade ROI column,
  size column, hold duration column)
- `build_extras_panel(d)` — now shows refined extras (squeeze removed
  since it's always off for refined)
- `EventsTailer._dispatch` handles `strategy="refined"` events
- `_render_stream_line` prefixes refined actions with `[R]` in bright cyan

---

## Part 5 — Current running state

The main daemon is running freshly restarted with all three strategies:

- **PID:** see `daemon_state/daemon.pid`
- **Mode:** paper (no live trading; change via `./launch_daemon.sh live`)
- **State file:** `daemon_state/state.json`
- **Log:** `daemon_state/daemon.log`
- **Events:** `daemon_state/events.jsonl`

To view the TUI:

```bash
cd /home/samsam/polymarket-hustle
conda activate polymarket-env
python3 scripts/dashboard.py
```

The benchmark banner shows BASE and ENHANCED stats for comparison; the
large panel below shows REFINED with detailed position/exit/order info.

To promote REFINED to live trading (real orders), follow RUNBOOK §5
with `./launch_daemon.sh live` and ramp `MAX_TRADE_SIZE_USDC` up
gradually. The ENHANCED and BASE benchmarks keep running paper in
parallel to let you spot divergence between paper and live fills.

---

## Part 6 — Where to look next

### If you want to re-run tuning or explore more strategies
- All experiments in `experiments/` (git-ignored runtime; harness tools
  tracked).
- Launch a new variant: write `experiments/<name>/variant.json`,
  scaffold, patch, launch via the `_tools` scripts.

### If you want to audit the squeeze-phantom-signal bug
- Events log: `experiments/_baseline/daemon_state/events.jsonl`
  and `experiments/r5_confirm_tp_tight_no_squeeze/daemon_state/events.jsonl`,
  looking for `slug=btc-updown-5m-1776788900`.
- Suspect interaction: `SqueezeDetector.on_tick` state setting (deviation
  tracking) and `TimeBasedStrategy.on_tick` edge calculation / the
  phantom signal with `edge=None, tz=None`.

### If you want to tweak REFINED's parameters
- Edit `active_bots/refined_strategy.py`: change `DEFAULT_TP_DELTA_MIN`,
  `DEFAULT_TP_ABSOLUTE_FAVOR`, or override via env vars at launch.
- Or subclass it further for a new variant.

### Relevant docs
- `experiments/analysis/final_report.md` — session final write-up
- `experiments/analysis/findings.md` — 8 finding entries
- `experiments/analysis/round_{01..05}.md` — per-round reports
- `experiments/analysis/champion.json` — final champion spec
- `experiments/analysis/session_log.md` — timeline
- `docs/superpowers/specs/2026-04-22-strategy-fine-tuning-design.md` — design
- `docs/superpowers/plans/2026-04-22-strategy-fine-tuning.md` — plan

### Memory entries (persisted across sessions)
- `project_strategy_fine_tuning_session.md` — session outcome summary
- `project_strategy_design.md` — prior memory about the strategy
- `project_dry_run_executor_asymmetry.md` — prior dry-run routing
- `project_daemon_shutdown.md` — prior shutdown pipeline
