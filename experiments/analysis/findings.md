# Findings Log

Append-only. One `###` section per finding. See spec §Findings Log for format.

### 2026-04-21 17:28 UTC — pure mean-reversion over-trades without a once-per-market guard
**Source:** Round 1, variant n1_mean_revert
**Observation:** n1_mean_revert took 115 trades in 30 min (~19 per 5-min market). After the first exit in a market, the strategy immediately re-enters on the next tick because the entry check does not require `_resolved=False`. Result: 2 TPs + 113 SLs, ROI −5.9%. Events show enter→exit→enter cycles 1–3 seconds apart repeating inside a single market window.
**Evidence:** `experiments/n1_mean_revert/daemon_state/events.jsonl` (361 event lines). First 3 trade cycles: ENTER t=0630 Down@0.520 → ENTER 0864 Up@0.310 (next market) → EXIT +25.81 TP (hold 2s) → ENTER 0867 Up@0.400 → EXIT −2.50 SL (hold 0s) → ENTER 0868 Up@0.380 (same market, re-entry 1s after SL).
**Implication:** Any strategy that exits before market resolution must gate re-entry with a `_resolved` flag or a per-market one-shot variable. This is an important lesson for all Round 2+ new-strategy prototypes. Fix pattern: add `if self._resolved: return None` at the top of the entry branch, reset on `t_zero` change.
**Confidence:** high

### 2026-04-21 17:28 UTC — passive posting at fair ± 0.03 exhibits adverse selection
**Source:** Round 1, variant n4_market_maker
**Observation:** 3 trades, 1 TP + 2 SL, ROI −41.5%, max DD 44.8% of risked. Small sample but directionally consistent: when market reaches the passive offset from fair (`market <= fair − 0.03` for Up side), that move is predictive of further adverse movement — we get filled exactly when the thesis is weakest.
**Evidence:** `experiments/analysis/round_01/n4_market_maker_enhanced.json` — all 3 trades sourced from `edge`, 2 hit SL, max_dd_pct 0.448.
**Implication:** FAK (market-take) entries are paying spread for a reason — the spread protects against adverse-selection of passive orders. Rules out passive posting as an alpha source on BTC 5m at this size without a much more sophisticated resting-order model (cancel/replace, orderbook pressure signals).
**Confidence:** medium (small sample, but signal is strong and consistent with market-microstructure theory).

### 2026-04-21 18:02 UTC — exit-parameter tuning dominates time-zone tuning for short-run improvements
**Source:** Round 2 cross-variant synthesis
**Observation:** In Round 2, the two variants that beat baseline (r2_sl_wide +37.3%, r2_tp_tight +34.1%) both tuned exit parameters (SL bounds, TP thresholds) and kept the default time-zone structure. The two variants that modified time zones (r2_sweet_only -1.8%, r2_mid_aggressive -14.6%) both lost money. r2_no_squeeze was neutral (+21.2%).
**Evidence:** `experiments/analysis/round_02.md` full table; `experiments/analysis/round_02/*.json`.
**Implication:** The default TIME_ZONES structure captures edge opportunities well, and perturbing it — either by restricting (sweet_only) or by lowering zone edge floors (mid_aggressive) — hurts. The higher-value lever in the current regime is exit management. Round 3 should continue exploring exit-axis tuning; further time-zone exploration is deprioritized unless a fresh hypothesis emerges.
**Confidence:** medium (small per-variant samples, but consistent direction across all four time-zone vs exit-axis comparisons).

### 2026-04-21 18:38 UTC — baseline ROI swings widely round-to-round; absolute ROI is not a stable signal
**Source:** Round 2 vs Round 3 cross-round comparison of `_baseline`
**Observation:** Unmodified baseline ran +31.1% in R2 and +73.7% in R3 — a +137% relative swing on the same code with the same daemon configuration. This is market-regime variation, not strategy improvement.
**Evidence:** `experiments/analysis/round_02/_baseline_enhanced.json` vs `experiments/analysis/round_03/_baseline_enhanced.json`.
**Implication:** Raw ROI comparison across rounds is meaningless for decision-making. Must normalize by same-round baseline: "strategy delta = variant ROI − baseline ROI" (or relative). Champion progression should be judged by whether the strategy pulls away from baseline consistently, not by absolute ROI trajectory.
**Confidence:** high

