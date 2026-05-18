# Sam-Dev repo state snapshot

## Plain-language summary

This is a point-in-time, fact-only audit of the `Sam-Dev` branch taken on
2026-05-18 to decide cleanup priorities and next steps. The good news: the
scraper empty-bids bug is fixed (a clean two-sided, capture-verified session
exists for 2026-05-12) and the test suite is effectively green. The blocker is
that the walked_vwap replay is non-deterministic — against the same verified
capture it produces three different PnL outcomes depending on config, so a
parameter sweep run today would measure harness variance rather than strategy
behaviour. Branch hygiene is secondary: Phase-5/5b are unmerged, several reports
and configs are untracked, and macOS junk files persist. No fixes were attempted;
this report only states what is, so the next session can decide what to do.

---

Generated read-only on 2026-05-18. No edits, commits, or pushes were made.
Replay output (`replay_summary.json`) was regenerated in the session dir — that
is a generated artefact, the only filesystem write, explicitly requested.

---

## 1. Branch and commit state

| Ref | SHA |
|---|---|
| `Sam-Dev` | `29570526` (2957052 docs(claude): codify default subagent invocation rules) |
| `origin/Sam-Dev` | `3b8dda34` |
| `polysam/Sam-Dev` | `29570526` — **identical to local Sam-Dev (0 ahead/behind)** |

- Remotes: `origin` = github.com/Wailydest/polymarket-hustle, `polysam` = github.com/SamuelD27/polysam.
- `git log Sam-Dev ^origin/Sam-Dev | wc -l` = **219** (origin is far behind; polysam is in sync).
- `git log polysam/Sam-Dev..Sam-Dev | wc -l` = **0**.

Last 30 commits on Sam-Dev (newest first):
```
2957052 docs(claude): codify default subagent invocation rules
90ae837 feat(agents): add session-analyst, code-reviewer, dead-code-auditor, architecture-advisor, capture-verifier, replay-runner
c707f71 feat(hooks): add protected-files, git-push, trade-size, session-info guards
0f85af4 docs(claude): rewrite for conciseness; cut historical content
a343413 fix(tests): integer t0 in btc_ticks round-trip to dodge .6f precision
5d5247a docs(scraper): H1 BTC tick tape; capture verification updated
3694429 test(daemon): btc_ticks.jsonl emission + replay round-trip
970b567 feat(replay): load BTC tape from session btc_ticks.jsonl with fallback
3656fc1 feat(daemon): write btc_ticks.jsonl per session
f6aaa02 style(tests): ruff fix for commit-1 entry_rejected tests
e17d592 fix(orchestrator): emit entry_rejected on trader-side ENTER rejection
b687160 feat(replay): EWMA sigma warmup over BTC tape
d784a8a test(strategy): on_tick now= kwarg respected per strategy
f9693bf feat(orchestrator): pass tick.timestamp as now= in dispatch
6ea6134 refactor(strategies): canonical on_tick signature with **kwargs ignore
a51fa1f docs(reports): R3 capture failure diagnostic
498f7e0 docs(scripts): R3 path update in check_book_feed.py example after rename
66b44e3 style(tests): ruff fixes for r3-aftermath scraper tests
1a4e2ee docs(scraper): canonical capture verification procedure
d3e4394 docs(scraper): F2 resolved-market accumulation; F1 deferred
f288d4b fix(diagnostics): book-feed populated-frame predicate counts all event classes
dcfe272 fix(scraper): snapshot iteration in _prime_from_rest; catch RuntimeError in subscribe stream
cde96ac docs(scraper): root cause + verification in CLAUDE.md §18
a16d66c test(scraper): resubscribe-on-discovery integration test
6f9fedb fix(scraper): loud WARNING on _prime_from_rest empty for active token
e3724dd fix(scraper): resubscribe on discovery loop token-add
74b9d89 docs(replay): R2.2 validation report
a2e6da6 docs(replay): REPLAY.md operator guide
22d50dd perf(replay): R2.2 wall-clock 32s via filename-window filter + bisect caches
9807c54 feat(orchestrator): replay mode (muted I/O, fast tick loop)
```

