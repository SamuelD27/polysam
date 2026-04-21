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
        self.assertEqual(src, "portfolio")

    def test_portfolio_with_explicit_pct(self):
        os.environ["PORTFOLIO_SIZE_USDC"] = "1000"
        os.environ["MAX_BET_PCT"] = "0.10"
        val, src = compute_effective_max_risk()
        self.assertEqual(val, 100.0)
        self.assertEqual(src, "portfolio")

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
        self.assertEqual(src, "portfolio")

    def test_absolute_only(self):
        os.environ["MAX_TRADE_SIZE_USDC"] = "25"
        val, src = compute_effective_max_risk()
        self.assertEqual(val, 25.0)
        self.assertEqual(src, "absolute")


if __name__ == "__main__":
    unittest.main()
