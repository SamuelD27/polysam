"""Data layer: provider ABC, MarketTick, and live/paper/replay implementations.

Populated incrementally during the modular-architecture refactor.
``provider`` (Commit 2) defines the ABC + MarketTick. ``live`` (Commit 3)
wraps the daemon's websocket+state-stitching. ``paper`` reuses ``live``
for data, differing only in trader wiring. ``replay`` is a stub for
Session C.
"""