Local feature branches (excl. Sam-Dev):
```
  cleanup/repo-organization-and-sl-asymmetry 94e476f  (merged)
  dev/btc5m-scraper                          3b8dda3  (merged) [== origin/Sam-Dev]
  feat/daemon-btc-tick-tape                  a343413  (merged)
  feat/daemon-market-price-tape              711aa8a  (NOT merged)
+ feat/dashboard-charts                      23c90d7  (worktree: superpowers/.../dashboard-charts)
  feat/launch-menu                           3a13f0b  (merged)
+ feat/live-dashboard                        7a7a86e  (worktree: superpowers/.../live-dashboard)
  feat/quant-flags-phase5                    510f759  (NOT merged)
  feat/quant-flags-phase5b                   f667eb4  (NOT merged)
+ feat/scraper-two-sided-fix                 dcfe272  (merged) (worktree: .worktrees/scraper-two-sided-fix)
  fix/replay-time-threading                  b687160  (NOT merged)
  main                                       1768cfd  [origin/main: ahead 4]
  refactor/modular-architecture              4953e98  (merged)
```

Named-branch detail (`left` = Sam-Dev-only commits, `right` = branch-only):

| Branch | Exists | Merged into Sam-Dev | Sam-Dev ahead / branch ahead |
|---|---|---|---|
| `feat/quant-flags-phase5` | yes | **NO** | 72 / 11 |
| `feat/quant-flags-phase5b` | yes | **NO** | 61 / 3 |
| `feat/realistic-paper-and-replay` | **no — does not exist locally** | — | — |

Stash list (4 entries):
```
stash@{0}: On feat/quant-flags-phase5b: sweep-design-pending-harness-rebuild
stash@{1}: On Sam-Dev: scrape_all-sequential-helper-pending-caller
stash@{2}: WIP on main: 1768cfd feat(backtest): add enhanced strategy backtester...
stash@{3}: WIP on main: c90c643 rtds monitor
```

`git status --short` (no modified tracked files; 21 untracked):
```
?? .claude/scheduled_tasks.lock
?? active_bots/GOAL.md
?? daemon_base_v1
?? docs/CURRENT_STATUS.md
?? docs/superpowers/plans/2026-05-06-realistic-paper-and-replay.md
?? docs/superpowers/plans/configs/{h2_smoke_replay,h2_smoke_replay_matched,r3_replay,r4_smoke_replay}.json
?? experiments/backtest/runs/dryrun_r1.parquet.manifest.json
?? experiments/backtest/runs/golden_2026-04-24T07-10-19Z.parquet.manifest.json
?? experiments/backtest/runs/golden_2026-04-24T07-10-19Z.validation.txt
?? reports/{h2_smoke_replay_validation,r4_market_price_diagnostic,r4_replay_wiring_diag,r4_smoke_replay_validation,r4_smoke_replay_validation_v2}.md
?? scripts/{check_v2_allowances,csv_to_parquet,onboard_check,refresh_v2_balance_cache}.py
```
Note: `daemon_base_v1` (the bash control script) shows as untracked at repo root.

---

## 2. Scraper bug status (empty-bids)

**FIXED.** Book feed is now two-sided and healthy.

`python scripts/check_book_feed.py daemon_state/book_feed/2026-05-12/ --per-slug --btc-tape <session>/btc_ticks.jsonl`:
```
files:  9
median: 93.5%   p25: 91.7%   p75: 94.8%
BTC tape: OK daemon_state/scrapes/2026-05-12T08-54-33Z/btc_ticks.jsonl (57390 lines)
verdict: HEALTHY (median >= 80%)   exit code 0
```

Per-slug populated ratios (the script reports a single % per slug, not a YES/NO
bids/asks split — that breakdown is not a feature of `check_book_feed.py`):

| Slug (5m window) | Populated ratio |
|---|---|
| btc-updown-5m-1778575800 | 68.4% (3032/4433) — market in its closing minutes at session start |
| btc-updown-5m-1778576100 | 91.3% |
| btc-updown-5m-1778576400 | 92.2% |
| btc-updown-5m-1778576700 | 93.5% |
| btc-updown-5m-1778577000 | 93.3% |
| btc-updown-5m-1778577300 | 93.8% |
| btc-updown-5m-1778577600 | 94.7% |
| btc-updown-5m-1778577900 | 96.0% |
| btc-updown-5m-1778578200 | 95.0% |

