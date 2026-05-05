"""EWMA variance estimator for BTC returns."""

from __future__ import annotations

import math
from dataclasses import dataclass

from .constants import (
    DEFAULT_DELTA_SECONDS,
    DEFAULT_EWMA_LAMBDA,
    DEFAULT_MIN_WARMUP,
    SECONDS_PER_YEAR,
)


@dataclass
class VarianceSnapshot:
    """One annualized-sigma reading, emitted once per accepted EWMA sample."""

    timestamp: float
    sigma_annualized: float
    variance_per_interval: float
    n_observations: int


class EWMAVariance:
    """Online EWMA estimator for log-return variance over a fixed sampling interval.

    Annualises by ``SECONDS_PER_YEAR / delta_seconds``. Returns None until
    ``min_warmup`` samples have been observed.
    """

    def __init__(
        self,
        lam: float = DEFAULT_EWMA_LAMBDA,
        delta_seconds: float = DEFAULT_DELTA_SECONDS,
        min_warmup: int = DEFAULT_MIN_WARMUP,
    ) -> None:
        self.lam = lam
        self.delta_seconds = delta_seconds
        self.min_warmup = min_warmup
        self._ann_factor = SECONDS_PER_YEAR / delta_seconds
        self._last_price: float | None = None
        self._last_sample_ts: float | None = None
        self._variance: float = 0.0
        self._n: int = 0

    def update(self, price: float, timestamp: float) -> VarianceSnapshot | None:
        """Ingest one (price, timestamp) sample.

        Returns a snapshot iff (a) at least ``delta_seconds`` have elapsed
        since the last accepted sample AND (b) at least ``min_warmup``
        samples have been ingested. Otherwise returns None.
        """
        if self._last_sample_ts is not None:
            if timestamp - self._last_sample_ts < self.delta_seconds:
                return None
        if self._last_price is not None and self._last_price > 0:
            r = math.log(price / self._last_price)
            r2 = r * r
            if self._n == 0:
                self._variance = r2
            else:
                self._variance = self.lam * self._variance + (1 - self.lam) * r2
            self._n += 1
        self._last_price = price
        self._last_sample_ts = timestamp
        if self._n < self.min_warmup:
            return None
        sigma = math.sqrt(self._variance * self._ann_factor)
        return VarianceSnapshot(
            timestamp=timestamp,
            sigma_annualized=sigma,
            variance_per_interval=self._variance,
            n_observations=self._n,
        )

    def current_sigma(self) -> float | None:
        """Return the current annualized sigma estimate, or None if pre-warmup."""
        if self._n < self.min_warmup:
            return None
        return math.sqrt(self._variance * self._ann_factor)

    def reset(self) -> None:
        """Discard all accumulated state — caller resumes from a cold start."""
        self._last_price = None
        self._last_sample_ts = None
        self._variance = 0.0
        self._n = 0


def ewma_sigma_from_arrays(
    prices,
    timestamps,
    lam: float = DEFAULT_EWMA_LAMBDA,
    delta_seconds: float = DEFAULT_DELTA_SECONDS,
    min_warmup: int = DEFAULT_MIN_WARMUP,
):
    """Vectorized EWMA sigma over a price/timestamp array; backtest helper.

    Caller supplies parallel arrays. Output array of the same length, with
    NaN padding before warmup is reached. Used by backtest harnesses, not on
    the live tick path.
    """
    import numpy as np

    n = len(prices)
    sigmas = np.full(n, np.nan)
    if n < 2:
        return sigmas
    log_returns = np.log(prices[1:] / prices[:-1])
    r2 = log_returns**2
    dt = np.median(np.diff(timestamps))
    ann = SECONDS_PER_YEAR / dt
    var_t = r2[0]
    for i in range(1, len(r2)):
        var_t = lam * var_t + (1 - lam) * r2[i]
        if i + 1 >= min_warmup:
            sigmas[i + 1] = math.sqrt(var_t * ann)
    return sigmas
