# Round 1 Report

**Window:** 2026-04-21 16:54 UTC → 17:24 UTC (30 min wall clock)
**Variants launched:** 7 (1 reference + 6 new strategies)
**Execution:** all dry-run, dual-strategy (BASE + ENHANCED inside each daemon)

## ENHANCED-strategy metrics (the variant-under-test)

| Variant          | Trades | ROI      | Win rate | Max DD % | Composite | Exits (TP/SL/Res) | Status             |
|------------------|-------:|---------:|---------:|---------:|----------:|:------------------|:-------------------|
| `_baseline`      |      3 |  +30.7 % |   100 %  |   0.00 % |   +0.507 | 3 / 0 / 0         | insufficient data  |
| `n1_mean_revert` |    115 |   −5.9 % |   1.7 %  |   6.15 % |   −0.086 | 2 / 113 / 0       | OVER-TRADING BUG   |
| `n2_late_gamma`  |      0 |   0.0 %  |     –    |     –    |    0.000 | 0 / 0 / 0         | no triggers in 30m |
| `n3_momentum`    |      2 |  +8.5 %  |   100 %  |   0.00 % |   +0.285 | 2 / 0 / 0         | insufficient data  |
| `n4_market_maker`|      3 | −41.5 %  |    33 %  |  44.84 % |   −0.573 | 1 / 2 / 0         | insufficient data  |
| `n5_no_squeeze`  |      2 | +23.8 %  |   100 %  |   0.00 % |   +0.438 | 2 / 0 / 0         | insufficient data  |
| `n6_vol_regime`  |      1 | +43.2 %  |   100 %  |   0.00 % |   +0.632 | 1 / 0 / 0         | insufficient data  |

## Reference: BASE-strategy metrics (benchmark, same in every daemon)

_baseline's BASE: 2 trades, ROI +65%, both hold-to-resolution (0 TP / 0 SL / 2 Res). Tiny sample, directional only.

## Key observations

- **Only one variant hit the 8-trade floor: `n1_mean_revert`**. By the spec rule (min 8 trades for metrics to count), only it is rankable. But its −5.9% ROI is a bug, not a signal — see Findings.
- **All other variants: insufficient data** (0–3 trades). The 30-min window combined with realistic entry conditions (T+120 onward, edge ≥ 0.10) produces ~1 market-cycle worth of trade opportunities per variant. Noise dominates.
- **Directionally, ENHANCED baseline (3 TP, ROI +30.7%) looks strong** — three clean take-profits in three tries. But the sample is too small to commit.
- **`n2_late_gamma` took zero trades**. The combination of (a) TIME_ZONES shrunk to T+260–285, (b) required edge ≥ 0.20, (c) squeeze disabled, is too restrictive for a 30-min window. Valid hypothesis but needs a longer run to test.
- **`n4_market_maker` lost $23 of $57 risked in 3 trades**. Small sample but consistent with the "adverse selection" hypothesis for passive posters — you get filled exactly when the market is about to move further against you. Dropping this direction.

## Ranking decision

- No variant meets the data floor with positive ROI → **no new champion crowned this round**.
- `champion.json` remains `null`. The directional signal is that **vanilla ENHANCED is healthy** (3/3 TPs, ROI +30.7%) and should be the template for Round 2 fine-tuning.

## Round 2 composition

Per spec §Round ≥ 2, majority shifts to Enhanced fine-tuning, keeping any survivor N-variants that ranked above baseline (none qualified).

**Dropped:**
- `n1_mean_revert` — over-trading bug; re-entry not gated by `_resolved`. Hypothesis inconclusive pending a fix.
- `n4_market_maker` — worst ROI, clear adverse-selection behavior. Passive posting doesn't help on FAK-liquidity markets in this time window.
- `n3_momentum` — hypothesis plausible, but defer — squeeze behavior is easier to ablate via n5/squeeze params.

**Kept (ran again with reset state, confirmatory pass):**
- `_baseline` — reference.
- `r2_no_squeeze` — same idea as n5; repeat to confirm 2-trade positive signal.

**Added Enhanced fine-tunes (6 one-axis perturbations):**
- `r2_tp_tight` — `TP_DELTA_MIN=0.08, TP_ABSOLUTE_FAVOR=0.15` (hold longer, exit only on bigger moves).
- `r2_tp_loose` — `TP_DELTA_MIN=0.03, TP_ABSOLUTE_FAVOR=0.05` (take profit faster, more turnover).
- `r2_sl_wide`  — `SL_DELTA_MIN=0.08, SL_DELTA_MAX=0.25` (less whip-out on high-conviction trades).
- `r2_force_late` — `FORCE_EXIT_BEFORE_S=60` (exit 60s before close instead of 30).
- `r2_sweet_only` — `TIME_ZONES` collapsed to (210, 260, 0.08, "sweet_spot") only; no early/mid/late, no squeeze.
- `r2_mid_aggressive` — `TIME_ZONES` mid lowered to edge_min=0.08 (more mid-zone trades); keep other zones as-is.

Plus `_baseline` = 7 daemons for Round 2.
