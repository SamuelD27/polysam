from __future__ import annotations

from decimal import Decimal

import pytest

from active_bots.execution.fees import (
    CRYPTO,
    DUST_THRESHOLD,
    GEOPOLITICS,
    fee_usdc,
    fee_usdc_us_flat,
)


def test_symmetry_around_half():
    shares = Decimal("100")
    for p_str in ["0.01", "0.1", "0.2", "0.3", "0.49"]:
        p = Decimal(p_str)
        mirror = Decimal("1") - p
        assert fee_usdc(p, shares, CRYPTO) == fee_usdc(mirror, shares, CRYPTO)


def test_max_at_half():
    shares = Decimal("100")
    grid = [Decimal(str(x / 10)) for x in range(1, 10)]
    fees = [(p, fee_usdc(p, shares, CRYPTO)) for p in grid]
    argmax_p, _ = max(fees, key=lambda kv: kv[1])
    assert argmax_p == Decimal("0.5")


def test_zero_at_boundaries():
    assert fee_usdc(Decimal("0"), Decimal("100"), CRYPTO) == Decimal("0")
    assert fee_usdc(Decimal("1"), Decimal("100"), CRYPTO) == Decimal("0")


def test_dust_rounding():
    # at p=0.5, raw = (180/10000) * 0.25 * shares = 0.0045 * shares
    # to be below DUST_THRESHOLD=0.00001, shares must be < ~0.00222
    tiny_shares = Decimal("0.001")
    raw_estimate = Decimal("0.0045") * tiny_shares
    assert raw_estimate < DUST_THRESHOLD
    assert fee_usdc(Decimal("0.5"), tiny_shares, CRYPTO) == Decimal("0")


def test_zero_shares():
    assert fee_usdc(Decimal("0.5"), Decimal("0"), CRYPTO) == Decimal("0")


def test_negative_shares_raises():
    with pytest.raises(ValueError):
        fee_usdc(Decimal("0.5"), Decimal("-1"), CRYPTO)


def test_out_of_range_p_raises():
    with pytest.raises(ValueError):
        fee_usdc(Decimal("1.1"), Decimal("100"), CRYPTO)


def test_float_input_type_error():
    with pytest.raises(TypeError):
        fee_usdc(Decimal("0.5"), 100.0, CRYPTO)  # type: ignore[arg-type]


def test_geopolitics_returns_zero():
    assert fee_usdc(Decimal("0.5"), Decimal("100"), GEOPOLITICS) == Decimal("0")


def test_peak_rate_matches_spec():
    got = fee_usdc(Decimal("0.5"), Decimal("100"), CRYPTO)
    expected = Decimal("0.45")
    assert abs(got - expected) < Decimal("0.000001")


def test_us_flat_less_than_intl():
    us = fee_usdc_us_flat(Decimal("0.5"), Decimal("100"))
    intl = fee_usdc(Decimal("0.5"), Decimal("100"), CRYPTO)
    assert us < intl


def test_us_flat_docstring_mentions_us_only():
    doc = fee_usdc_us_flat.__doc__
    assert doc is not None
    assert "Polymarket US" in doc
    assert "NOT" in doc
