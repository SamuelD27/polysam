"""PaperDataProvider — same data path as live; differs only in trader wiring.

Polymarket has no separate "paper" data feed — paper-mode runs are
distinguished by the executor, not the data stream. This module is a
thin alias on ``LiveDataProvider`` so callers can write
``PaperDataProvider()`` for clarity at config-time, while still
getting the real-time websocket feeds.

When the Orchestrator selects paper mode, it pairs this provider with
``PaperTrader`` (no real orders posted). The split between data and
trader keeps the I/O contract single-sourced.
"""

from polyhustle.data.live import LiveDataProvider


class PaperDataProvider(LiveDataProvider):
    """Alias of ``LiveDataProvider`` — paper mode reuses the live data feed."""
