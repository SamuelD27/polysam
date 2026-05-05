"""Tests for the per-strategy role attribute that gates live trading.

The bug this guards against: refined was wired to the same live executor as
walked_vwap, so observer benchmarks were placing real orders. The fix in
fix(daemon): gate executor on per-strategy role is twofold —

1. Strategy classes carry a ``role`` attribute ("trader" or "observer",
   default observer). Invalid values fail loud at __init__.
2. daemon_base_v1.strategy_loop binds (executor, event_name_map) once per
   strategy based on role; observer roles get the shared paper executor and
   shadow_-prefixed event names.

These tests cover the strategy-class side of the contract. The
end-to-end "wrapper emits entry_filled, refined emits shadow_entry"
verification lives in the 60-second paper smoke test the operator runs
post-deploy (manifest-based, not part of pytest).
"""
from __future__ import annotations

import pytest

from active_bots.base_strategy import BaseStrategy
from active_bots.enhanced_strategy import EnhancedStrategy
from active_bots.refined_strategy import RefinedStrategy
from active_bots.walked_vwap_strategy import WalkedVWAPStrategy

# ── default role is observer (safe-by-construction) ────────────────────────

def test_base_strategy_default_role_is_observer():
    s = BaseStrategy()
    assert s.role == "observer"


def test_enhanced_strategy_default_role_is_observer():
    s = EnhancedStrategy()
    assert s.role == "observer"


def test_refined_strategy_default_role_is_observer():
    s = RefinedStrategy()
    assert s.role == "observer"


def test_walked_vwap_strategy_default_role_is_observer():
    s = WalkedVWAPStrategy()
    # Walked-VWAP inherits __init__ from RefinedStrategy; default still
    # observer. The daemon explicitly sets role="trader" at instantiation.
    assert s.role == "observer"


# ── explicit trader role on the live trader ───────────────────────────────

def test_walked_vwap_can_be_constructed_as_trader():
    s = WalkedVWAPStrategy(role="trader")
    assert s.role == "trader"


def test_refined_can_be_constructed_as_trader_too():
    # The role is a generic abstraction — a future ops decision could promote
    # any strategy to trader. The contract is "you must opt in explicitly".
    s = RefinedStrategy(role="trader")
    assert s.role == "trader"


# ── invalid role values fail loud ──────────────────────────────────────────

@pytest.mark.parametrize("bad_role", [
    "TRADER",        # case mismatch
    "Observer",      # case mismatch
    "shadow",        # not a valid role even if event prefix
    "trader ",       # whitespace drift
    "",              # empty
    None,            # not a string
    1,               # not a string
])
def test_invalid_role_raises_value_error_on_base_strategy(bad_role):
    with pytest.raises(ValueError, match="role must be 'trader' or 'observer'"):
        BaseStrategy(role=bad_role)


@pytest.mark.parametrize("bad_role", ["TRADER", "Observer", ""])
def test_invalid_role_raises_value_error_on_enhanced_strategy(bad_role):
    with pytest.raises(ValueError, match="role must be 'trader' or 'observer'"):
        EnhancedStrategy(role=bad_role)


@pytest.mark.parametrize("bad_role", ["maker", "main", "primary"])
def test_invalid_role_raises_value_error_on_refined_strategy(bad_role):
    with pytest.raises(ValueError, match="role must be 'trader' or 'observer'"):
        RefinedStrategy(role=bad_role)


def test_invalid_role_raises_value_error_on_walked_vwap_strategy():
    with pytest.raises(ValueError, match="role must be 'trader' or 'observer'"):
        WalkedVWAPStrategy(role="bogus")


# ── role survives inheritance ──────────────────────────────────────────────

def test_role_preserved_through_refined_inheritance():
    # RefinedStrategy.__init__ forwards role to EnhancedStrategy.__init__
    # via super(); make sure the trip-through doesn't drop it.
    s = RefinedStrategy(role="trader")
    assert s.role == "trader"


def test_role_preserved_through_walked_vwap_inheritance():
    # WalkedVWAPStrategy has no __init__ override — relies on RefinedStrategy's.
    s = WalkedVWAPStrategy(role="trader")
    assert s.role == "trader"


# ── role doesn't break existing behaviour ──────────────────────────────────

def test_refined_squeeze_disabled_config_preserved_with_role_kwarg():
    """Regression guard: the role kwarg must not interfere with RefinedStrategy's
    documented design choice of enable_squeeze=False (the session champion
    config the off-limits constraint protects)."""
    s = RefinedStrategy(role="observer")
    assert s.squeeze is None, (
        "RefinedStrategy must keep squeeze disabled regardless of role"
    )
    s_trader = RefinedStrategy(role="trader")
    assert s_trader.squeeze is None
