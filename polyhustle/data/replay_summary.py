"""In-memory event collector + per-strategy summary aggregator.

Used by Orchestrator(replay_mode=True) and by polyhustle.cli's replay
output. The shape is deliberately simple JSON so flag-sweep harnesses
can consume by ``json.load``.

Schema of replay_summary.json::

    {
      "session_id": "<from manifest>",
      "runtime_seconds": float,
      "tick_count": int,
      "by_strategy": {
        "<strategy_name>": {
          "n_trades": int,
          "pnl_total": float,
          "pnl_mid_total": float | null,
          "win_rate": float,
          "exit_types": {"TP": int, "SL": int, "RESOLUTION": int},
          "fill_realism_tax": float | null,
        },
        ...
      }
    }

The fill_realism_tax is sum(pnl_mid - pnl) over closed trades that have
both populated. Reproduces the R2.2 attribution figure when the
captured walked_vwap entries are replayed.
"""

from __future__ import annotations

import time
from collections import Counter, defaultdict
from collections.abc import Iterable
from typing import Any


class InMemoryEventLogger:
    """An EventLogger-shaped object that buffers events instead of
    writing to disk.

    Compatible with the Orchestrator's ``events.log(name, **kwargs)``
    contract. ``close()`` is a no-op so the legacy try/finally
    cleanup paths work unchanged.
    """

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def log(self, event_type: str, **fields: Any) -> None:
        rec: dict[str, Any] = {"ts": time.time(), "type": event_type}
        rec.update(fields)
        self.events.append(rec)

    def close(self) -> None:
        return None


def aggregate_summary(events: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate per-strategy metrics from a captured event stream.

    Reads ``exit_filled`` / ``shadow_exit`` / ``resolve`` / ``shadow_resolve``
    rows; each row's ``trade`` dict carries ``pnl`` / ``pnl_mid`` /
    ``won`` / ``exit_type``.
    """
    by_strategy: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "n_trades": 0,
        "pnl_total": 0.0,
        "pnl_mid_total": 0.0,
        "n_won": 0,
        "exit_types": Counter(),
        "fill_realism_tax": 0.0,
        "_has_pnl_mid": False,
    })

    for ev in events:
        t = ev.get("type", "")
        if t not in (
            "exit_filled", "shadow_exit", "resolve", "shadow_resolve",
        ):
            continue
        strategy = ev.get("strategy") or "unknown"
        trade = ev.get("trade") or {}
        if not trade:
            continue
        pnl = float(trade.get("pnl") or 0.0)
        pnl_mid = trade.get("pnl_mid")
        won = bool(trade.get("won"))
        exit_type = trade.get("exit_type") or "UNKNOWN"
        bucket = by_strategy[strategy]
        bucket["n_trades"] += 1
        bucket["pnl_total"] += pnl
        bucket["exit_types"][exit_type] += 1
        if won:
            bucket["n_won"] += 1
        if pnl_mid is not None:
            bucket["pnl_mid_total"] += float(pnl_mid)
            bucket["fill_realism_tax"] += float(pnl_mid) - pnl
            bucket["_has_pnl_mid"] = True

    finalised: dict[str, dict[str, Any]] = {}
    for strategy, b in by_strategy.items():
        n = b["n_trades"]
        out: dict[str, Any] = {
            "n_trades": n,
            "pnl_total": round(b["pnl_total"], 4),
            "win_rate": round(b["n_won"] / n, 4) if n > 0 else 0.0,
            "exit_types": dict(b["exit_types"]),
        }
        if b["_has_pnl_mid"]:
            out["pnl_mid_total"] = round(b["pnl_mid_total"], 4)
            out["fill_realism_tax"] = round(b["fill_realism_tax"], 4)
        else:
            out["pnl_mid_total"] = None
            out["fill_realism_tax"] = None
        finalised[strategy] = out

    return {"by_strategy": finalised}
