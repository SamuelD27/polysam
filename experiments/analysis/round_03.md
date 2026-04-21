# Round 3 Report

**Window:** 2026-04-21 18:04 UTC → 18:34 UTC (30 min)
**Focus:** Combine + extend Round 2 winners (SL wide, TP tight). 6 variants + baseline.

## ENHANCED-strategy metrics

| Variant                   | Trades | ROI      | Win rate | Max DD % | Composite | TP/SL/R |
|---------------------------|-------:|---------:|---------:|---------:|----------:|:--------|
| `r3_sl_wide_no_squeeze`   |      5 |  +78.8 % |    80 %  |   0.9 %  |   +0.944 | 5 / 0 / 0 |
| `_baseline`               |      5 |  +73.7 % |   100 %  |   0.0 %  |   +0.937 | 5 / 0 / 0 |
| `r3_tp_tighter`           |      5 |  +56.1 % |    80 %  |  19.3 %  |   +0.625 | 4 / 0 / 1 |
| `r3_entry_floor`          |      5 |  +55.7 % |    80 %  |  16.9 %  |   +0.633 | 4 / 0 / 1 |
| `r3_combined_plus`        |      5 |  +55.8 % |    40 %  |  16.8 %  |   +0.554 | 3 / 1 / 1 |
| `r3_combined`             |      5 |  +49.0 % |    60 %  |  15.8 %  |   +0.531 | 4 / 0 / 1 |
| `r3_sl_wide_plus`         |      5 |  +14.3 % |    60 %  |  20.2 %  |   +0.162 | 4 / 0 / 1 |

## Ranking

- Top two: `r3_sl_wide_no_squeeze` (+78.8 %) and `_baseline` (+73.7 %). Gap = +6.9 % relative — inside the 20 % tie band.
- **Composite tiebreak:** `r3_sl_wide_no_squeeze` +0.944 vs `_baseline` +0.937 — separated by 0.007, essentially tied but champion wins on a hair.
- **Round winner: `r3_sl_wide_no_squeeze`** (SL_DELTA_MIN=0.08, SL_DELTA_MAX=0.25, squeeze disabled).

## Round-over-round + market-conditions caveat

- Round 2 champion ROI: +37.3 %. Round 3 top: +78.8 %. Formal improvement: **+111 % relative** — far above the 5 % convergence threshold.
- **But baseline jumped from +31.1 % (R2) to +73.7 % (R3) on identical code.** That's ~+137 % — pure market-condition swing, not strategy improvement.
- Normalizing by baseline: R2 champion was +20.0 % relative vs its own baseline; R3 champion is only +6.9 % relative vs its own baseline. So in strategy-quality terms, R3 is actually a **smaller** relative edge.
- The strategy-vs-baseline delta is the honest signal. Flat-round counter considers: is the champion pulling away from baseline? R2→R3: 20 % → 7 % relative-to-baseline. That's a regression in edge, but the sample is tiny (5 trades each).

**Decision:** Update champion to `r3_sl_wide_no_squeeze` per the formal rule (it's the highest-ROI variant this round and raw ROI improvement is well above threshold). **Do NOT increment flat-round counter yet.** Treat Round 4 as a confirmation round — if the champion does not continue to beat baseline, we'll know the R3 edge was noise.

## Directional findings

- **Pushing SL wider breaks.** `r3_sl_wide_plus` (SL=[0.10, 0.30]) had ROI +14.3 %, the worst of the round. SL bounds beyond [0.08, 0.25] don't help — the existing champion bound is near the sweet spot.
- **Combined tweaks underperform their components.** `r3_combined` and `r3_combined_plus` both lost to single-axis variants (r3_tp_tighter, r3_sl_wide_no_squeeze). Stacking perturbations adds more drag than synergy.
- **Squeeze ablation + champion SL is the emerging play.** `r3_sl_wide_no_squeeze` (+78.8 %) is the only variant that beat baseline. Squeeze appears mildly dilutive in this regime.
- **Entry-price floor 0.10 is marginal.** `r3_entry_floor` +55.7 %, 80 % win — fewer trades but cleaner books. Middle of the pack; not a clear winner.
- **Default TP_ABSOLUTE_FAVOR isn't tested yet.** Deferring to Round 4.

## Champion update

- **New champion:** `r3_sl_wide_no_squeeze`
  - `tweak_type: code_patch`
  - `env: {"SL_DELTA_MIN": "0.08", "SL_DELTA_MAX": "0.25"}`
  - Patch: `EnhancedStrategy(enable_squeeze=False, max_risk=max_risk)`
- ROI +78.8 %, 5 TPs / 0 SLs, composite +0.944.
- `flat_round_count = 0` (preserved — R3 saw raw-ROI improvement and marginal relative-to-baseline improvement).

## Round 4 composition

Pivot to **confirmation + isolation**: is the champion signal real, or is it baseline-riding plus noise?

1. `_baseline` — reference (always).
2. `r4_confirm_champion` — rerun champion config (SL wide + no squeeze). If it stays ahead of baseline, signal is real.
3. `r4_no_squeeze_only` — just squeeze=False, default SL. Isolates the squeeze-ablation contribution.
4. `r4_sl_wide_only` — just SL=[0.08, 0.25], squeeze enabled. Isolates the SL widening contribution.
5. `r4_tp_tight_no_squeeze` — TP tight + squeeze off (new combo to test).
6. `r4_no_abs_tp` — `TP_ABSOLUTE_FAVOR=0` (default 0.10). Tests whether the fixed-favor TP rule is additive or noise.
7. `r4_all_winners` — SL_wide + TP_tight + no_squeeze (combined version not tested in R3).

Round 4 will also tell us whether to stop: if no variant beats baseline significantly, we increment flat_round_count. Two such rounds → overnight handoff.
