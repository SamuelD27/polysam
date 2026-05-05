"""Audit on-chain/exchange trade activity against what the daemon believes.

Design note: `post_order` returns the fill synchronously, so the *primary*
entry/exit bookkeeping does not depend on this polling loop. The reconciler's
job is to surface discrepancies (partial fills the daemon missed, unexpected
trades on the funder account, settlement amounts diverging from prediction).

The Polymarket public data API is:
    GET https://data-api.polymarket.com/trades?user=<funder>&limit=50

Each row looks roughly like:
    { "proxyWallet": "0x…", "side": "BUY"|"SELL", "size": "12.34",
      "price": "0.543", "asset": "<token_id>", "timestamp": 1713638500, … }
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

import requests

DATA_TRADES_URL = "https://data-api.polymarket.com/trades"

logger = logging.getLogger("execution.reconciler")


@dataclass
class ReconcilerState:
    last_seen_ts: float = 0.0
    last_poll_ts: float = 0.0
    known_trade_ids: set[str] = field(default_factory=set)


class Reconciler:
    """Polls Polymarket data API to detect on-chain fills for a funder address.

    Used by ``LiveExecutor.reconcile`` to match posted FAK orders against
    actual fills (orders can fill / partially fill / no-match independent of
    the order ack). Throttled by ``poll_interval_s``.
    """

    def __init__(
        self,
        funder_address: str,
        *,
        session: requests.Session | None = None,
        poll_interval_s: float = 30.0,
        timeout: float = 5.0,
    ) -> None:
        self._funder = funder_address.lower()
        self._session = session or requests.Session()
        self._poll_interval_s = poll_interval_s
        self._timeout = timeout
        self.state = ReconcilerState()

    def poll(self, now: float | None = None) -> list[dict[str, Any]]:
        """Return the list of new trades observed since the last poll."""
        now = now or time.time()
        if now - self.state.last_poll_ts < self._poll_interval_s:
            return []
        self.state.last_poll_ts = now

        try:
            resp = self._session.get(
                DATA_TRADES_URL,
                params={"user": self._funder, "limit": 50},
                timeout=self._timeout,
            )
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as e:
            logger.warning("reconciler poll failed: %s", e)
            return []

        if not isinstance(data, list):
            return []

        new: list[dict[str, Any]] = []
        for row in data:
            trade_id = str(
                row.get("transactionHash")
                or row.get("id")
                or f"{row.get('timestamp')}_{row.get('asset')}_{row.get('side')}"
            )
            if trade_id in self.state.known_trade_ids:
                continue
            self.state.known_trade_ids.add(trade_id)
            new.append(row)

            ts = _to_float(row.get("timestamp"))
            if ts > self.state.last_seen_ts:
                self.state.last_seen_ts = ts

        # Cap set size so we don't leak memory over long daemon runtimes.
        if len(self.state.known_trade_ids) > 2000:
            # Keep only the most recent 1000 ids by dropping and rebuilding.
            self.state.known_trade_ids = set(list(self.state.known_trade_ids)[-1000:])

        for row in new:
            logger.info(
                "RECONCILE trade side=%s size=%s price=%s asset=%s… ts=%s",
                row.get("side"),
                row.get("size"),
                row.get("price"),
                str(row.get("asset", ""))[:10],
                row.get("timestamp"),
            )
        return new


def _to_float(v: Any) -> float:
    if v is None:
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


__all__ = ["Reconciler"]
