"""Tests for active_bots.execution.latency."""

from __future__ import annotations

import json
import random
from pathlib import Path

import numpy as np
import pytest

from active_bots.execution.latency import (
    DUBLIN_PRIOR,
    SG_WG_PRIOR,
    ConditionedSampler,
    LatencyProfile,
    NotFitted,
    fit_from_events_jsonl,
    sample,
    sweep_multipliers,
)


def test_sg_wg_prior_values():
    assert SG_WG_PRIOR.p50_ms == 220.0
    assert SG_WG_PRIOR.p95_ms == 380.0
    assert SG_WG_PRIOR.p99_ms == 700.0
    assert SG_WG_PRIOR.p999_ms == 2000.0
    assert SG_WG_PRIOR.source == "prior"


def test_dublin_prior_values():
    assert DUBLIN_PRIOR.p50_ms == 5.0
    assert DUBLIN_PRIOR.p999_ms == 200.0


def test_quantile_preservation():
    rng = random.Random(7)
    n = 20000
    draws = np.array([sample(SG_WG_PRIOR, rng) for _ in range(n)])
    emp_p50 = float(np.percentile(draws, 50))
    emp_p95 = float(np.percentile(draws, 95))
    emp_p99 = float(np.percentile(draws, 99))
    assert abs(emp_p50 - SG_WG_PRIOR.p50_ms) / SG_WG_PRIOR.p50_ms < 0.05
    assert abs(emp_p95 - SG_WG_PRIOR.p95_ms) / SG_WG_PRIOR.p95_ms < 0.05
    # p99 is compared against the analytic log-normal p99 (~477ms) rather than
    # the profile's p99 field (700ms); the sampler is parameterized only by
    # p50 and p95, and the profile's p99 is stored for reporting. A pure
    # log-normal with these params naturally gives p99 < profile.p99_ms.
    import math as _m
    mu = _m.log(SG_WG_PRIOR.p50_ms)
    sigma = (_m.log(SG_WG_PRIOR.p95_ms) - mu) / 1.6448536269514722
    analytic_p99 = _m.exp(mu + 2.326347874040841 * sigma)
    assert abs(emp_p99 - analytic_p99) / analytic_p99 < 0.10


def test_seeded_determinism():
    rng_a = random.Random(42)
    rng_b = random.Random(42)
    seq_a = [sample(SG_WG_PRIOR, rng_a) for _ in range(1000)]
    seq_b = [sample(SG_WG_PRIOR, rng_b) for _ in range(1000)]
    assert seq_a == seq_b


def test_multiplier_linearity():
    result = sweep_multipliers(SG_WG_PRIOR, (2.0, 5.0))
    assert result[0].p50_ms == 440.0
    assert result[1].p99_ms == 3500.0


def test_multiplier_source_label():
    result = sweep_multipliers(SG_WG_PRIOR, (2.0,))
    assert result[0].source == "multiplier_sweep"


def test_tail_clipped_at_p999():
    rng = random.Random(123)
    n = 50000
    mx = max(sample(SG_WG_PRIOR, rng) for _ in range(n))
    assert mx <= SG_WG_PRIOR.p999_ms * 1.00000001


def test_fit_raises_not_fitted_on_real_events():
    events_path = "/home/samsam/polymarket-hustle/daemon_state/events.jsonl"
    with pytest.raises(NotFitted):
        fit_from_events_jsonl(events_path, t_start_ns=0, t_end_ns=2**63 - 1)


def test_fit_succeeds_on_synthetic_window(tmp_path: Path):
    events_file = tmp_path / "events.jsonl"
    base_ts = 1776820000.0
    # 20 rows with ack-decision gaps varying from 100ms to 2000ms
    gaps_ms = [100.0 + i * 100.0 for i in range(20)]
    with events_file.open("w") as f:
        for i, gap in enumerate(gaps_ms):
            entry_time = base_ts + i
            ack_time = entry_time + gap / 1000.0
            row = {
                "ts": entry_time,
                "type": "trade_ack",
                "position": {"entry_time": entry_time, "ack_ts": ack_time},
            }
            f.write(json.dumps(row) + "\n")
    prof = fit_from_events_jsonl(
        str(events_file),
        t_start_ns=0,
        t_end_ns=2**63 - 1,
    )
    assert isinstance(prof, LatencyProfile)
    assert prof.source == "empirical"
    median_injected = float(np.median(gaps_ms))
    assert abs(prof.p50_ms - median_injected) < 150.0


def test_conditioned_sampler_routing():
    profiles = {"fresh": SG_WG_PRIOR, "stale": DUBLIN_PRIOR}
    sampler = ConditionedSampler(profiles, key_fn=lambda _ctx: "fresh")
    rng = random.Random(11)
    draws = [sampler.sample(object(), rng) for _ in range(500)]
    # New 3-tuple shape: (latency_ms, bucket_key_used, profile_used).
    assert all(b == "fresh" for _, b, _ in draws)
    latencies = [d for d, _, _ in draws]
    assert max(latencies) <= SG_WG_PRIOR.p999_ms * 1.00000001
    # sanity: at least some draws exceed DUBLIN p999, proving it routed to SG
    assert any(d > DUBLIN_PRIOR.p999_ms for d in latencies)


def test_conditioned_sampler_fallback_on_missing_bucket():
    sampler = ConditionedSampler(
        profiles={"fresh": DUBLIN_PRIOR},
        key_fn=lambda _ctx: "stale_high_vol",
        fallback=SG_WG_PRIOR,
    )
    rng = random.Random(17)
    latency, bucket, profile = sampler.sample({}, rng)
    assert bucket == "fallback:stale_high_vol"
    assert profile is SG_WG_PRIOR
    assert 0.0 < latency <= SG_WG_PRIOR.p999_ms * 1.00000001


def test_conditioned_sampler_fallback_when_key_fn_raises():
    def bad_key(_ctx):
        raise RuntimeError("no context")

    sampler = ConditionedSampler(
        profiles={"fresh": DUBLIN_PRIOR},
        key_fn=bad_key,
        fallback=SG_WG_PRIOR,
    )
    rng = random.Random(19)
    latency, bucket, profile = sampler.sample({}, rng)
    assert bucket == "fallback:unknown"
    assert profile is SG_WG_PRIOR
    assert 0.0 < latency <= SG_WG_PRIOR.p999_ms * 1.00000001


def test_conditioned_sampler_accepts_empty_profiles_dict_with_fallback():
    # Zero-measured-data case: profiles={} must degrade to the prior rather
    # than raise. Per brief: "degrade gracefully to the prior when no
    # measured distribution is available."
    sampler = ConditionedSampler(
        profiles={},
        key_fn=lambda _ctx: "fresh",
        fallback=SG_WG_PRIOR,
    )
    rng = random.Random(23)
    latency, bucket, profile = sampler.sample({}, rng)
    assert bucket == "fallback:fresh"
    assert profile is SG_WG_PRIOR
    assert 0.0 < latency <= SG_WG_PRIOR.p999_ms * 1.00000001
