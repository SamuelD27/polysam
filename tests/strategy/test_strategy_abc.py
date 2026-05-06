"""Tests for ``polyhustle.strategies.strategy_abc.Strategy``.

Two contracts:

1. The four production strategy classes inherit from ``Strategy`` and
   instantiate cleanly (no missing abstract methods).
2. A class that does NOT implement the abstract methods raises
   ``TypeError`` at instantiation — proving the ABC is doing its job.

The on_tick signature divergences (``BaseStrategy`` does not accept
``market_price_ts``; ``EnhancedStrategy`` does not accept ``books``)
are documented as findings in ``strategy_abc.py``'s module docstring;
Python's ABC machinery does not enforce signature matching, so the
classes still register as ``Strategy`` instances.
"""

from __future__ import annotations

import pytest

from active_bots.base_strategy import BaseStrategy
from active_bots.enhanced_strategy import EnhancedStrategy
from active_bots.refined_strategy import RefinedStrategy
from active_bots.walked_vwap_strategy import WalkedVWAPStrategy
from polyhustle.strategies.strategy_abc import Strategy


@pytest.mark.parametrize(
    "cls",
    [BaseStrategy, EnhancedStrategy, RefinedStrategy, WalkedVWAPStrategy],
)
def test_production_strategies_inherit_strategy(cls):
    """Each production class is a Strategy and instantiates without error."""
    inst = cls()
    assert isinstance(inst, Strategy)


def test_abstract_methods_enforced():
    """A subclass missing the abstract methods cannot be instantiated."""

    class Incomplete(Strategy):
        pass

    with pytest.raises(TypeError):
        Incomplete()  # type: ignore[abstract]


def test_minimal_strategy_implementation_works():
    """A subclass implementing all three abstract members instantiates."""

    class Minimal(Strategy):
        def __init__(self):
            self._open = False

        def on_tick(self, btc_price, market_price_up, sigma, t_zero,
                    market_price_ts=0.0, *, books=None):
            return None

        def reset(self, t_zero=None, strike=None):
            self._open = False

        @property
        def has_position(self) -> bool:
            return self._open

    s = Minimal()
    assert isinstance(s, Strategy)
    assert s.has_position is False
    assert s.on_tick(1.0, 0.5, 0.3, 0.0) is None
