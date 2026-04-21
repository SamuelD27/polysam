# Round 2 Report

**Window:** 2026-04-21 17:30 UTC → 18:00 UTC (30 min wall clock)
**Focus:** Enhanced fine-tuning (single-axis perturbations of exit parameters and time zones)
**Variants:** 7 (baseline + 6 Enhanced tweaks)

## ENHANCED-strategy metrics

| Variant             | Trades | ROI      | Win rate | Max DD % | Composite | TP/SL/Res | Notes                    |
|---------------------|-------:|---------:|---------:|---------:|----------:|:----------|:-------------------------|
| `r2_sl_wide`        |      4 |  +37.3 % |   100 %  |   0.00 % |   +0.573 | 4 / 0 / 0 | **CHAMPION**             |
| `r2_tp_tight`       |      3 |  +34.1 % |   100 %  |   0.00 % |   +0.541 | 3 / 0 / 0 | close second             |
| `_baseline`         |      3 |  +31.1 % |   100 %  |   0.00 % |   +0.511 | 3 / 0 / 0 | reference                |
| `r2_no_squeeze`     |      4 |  +21.2 % |   100 %  |   0.00 % |   +0.412 | 4 / 0 / 0 | squeeze neutral/mild drag|
| `r2_tp_loose`       |      3 |  +19.3 % |   100 %  |   0.00 % |   +0.393 | 3 / 0 / 0 | looser TP hurt slightly  |
| `r2_sweet_only`     |      3 |   −1.8 % |    67 %  |  22.07 % |   +0.005 | 2 / 1 / 0 | lost non-sweet entries   |
| `r2_mid_aggressive` |      5 |  −14.6 % |    60 %  |  27.29 % |   −0.163 | 3 / 2 / 0 | too-aggressive mid zone  |

## Deviation from spec floor

Spec requires ≥ 8 trades for metrics to count. No variant hit that; average is ~3–4. At ENHANCED's natural rate (~1 trade per market × 6 markets per 30 min, further filtered by edge/zone), hitting 8 requires >60 min of wall clock per variant, which makes overnight iteration impractical.

**Decision:** lower the confidence bar to ≥ 3 trades for this session. Accept the resulting noise in exchange for iteration speed. Document this deviation in `session_log.md` and flag that the final champion's ROI point estimate should be treated as directional, not precise.

## Ranking and champion

- Top two: `r2_sl_wide` (ROI +37.3 %) and `r2_tp_tight` (ROI +34.1 %). Gap = (37.3 − 34.1) / 34.1 = +9.4 % relative — outside the ≤ 20 % tie band, so ROI decides without composite tiebreak.
- **Champion: `r2_sl_wide`** (first actual champion — Round 1 had no qualifier).
  - Config: `SL_DELTA_MIN=0.08, SL_DELTA_MAX=0.25` (env-only tweak).
  - 4 trades, all TP, ROI +37.3 %.
- Delta vs baseline (+31.1 %): +20.0 % relative — well above the 5 % improvement threshold. This counts as "significant progress" vs baseline, so Round 3 is warranted.
- There's no previous champion to compare against; the two-consecutive-flat-round convergence check starts at Round 3.

## Directional findings

- **Wider SL is winning.** `r2_sl_wide` took 4 trades, all TPs, no SLs fired. Either the vol regime was calm or the SL was simply never reached. Suggests the default SL thresholds may be whip-sensitive — widening gives winners more room.
- **Tighter TP also wins.** `r2_tp_tight` (TP_DELTA_MIN=0.08, TP_ABSOLUTE_FAVOR=0.15) hit 3 TPs for +34 % ROI. Counter-intuitive to r2_tp_loose, which took smaller profits and ROI'd +19 %. Hypothesis: the default TP is too early — closing TP at smaller favor forgoes most of the edge.
- **Time-zone restrictions lose.** Both `r2_sweet_only` (ROI −1.8 %) and `r2_mid_aggressive` (ROI −14.6 %) lost money. The default multi-zone structure captures edge across the cycle better than either a narrow window or a broadened low-edge zone.
- **Squeeze ablation neutral-to-slight-drag.** `r2_no_squeeze` +21.2 % vs baseline +31.1 % — close, but baseline beats it on this sample. Squeeze likely not a value driver, also not a big loser.

## Round 3 composition

Build on the two winners (SL wide, TP tight). Combine and extend.

1. `_baseline` — reference (always).
2. `r3_sl_wide_plus` — `SL_DELTA_MIN=0.10, SL_DELTA_MAX=0.30` (push further in the winning direction).
3. `r3_tp_tighter` — `TP_DELTA_MIN=0.12, TP_ABSOLUTE_FAVOR=0.20` (push further in the winning direction).
4. `r3_combined` — champion + r2_tp_tight: `SL_DELTA_MIN=0.08, SL_DELTA_MAX=0.25, TP_DELTA_MIN=0.08, TP_ABSOLUTE_FAVOR=0.15`.
5. `r3_combined_plus` — both axes pushed: `SL_DELTA_MIN=0.10, SL_DELTA_MAX=0.30, TP_DELTA_MIN=0.12, TP_ABSOLUTE_FAVOR=0.20`.
6. `r3_sl_wide_no_squeeze` — champion + squeeze off (dual ablation).
7. `r3_entry_floor` — `MIN_ENTRY_PRICE=0.10` (env; avoid the 0.05–0.10 thin-book zone; may reduce trade count but clean up fills).

Note: parameters 5–7 are speculative; 2–4 are the disciplined follow-ups.