### 2026-04-21 18:38 UTC — combined perturbations underperform their single-axis components
**Source:** Round 3 cross-variant comparison
**Observation:** `r3_combined` (SL wide + TP tight) scored +49.0%. `r3_combined_plus` (both pushed further) scored +55.8% but with only 40% win rate. Both lost to r3_tp_tighter alone (+56.1%) and far underperformed the single-axis winner r3_sl_wide_no_squeeze (+78.8%).
**Evidence:** `experiments/analysis/round_03.md` variant table.
**Implication:** Simultaneously perturbing TP and SL parameters appears to introduce interaction drag — positions that survive the wider SL end up exiting early at tighter TP, capturing smaller edge. For overnight champion, prefer single-axis or carefully-ablated tweaks over stacked combinations.
**Confidence:** medium (5 trades per variant; signal is directional but sample-limited).

### 2026-04-21 18:38 UTC — pushing SL beyond [0.08, 0.25] reduces ROI
**Source:** Round 3, r3_sl_wide_plus
**Observation:** SL_DELTA_MIN=0.10, SL_DELTA_MAX=0.30 scored +14.3% ROI — the worst of Round 3 and well below baseline's +73.7%. 4 TPs / 0 SLs / 1 resolution — the SL never actually fired even at wider bounds.
**Evidence:** `experiments/analysis/round_03/r3_sl_wide_plus_enhanced.json`.
**Implication:** The wider SL did not save any losers (none materialized) but let the strategy hold through ambivalent states that ended up resolving rather than TP-ing. The sweet spot for SL bounds is the R2 champion [0.08, 0.25]; further widening is self-defeating in calm markets.
**Confidence:** medium.

### 2026-04-21 19:10 UTC — Enhanced defaults appear to be a local optimum on 5-trade samples
**Source:** Rounds 2-4 cross-round synthesis
**Observation:** Across 12 fine-tuning variants run in Rounds 2-4, no configuration has beaten vanilla baseline for more than one round. The R2 champion (`r2_sl_wide`, +37.3%) was replaced by R3's `r3_sl_wide_no_squeeze` (+78.8%), which failed to reproduce in R4 (+39.7% on same config). In R3 and R4, baseline Enhanced scored +73.7% and +77.2% respectively — at or near the top of the rankings both rounds.
**Evidence:** `experiments/analysis/round_{02,03,04}.md`, `experiments/analysis/champion_history.jsonl`.
**Implication:** The default Enhanced tuning (TP_DELTA_MIN=0.05, SL_DELTA_MIN=0.05, SL_DELTA_MAX=0.20, TP_ABSOLUTE_FAVOR=0.10, TIME_ZONES as defined, squeeze enabled) is a local optimum at the 5-trade-per-30-min sample size. Larger perturbations harm ROI; smaller perturbations are within noise. To detect a real improvement would require either (a) hours-long runs per variant to accumulate 20+ trades, or (b) synthetic market data / backtest replay for faster statistical power. For overnight, baseline is the conservative choice.
**Confidence:** medium-high

### 2026-04-22 03:42 SG (19:42 UTC Apr 21) — squeeze-enabled path can strand a position at resolution after a rejected phantom signal
**Source:** Round 5 cross-variant comparison on market slug ..8900
**Observation:** All seven R5 variants received identical Down-entry signals in market 8900 (edge 0.262, entry_price 0.390, $100 size). Squeeze-disabled variants exited via profit-grabber at 0.540-0.550 for +$36 to +$38 profit; squeeze-enabled variants (including baseline) held to resolution and lost -$102.56 each. The squeeze-on variants logged an additional rejected entry signal (`side=Up, entry_price=0.413, edge=None, tz=None`) immediately before the real fill — not present in squeeze-off logs.
**Evidence:** `experiments/_baseline/daemon_state/events.jsonl` and `experiments/r5_confirm_tp_tight_no_squeeze/daemon_state/events.jsonl`, events for slug=btc-updown-5m-1776788900.
**Implication:** Enabling the squeeze detector appears to allow a state where a subsequent edge entry's profit-grabber doesn't evaluate normally, leading to hold-to-resolution on trades that should have TP'd. Root cause likely in the interaction between `SqueezeDetector.on_tick` setting internal state (e.g., `_is_squeeze_candidate` or deviation tracking) and the profit-grabber's market-freshness or edge lookup. **For production, disable squeeze OR audit the profit-grabber / squeeze-detector state interaction before re-enabling.** This is a high-impact bug/feature-interaction, not mere tuning noise.
**Confidence:** high (4 squeeze-on variants all failed identically; 3 squeeze-off variants all succeeded identically; same market data).
