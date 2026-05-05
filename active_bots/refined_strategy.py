"""Refined strategy — session champion from the 2026-04-21 fine-tuning run.

Configuration pillars that differ from vanilla EnhancedStrategy:

- **Squeeze disabled.** Round 5 of the fine-tuning session surfaced a
  state-interaction issue where the squeeze detector's tick loop can leave
  a subsequent edge-sourced position unable to exit via the profit grabber,
  causing it to hold to resolution. Every squeeze-enabled variant (including
  unmodified baseline) lost −$102.56 on one adverse market; every squeeze-
  disabled variant exited profitably at +$36 to +$38 on the same entry.
  See ``experiments/analysis/findings.md`` for the full write-up.

- **Tighter take-profit.** ``TP_DELTA_MIN=0.08`` (default 0.05) and
  ``TP_ABSOLUTE_FAVOR=0.15`` (default 0.10) — forces the strategy to wait
  for a larger favorable move before cashing out. Across the session,
  variants with tighter TP thresholds combined with squeeze-off produced
  100 % TP rates on every round they ran.

Env vars can still override (pass-through remains) but the defaults bake in
the session's preferred tuning.
"""

from __future__ import annotations

from .enhanced_strategy import MAX_RISK, EnhancedStrategy


class RefinedStrategy(EnhancedStrategy):
    """EnhancedStrategy pre-tuned with the session champion parameters."""

    DEFAULT_TP_DELTA_MIN = 0.08
    DEFAULT_TP_ABSOLUTE_FAVOR = 0.15

    def __init__(self, max_risk: float = MAX_RISK, role: str = "observer"):
        super().__init__(
            enable_time_based=True,
            enable_profit_grabber=True,
            enable_squeeze=False,
            max_risk=max_risk,
            tp_delta_min=self.DEFAULT_TP_DELTA_MIN,
            tp_absolute_favor=self.DEFAULT_TP_ABSOLUTE_FAVOR,
            role=role,
        )
