"""Latency sampler and profile types for the book-walked replay backtester.

Spec §3.1: default Singapore + WireGuard prior is
{p50: 220, p95: 380, p99: 700, p99.9: 2000} ms.

Log-normal fit:
    mu = ln(p50)
    sigma = (ln(p95) - mu) / z_95   with z_95 = 1.6448536269514722
    x = exp(mu + sigma * z),  z ~ N(0, 1)
    clip: min(x, p999)

Pure stdlib. No imports from book.py, fees.py, replay_executor.py, harness.py.
"""

from __future__ import annotations

import dataclasses
import json
import math
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

Z_95 = 1.6448536269514722


@dataclass(frozen=True)
class LatencyProfile:
    p50_ms: float
    p95_ms: float
    p99_ms: float
    p999_ms: float
    source: Literal["prior", "empirical", "multiplier_sweep"] = "prior"


SG_WG_PRIOR: LatencyProfile = LatencyProfile(220.0, 380.0, 700.0, 2000.0, "prior")
DUBLIN_PRIOR: LatencyProfile = LatencyProfile(5.0, 15.0, 40.0, 200.0, "prior")


class NotFitted(Exception):
    """Raised when fit_from_events_jsonl cannot compute empirical quantiles
    (missing ack_ts column in the events window)."""


def sample(profile: LatencyProfile, rng: random.Random) -> float:
    """Draw one latency in milliseconds from a log-normal whose median is p50
    and whose 95th percentile is p95. Tail is clipped at p999 (hard cap).
    Deterministic given rng seed."""
    mu = math.log(profile.p50_ms)
    sigma = (math.log(profile.p95_ms) - mu) / Z_95
    z = rng.normalvariate(0.0, 1.0)
    x = math.exp(mu + sigma * z)
    return float(min(x, profile.p999_ms))


def sweep_multipliers(
    profile: LatencyProfile,
    multipliers: tuple[float, ...] = (1.0, 2.0, 5.0, 10.0),
) -> list[LatencyProfile]:
    """Scale every quantile by each multiplier. Source becomes
    'multiplier_sweep'."""
    out: list[LatencyProfile] = []
    for m in multipliers:
        out.append(
            dataclasses.replace(
                profile,
                p50_ms=profile.p50_ms * m,
                p95_ms=profile.p95_ms * m,
                p99_ms=profile.p99_ms * m,
                p999_ms=profile.p999_ms * m,
                source="multiplier_sweep",
            )
        )
    return out


def _percentile_sorted(values_sorted: list[float], q: float) -> float:
    """Linear-interpolation percentile on a pre-sorted list. q in [0, 1]."""
    if not values_sorted:
        raise ValueError("empty list")
    if len(values_sorted) == 1:
        return values_sorted[0]
    pos = q * (len(values_sorted) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return values_sorted[lo]
    frac = pos - lo
    return values_sorted[lo] * (1.0 - frac) + values_sorted[hi] * frac


def _extract(row: dict, key: str):
    if key in row:
        return row.get(key)
    sub = row.get("position") or row.get("trade")
    if isinstance(sub, dict) and key in sub:
        return sub.get(key)
    return None


def fit_from_events_jsonl(
    path: str | Path,
    t_start_ns: int,
    t_end_ns: int,
    ack_key: str = "ack_ts",
    decision_key: str = "entry_time",
) -> LatencyProfile:
    """Walk events.jsonl rows in [t_start_ns, t_end_ns] where both ack_key and
    decision_key are present. Compute (ack - decision) * 1000 in ms; return a
    LatencyProfile from empirical p50/p95/p99/p99.9.

    Rows missing either key are skipped. Fewer than 10 usable rows raises
    NotFitted.
    """
    p = Path(path)
    gaps_ms: list[float] = []
    unusable = 0
    if not p.exists():
        raise NotFitted(f"file not found: {p}")
    with p.open("r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                unusable += 1
                continue
            ts = row.get("ts")
            if ts is None:
                unusable += 1
                continue
            ts_ns = int(float(ts) * 1e9)
            if ts_ns < t_start_ns or ts_ns > t_end_ns:
                continue
            ack = _extract(row, ack_key)
            dec = _extract(row, decision_key)
            if ack is None or dec is None:
                unusable += 1
                continue
            try:
                gap = (float(ack) - float(dec)) * 1000.0
            except (TypeError, ValueError):
                unusable += 1
                continue
            if gap < 0:
                unusable += 1
                continue
            gaps_ms.append(gap)
    if len(gaps_ms) < 10:
        raise NotFitted(
            f"only {len(gaps_ms)} usable rows (unusable={unusable}); "
            f"need >= 10 to fit empirical quantiles"
        )
    gaps_ms.sort()
    return LatencyProfile(
        p50_ms=float(_percentile_sorted(gaps_ms, 0.50)),
        p95_ms=float(_percentile_sorted(gaps_ms, 0.95)),
        p99_ms=float(_percentile_sorted(gaps_ms, 0.99)),
        p999_ms=float(_percentile_sorted(gaps_ms, 0.999)),
        source="empirical",
    )


class ConditionedSampler:
    """Multi-bucket latency sampler with graceful fallback.

    Intended use: bucket by (tunnel_age_bucket, vol_regime) — e.g.
    {"fresh_low": profile_a, "fresh_high": profile_b, "stale_low": ...}.
    ``key_fn`` takes a context object (typically a dict) and returns a bucket
    key. If the key is not present in ``profiles``, ``fallback`` is used; the
    returned bucket label on ``sample`` is ``"fallback:{key}"`` so downstream
    audit can distinguish measured-per-bucket data from a prior-backed draw.

    Default ``fallback = SG_WG_PRIOR`` per spec §3.1, so a fresh session
    (no empirical fit yet) that still wants bucket-aware plumbing degrades
    cleanly to the documented Singapore+WireGuard prior.
    """

    def __init__(
        self,
        profiles: dict[str, LatencyProfile],
        key_fn: Callable[[object], str],
        fallback: LatencyProfile = SG_WG_PRIOR,
    ):
        self._profiles: dict[str, LatencyProfile] = dict(profiles)  # allowed to be empty
        self._key_fn = key_fn
        self._fallback = fallback

    def sample(self, context: object, rng: random.Random) -> tuple[float, str, LatencyProfile]:
        """Return ``(latency_ms, bucket_key_used, profile_used)``.

        ``bucket_key_used`` is:
          - the caller's key verbatim when ``profiles[key]`` is present;
          - ``"fallback:{key}"`` when the key is not configured and the
            fallback profile was used;
          - ``"fallback:unknown"`` if ``key_fn`` raised.
        """
        try:
            key = self._key_fn(context)
        except Exception:
            return sample(self._fallback, rng), "fallback:unknown", self._fallback
        profile = self._profiles.get(key)
        if profile is None:
            return sample(self._fallback, rng), f"fallback:{key}", self._fallback
        return sample(profile, rng), key, profile
