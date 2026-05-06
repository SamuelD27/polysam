"""ReplayDataProvider — drives the production strategy code path against
a captured session, in-memory and as fast as possible.

Layout consumed:
  <session_dir>/manifest.json        — session window + paths
  <session_dir>/book_feed/<DATE>/<slug>.jsonl.gz
                                     — gzipped JSONL of snapshot+delta records
  manifest["events_jsonl_path"]      — events stream (filtered to window)
  Path(manifest["daemon_log_path"]).parent / "dashboard_history.jsonl"
                                     — BTC tape (epoch ts + btc_price)
  fallback: daemon_state/dashboard_history.jsonl from cwd

Loading strategy (one-time, in __init__ via load()):
  - Decompress every book_feed/*.jsonl.gz, fold yes/no streams into a
    single time-sorted sequence of joint MarketBooks snapshots per slug.
  - Filter events.jsonl rows to [launch_ts_ns, stop_ts_ns] only.
  - Read dashboard_history.jsonl for the BTC tape (ts, btc_price).

Streaming strategy (in stream()):
  - For each second in the session window, build a MarketTick:
      btc_price       — nearest BTC sample <= now
      market_price_up — last RTDS rtds_market_price for the active slug
      sigma           — 0.0 placeholder; perf-pass may rebuild EWMA later
      t_zero, slug    — active 5-minute market
      books           — MarketBooks at-or-before now (bisect)
      market_price_ts — last RTDS event timestamp
  - No asyncio.sleep; the orchestrator drains as fast as the strategies
    + traders can process.

Memory budget: ~600MB decompressed for R2.2 (123MB compressed),
fits well within GX10's 128GB unified memory.
"""

from __future__ import annotations

import asyncio
import bisect
import gzip
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from active_bots.execution.live_book_state import LiveBookState, MarketBooks
from polyhustle.data.provider import DataProvider, MarketTick

logger = logging.getLogger("polyhustle.data.replay")


@dataclass
class _BookRecord:
    """A single point-in-time MarketBooks snapshot for one slug."""

    ts_ns: int
    books: MarketBooks


@dataclass
class _BTCTick:
    ts: float
    btc_price: float


