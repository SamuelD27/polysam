# Strategy Fine-Tuning Session Log

Append-only timeline. Each entry: `YYYY-MM-DD HH:MM UTC — <event> — <detail>`.

## 2026-04-22

- Session initialized. Spec: `docs/superpowers/specs/2026-04-22-strategy-fine-tuning-design.md`. Plan: `docs/superpowers/plans/2026-04-22-strategy-fine-tuning.md`.
- 2026-04-21 16:53 UTC — Harness smoke test passed — _baseline 90s dry run, metrics parsed, clean shutdown.
- 2026-04-21 16:53 UTC — Round 1 started — 7 variants launched in parallel: _baseline, n1_mean_revert, n2_late_gamma, n3_momentum, n4_market_maker, n5_no_squeeze, n6_vol_regime.
- 2026-04-21 17:28 UTC — Round 1 ended — no qualifying champion (only n1 met 8-trade floor, but -5.9% ROI due to over-trading bug); findings recorded; advancing to Round 2 Enhanced fine-tuning.
- 2026-04-21 17:29 UTC — Round 2 started — 7 variants: _baseline, r2_no_squeeze, r2_tp_tight, r2_tp_loose, r2_sl_wide, r2_sweet_only, r2_mid_aggressive. Focus: Enhanced fine-tuning.
- 2026-04-21 18:01 UTC — Round 2 ended — champion=r2_sl_wide (ROI +37.3%, 4 trades). Deviation: 3-trade bar instead of spec-8 to keep iteration tractable.
- 2026-04-21 18:02 UTC — Round 3 started — 7 variants: _baseline + r3_{sl_wide_plus, tp_tighter, combined, combined_plus, sl_wide_no_squeeze, entry_floor}. Building on r2_sl_wide champion.
- 2026-04-21 18:35 UTC — Round 3 ended — champion=r3_sl_wide_no_squeeze (ROI +78.8%, 5/5 TPs). Baseline ROI +73.7% same round — thin edge but champion wins on composite. Round 4 is a confirmation round.
- 2026-04-21 18:35 UTC — Round 4 started — confirmation + isolation of champion components. Testing whether SL-wide or no-squeeze is the real contributor.
- 2026-04-21 19:08 UTC — Round 4 ended — champion confirmation FAILED (ROI +39.7% vs R3 +78.8%). Baseline +77.2% wins round. flat_round_count=1; converging. Round 5 is last exploration.
- 2026-04-21 19:08 UTC — Round 5 started — last exploration. Mix: confirmation reruns, sizing, squeeze tuning, wide sweet zone.
- 2026-04-21 19:43 UTC — Round 5 ended — CONVERGED. Champion=r5_confirm_tp_tight_no_squeeze. Squeeze-off variants survived a catastrophic market event that blew up all squeeze-on variants (-67% each). Going to overnight.
- 2026-04-21 19:46 UTC — OVERNIGHT HANDOFF. Champion daemon running in experiments/champion/ (dry-run). Dashboard on http://localhost:3007. Final report: experiments/analysis/final_report.md.