`git log --oneline -20 scripts/scrape_book.py`:
```
dcfe272 fix(scraper): snapshot iteration in _prime_from_rest; catch RuntimeError in subscribe stream
6f9fedb fix(scraper): loud WARNING on _prime_from_rest empty for active token
e3724dd fix(scraper): resubscribe on discovery loop token-add
00b2269 style: ruff --fix mechanical lint cleanup
1c66482 feat(backtest): add CLOB WebSocket book/delta scraper
```
The two-sided fix is `dcfe272` (also the tip of `feat/scraper-two-sided-fix`, which
is merged into Sam-Dev).

Captures **later than** `2026-05-05T12-50-04Z` exist (3):
```
2026-05-12T08-54-33Z   (mode live_dryrun, git 711aa8a, 40.9 min, two-sided, verified)
2026-05-08T08-26-08Z
2026-05-08T07-17-14Z
```

---

## 3. Replay reproduction against current data

Target: `daemon_state/scrapes/2026-05-12T08-54-33Z` (only post-fix two-sided
capture with a full BTC tape).

**capture-verifier verdict: PASS** — manifest clean SIGTERM; BTC tape 57,390
records full-window monotonic; market_price tape 24,245 records, only 1.3% are
0.5 (not the inverted-PnL fallback); book median 93.5%. A replay conclusion drawn
from this session can be trusted.

Replay run: `PORTFOLIO_SIZE_USDC=10 python -m polyhustle.cli --config <walked_vwap
main, benchmarks [], execution_mode replay>`. Exit 0, 2430 ticks, 30.6 s.

**The replay does NOT reproduce the captured live-dryrun walked_vwap PnL, and the
replay result is not even stable across configs.** Same capture, same 2 trades,
three different outcomes:

| Source | n_trades | pnl_total | pnl_mid | win_rate | exit_types |
|---|---|---|---|---|---|
| Captured live-dryrun (events.jsonl, session window) | 2 | **+1.46** | +1.61 | 1.0 | TP, TP |
| Prior `replay_summary.json` (walked_vwap main **+ benchmarks** refined/enhanced/base) | 2 | **+0.87** | +0.92 | 1.0 | TP, TP |
| This run (walked_vwap **solo**, benchmarks [], PORTFOLIO_SIZE_USDC=10) | 2 | **−0.91** | −0.05 | 0.0 | SL, SL |

Trade *count* matches (2) everywhere; sign and exit type do not. Adding/removing
benchmark strategies flips walked_vwap from 2×TP to 2×SL. Reported as fact only —
not diagnosed here.

---

## 4. TODO(probe) status

