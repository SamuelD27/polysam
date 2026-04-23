# Book-walked replay backtester — implementation progress

**Status: PAUSED.** All work frozen pending in-between tasks the operator
wants to address first. This document is the authoritative resume point —
pick it up here when ready. Nothing is half-shipped; every listed commit
has passing tests; no uncommitted code is load-bearing.

**Spec (authoritative contract):**
`docs/book_walked_replay_backtester_spec.md` (committed 098123b).

**Last activity on backtest/execution:** `5bf31f1 fix(execution): DryRunExecutor
for behavioural-realistic live dryrun` (R2.1 Option C).

**Local-only commits not pushed to `polysam` yet:**
`aab45e7`, `d92026b`, `5bf31f1`. Pending the post-Option-C dryrun re-run
verification before push.

---

## Goal

Quantify the paper-to-live ROI haircut of the RefinedStrategy before
committing real capital, by replaying past daemon decisions against real
L2 orderbook evidence and producing a regime-conditional attribution
(half_spread / book_walk / latency_drift / fees / opportunity_cost).

The session context: observed ROI swings ±137% round-to-round on
identical code, meaning the current paper executor's flat-mid fills are
hiding material live costs. The spec
(`docs/book_walked_replay_backtester_spec.md`) argues the real haircut
sits in a 20–60% band with Kaiko 2024–25 / Harvey-Liu 2015 grounding.
The backtester is the measurement instrument. Once the haircut is
defensibly quantified, the open strategic question is whether to add a
depth-based input to `RefinedStrategy.on_tick` — that conversation is
explicitly downstream of the measurement.

Secondary goal: a behavioural-realistic `./launch_daemon.sh live dryrun`
mode (R2.1 Option C) that lets the operator preview the bot against real
RTDS mid with zero USDC risk and a CLOB-shaped event stream.

---

## Round plan overview

| Round | Scope | Status |
|---|---|---|
| R1 | Spec §8 priorities 1–6: WS scraper, book/fees/latency/invariants/executor/harness/attribution/report, reconcile stub | **complete** (9/9 commits) |
| R2.1 | Route live-mode dryrun so it behaves live-like and stamps predicate fields | **complete pending re-run verification** (Option C shipped) |
| R2.2 | Flesh out `reconcile.py` per §6.3: per-fill replay, diff_bps acceptance gate | **blocked** on first real LIVE_MODE capture ≥100 fills |
| R3 | Regime-conditional paper-vs-live ROI decomposition on real captured window | **not started**; blocked on R2.2 |
| R4 | CPCV / Deflated-Sharpe folds (spec §6.4) once N-per-fold is adequate | **not started**; downstream of R3 |
| R5 | Go / no-go on adding a depth-based input to `RefinedStrategy.on_tick`; if go, design + backtest + ship | **not started**; explicit prerequisite is "the backtester told us WHICH regime would justify it" |

---

## Round 1 — complete

Nine commits, all on `Sam-Dev`, all pushed to `polysam`, 82→89 tests green
through the round.

