"""Latency injection for the runtime PaperTrader fill simulator.

Distinct from ``active_bots/execution/latency.py`` — that module is the
offline backtester's ``LatencyProfile`` (Decimal-typed, profile-based).
This one is a per-call float-ms sampler used by the runtime trader.

Spec: see §3.2 of docs/book_walked_replay_backtester_spec.md and the
"REALISTIC_PAPER_REPLAY" implementation plan §0/§1. The runtime
sampler stamps a sampled latency on every fill so downstream
attribution can correlate latency-drift cost with paper PnL gaps.

Key contract: stochastic models REQUIRE a seed (per spec §3.3 — fix
injection seed per experiment, sweep multiplier instead of tuning).

The LATENCY_MULTIPLIER env var scales every sampled value at call time
(not import time) so a single test run can sweep multipliers without
re-importing the module.
"""

from __future__ import annotations

import logging
import os
import random
from abc import ABC, abstractmethod

logger = logging.getLogger("polyhustle.execution.latency")


class LatencyMultiplierError(ValueError):
    """Raised when LATENCY_MULTIPLIER is set to a negative value."""


def _current_multiplier() -> float:
    """Read LATENCY_MULTIPLIER at call time. Default 1.0; non-negative.

    Negative -> LatencyMultiplierError (caller's bug, not silent).
    Garbage -> log warning, fall back to 1.0.
    """
    raw = os.environ.get("LATENCY_MULTIPLIER")
    if raw is None or raw == "":
        return 1.0
    try:
        m = float(raw)
    except ValueError:
        logger.warning(
            "LATENCY_MULTIPLIER=%r could not be parsed as float; "
            "falling back to 1.0.",
            raw,
        )
        return 1.0
    if m < 0.0:
        raise LatencyMultiplierError(
            f"LATENCY_MULTIPLIER={raw!r} must be non-negative."
        )
    return m


class LatencyModel(ABC):
    """Per-call one-way-latency sampler (milliseconds).

    Implementations may use ``side`` (``"BUY"`` / ``"SELL"``) for
    asymmetric models in the future; the v1 classes ignore it.
    """

    @abstractmethod
    def _sample_raw_ms(self, side: str) -> float:
        """Implementation hook — produce one un-scaled sample in ms.

        Concrete classes implement this; the public ``sample_one_way_ms``
        applies LATENCY_MULTIPLIER + clipping uniformly.
        """

    def sample_one_way_ms(self, side: str) -> float:
        """Return one latency sample in ms, scaled by LATENCY_MULTIPLIER.

        Always non-negative (clipped at 0).
        """
        raw = self._sample_raw_ms(side)
        scaled = raw * _current_multiplier()
        return max(0.0, scaled)


class ZeroLatency(LatencyModel):
    """Backward-compat default — always 0 ms.

    Use for replay parity checks where the captured run had its own
    latency baked into the snapshots.
    """

    def _sample_raw_ms(self, side: str) -> float:  # noqa: ARG002
        return 0.0


class FixedLatency(LatencyModel):
    """Deterministic constant latency. Useful for sweeps."""

    def __init__(self, ms: float) -> None:
        if ms < 0.0:
            raise ValueError(f"ms must be non-negative; got {ms}")
        self._ms = float(ms)

    def _sample_raw_ms(self, side: str) -> float:  # noqa: ARG002
        return self._ms


class GaussianLatency(LatencyModel):
    """Gaussian sampler, clipped at 0. Seed required.

    Spec §3.3: stochastic models MUST take a seed so multiplier sweeps
    are reproducible across runs.
    """

    def __init__(self, mean_ms: float, stdev_ms: float, seed: int) -> None:
        if mean_ms < 0.0:
            raise ValueError(f"mean_ms must be non-negative; got {mean_ms}")
        if stdev_ms < 0.0:
            raise ValueError(f"stdev_ms must be non-negative; got {stdev_ms}")
        self._mean = float(mean_ms)
        self._stdev = float(stdev_ms)
        self._rng = random.Random(seed)

    def _sample_raw_ms(self, side: str) -> float:  # noqa: ARG002
        x = self._rng.normalvariate(self._mean, self._stdev)
        return max(0.0, x)


class EmpiricalLatency(LatencyModel):
    """Sample from a captured distribution. Seed required.

    Default sample set is shipped as a placeholder Gaussian
    {p50: 200, stdev: 100} — calibrate from real data when available
    by passing the measured samples in.
    """

    def __init__(self, samples_ms: list[float], seed: int) -> None:
        if not samples_ms:
            raise ValueError("samples_ms must not be empty")
        if any(s < 0.0 for s in samples_ms):
            raise ValueError("all samples_ms must be non-negative")
        self._samples = list(samples_ms)
        self._rng = random.Random(seed)

    def _sample_raw_ms(self, side: str) -> float:  # noqa: ARG002
        return float(self._rng.choice(self._samples))
