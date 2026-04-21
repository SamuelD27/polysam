"""Unit tests for compute_effective_max_risk env resolution."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from daemon_base_v1 import compute_effective_max_risk


_ENV_KEYS = ("PORTFOLIO_SIZE_USDC", "MAX_BET_PCT", "MAX_TRADE_SIZE_USDC")


class EffectiveMaxRiskTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {k: os.environ.get(k) for k in _ENV_KEYS}
        for k in _ENV_KEYS:
            os.environ.pop(k, None)

    def tearDown(self) -> None:
        for k in _ENV_KEYS:
            old = self._saved.get(k)
            if old is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old

    def test_no_env_returns_default(self):
        val, src = compute_effective_max_risk()
        self.assertEqual(val, 100.0)
        self.assertEqual(src, "default")

    def test_portfolio_only_default_pct(self):
        os.environ["PORTFOLIO_SIZE_USDC"] = "500"
        val, src = compute_effective_max_risk()
        self.assertEqual(val, 100.0)
        self.assertEqual(src, "env")

    def test_portfolio_with_explicit_pct(self):
        os.environ["PORTFOLIO_SIZE_USDC"] = "1000"
        os.environ["MAX_BET_PCT"] = "0.10"
        val, src = compute_effective_max_risk()
        self.assertEqual(val, 100.0)
        self.assertEqual(src, "env")

    def test_absolute_cap_wins_when_smaller(self):
        os.environ["PORTFOLIO_SIZE_USDC"] = "5000"
        os.environ["MAX_TRADE_SIZE_USDC"] = "50"
        val, src = compute_effective_max_risk()
        self.assertEqual(val, 50.0)
        self.assertEqual(src, "absolute")

    def test_small_portfolio_scales_down(self):
        os.environ["PORTFOLIO_SIZE_USDC"] = "50"
        os.environ["MAX_BET_PCT"] = "0.20"
        val, src = compute_effective_max_risk()
        self.assertAlmostEqual(val, 10.0)
        self.assertEqual(src, "env")

    def test_absolute_only(self):
        os.environ["MAX_TRADE_SIZE_USDC"] = "25"
        val, src = compute_effective_max_risk()
        self.assertEqual(val, 25.0)
        self.assertEqual(src, "absolute")

    # ── Defensive parsing ────────────────────────────────────────────

    def test_portfolio_non_numeric_treated_as_unset(self):
        os.environ["PORTFOLIO_SIZE_USDC"] = "notanumber"
        val, src = compute_effective_max_risk()
        self.assertEqual(val, 100.0)
        self.assertEqual(src, "default")

    def test_portfolio_negative_treated_as_unset(self):
        os.environ["PORTFOLIO_SIZE_USDC"] = "-1000"
        val, src = compute_effective_max_risk()
        self.assertEqual(val, 100.0)
        self.assertEqual(src, "default")

    def test_pct_over_one_falls_back_to_default(self):
        # MAX_BET_PCT=2.0 is nonsensical (>100%); fall back to 0.20 default
        # but keep the portfolio branch active.
        os.environ["PORTFOLIO_SIZE_USDC"] = "1000"
        os.environ["MAX_BET_PCT"] = "2.0"
        val, src = compute_effective_max_risk()
        self.assertAlmostEqual(val, 200.0)  # 1000 * 0.20 default
        self.assertEqual(src, "env")

    def test_pct_zero_falls_back_to_default(self):
        os.environ["PORTFOLIO_SIZE_USDC"] = "1000"
        os.environ["MAX_BET_PCT"] = "0"
        val, src = compute_effective_max_risk()
        self.assertAlmostEqual(val, 200.0)  # 1000 * 0.20 default
        self.assertEqual(src, "env")

    def test_portfolio_derived_beats_larger_abs_cap(self):
        # 1000 * 0.20 = 200; abs cap 500; min(200, 500) = 200 → portfolio wins.
        os.environ["PORTFOLIO_SIZE_USDC"] = "1000"
        os.environ["MAX_BET_PCT"] = "0.20"
        os.environ["MAX_TRADE_SIZE_USDC"] = "500"
        val, src = compute_effective_max_risk()
        self.assertAlmostEqual(val, 200.0)
        self.assertEqual(src, "env")

    # ── Autodetected portfolio override ─────────────────────────────

    def test_autodetect_used_when_env_absent(self):
        # No PORTFOLIO_SIZE_USDC in env; autodetect=500 → 500 * 0.20 = 100.
        val, src = compute_effective_max_risk(portfolio_override=500.0)
        self.assertAlmostEqual(val, 100.0)
        self.assertEqual(src, "autodetect")

    def test_env_beats_autodetect(self):
        # Env-set portfolio dominates any autodetect result.
        # 1000 * 0.20 = 200, not 500 * 0.20 = 100.
        os.environ["PORTFOLIO_SIZE_USDC"] = "1000"
        val, src = compute_effective_max_risk(portfolio_override=500.0)
        self.assertAlmostEqual(val, 200.0)
        self.assertEqual(src, "env")

    def test_autodetect_zero_ignored(self):
        # Zero balance is not a usable portfolio; fall through to default.
        val, src = compute_effective_max_risk(portfolio_override=0.0)
        self.assertEqual(val, 100.0)
        self.assertEqual(src, "default")

    def test_autodetect_negative_ignored(self):
        # Negative portfolio is nonsense; fall through to default.
        val, src = compute_effective_max_risk(portfolio_override=-50.0)
        self.assertEqual(val, 100.0)
        self.assertEqual(src, "default")

    def test_abs_cap_applies_to_autodetect(self):
        # 5000 * 0.20 = 1000; abs cap 200 wins via min(). Source flips to
        # "absolute" — mirrors the existing env-portfolio behavior where
        # the tighter ceiling takes over the label.
        os.environ["MAX_TRADE_SIZE_USDC"] = "200"
        val, src = compute_effective_max_risk(portfolio_override=5000.0)
        self.assertEqual(val, 200.0)
        self.assertEqual(src, "absolute")


if __name__ == "__main__":
    unittest.main()
