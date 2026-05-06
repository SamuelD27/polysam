"""Unit tests for polyhustle.execution.latency.LatencyModel hierarchy.

Covers:
- ZeroLatency: always 0.0.
- FixedLatency: returns the constant.
- GaussianLatency: deterministic given seed; mean/stdev approximately
  match input over a 10k-sample loop.
- EmpiricalLatency: deterministic given seed; samples are drawn only
  from the input list.
- LATENCY_MULTIPLIER env: scales the sampled value at call time.
"""

from __future__ import annotations

import statistics

import pytest


def test_zero_latency_returns_zero():
    from polyhustle.execution.latency import ZeroLatency
    m = ZeroLatency()
    assert m.sample_one_way_ms("BUY") == 0.0
    assert m.sample_one_way_ms("SELL") == 0.0


def test_fixed_latency_returns_constant():
    from polyhustle.execution.latency import FixedLatency
    m = FixedLatency(ms=200.0)
    assert m.sample_one_way_ms("BUY") == 200.0
    assert m.sample_one_way_ms("SELL") == 200.0


def test_fixed_latency_rejects_negative():
    from polyhustle.execution.latency import FixedLatency
    with pytest.raises(ValueError):
        FixedLatency(ms=-1.0)


def test_gaussian_latency_is_seed_deterministic():
    from polyhustle.execution.latency import GaussianLatency
    m1 = GaussianLatency(mean_ms=200.0, stdev_ms=50.0, seed=42)
    m2 = GaussianLatency(mean_ms=200.0, stdev_ms=50.0, seed=42)
    samples1 = [m1.sample_one_way_ms("BUY") for _ in range(100)]
    samples2 = [m2.sample_one_way_ms("BUY") for _ in range(100)]
    assert samples1 == samples2


def test_gaussian_latency_mean_approximates_input():
    from polyhustle.execution.latency import GaussianLatency
    m = GaussianLatency(mean_ms=200.0, stdev_ms=50.0, seed=42)
    samples = [m.sample_one_way_ms("BUY") for _ in range(10_000)]
    assert abs(statistics.mean(samples) - 200.0) < 5.0
    assert abs(statistics.stdev(samples) - 50.0) < 5.0


def test_gaussian_latency_clips_at_zero():
    """Samples below 0 are clipped to 0 — never negative latency."""
    from polyhustle.execution.latency import GaussianLatency
    m = GaussianLatency(mean_ms=10.0, stdev_ms=100.0, seed=42)
    samples = [m.sample_one_way_ms("BUY") for _ in range(1000)]
    assert min(samples) >= 0.0


def test_gaussian_latency_requires_seed():
    """Stochastic models must take a seed (per spec §3.3)."""
    import inspect

    from polyhustle.execution.latency import GaussianLatency
    sig = inspect.signature(GaussianLatency.__init__)
    assert "seed" in sig.parameters
    assert sig.parameters["seed"].default is inspect.Parameter.empty, (
        "seed must be required, not optional"
    )


def test_empirical_latency_samples_only_from_input():
    from polyhustle.execution.latency import EmpiricalLatency
    samples_ms = [100.0, 200.0, 500.0]
    m = EmpiricalLatency(samples_ms=samples_ms, seed=7)
    drawn = [m.sample_one_way_ms("BUY") for _ in range(100)]
    assert set(drawn) <= set(samples_ms)


def test_empirical_latency_is_seed_deterministic():
    from polyhustle.execution.latency import EmpiricalLatency
    samples_ms = [100.0, 200.0, 500.0]
    m1 = EmpiricalLatency(samples_ms=samples_ms, seed=7)
    m2 = EmpiricalLatency(samples_ms=samples_ms, seed=7)
    s1 = [m1.sample_one_way_ms("BUY") for _ in range(50)]
    s2 = [m2.sample_one_way_ms("BUY") for _ in range(50)]
    assert s1 == s2


def test_empirical_latency_rejects_empty_samples():
    from polyhustle.execution.latency import EmpiricalLatency
    with pytest.raises(ValueError):
        EmpiricalLatency(samples_ms=[], seed=1)


def test_latency_multiplier_env_scales_sampled_value(monkeypatch):
    """LATENCY_MULTIPLIER scales every sample at call time."""
    from polyhustle.execution.latency import FixedLatency
    m = FixedLatency(ms=100.0)
    monkeypatch.setenv("LATENCY_MULTIPLIER", "2.0")
    assert m.sample_one_way_ms("BUY") == 200.0
    monkeypatch.setenv("LATENCY_MULTIPLIER", "0.5")
    assert m.sample_one_way_ms("BUY") == 50.0


def test_latency_multiplier_default_is_one(monkeypatch):
    from polyhustle.execution.latency import FixedLatency
    monkeypatch.delenv("LATENCY_MULTIPLIER", raising=False)
    m = FixedLatency(ms=123.0)
    assert m.sample_one_way_ms("BUY") == 123.0


def test_latency_multiplier_rejects_negative(monkeypatch):
    from polyhustle.execution.latency import FixedLatency, LatencyMultiplierError
    m = FixedLatency(ms=100.0)
    monkeypatch.setenv("LATENCY_MULTIPLIER", "-1.0")
    with pytest.raises(LatencyMultiplierError):
        m.sample_one_way_ms("BUY")


def test_latency_multiplier_invalid_falls_back_to_one(monkeypatch, caplog):
    """A garbage value logs a warning and falls back to 1.0 — never crash a run."""
    from polyhustle.execution.latency import FixedLatency
    m = FixedLatency(ms=100.0)
    monkeypatch.setenv("LATENCY_MULTIPLIER", "not-a-number")
    with caplog.at_level("WARNING"):
        result = m.sample_one_way_ms("BUY")
    assert result == 100.0
    assert any("LATENCY_MULTIPLIER" in rec.message for rec in caplog.records)
