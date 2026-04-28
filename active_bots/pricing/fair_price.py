"""Binary option fair price using the digital option formula.

P(Up) = Phi(d2)  where  d2 = ln(S/K) / (sigma * sqrt(tau))

tau = time_to_expiry in years.  Drift term dropped (negligible at 5 min).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .constants import SECONDS_PER_YEAR


def _norm_cdf(x: float) -> float:
    """Standard normal CDF via erfc -- no scipy needed."""
    return 0.5 * math.erfc(-x / math.sqrt(2))


@dataclass
class FairPriceResult:
    p_up: float
    d2: float
    sigma_used: float
    time_to_expiry_s: float
    spot: float
    strike: float


class FairPriceModel:
    """Compute fair price of a binary Up/Down option."""

    def compute(
        self,
        spot: float,
        strike: float,
        sigma: float,
        time_to_expiry_s: float,
    ) -> FairPriceResult:
        if time_to_expiry_s <= 0:
            if spot > strike:
                p = 1.0
            elif spot < strike:
                p = 0.0
            else:
                p = 0.5
            return FairPriceResult(p, 0.0, sigma, 0.0, spot, strike)

        if sigma <= 1e-12:
            if spot > strike:
                p = 1.0
            elif spot < strike:
                p = 0.0
            else:
                p = 0.5
            return FairPriceResult(p, 0.0, sigma, time_to_expiry_s, spot, strike)

        if spot == strike:
            return FairPriceResult(0.5, 0.0, sigma, time_to_expiry_s, spot, strike)

        tau = time_to_expiry_s / SECONDS_PER_YEAR
        d2 = math.log(spot / strike) / (sigma * math.sqrt(tau))
        p_up = _norm_cdf(d2)

        return FairPriceResult(p_up, d2, sigma, time_to_expiry_s, spot, strike)

    def compute_batch(self, spots, strike: float, sigmas, times_s):
        import numpy as np
        tau = times_s / SECONDS_PER_YEAR
        sqrt_tau = np.sqrt(np.maximum(tau, 1e-20))
        safe_sigma = np.maximum(sigmas, 1e-12)
        d2 = np.log(spots / strike) / (safe_sigma * sqrt_tau)
        p_up = 0.5 * np.vectorize(math.erfc)(-d2 / math.sqrt(2))
        return np.clip(p_up, 0.001, 0.999)