class ReplayDataProvider(DataProvider):
    """Reads a captured session from disk and yields its ticks.

    Args:
        session_path: directory under daemon_state/scrapes/<SESSION>/
            containing manifest.json + book_feed/.
        tick_interval_s: seconds between yielded ticks (default 1.0,
            matches the live cadence).
    """

    def __init__(
        self,
        session_path: str | Path,
        *,
        tick_interval_s: float = 1.0,
    ) -> None:
        self.session_path = Path(session_path)
        self._tick_interval_s = tick_interval_s
        self.manifest: dict[str, Any] = {}
        self.session_id: str = ""
        self.launch_ts_ns: int = 0
        self.stop_ts_ns: int = 0
        self.books_by_slug: dict[str, list[_BookRecord]] = {}
        self.events: list[dict[str, Any]] = []
        self.btc_ticks: list[_BTCTick] = []
        # Derived per-slug active windows: (t_zero_s, t_end_s, slug, strike)
        self.markets: list[tuple[float, float, str, float]] = []
        # Per-slug RTDS price points: slug -> list[(ts, market_price_up)]
        self.rtds_by_slug: dict[str, list[tuple[float, float]]] = {}
        self._loaded = False
        self._stop = asyncio.Event()

    # Loading

    def load(self) -> None:
        """One-time decompress + index. Idempotent."""
        if self._loaded:
            return
        self._read_manifest()
        self._load_book_feed()
        self._load_events()
        self._load_btc_tape()
        self._index_markets_and_rtds()
        self._loaded = True

    def _read_manifest(self) -> None:
        path = self.session_path / "manifest.json"
        self.manifest = json.loads(path.read_text())
        self.session_id = self.manifest.get("session_id", self.session_path.name)
        self.launch_ts_ns = int(self.manifest["launch_ts_ns"])
        self.stop_ts_ns = int(self.manifest["stop_ts_ns"])

    def _load_book_feed(self) -> None:
        """Read every book_feed/*.jsonl.gz and fold into per-slug records."""
        bf = self.session_path / "book_feed"
        if not bf.exists():
            return
        per_slug_yes: dict[str, list[tuple[int, dict]]] = {}
        per_slug_no: dict[str, list[tuple[int, dict]]] = {}
        for date_dir in sorted(bf.iterdir()):
            if not date_dir.is_dir():
                continue
            for f in sorted(date_dir.iterdir()):
                if not f.name.endswith(".jsonl.gz"):
                    continue
                slug = f.name[: -len(".jsonl.gz")]
                with gzip.open(f, "rt") as fh:
                    for line in fh:
                        try:
                            rec = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        ts_ns = int(rec.get("ts_ns") or 0)
                        if not ts_ns:
                            continue
                        side = rec.get("side", "")
                        bucket = per_slug_yes if side == "yes" else per_slug_no
                        bucket.setdefault(slug, []).append((ts_ns, rec))

        all_slugs = set(per_slug_yes) | set(per_slug_no)
        for slug in all_slugs:
            yes_records = sorted(per_slug_yes.get(slug, []), key=lambda x: x[0])
            no_records = sorted(per_slug_no.get(slug, []), key=lambda x: x[0])
            self.books_by_slug[slug] = self._fold_into_market_books(
                yes_records, no_records,
            )

    @staticmethod
    def _fold_into_market_books(
        yes: list[tuple[int, dict]],
        no: list[tuple[int, dict]],
    ) -> list[_BookRecord]:
        """Merge yes + no streams into a single time-sorted sequence of
        joint MarketBooks snapshots.

        At every yes/no event timestamp, output a MarketBooks reflecting
        the latest known YES + NO state. Each side maintains its own
        running LiveBookState; deltas mutate it; snapshots replace it.
        Each output _BookRecord carries IMMUTABLE-by-copy lists (we
        never let downstream mutate the running state).
        """
        yes_state = LiveBookState(tick_size=0.01)
        no_state = LiveBookState(tick_size=0.01)

        # Merge the two streams by ts_ns; on ties, yes first.
        merged: list[tuple[int, str, dict]] = []
        for ts, rec in yes:
            merged.append((ts, "yes", rec))
        for ts, rec in no:
            merged.append((ts, "no", rec))
        merged.sort(key=lambda x: (x[0], 0 if x[1] == "yes" else 1))

        out: list[_BookRecord] = []
        for ts_ns, side, rec in merged:
            target = yes_state if side == "yes" else no_state
            rtype = rec.get("type")
            ts_ms = ts_ns // 1_000_000
            if rtype == "snapshot":
                target.apply_snapshot(
                    bids=rec.get("bids") or [],
                    asks=rec.get("asks") or [],
                    ts_ms=ts_ms,
                    tick_size=float(
                        rec.get("effective_tick")
                        or rec.get("canonical_tick")
                        or 0.01
                    ),
                )
            elif rtype == "book":
                raw = rec.get("raw") or {}
                target.apply_snapshot(
                    bids=raw.get("bids") or [],
                    asks=raw.get("asks") or [],
                    ts_ms=ts_ms,
                    tick_size=float(raw.get("tick_size") or 0.01),
                )
            elif rtype == "price_change":
                target.apply_delta(rec, ts_ms)
            else:
                continue

            yes_snap = LiveBookState(
                tick_size=yes_state.tick_size,
                bids=list(yes_state.bids),
                asks=list(yes_state.asks),
                ts_ms=yes_state.ts_ms,
                has_baseline=yes_state.has_baseline,
            )
            no_snap = LiveBookState(
                tick_size=no_state.tick_size,
                bids=list(no_state.bids),
                asks=list(no_state.asks),
                ts_ms=no_state.ts_ms,
                has_baseline=no_state.has_baseline,
            )
            out.append(_BookRecord(
                ts_ns=ts_ns,
                books=MarketBooks(yes=yes_snap, no=no_snap),
            ))
        return out

    def _load_events(self) -> None:
        path_s = self.manifest.get("events_jsonl_path")
        if not path_s:
            return
        events_path = Path(path_s)
        if not events_path.exists():
            return
        t_lo = self.launch_ts_ns / 1e9
        t_hi = self.stop_ts_ns / 1e9
        with events_path.open() as f:
            for line in f:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = row.get("ts")
                if ts is None or not (t_lo <= ts <= t_hi):
                    continue
                self.events.append(row)

    def _load_btc_tape(self) -> None:
        """Read dashboard_history.jsonl from the manifest's daemon_log
        sibling, falling back to daemon_state/dashboard_history.jsonl
        relative to cwd.
        """
        candidates = []
        log_path = self.manifest.get("daemon_log_path")
        if log_path:
            candidates.append(Path(log_path).parent / "dashboard_history.jsonl")
        candidates.append(Path("daemon_state") / "dashboard_history.jsonl")
        t_lo = self.launch_ts_ns / 1e9
        t_hi = self.stop_ts_ns / 1e9
        for path in candidates:
            if path.is_file():
                with path.open() as f:
                    for line in f:
                        try:
                            row = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        ts = row.get("ts")
                        btc = row.get("btc_price")
                        if ts is None or btc is None:
                            continue
                        if not (t_lo <= ts <= t_hi):
                            continue
                        self.btc_ticks.append(_BTCTick(ts=ts, btc_price=btc))
                if self.btc_ticks:
                    break
        self.btc_ticks.sort(key=lambda x: x.ts)

    def _index_markets_and_rtds(self) -> None:
        """Build the (t_zero, t_end, slug, strike) sequence + per-slug RTDS."""
        for ev in self.events:
            t = ev.get("type")
            if t == "market_rollover":
                t_zero = float(ev.get("t_zero") or ev.get("ts") or 0.0)
                slug = str(ev.get("slug") or "")
                strike = float(ev.get("strike") or 0.0)
                if slug and t_zero > 0:
                    self.markets.append((t_zero, t_zero + 300.0, slug, strike))
            elif t in ("rtds_market_price", "market_price_update"):
                slug = str(ev.get("slug") or "")
                m = ev.get("market_price_up")
                if not slug or m is None:
                    continue
                self.rtds_by_slug.setdefault(slug, []).append(
                    (float(ev.get("ts") or 0.0), float(m))
                )
        self.markets.sort(key=lambda x: x[0])
        for slug in self.rtds_by_slug:
            self.rtds_by_slug[slug].sort(key=lambda x: x[0])

    # Streaming

    async def stream(self) -> AsyncIterator[MarketTick]:
        """Yield MarketTicks chronologically, no real-time delay."""
        if not self._loaded:
            self.load()
        if not self.markets:
            return

        t_lo = self.launch_ts_ns / 1e9
        t_hi = self.stop_ts_ns / 1e9
        t = t_lo
        sigma = 0.0
        while t <= t_hi:
            if self._stop.is_set():
                return
            tick = self._build_tick(t, sigma)
            if tick is not None:
                yield tick
            t += self._tick_interval_s

    def _build_tick(self, now: float, sigma: float) -> MarketTick | None:
        slug, t_zero, strike = self._active_market(now)
        if slug is None:
            return None
        btc = self._btc_at(now)
        if btc is None or btc <= 0:
            return None
        m_up, m_ts = self._market_price_at(slug, now)
        books = self._books_at(slug, int(now * 1e9))
        return MarketTick(
            timestamp=now,
            btc_price=btc,
            market_price_up=m_up,
            sigma=sigma,
            t_zero=t_zero,
            strike=strike,
            slug=slug,
            books=books,
            market_price_ts=m_ts if m_ts is not None else now,
        )

    def _active_market(self, now: float) -> tuple[str | None, float, float]:
        for t_zero, t_end, slug, strike in self.markets:
            if t_zero <= now < t_end:
                return slug, t_zero, strike
        return None, 0.0, 0.0

    def _btc_at(self, now: float) -> float | None:
        if not self.btc_ticks:
            return None
        ts_list = [b.ts for b in self.btc_ticks]
        i = bisect.bisect_right(ts_list, now) - 1
        if i < 0:
            return None
        return self.btc_ticks[i].btc_price

    def _market_price_at(
        self, slug: str, now: float,
    ) -> tuple[float | None, float | None]:
        records = self.rtds_by_slug.get(slug, [])
        if not records:
            return None, None
        ts_list = [r[0] for r in records]
        i = bisect.bisect_right(ts_list, now) - 1
        if i < 0:
            return None, None
        return records[i][1], records[i][0]

    def _books_at(self, slug: str, ts_ns: int) -> MarketBooks | None:
        records = self.books_by_slug.get(slug)
        if not records:
            return None
        ts_list = [r.ts_ns for r in records]
        i = bisect.bisect_right(ts_list, ts_ns) - 1
        if i < 0:
            return None
        return records[i].books

    async def shutdown(self) -> None:
        self._stop.set()
