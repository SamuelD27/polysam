from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

BPS_PEAK: Decimal = Decimal("180")  # peak rate in basis points at p=0.5
DUST_THRESHOLD: Decimal = Decimal("0.00001")  # USDC; per help.polymarket.com/en/articles/13364471

_ZERO = Decimal("0")
_ONE = Decimal("1")
_BPS_DIVISOR = Decimal("10000")


@dataclass(frozen=True)
class FeeCategory:
    name: Literal["crypto", "finance", "geopolitics"]
    taker_coefficient: Decimal  # 1 for paid categories, 0 for geopolitics


CRYPTO: FeeCategory = FeeCategory(name="crypto", taker_coefficient=Decimal("1"))
FINANCE: FeeCategory = FeeCategory(name="finance", taker_coefficient=Decimal("1"))
GEOPOLITICS: FeeCategory = FeeCategory(name="geopolitics", taker_coefficient=Decimal("0"))

CATEGORIES: dict[str, FeeCategory] = {
    "crypto": CRYPTO,
    "finance": FINANCE,
    "geopolitics": GEOPOLITICS,
}


def _coerce(x: Decimal | int | str, name: str) -> Decimal:
    if isinstance(x, bool):
        raise TypeError(f"{name} must not be bool")
    if isinstance(x, float):
        raise TypeError(f"{name} must not be float; use Decimal or str")
    if isinstance(x, Decimal):
        return x
    if isinstance(x, (int, str)):
        return Decimal(str(x))
    raise TypeError(f"{name} must be Decimal, int, or str; got {type(x).__name__}")


def fee_usdc(p: Decimal, shares: Decimal, category: FeeCategory) -> Decimal:
    """Polymarket international bell-curve taker fee.

    Raises ValueError for shares < 0 or p outside [0, 1].
    Returns Decimal(0) for shares == 0, p == 0, p == 1, or result below DUST_THRESHOLD.
    Result is symmetric around p=0.5 and peaks at p=0.5.
    """
    p_d = _coerce(p, "p")
    shares_d = _coerce(shares, "shares")

    if shares_d < _ZERO:
        raise ValueError(f"shares must be >= 0; got {shares_d}")
    if p_d < _ZERO or p_d > _ONE:
        raise ValueError(f"p must be in [0, 1]; got {p_d}")

    if shares_d == _ZERO or p_d == _ZERO or p_d == _ONE:
        return _ZERO

    raw = (BPS_PEAK / _BPS_DIVISOR) * p_d * (_ONE - p_d) * shares_d * category.taker_coefficient

    if raw < DUST_THRESHOLD:
        return _ZERO
    return raw


def fee_usdc_us_flat(p: Decimal, shares: Decimal, theta: Decimal = Decimal("0.05")) -> Decimal:
    """Polymarket US only (CFTC-licensed venue, polymarketexchange.com/fees-hours.html,
    effective 2026-04-03). NOT used on our international venue; shipped so downstream
    code has a typed reference. Flat taker coefficient theta in bps-style units applied
    as (theta / 100) * p * (1-p) * shares, which is strictly below the international
    bell-curve rate at default theta=0.05.
    """
    p_d = _coerce(p, "p")
    shares_d = _coerce(shares, "shares")
    theta_d = _coerce(theta, "theta")

    if shares_d < _ZERO:
        raise ValueError(f"shares must be >= 0; got {shares_d}")
    if p_d < _ZERO or p_d > _ONE:
        raise ValueError(f"p must be in [0, 1]; got {p_d}")
    if theta_d < _ZERO:
        raise ValueError(f"theta must be >= 0; got {theta_d}")

    if shares_d == _ZERO or p_d == _ZERO or p_d == _ONE:
        return _ZERO

    raw = (theta_d / Decimal("100")) * p_d * (_ONE - p_d) * shares_d

    if raw < DUST_THRESHOLD:
        return _ZERO
    return raw
