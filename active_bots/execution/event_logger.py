"""Structured event logger — one JSON object per line.

Writes to daemon_state/events.jsonl alongside the human-readable daemon.log.
Every entry, exit, resolve, risk block, and reconcile pass is recorded with
enough fields to reconstruct the full trading session offline.

The format is stable JSONL so `jq`, pandas, or a later analysis script can
consume it without parsing log strings.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("execution.events")


class EventLogger:
    def __init__(self, path: Path):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._fh = self._path.open("a", buffering=1)  # line-buffered
        # mark a new session so we can find the start of this run in the file
        self.log("session_start", pid=os.getpid())

    def log(self, event_type: str, **fields: Any) -> None:
        rec: dict[str, Any] = {"ts": time.time(), "type": event_type}
        for k, v in fields.items():
            rec[k] = _to_jsonable(v)
        line = json.dumps(rec, default=str, separators=(",", ":"))
        with self._lock:
            try:
                self._fh.write(line + "\n")
            except OSError as e:
                logger.warning("event log write failed: %s", e)

    def close(self) -> None:
        with self._lock:
            try:
                self._fh.close()
            except OSError:
                pass


def _to_jsonable(v: Any) -> Any:
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, dict):
        return {k: _to_jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_to_jsonable(x) for x in v]
    return str(v)


_NULL_LOGGER: EventLogger | None = None


class _NullEventLogger:
    def log(self, *a, **k): ...  # noqa: D401, E701
    def close(self): ...


def null_logger() -> Any:
    global _NULL_LOGGER
    if _NULL_LOGGER is None:
        _NULL_LOGGER = _NullEventLogger()  # type: ignore[assignment]
    return _NULL_LOGGER
