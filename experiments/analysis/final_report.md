# Strategy Fine-Tuning Session — Final Report

**Session:** 2026-04-21 16:54 UTC → 19:45 UTC (≈2h 50min)
**Rounds:** 5
**Variants evaluated:** 35 (7 parallel per round)
**Environment:** all dry-run (PaperExecutor), live BTC feed + live Polymarket RTDS
**Repo branch:** `dev/btc5m-scraper`
**Experiment root:** `experiments/` (git-ignored)

---

## Overnight champion — running now

**`experiments/champion/`** — final champion, launched as background daemon on top of the session.

| Property                 | Value                                                           |
|--------------------------|-----------------------------------------------------------------|
| Provenance               | `r5_confirm_tp_tight_no_squeeze`                                |
| Tweak type               | `code_patch` + env                                              |
| Env                      | `TP_DELTA_MIN=0.08`, `TP_ABSOLUTE_FAVOR=0.15`                   |
| Code patch               | `EnhancedStrategy(enable_squeeze=False, max_risk=max_risk)`     |
| Mode                     | `POLYMARKET_MODE=live POLYMARKET_DRY_RUN=1` → PaperExecutor     |
| Daemon PID               | see `experiments/champion/runtime.json`                         |
| Dashboard                | **http://localhost:3007** (separate from main daemon's 3006)    |
| Logs                     | `experiments/champion/daemon_state/daemon.log`                  |
| Events                   | `experiments/champion/daemon_state/events.jsonl`                |
| Dashboard server log     | `experiments/champion/dashboard.log`                            |

**Pre-existing daemons on this host are untouched.** The main-project daemon is still running in the polybot netns (live-dryrun from before this session), writing to `/home/samsam/polymarket-hustle/daemon_state/` and serving its dashboard on port 3006. The champion daemon + dashboard run on different paths and different port.

To stop the champion daemon in the morning:

```bash
cd /home/samsam/polymarket-hustle
experiments/_tools/stop_variant.sh experiments/champion
kill $(cat experiments/champion/dashboard.pid)
```

---

## Session trajectory

| Round | Start UTC | Champion after          | Top ROI    | Notes                                   |
|------:|:---------:|:------------------------|:-----------|:----------------------------------------|
| 1     | 16:54     | (none)                  |  −5.9 %*   | only n1 met 8-trade floor, had a bug    |
| 2     | 17:30     | `r2_sl_wide`            | +37.3 %    | first qualifying champion                |
| 3     | 18:04     | `r3_sl_wide_no_squeeze` | +78.8 %    | marginal edge vs baseline, market hot   |
| 4     | 18:38     | (kept; failed confirm)  | +67.8 %    | baseline wins round; flat_round_count=1 |
| 5     | 19:08     | **`r5_confirm_tp_tight_no_squeeze`** | +35.5 % | **pivotal — see below** |

\* n1's hypothesis was inconclusive because of an over-trading implementation bug.

---

## Key findings (see `experiments/analysis/findings.md` for full entries)

1. **Pure mean-reversion over-trades catastrophically without a once-per-market re-entry guard.** 115 trades in 30 min for n1 because `_resolved=True` was set but not checked on re-entry. Strategy hypothesis remains inconclusive pending the fix.

2. **Passive posting at fair ± 0.03 exhibits adverse selection.** FAK spread premium protects against the "I get filled precisely when the thesis weakens" problem. Rules out passive MM at this size without orderbook pressure signals.

3. **Exit-parameter tuning dominates time-zone tuning for short-run improvements.** All four time-zone perturbations (sweet_only, mid_aggressive, wide_sweet, late_gamma) underperformed; SL/TP/squeeze-axis tweaks did not.

4. **Combined perturbations underperform their single-axis components.** `combined` and `combined_plus` in Round 3; `all_winners` in Round 4. Stacking tweaks adds interaction drag, not synergy.

5. **Pushing SL beyond [0.08, 0.25] reduces ROI.** `r3_sl_wide_plus` (SL=[0.10, 0.30]) scored the worst of its round because it held through ambivalent states that resolved rather than TP'd.

6. **Baseline ROI swings wildly round-to-round** (−67.9 % to +77.2 % across rounds on identical code). Absolute ROI is not a stable cross-round signal; must normalize by same-round baseline.

