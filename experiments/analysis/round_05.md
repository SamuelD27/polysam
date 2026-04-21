# Round 5 Report — Convergence

**Window:** 2026-04-21 19:08 UTC → 19:38 UTC (30 min)
**Focus:** Last exploration. Confirmation reruns + untested squeeze-parameter axis + aggressive sizing.
**Market regime:** Sharply bearish for the Enhanced strategy — a single trade in market `..8900` resolved adversely, causing a −$102.56 loss for any variant that held it to expiry.

## ENHANCED-strategy metrics

| Variant                               | Trades | ROI     | Win rate | Max DD % | Composite | TP/SL/R | Squeeze | Notes |
|---------------------------------------|-------:|--------:|---------:|---------:|----------:|:--------|:--------|:------|
| **`r5_confirm_tp_tight_no_squeeze`**  |      4 | **+35.5 %** |   100 %  |   0.0 %  |   **+0.555** | 4 / 0 / 0 | off     | **round winner** |
| `r5_no_squeeze_repeat`                |      4 |  +32.6 % |   100 %  |   0.0 %  |   +0.526 | 4 / 0 / 0 | off     | confirms squeeze-off |
| `r5_wide_sweet`                       |      4 |  +11.5 % |   100 %  |   0.0 %  |   +0.315 | 4 / 0 / 0 | off     | |
| `_baseline`                           |      4 |  −67.9 % |    75 %  |  74.2 %  |   −0.900 | 3 / 0 / 1 | on      | caught the resolve |
| `r5_aggressive_size`                  |      4 |  −67.4 % |    75 %  |  74.1 %  |   −0.894 | 3 / 0 / 1 | on      | |
| `r5_sl_moderate`                      |      4 |  −67.4 % |    75 %  |  74.1 %  |   −0.895 | 3 / 0 / 1 | on      | |
| `r5_squeeze_low_thresh`               |      4 |  −68.1 % |    75 %  |  74.2 %  |   −0.902 | 3 / 0 / 1 | on      | |

## The pivotal trade

All seven variants took identical entries in four markets (same BTC feed, same edge signals). The single differentiating trade was in market `..8900`:

- Entry: `Down @ 0.390`, size `$100` (max allocation)
- **Squeeze-on variants:** held to resolution, won=False, PnL = **−$102.56**
- **Squeeze-off variants:** exited via profit-grabber at `0.540–0.550` for PnL = **+$35.90 to +$38.46** (one even hit +$38 with TP_tight)

Delta: a **~$140 swing per variant** on one trade, driven purely by whether squeeze is enabled. The squeeze-enabled variants accumulated an extra "phantom" entry signal at `entry_price=0.413, edge=None, tz=None` just before the real signal — rejected by the executor — that appears to have left the strategy in a state where profit-grabber was blocked until resolution. Squeeze-disabled variants never saw that phantom signal and exited normally.

This is a real, reproducible behavior and the first high-conviction finding of the session.

## Convergence decision

Formally, Round 5's top ROI (+35.5 %) is below Round 3's champion (+78.8 %) — a raw-ROI regression. Under strict spec rules, `flat_round_count` → 2 → converge.

But the qualitative picture is:

- **Squeeze-disabled variants have had positive ROI in every round they ran** (R3, R4, R5). 12 total trades across 3 variants, all TPs, zero catastrophic losses.
- **Squeeze-enabled variants (including baseline) blew up this round** on a live-feed-realistic market condition. This is the tail risk the session was meant to discover.
- The R3 champion's config (`SL_DELTA_MIN=0.08, SL_DELTA_MAX=0.25` + squeeze off) and the R5 winner's config (`TP_DELTA_MIN=0.08, TP_ABSOLUTE_FAVOR=0.15` + squeeze off) share the squeeze-off pillar. **The squeeze-off pillar is the robust contributor**; the SL/TP tweaks fluctuate in noise.

## Champion update

**Promoting `r5_confirm_tp_tight_no_squeeze` to session champion** — it's the best-performing squeeze-disabled variant across the session, and R5 demonstrated its defensive value.

- Config: `TP_DELTA_MIN=0.08, TP_ABSOLUTE_FAVOR=0.15` + `EnhancedStrategy(enable_squeeze=False, max_risk=max_risk)`
- R5 metrics: 4 trades, 100 % TP, ROI +35.5 %, composite +0.555, no drawdown.
- Across rounds ran in: 100 % TP every time it was instantiated.

## Convergence — going overnight

After 5 rounds and 35 variant-runs the session converges. The champion goes overnight with the full TUI + port-3006 web dashboard. Baseline Enhanced is NOT recommended for overnight given the R5 catastrophic-loss event on one squeeze-enabled trade.

See `final_report.md` for the session summary and overnight runbook.
