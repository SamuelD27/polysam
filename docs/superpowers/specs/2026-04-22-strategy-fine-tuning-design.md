# Strategy Fine-Tuning Session — Design

**Date:** 2026-04-22
**Author:** Sam (via Claude)
**Status:** Approved (awaiting implementation plan)

## Purpose

Run a multi-round, parallel strategy fine-tuning session on the Polymarket BTC 5m
binary-options daemon. Explore new strategy directions in early rounds, converge
onto the best Enhanced-strategy fine-tune in later rounds, then hand the winning
configuration to an overnight dry-run monitored via the live dashboard.

All work is done in isolated `experiments/` subdirectories. **The main codebase
is not modified.** Variants run in parallel as independent daemon processes,
each in dry-run mode (`POLYMARKET_DRY_RUN=1`), so no real capital is at risk.

## Success Criteria

- Primary ranking metric: **ROI = total_pnl / total_risked** per variant per round.
- Tiebreaker (when top two variants' ROI gap ≤ 20% relative):
  `composite = ROI − 0.5 × max_drawdown_pct + 0.2 × win_rate`.
- Minimum **8 trades** per variant per round for metrics to count; variants with
  fewer trades are marked "insufficient data" and neither ranked nor eliminated.
- Convergence: stop when the champion's ROI fails to improve by ≥ 5% relative
  for **two consecutive rounds**, or after **10 rounds maximum**.
- Final champion runs overnight in dry-run against live data, monitored via the
  existing web dashboard on port 3006.

## Architecture

### Directory layout

```
polymarket-hustle/
  experiments/                   (git-ignored)
    _baseline/                   # vanilla daemon — BASE + ENH unchanged
      daemon_base_v1.py          # copied from project root
      active_bots/               # copied from project root
      daemon_state/              # isolated per-variant state
      variant.json               # static spec: name, hypothesis, tweak type
      runtime.json               # runtime state: PID, start, end, status
    n1_mean_revert/              # pure mean-reversion
    n2_late_gamma/               # late-gamma sniper
    n3_momentum/                 # momentum follow
    n4_market_maker/             # simulated market-maker
    n5_no_squeeze/               # Enhanced w/ squeeze disabled
    n6_vol_regime/               # Enhanced w/ vol-regime gate
    <round_N_variants>/          # created as rounds progress
    champion/                    # snapshot of final winner for overnight
    analysis/                    # human + machine-readable reports
      session_log.md             # append-only timeline
      round_01.md ... round_NN.md
      variants/<name>.md
      findings.md                # append-only phenomena log
      champion.json              # current champion spec + metrics
      champion_history.jsonl     # every champion change, append-only
      final_report.md            # written before overnight handoff
```

### Isolation model

Each variant is a full copy of `daemon_base_v1.py` + `active_bots/` (~5 MB)
in its own subdir. Because the daemon defines
`STATE_DIR = Path(__file__).parent / "daemon_state"`, each process writes to
its own state dir with no cross-contamination. The daemon's existing
`_another_daemon_running()` PID guard uses the parent-dir name in its match,
so sibling variants don't block each other.

Shared nothing on the filesystem except the read-only main codebase (which
the copy step reads once).

### Data feeds

Each variant daemon opens its own Binance + Polymarket RTDS WebSocket
connection. 7+ simultaneous connections is trivial on the GX10
(128 GB RAM, 20 cores). All daemons observe identical market data, giving
apples-to-apples cross-variant comparisons.

### Execution mode

Every variant runs with:

```
POLYMARKET_MODE=live POLYMARKET_DRY_RUN=1
```

Per `daemon_base_v1.build_executor`, this routes to `PaperExecutor` regardless
of mode (dry-run overrides live), so no real orders are posted. This matches
the existing "live dryrun" path used by `./launch_daemon.sh live dryrun`.

### No dashboard for variants

The existing launcher spawns a rich TUI + web dashboard on port 3006 — fine
for one daemon, breaks for seven. Variant daemons are launched as bare
`python3 daemon_base_v1.py` background processes; monitoring is done by
reading each variant's `daemon_state/state.json` and `events.jsonl`.

The **baseline** variant (`_baseline/`) is launched the same way as the
other variants during rounds (bare python3, no dashboard) — its numbers
are what we compare against, not a human-watched pane. The **champion**
is launched with the full `./launch_daemon.sh live dryrun` only at
overnight handoff, so the user sees the TUI + port-3006 web dashboard
in the morning.

### Orchestration

- The main Claude session acts as orchestrator: scaffolds variant dirs,
  dispatches one parallel subagent per variant per round, synthesizes
  results, picks winner, decides next round.
- Each subagent is dispatched via the superpowers `dispatching-parallel-agents`
  flow. Subagents do not launch additional subagents.
- If the orchestrator session dies, running daemons keep running (they are
  not child processes of the Claude session). No new rounds will spawn;
  the user restarts orchestration manually if desired.

## Round 1 Variant Specifications

Six variants, each a minimal change relative to the copied codebase. None
require changes to the executor, reconciler, risk manager, event logger, or
WebSocket feed code.

### N1 — Pure mean-reversion (`n1_mean_revert/`)

**Hypothesis:** The edge model adds no value; all the real alpha is in
mean-reversion. Running without the GBM fair-price model isolates this.

**Change:** Replace the `EnhancedStrategy.on_tick` path with a new
`PureReversionStrategy`. Tracks max |ln(S/K)| over T+0..T+120. If
spike_score (deviation / expected σ×√(Δt/yr)) ≥ 1.0 AND deviation drops
to < 70% of its peak, enter counter-trend with full max_risk. Exit: 50%
further reversion, or force-exit at T+270.

No edge model, no TIME_ZONES, no squeeze-plus-edge branching. The rest of
the daemon (state persistence, event logging, executor) is untouched.

### N2 — Late-gamma sniper (`n2_late_gamma/`)

**Hypothesis:** The peak model accuracy is at T+250–285; a large-edge-only
strategy in that window with aggressive sizing dominates.

**Change:** In the copy's `active_bots/enhanced_strategy.py`:

- `TIME_ZONES = [(260, 285, 0.20, "late_gamma_only")]`
- `enable_squeeze=False` at `EnhancedStrategy` init.
- Env: `MAX_BET_PCT=0.40` (double normal, so Kelly-ish bump on this
  narrow, high-conviction window).

### N3 — Momentum follow (`n3_momentum/`)

**Hypothesis:** Early moves that *don't* revert by T+90 are momentum
signals, not squeezes. Enter with the spike instead of against it.

**Change:** Add a `MomentumDetector` class in the copy's
`enhanced_strategy.py`. Same detection phase as `SqueezeDetector` (max
deviation T+0..T+60), but entry condition is inverted:

- `spike_score ≥ 2.0` AND
- At T+90, current deviation ≥ 85% of peak (i.e., spike is *not*
  reverting).

Enter *with* spike direction. Exit: profit-grabber with TP_DELTA=0.08,
SL_DELTA=0.10, force-exit before T+270.

`SqueezeDetector` disabled (the momentum thesis is the opposite bet).

### N4 — Market-maker sim (`n4_market_maker/`)

**Hypothesis:** FAK market orders overpay the spread. A passive posting
strategy that waits for the market to come to it can capture the spread.

**Change:** New `PassiveMMStrategy`. Instead of sending ENTER at the
current market price, posts a "passive intent" at `fair ± 0.03` for
whichever side it believes correct. The daemon's strategy-loop code path
is unchanged — the variant strategy emits ENTER only when the live
`state.market_price_up` crosses the passive target level. PaperExecutor
then fills at the passive target. Tests whether better fills are
achievable without real limit-order plumbing.

Exits use the standard profit-grabber.

### N5 — No-squeeze Enhanced (`n5_no_squeeze/`)

**Hypothesis:** The squeeze pillar is either additive or dilutive — an
ablation test tells us which.

**Change:** One-line edit in the copy's daemon:
`enh = EnhancedStrategy(enable_squeeze=False, max_risk=max_risk)`.
Everything else identical to vanilla Enhanced.

### N6 — Vol-regime gate (`n6_vol_regime/`)

**Hypothesis:** The edge model is noise in calm regimes; only trust it
when volatility is elevated.

**Change:** Add a rolling 60-minute ring buffer of EWMA sigma samples
inside the copy's daemon. Expose `current_sigma_percentile()`. In
`EnhancedStrategy.on_tick`, gate entries on `percentile ≥ 70`. No other
changes. Low-vol markets are skipped entirely.

## Round ≥ 2 Strategy

After round 1, round 2+ composition is decided at synthesis time:

- **Floor:** at least 1 survivor from the top-2 N-variants (round 1 NEW
  strategies) continues, if any of them ranked above the vanilla
  Enhanced baseline.
- **Majority:** remaining slots (typically 4–5 of 6) become Enhanced
  fine-tuning variants. Each tunes a single axis (one-at-a-time changes
  for attribution clarity):
  - `TP_DELTA_MIN` (current 0.05)
  - `SL_DELTA_MIN` / `SL_DELTA_MAX` (current 0.05 / 0.20)
  - `TP_ABSOLUTE_FAVOR` (current 0.10)
  - `FORCE_EXIT_BEFORE_S` (current 30)
  - Individual `TIME_ZONES` edge thresholds
  - `MIN_ENTRY_PRICE` / `MAX_ENTRY_PRICE` (current 0.05 / 0.95)
  - Squeeze `SPIKE_THRESHOLD`, `SPIKE_WINDOW`, `SQUEEZE_REVERSION_FRAC`
- Exact perturbations are chosen based on round 1 findings (e.g., if
  round 1 shows most Enhanced wins are late-gamma, round 2 explores
  boosting the late-gamma edge threshold while holding others fixed).

## Per-Variant Subagent Lifecycle

A subagent is given:

- Variant name (e.g., `n1_mean_revert`)
- Variant dir path (`experiments/n1_mean_revert/`)
- Tweak spec (pointer to `variant.json` in that dir, pre-written by
  orchestrator)
- 30-minute run duration
- Instructions to launch in dry-run, monitor, collect metrics, kill.

### Variant tweak types

`variant.json` declares one of three tweak shapes so the subagent knows what
to apply:

- **`env_only`** — variant only changes env vars at launch (e.g., N2's
  `MAX_BET_PCT=0.40` piece). `variant.json.env` is a dict of env var → value.
- **`code_patch`** — variant modifies the copied code. `variant.json.patches`
  is a list of `{file, find, replace}` entries the subagent applies via
  string-literal replacement. Keep find-blocks ~≤ 10 lines for safety.
- **`new_module`** — variant adds a new strategy class or module.
  `variant.json.new_files` lists `{path, content}`, and `patches` wires
  them into the copied daemon (e.g., swap `EnhancedStrategy` import).

Subagent steps:

1. Read `variant.json` for the tweak spec.
2. Copy `daemon_base_v1.py` and `active_bots/` into the variant dir
   (idempotent; skip if already present).
3. Apply tweaks per the declared type (`env_only`, `code_patch`, or
   `new_module`). All file writes target the variant dir only.
4. Launch daemon as a background process:
   `POLYMARKET_MODE=live POLYMARKET_DRY_RUN=1 <variant.env> python3 daemon_base_v1.py`
   Use `Bash` with `run_in_background=true`. Capture PID from the daemon's
   own PID file at `<variant>/daemon_state/daemon.pid`. Write PID,
   start_ts, status=`running` to `runtime.json`.
5. Monitor for 30 min: sleep-poll in a bash loop or use a long-timeout
   Bash call. Every 60s, verify PID still alive; if not, mark
   `runtime.json.status=crashed`, parse whatever partial state exists,
   return early with a crash report.
6. Stop daemon: `kill <PID>`; wait up to 10s; `kill -9 <PID>` if needed.
   Update `runtime.json.status=stopped`, `end_ts=<now>`.
7. Parse `daemon_state/state.json` + `events.jsonl`; compute metrics:
   - ROI = total_pnl / total_risked
   - trade count, win rate, max drawdown %, composite score
   - Per-exit-type breakdown (TP / SL / RESOLUTION / force-exit)
   - Per-source breakdown (edge / squeeze / N-specific sources)
8. Return structured metrics report (JSON-shaped in the subagent reply)
   + one-sentence qualitative observation + `candidate_findings` array.

## Orchestrator Responsibilities (Between Rounds)

1. Collect 6 variant reports + baseline metrics (from
   `_baseline/daemon_state/state.json`).
2. Discard variants with <8 trades (not ranked, not eliminated).
3. Rank survivors: ROI primary; composite tiebreak when top-2 gap ≤ 20%.
4. Write `experiments/analysis/round_NN.md`:
   - Full variant table with all metrics
   - Ranking + commentary
   - Decision for next round
5. Update `experiments/analysis/champion.json` and append to
   `champion_history.jsonl` if champion changed.
6. Update `experiments/analysis/session_log.md` with the round-end
   milestone.
7. Decide:
   - If round-N champion ROI improved ≥ 5% relative vs round N-1 champion:
     spawn round N+1.
   - Else increment `flat_round_count`. If `flat_round_count ≥ 2`: stop
     and go to overnight.
   - If `N ≥ 10`: stop and go to overnight regardless.

## Overnight Handoff

1. Snapshot winning variant's dir to `experiments/champion/`.
2. Write `experiments/analysis/final_report.md` summarizing:
   - Total rounds, total trades observed, total wall-clock time
   - Champion's journey (which rounds it entered, metrics evolution)
   - What worked, what didn't (per N-variant verdict)
   - Specific recommendations for live-money ramp-up (sizing, guardrails)
3. Launch champion via `./launch_daemon.sh live dryrun` invoked from the
   champion dir (which gives it the full TUI + port-3006 web dashboard).
4. Update `session_log.md` with handoff milestone + dashboard URL.
5. Save relevant findings to auto-memory (convergence rule, generalizable
   tuning insights, final champion config).

## Safety

- **Dry-run enforced:** every launch sets `POLYMARKET_DRY_RUN=1`. The
  daemon's `build_executor` already routes dry-run to PaperExecutor
  regardless of mode (verified in `daemon_base_v1.py:274-280`).
- **Kill switches per variant:**
  `touch experiments/<variant>/daemon_state/KILL` blocks new entries.
- **No main-codebase edits:** all modifications live in
  `experiments/<variant>/`. The main `active_bots/` and
  `daemon_base_v1.py` are read-only from this session's perspective.
- **Git-ignore `experiments/`** to prevent accidental commits of
  variant code copies.

## Findings Log

A findings log captures non-obvious, generalizable phenomena observed during
the session — separate from round reports (which are tactical) and variant
cards (which are per-variant). A finding is something that would help someone
reason about this market on a future iteration *even if the current variant
set is abandoned*.

### What counts as a finding

Write a finding when any of the following is observed with evidence:

- A **regime dependency**: behavior differs by volatility / time-of-day / BTC
  price range.
- A **parameter cliff**: a threshold (edge, TP delta, spike score, …) where
  behavior flips sharply — suggests the parameter isn't smoothly tunable.
- A **strategy interaction**: two pillars that combine worse (or better)
  than the sum of their parts.
- A **market-structure surprise**: RTDS / orderbook behavior that contradicts
  model assumptions (e.g., market price leads the model, stale quote
  patterns, persistent one-sided order flow).
- An **exit-path asymmetry**: e.g., SLs systematically happen at prices worse
  than assumed due to thin books.

Routine metric comparisons (A beats B by X%) are NOT findings — those live
in round reports.

### Where findings are written

`experiments/analysis/findings.md` — single append-only file, one finding per
section. Each finding must include:

```
### YYYY-MM-DD HH:MM UTC — <short title>
**Source:** round N, variant <name> (or cross-variant synthesis)
**Observation:** <one paragraph, concrete>
**Evidence:** <pointer to events.jsonl line ranges, state.json values,
              round_NN.md section — must be reproducible>
**Implication:** <what this suggests for future variants or live trading>
**Confidence:** low / medium / high
```

### Who writes findings

- **Subagents** flag candidate findings in their return report under a
  `candidate_findings` field. They do NOT write to `findings.md` directly
  (to avoid concurrent writes from 6 parallel agents).
- **Orchestrator** promotes candidates into `findings.md` at round synthesis
  time, after reviewing them against the criteria above. Promotion is
  append-only; findings are never edited or deleted — if superseded, a new
  finding citing the original is added.
- High-confidence findings that generalize beyond this session are also
  written to **auto-memory** (project type) so future sessions can benefit.

## Documentation Artifacts (Summary)

- `experiments/analysis/session_log.md` — append-only timeline
- `experiments/analysis/round_NN.md` — per-round reports
- `experiments/analysis/variants/<name>.md` — per-variant cards
- `experiments/analysis/findings.md` — append-only phenomena log
- `experiments/analysis/champion.json` — current champion spec
- `experiments/analysis/champion_history.jsonl` — append-only champion log
- `experiments/analysis/final_report.md` — end-of-iteration summary
- `experiments/<variant>/variant.json` — per-variant tweak spec
- Auto-memory entries — generalizable findings + final config

## Open Questions / Deferred

- **N4 (market-maker):** Simulated via PaperExecutor only. Real limit-order
  plumbing deferred to a future feature.
- **Post-overnight morning report:** Not this session's responsibility.
  The user's next session (or a `summarize overnight` subagent) reads
  `experiments/champion/daemon_state/` and writes `overnight_report.md`.
- **Cross-round correlation of market conditions:** Because all 7+ daemons
  see the same markets in parallel, in-round comparison is clean. Across
  rounds, conditions change — ROI trends are directional only, not
  strictly comparable. Convergence rule is tolerant of this noise via
  the 2-consecutive-flat-round requirement.
