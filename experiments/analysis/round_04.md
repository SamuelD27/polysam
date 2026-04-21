# Round 4 Report

**Window:** 2026-04-21 18:38 UTC → 19:08 UTC (30 min)
**Focus:** Confirm champion + isolate components (SL-wide alone, no-squeeze alone) + new combos.
**Variants:** 7 (baseline + 6 champion-derived).

## ENHANCED-strategy metrics

| Variant                   | Trades | ROI      | Win rate | Max DD % | Composite | TP/SL/R | Note                       |
|---------------------------|-------:|---------:|---------:|---------:|----------:|:--------|:---------------------------|
| `_baseline`               |      4 |  +77.2 % |    75 %  |   1.1 %  |   +0.916 | 2 / 0 / 2 | **round winner**           |
| `r4_tp_tight_no_squeeze`  |      4 |  +67.8 % |   100 %  |   0.0 %  |   +0.878 | 4 / 0 / 0 | best non-baseline          |
| `r4_sl_wide_only`         |      4 |  +59.2 % |    75 %  |   1.1 %  |   +0.737 | 2 / 0 / 2 | SL-axis isolated           |
| `r4_all_winners`          |      4 |  +51.4 % |   100 %  |   0.0 %  |   +0.714 | 4 / 0 / 0 | combination drag visible   |
| `r4_no_squeeze_only`      |      4 |  +47.9 % |   100 %  |   0.0 %  |   +0.679 | 4 / 0 / 0 | squeeze-axis isolated      |
| `r4_confirm_champion`     |      4 |  +39.7 % |   100 %  |   0.0 %  |   +0.597 | 4 / 0 / 0 | **failed to confirm**      |
| `r4_no_abs_tp`            |      2 | +202.0 % |   100 %  |   0.0 %  |   +2.220 | 0 / 0 / 2 | insufficient data (<3)     |

## Key findings

- **Champion confirmation FAILED.** `r4_confirm_champion` (identical config to R3 champion) scored +39.7 % this round vs R3's +78.8 %. The R3 edge was market-condition noise, not strategy quality.
- **Baseline takes the round** with +77.2 % ROI and the best composite among qualifying variants. Across 4 rounds: R1 too few trades, R2 +31.1 %, R3 +73.7 %, R4 +77.2 %. Baseline is within-or-above the top-ranked variant in 3 of 4 rounds.
- **Isolation data:**
  - SL-wide alone (`r4_sl_wide_only`): +59.2 %, below baseline.
  - No-squeeze alone (`r4_no_squeeze_only`): +47.9 %, below baseline.
  - Neither axis individually beats baseline on this sample.
  - Combining them (`r4_confirm_champion`) is the worst of the three (+39.7 %). Interaction drag again, consistent with R3's `r3_combined` finding.
- **`r4_tp_tight_no_squeeze`** (+67.8 %) is the best non-baseline variant, but still trails baseline. 100 % TP, no resolutions — the TP-tight rule is cashing out cleanly without giving trades back.
- **`r4_no_abs_tp`** had only 2 trades, both hold-to-resolution — insufficient data at the 3-trade bar. The 202 % headline is 2 lucky resolutions, not signal.

## Convergence check

- Current champion: `r3_sl_wide_no_squeeze`, recorded at ROI +78.8 %.
- Round 4 highest-ROI variant (excluding baseline reference): `r4_tp_tight_no_squeeze` at +67.8 %.
- Relative change: (67.8 − 78.8) / 78.8 = −14.0 %. **No improvement.**
- **`flat_round_count` → 1.** One more flat round triggers convergence → overnight handoff.

## Champion status

Keep `r3_sl_wide_no_squeeze` on the record as formal champion (per spec: don't downgrade on a single flat round), but **flag concern**: its R4 rerun failed to reproduce, and baseline now clearly outperforms it. If R5 doesn't produce a new winner above baseline, overnight should run **baseline** (i.e., the `_baseline` variant dir unmodified) rather than the current champion.

## Round 5 composition

Last exploration round before convergence check. Mix of: confirmation pulls, one untested axis (squeeze parameters), and a sizing experiment.

1. `_baseline` — reference.
2. `r5_confirm_tp_tight_no_squeeze` — rerun R4 #2. If it holds above baseline → promote to champion.
3. `r5_no_squeeze_repeat` — rerun R4 `r4_no_squeeze_only` — more data on the squeeze ablation.
4. `r5_aggressive_size` — `MAX_BET_PCT=0.50` on baseline. Tests whether sizing up captures the baseline edge better.
5. `r5_squeeze_low_thresh` — `SPIKE_THRESHOLD=1.5` (vs default 2.0). First squeeze-parameter test of the session.
6. `r5_wide_sweet` — extend sweet spot to `(180, 270, 0.08)`, drop other zones and squeeze. Tests if the model is consistently accurate across a broader late window.
7. `r5_sl_moderate` — `SL_DELTA_MIN=0.06, SL_DELTA_MAX=0.20`. Midpoint between baseline and champion SL.