| # | Commit | Scope |
|---|---|---|
| R1.1 | `098123b docs: add book-walked replay backtester spec` | Persisted the authoritative spec under `docs/` |
| R1.2 | `1c66482 feat(backtest): add CLOB WebSocket book/delta scraper` | `scripts/scrape_book.py`: subscribes to `wss://ws-subscriptions-clob.polymarket.com/ws/market`, persists `book` / `price_change` / `last_trade_price` / `tick_size_change` to `daemon_state/book_feed/{date}/{slug}.jsonl.gz`. Narrow subscription set (currently-live + next 5-min window), parallel tick-size REST, `tick_size_disagreement` cross-check. Replaces the 30-s REST polling per spec §1.2 |
| R1.3 | `9f915fc feat(execution/book): per-side tick size + apply_deltas` | Book dataclass gains `tick_size_bids` / `tick_size_asks` overrides per §2.2 / §8.2 nautilus_trader #2980 asymmetry; new `apply_deltas(base, deltas)` pure function for delta rebuild |
| R1.4 | `ae612c2 feat(backtest): add book_feed reader, --input-format, run-manifest` | Harness grows `--input-format=book_feed \| sqlite` (default book_feed). Emits `{out}.manifest.json` with run_id, git_sha, latency profile, fee schedule version, staleness_policy |
| R1.5 | `f73959d feat(execution): plumb ConditionedSampler through ReplayExecutor` | `ConditionedSampler(profiles, key_fn, fallback)` with graceful degradation to `SG_WG_PRIOR` when no bucket-measured distribution exists. `ReplayExecutor` accepts it; `OrderRequest` gains `tunnel_age_bucket` / `vol_regime` |
| R1.6 | `8f2c2f0 feat(backtest): staleness-policy quarantine at report layer` | `report.py` refuses the paper-vs-realistic ROI headline when any parquet row has `staleness_policy != "strict"`. Row counts, classification breakdown, attribution-sum invariant check, regime table, and manifest echo still print |
| R1.7 | `3fd8d9d test(execution): add py-clob-client #218 order-price invariant` | `invariant_order_price_respects_218_bound(worst_price_limit, side, book)` refuses replay orders whose price the Polymarket REST validator would reject (0.01–0.99 on 0.01-tick, per-side analogues). Per-side tick path added to `invariant_price_bounds_respect_tick` |
| R1.8 | `bb09488 feat(backtest): report MinTRL per spec §6.4` | Bailey & López de Prado 2014 MinTRL on per-trade PnL, α=0.05, against SR*=0 and SR*=1. Printed in both strict and non-strict modes (sample-size statement, not an ROI claim) |
| R1.9 | `2713072 feat(backtest): stub reconcile.py with NoLiveFillsCaptured` | `experiments/backtest/reconcile.py` with `NoLiveFillsCaptured` raise naming the predicate (`ack_ts NOT NULL AND order_id NOT NULL AND fill_price NOT NULL`) and the live-capture command verbatim. Will be replaced by R2.2 |