7. **Enhanced defaults are a local optimum at 5-trade sample sizes.** 12 fine-tuning variants over rounds 2–4 produced no reproducible winner above baseline.

8. **🏆 PIVOTAL: Enabling squeeze can strand a position at hold-to-resolution after a rejected phantom entry signal.** R5 showed a ~$140/trade swing on one market: every squeeze-enabled variant (including baseline) resolved at −$102.56; every squeeze-disabled variant exited at +$36–38. This is the highest-confidence finding of the session.

---

## Recommendations for live-money ramp-up

### Ship

1. **Always run with `enable_squeeze=False`** until the squeeze / profit-grabber state interaction is audited. Specifically, investigate what the rejected "phantom" edge signal (side=Up, edge=None, tz=None) was — likely a stale-cache or edge-recalc race that the squeeze-enabled path somehow enables. See `findings.md` entry dated 2026-04-22 03:42.
2. **`TP_DELTA_MIN=0.08, TP_ABSOLUTE_FAVOR=0.15`** pairs well with squeeze-off. 100 % TP rate across every round it ran.
3. **Leave `SL_DELTA_MIN`, `SL_DELTA_MAX`, `TIME_ZONES`, and size logic at defaults.** None of the session perturbations beat defaults in a reproducible way.
4. **Ramp schedule per RUNBOOK §5:** `MAX_TRADE_SIZE_USDC=1 → 10 → 25 → 100`. Monitor paper PnL (already-running main-project live-dryrun daemon) vs real PnL — divergence > 5 ¢/trade is the stop-and-investigate line.

### Do not ship

1. **Any new-strategy variant (n1–n6).** None beat baseline, and n1 had a bug.
2. **Time-zone perturbations.** Default zones dominate.
3. **Stacked multi-axis tweaks.** Combined perturbations consistently underperformed single-axis.

### Audit before re-enabling squeeze

Reproduce the market-8900 R5 scenario in isolation and verify:
- Was the phantom rejected signal from `EntrySignal` re-evaluation at a stale `market_price_up`?
- Did the squeeze detector's `_is_squeeze_candidate=True` interfere with edge entry's `fair` calculation in `TimeBasedStrategy.on_tick`?
- Does `ProfitGrabber.check_exit` skip exits for edge-sourced positions when squeeze state is set?

Events file for the 8900 market is preserved at `experiments/_baseline/daemon_state/events.jsonl` and `experiments/r5_confirm_tp_tight_no_squeeze/daemon_state/events.jsonl`.

---

## Process lessons

- **8-trade minimum was impractical** for 30-min rounds with ~1 trade/market × 6 markets. Used a 3-trade floor instead; noted the deviation.
- **Cross-round comparisons are noisy.** Market-regime shifts between rounds are larger than most tuning deltas; rely on delta-vs-same-round-baseline, not round-over-round absolute ROI.
- **Pivotal signals often emerge from adverse events, not lucky runs.** The R5 crash on market 8900 produced the most actionable finding of the session. Be patient during "bad" rounds; they are where tail risk surfaces.

---

## Artifacts

- `experiments/analysis/session_log.md` — full timeline
- `experiments/analysis/round_{01..05}.md` — per-round reports
- `experiments/analysis/round_{01..05}/*.json` — raw metrics JSON per variant per round
- `experiments/analysis/findings.md` — consolidated findings log
- `experiments/analysis/champion.json` / `champion_history.jsonl` — champion progression
- `experiments/*/variant.json` — 35 variant specs
- `experiments/*/daemon_state/events.jsonl` — full trade-level event logs per variant per round
- `docs/superpowers/specs/2026-04-22-strategy-fine-tuning-design.md` — session design
- `docs/superpowers/plans/2026-04-22-strategy-fine-tuning.md` — implementation plan

---

## Morning checklist

1. Open **http://localhost:3007** to see champion overnight performance.
2. Check `experiments/champion/daemon_state/daemon.log` for errors / disconnects.
3. Run `conda run -n polymarket-env python3 -m experiments._tools.metrics experiments/champion/daemon_state enhanced` for the overnight metrics summary.
4. Decide whether to proceed to the live-capital ramp (see recommendations above) or request another tuning session.
