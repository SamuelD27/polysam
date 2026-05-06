"""polyhustle — modular package for the Polymarket BTC 5m up/down trading bot.

Three layers connected by typed interfaces:
- ``polyhustle.data``      — DataProvider produces a stream of MarketTick.
- ``polyhustle.strategies``— Strategy consumes ticks, returns Decision dicts.
- ``polyhustle.execution`` — Trader consumes decisions, places orders.

The Orchestrator (``polyhustle.orchestrator``) owns the run loop and wires
the three layers together. ``polyhustle.cli`` is the entry point.

Today the strategy / execution shims re-export classes from ``active_bots``
without moving the implementations. The package shape is the contract;
moving the bodies happens once the new path is proven.
"""