**Diagnostic run at end of R1** against the legacy REST-scraped sqlite
(`--input-format=sqlite --allow-stale`): 7 rows, attribution-sum invariant
held 7/7, PnL numbers unreadable as expected due to 74 s median book
staleness — acceptable per operator ("PnL numbers from that run are
fiction by construction and I won't read them").

---

## Round 2 — in flight, paused

### R2.1 — live-mode dryrun stamps the predicate fields

**Landed locally (not yet pushed):**

| Commit | Scope |
|---|---|
| `aab45e7 fix(execution): route live-mode dryrun through LiveExecutor and stamp ack_ts` | Initial Option B fix: removed `daemon_base_v1.py`'s `if dry_run: return PaperExecutor()` short-circuit, passed `dry_run=dry_run` into `LiveExecutor(...)`, stamped `result.ack_ts = time.time()` at all three `enter()` call sites. Added `ack_ts: float \| None = None` to `EntryResult` and its `to_position_dict()` |
| `d92026b fix(backtest): reconcile.py accepts entry_price as fill_price alias` | `reconcile.py` predicate scan now accepts `entry_price` alongside `fill_price` / `filled_price` / `executed_price`, because `LiveExecutor.enter()` persists the actual avg fill as `entry_price` (not `fill_price`). Paper lacks `order_id` + `ack_ts` so this alias cannot let paper rows through |
| `5bf31f1 fix(execution): DryRunExecutor for behavioural-realistic live dryrun` | Option C: the original Option B routed dryrun through `LiveExecutor`, whose `_dry_run` branch returns `_fake_fill_response(avg_price=0.5)`, making every dryrun trade look like `BUY @ 0.50 → SELL @ 0.50`. Replaced with `active_bots/execution/dry_run_executor.py` — a `DryRunExecutor` that wraps `PaperExecutor` for realistic fill math and stamps LiveExecutor-shaped metadata (`order_id='dry-run-<ms>'`, `token_id` from `MarketCtx`). `daemon_base_v1.py` routes `mode=="live" AND dry_run` through this. Preserves the `active_bots/execution/live_executor.py` do-not-touch boundary |

**Side effects of R2.1 that matter:**

- `./launch_daemon.sh live dryrun` now loses behavioural fidelity with
  the real `LiveExecutor` signing/posting path — that path is exercised
  **only** the first time the operator runs real `LIVE_MODE` without
  `POLYMARKET_DRY_RUN`. This is an explicit trade-off: the operator
  wanted dryrun to look like paper-with-live-metadata, not like the
  `_fake_fill_response` 0.5/0.5 placeholder.

**Remaining R2.1 verification before push to `polysam`:**

1. Operator Ctrl-Cs the current daemon (the post-aab45e7, pre-5bf31f1 run
   is still live with the 0.5/0.5 artefact) and re-runs
   `./launch_daemon.sh live dryrun`.
2. On startup the log should say `executor=live-dryrun (paper fills +
   live-shape metadata; no CLOB posts)`.
3. During the ~15-min run the TUI should show varied entry prices
   (matching real RTDS mid at the signal moment), not 0.50/0.50 pairs.
4. Fresh `entry_filled` rows in `daemon_state/events.jsonl` should carry
   `order_id="dry-run-<ms>"`, `token_id=<yes or no token_id>`,
   `ack_ts=<epoch>`, and a realistic `entry_price`.
5. If all four pass: push `aab45e7`, `d92026b`, `5bf31f1` to `polysam`
   and move to the real `LIVE_MODE` capture call.

### R2.2 — flesh out `reconcile.py` per spec §6.3

**Blocked** on the operator running the first real `LIVE_MODE` capture
session (the predicate-satisfying event shape won't land until a dryrun-
free live session emits real CLOB `order_id` and post-response `ack_ts`).

**Target capture session (operator's call, not Claude's):**

```bash
MAX_TRADE_SIZE_USDC=1 bash launch_daemon.sh live   # NOT "dryrun"
# scripts/scrape_book.py running concurrent in a second terminal
# 2 target BTC 5-min markets (current + next window)
# ~3–4 h wall clock
# target: 100+ clean fills where book_staleness_ms < 200
```

When that session is on disk, `experiments/backtest/reconcile.py` stops
raising `NoLiveFillsCaptured` and starts raising `NotImplementedError`.
At that point R2.2 proceeds:

1. Partition captured live fills pre/post-2026-02-01 (the 500-ms
   taker-delay removal — spec §8.2 footnote). Do not pool.
2. For each live fill, replay the same `decision_ts_ns` through
   `ReplayExecutor` using a latency sampler fit from the actual
   `(ack_ts − entry_time)` quantiles in the captured window, not the
   `SG_WG_PRIOR` prior.
3. Per-fill record in the output parquet (schema named verbatim in the
   spec §6.3): `t_decision, live_fill_px, paper_fill_px, diff_bps,
   live_filled_qty, paper_filled_qty, book_top_at_decision,
   book_staleness_ms, paper_latency_sample, live_latency_measured,
   attribution_delta`.
4. Acceptance gate: on rows with `book_staleness_ms < 200` and the
   measured-quantile latency sampler active, require
   `|median diff_bps| < 2` and `p95 diff_bps < 10` vs the live fill
   price. Fail the harness run if the gate is breached.
5. Output: `experiments/backtest/runs/golden_trace.parquet` + its
   manifest sidecar.

The live-executor-side fields that R2.2 assumes are present:
`order_id`, `entry_price` (as fill-price alias), `ack_ts` (stamped by
daemon caller in `daemon_base_v1.py` post-R2.1), `token_id`,
`size_shares`, `entry_time`.

### R2.3 (implicit) — empirical latency fit

Currently `active_bots/execution/latency.py::fit_from_events_jsonl`
raises `NotFitted` because paper-mode events never carry `ack_ts`. Once
live events land (post-R2.1 verification + first real capture), the
function should succeed and supply the ConditionedSampler with measured
quantiles instead of the SG_WG_PRIOR fallback. Wiring it into
`experiments/backtest/harness.py`'s manifest as the default profile
when a valid fit exists is ~10 lines. No code change in
`latency.py` itself needed.

---

## Round 3 — not started

**Prerequisite:** R2.2 has produced a non-empty `golden_trace.parquet`
with its acceptance gate passing, and a book-walked parquet run over
the same window exists under
`experiments/backtest/runs/dryrun_*.parquet`.

**Scope:** run `experiments/backtest/report.py` with `staleness_policy=
"strict"` on the first real captured window, read the regime-
conditional attribution table, identify the dominant cost regime,
write a session memo to `docs/` capturing:

- Per-regime median haircut (half_spread / book_walk / latency_drift /
  fees / opportunity_cost_unfilled).
- Which regime dominates the total_IS.
- Whether the observed haircut sits inside Harvey-Liu 2015's 25–60 %
  Sharpe-haircut band or outside it.
- MinTRL-required N per regime.

**Output:** `docs/session_N_haircut_findings.md`, dated.

## Round 4 — CPCV / Deflated Sharpe

**Prerequisite:** R3 + enough captured live fills per regime that CPCV
N=6 k=2 produces ≥ 30 trades per fold (floor per Lo 2002 / spec §6.4).

**Scope:** `experiments/backtest/cpcv.py` (new) — Combinatorial Purged
Cross-Validation per López de Prado 2018 Ch. 12. Output: empirical
distribution of Sharpe across 15 train-test combinations + 5 backtest
paths, plus Deflated Sharpe Ratio per Bailey & López de Prado 2014.

**Prompt-worthiness gate before writing code:** if R3 shows the haircut
is well inside Harvey-Liu's band and the dominant regime is obvious,
CPCV may be overkill. Revisit in R3's session memo.

## Round 5 — depth signal (maybe)

**This is the question that motivated the entire backtester.** Does
adding a depth-based input (book imbalance / aggressive-take rate /
microprice / time-in-market cross) to `RefinedStrategy.on_tick`
improve live-equivalent ROI in the regime(s) R3 identified as
dominant haircut contributors?

**Not a Claude-decides question.** R3's output is the evidence base;
the operator owns the go/no-go.

If go:

1. Design proposal memo: which signal, which regime, expected
   attribution improvement. Author: operator.
2. `active_bots/` extension: new optional input on
   `RefinedStrategy.on_tick`, backwards-compat default = ignore.
3. Backtest the new variant against R2.2's captured window using
   harness/report.
4. If the haircut in the target regime tightens, ship behind a feature
   flag and paper-benchmark for 1 week.
5. If the haircut doesn't tightens → we learned something, don't ship.

---

## Pre-flight checklist — status at pause

| # | Gate | Status |
|---|---|---|
| 1 | WS scraper 30-min clean run (5 sub-gates) | **GREEN.** 8 files / 35.5 MB, 383k `price_change`, 0 sequence_gaps, 0 reconnects, p50 inter-event gap = 0.1 ms |
| 2 | Tunnel + CLOB auth | **GREEN.** Funder address confirmed `0xf5f6fbf6d64890b59a70a33e45fd705957d34049` via CLOB `/auth/derive-api-key`; balance-allowance round-trip returned $91.79 |
| 3 | Funding + proxy-vs-EOA | **GREEN.** Trading address = EOA, not a proxy. No deposit/transfer pre-flight needed |
| 4 | Dryrun produces predicate-shape events | **PENDING** — verification of `5bf31f1` re-run still outstanding |

---

## Resume checklist (when unpausing)

1. **Ctrl-C** any running daemon (the one that launched during R2.1
   Option B may still be active — check `daemon_state/daemon.pid` and
   `ps -eo pid,etime,cmd | grep daemon_base_v1`).

2. **Re-run dryrun with the Option C fix:**
   ```bash
   cd /home/samsam/polymarket-hustle
   ./launch_daemon.sh live dryrun
   ```
   Expected startup log:
   `[daemon_base_v1] executor=live-dryrun (paper fills + live-shape metadata; no CLOB posts)`
   Let it run 15 min, Ctrl-C.

3. **Verify event shape** (Claude can do this part):
   ```bash
   grep '"type": *"entry_filled"' daemon_state/events.jsonl | tail -10 \
     | python3 -c 'import sys,json; [print(json.dumps(json.loads(l),indent=2)) for l in sys.stdin]'
   ```
   Expect: each position dict has `order_id` starting `"dry-run-"`,
   `ack_ts` populated, `entry_price` varying across rows (not a flat
   0.50).

4. **Push to polysam** if step 3 passes:
   ```bash
   git push polysam Sam-Dev
   ```
   Target commits: `aab45e7`, `d92026b`, `5bf31f1` (plus any R1 follow-ups
   not yet pushed — `git log polysam/Sam-Dev..Sam-Dev` to confirm).

5. **First real LIVE_MODE capture** — operator's explicit go-ahead
   required; Claude does not launch this. Command template:
   ```bash
   MAX_TRADE_SIZE_USDC=1 bash launch_daemon.sh live
   # scripts/scrape_book.py in a second terminal
   # 3–4 h wall clock; target 100+ clean fills
   ```

6. **R2.2 body implementation** — kicks off the moment
   `reconcile.py`'s `NoLiveFillsCaptured` gate stops firing.

---

## Explicit do-not-touch boundary (operator-set)

Preserved through every commit in R1 + R2.1:

- `active_bots/execution/live_executor.py` — never edited.
- `active_bots/execution/reconciler.py` — never edited. (Distinct from
  `experiments/backtest/reconcile.py` which we own.)
- `active_bots/execution/risk_manager.py` — never edited.
- ProfitGrabber TP/SL math — never touched.
- `RefinedStrategy.on_tick` signature — never touched.
- `RefinedStrategy` squeeze path — explicitly disabled and left alone.
- `daemon_state/` files other than the `book_feed/` subtree the scraper
  writes — read-only from Claude's side.

The `DryRunExecutor` (R2.1 Option C) was specifically designed to
preserve this boundary after Option B would have had to breach it.

---

## Tests green at pause

`python -m pytest -q tests/execution/ experiments/backtest/tests/`
→ **89 passed, 1 warning** (legitimate extreme-price warning on the
sqlite smoke test).

Test layout:

| Directory | Test count | Covers |
|---|---|---|
| `tests/execution/test_book.py` | 25 | per-side tick, apply_deltas, walk_book, quantisation |
| `tests/execution/test_fees.py` | 12 | Polymarket intl bell curve symmetry + dust + US flat stub |
| `tests/execution/test_latency.py` | 10 | SG_WG_PRIOR quantiles, fit_from_events_jsonl NotFitted, ConditionedSampler fallback paths |
| `tests/execution/test_replay_executor.py` | 11 | ReplayExecutor + DictBookStore + both run modes |
| `tests/execution/test_invariants.py` | 12 | spec §6.2 invariants incl. py-clob-client #218 |
| `tests/execution/test_dry_run_executor.py` | 6 | DryRunExecutor Option C: realistic fill prices, token_id mapping, exit routing |
| `experiments/backtest/tests/test_smoke.py` | 2 | end-to-end harness synthetic + skipif real-overlap |
| `experiments/backtest/tests/test_reconcile.py` | 8 | predicate scan, entry_price alias, NoLiveFillsCaptured |
| **Total** | **86** | (+3 from other touched files = 89 with full run) |

---

## Where to look first when resuming

- `docs/book_walked_replay_backtester_spec.md` — what we're building, why,
  section numbers load-bearing across commits.
- `experiments/backtest/README.md` — CLI surface and preconditions.
- This file — round-by-round state.
- `git log --oneline Sam-Dev ^main | head -50` — full commit history
  for the branch. Interleaved backtest and dashboard work; filter for
  `feat(backtest)` / `feat(execution)` / `test(execution)` /
  `fix(execution)` to see the backtest slice.