- `grep -rn "TODO(probe)" active_bots/execution/live_executor.py` → **no matches**.
  Removed in `6ad0ffe feat(execution): finalize V2 fill extraction post-probe`
  (commit message: "Drop the TODO(probe) options=None tick-size comment block;
  verified"). No `TODO` of any kind remains in `live_executor.py`.
- `scripts/probe_v2_order.py` exists (May 5) and **has been run**. Probe lineage:
  `e47791d` (add) → `3711c58` (loads .env) → `6ad0ffe` (finalize V2 fill
  extraction post-probe). No probe output artefacts under `daemon_state/`;
  `/tmp/replay_probe.py` (May 13) is an unrelated replay helper, not probe output.

---

## 5. Sweep status

- `git stash show -p stash@{0}` is empty for tracked files; `-u` shows it contains
  **one untracked file: `reports/r2.2_sweep_design.md` (+247 lines)**.
  Stash subject: `sweep-design-pending-harness-rebuild`. Still present.
- No `tune/quant-defaults` (or any `tune*`) branch exists, local or remote.
- New files under `reports/` since 2026-05-06:
  ```
  h2_smoke_replay_validation.md      May 13 16:16  (45 KB)
  r4_smoke_replay_validation_v2.md   May 11 22:41
  r4_market_price_diagnostic.md      May 11 21:56
  r4_smoke_replay_validation.md      May  9 10:09
  r4_replay_wiring_diag.md           May  7 17:19
  r3_failure_diagnostic.md           May  7 17:03
  ```

---

## 6. Live trading state

- `wc -l daemon_state/events.jsonl` = **12018**.
- Live, non-dry-run `entry_filled` events with `mode=live` in the last 7 days
  (since 2026-05-11): **0**.
- `daemon_state/state.json` `last_update` = `1778578527.92` =
  **2026-05-12T09:35:27Z** (file mtime 2026-05-12 17:35 SGT). Stale since the
  2026-05-12 live_dryrun session; daemon not running (confirmed by session-start hook).
- `daemon_state/KILL` is **absent**.

---

## 7. TUI state

`tui/README.md` (verbatim status):
- `tui/python/dashboard_legacy.py` — STABLE, DEFAULT (unset / `DASHBOARD=legacy`).
- `tui/python/dashboard.py` — Textual rewrite, opt-in `DASHBOARD=textual`.
- `tui/rust/polychart` — **IN DEVELOPMENT**, "not verified end-to-end against a
  live daemon", opt-in `DASHBOARD=rust`. **Not promoted.**

`pick_dashboard_cmd` appears only as a comment string in `launch:200`; it is not a
function being modified. `git log --oneline -10 launch_daemon.sh launch` newest:
`3656fc1` (btc_ticks per session), `3a13f0b` (restore capture flow), `e3c6f0a`
(deprecate launch_daemon.sh). No recent change promoting polychart or touching
dashboard selection.

---

## 8. Test posture

`pytest tests/ active_bots/tests/ -q` → **3 failed, 373 passed, 1 warning (64.6 s)**.

| Test | Status vs baseline |
|---|---|
| `tests/execution/test_latency.py::test_fit_raises_not_fitted_on_real_events` | Expected — the documented 1 pre-existing fragile latency test |
| `tests/launcher/test_launch_menu.py::test_launch_daemon_paper_writes_manifest_skeleton` | **UNEXPECTED — not in baseline** |
| `tests/launcher/test_launch_menu.py::test_launch_daemon_removes_stale_scraper_pidfile` | **UNEXPECTED — not in baseline** |

Both unexpected failures are launcher subprocess tests failing at the same point:
`AssertionError: preflight didn't proceed past the stale-pid check — either the rm
failed or the daemon never spawned` (manifest is `None`). Two unexpected failures
beyond the documented single-fragile-test baseline.

Warning (informational): `polyhustle/cli.py:124 RuntimeWarning: DryrunTrader
produces live-shape fills without signing` (follow-up branch
`feat/dryrun-true-sign`).

---

## 9. Untracked-or-cruft hot spots

Repo-root non-dir files:
```
CLAUDE.md  daemon_base_v1  daemon_base_v1.py  ._.DS_Store  .DS_Store  .env
.env.example  .gitignore  launch  launch_daemon.sh  pyproject.toml  README.md
requirements.txt  RUNBOOK.md  scrape_all.sh  STRATEGY.md
```

macOS junk **still present** (4 files):
```
./._.DS_Store     ./.DS_Store     ./data/._.DS_Store     ./data/.DS_Store
```

Untracked (21) — see §1 list. Notables: `daemon_base_v1` (bash control script,
untracked at root), `active_bots/GOAL.md`, `docs/CURRENT_STATUS.md`, 4 replay
configs, 5 reports, 4 scripts (`check_v2_allowances.py`, `csv_to_parquet.py`,
`onboard_check.py`, `refresh_v2_balance_cache.py`), 3 backtest-run manifests.

`experiments/` = **14 MB, 37 entries** (35 strategy forks + `analysis/`,
`backtest/`, `_tools`, `__pycache__`): `_baseline champion n1..n6 r2_* r3_* r4_*
r5_*`. `pyproject.toml` deliberately excludes these from collection.

---

## 10. Synthesis

The single biggest blocker to running the Phase-5 sweep today is **not** data or
infrastructure — the scraper empty-bids bug is fixed, a clean two-sided
capture-verified session exists (2026-05-12), and the test suite is green except
for two launcher subprocess tests and the known fragile latency test. The blocker
is **replay non-determinism**: against a capture-verified session the walked_vwap
replay neither reproduces the captured live-dryrun PnL (+1.46) nor agrees with the
prior replay (+0.87), and it flips from 2×TP to 2×SL purely by removing benchmark
strategies (−0.91). A sweep is only meaningful if a single config replays to a
stable, capture-faithful result; right now it does not, so any sweep ROI table
would be measuring replay-harness variance, not strategy parameters. The smallest
defensible next step is to pin down that divergence on the 2026-05-12 session —
isolate why benchmark presence changes the walked_vwap trader's fills and why
replay PnL departs from the captured live-dryrun PnL — before any parameter sweep
is run. Phase-5 (72 commits behind, 11 ahead) and phase-5b (61/3) merge state is a
secondary concern that can wait until replay is trustworthy.
```
