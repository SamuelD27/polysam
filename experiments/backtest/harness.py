"""Book-walked replay harness.

Pairs `entry_filled` / `exit_filled` / `resolve` events from events.jsonl with
captured L2 orderbook snapshots, runs each entry through ``ReplayExecutor``,
writes a §7 parquet, and prints a one-screen summary.

CLI:

    python -m experiments.backtest.harness \\
        --events /home/samsam/polymarket-hustle/daemon_state/events.jsonl \\
        --scrapes /home/samsam/polymarket-hustle/data/btc5m.db \\
        --out experiments/backtest/runs/dryrun_01.parquet \\
        --latency-profile sg_wg_prior \\
        --window '2026-04-22T01:10Z..2026-04-22T03:10Z' \\
        --tick-size 0.01 \\
        --fee-category crypto \\
        --mode freeze_depleted

`--tick-size` is required (no implicit default per spec §2.2). The harness
warns if any observed best_bid < 0.02 or best_ask > 0.98 with --tick-size 0.01
(would imply a 0.001/0.0001 tick regime the walk cannot represent honestly).
"""

from __future__ import annotations

import argparse
import dataclasses
import gzip
import json
import math
import random
import sqlite3
import subprocess
import sys
import uuid
import warnings
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Optional

from active_bots.execution.book import Book, Level
from active_bots.execution.fees import CATEGORIES, FeeCategory
from active_bots.execution.latency import (
    DUBLIN_PRIOR,
    SG_WG_PRIOR,
    LatencyProfile,
)
from active_bots.execution.replay_executor import (
    DictBookStore,
    ExecutionRecord,
    OrderRequest,
    ReplayExecutor,
)

FEE_SCHEDULE_VERSION = "polymarket_intl_2026-04_bellcurve_v1"

LATENCY_PROFILES: dict[str, LatencyProfile] = {
    "sg_wg_prior": SG_WG_PRIOR,
    "dublin_prior": DUBLIN_PRIOR,
}


def parse_window(s: str) -> tuple[int, int]:
    """Parse '<ISO>..<ISO>' into (t0_ns, t1_ns). Accepts 'Z' or '+00:00'."""
    a, b = s.split("..", 1)
    t0 = _iso_to_ns(a)
    t1 = _iso_to_ns(b)
    if t0 >= t1:
        raise ValueError(f"empty window: {a} >= {b}")
    return t0, t1


def _iso_to_ns(s: str) -> int:
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1e9)


def _iso_to_ns_from_db(s: str) -> int:
    """Parse a snapshot_time from sqlite like '2026-04-09T14:07:05.455420+00:00'."""
    return int(datetime.fromisoformat(s).timestamp() * 1e9)


def load_markets(scrapes_db: Path) -> dict[str, dict[str, str]]:
    """Return {slug: {yes_token_id, no_token_id, condition_id, end_date}}."""
    conn = sqlite3.connect(f"file:{scrapes_db}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT slug, yes_token_id, no_token_id, condition_id, end_date "
            "FROM markets"
        ).fetchall()
    finally:
        conn.close()
    out: dict[str, dict[str, str]] = {}
    for slug, yes_tok, no_tok, cid, end_date in rows:
        if not slug:
            continue
        out[slug] = {
            "yes_token_id": yes_tok or "",
            "no_token_id": no_tok or "",
            "condition_id": cid or "",
            "end_date": end_date or "",
        }
    return out


def load_snapshots_for_window(
    scrapes_db: Path,
    condition_ids: set[str],
    t0_ns: int,
    t1_ns: int,
    tick_size: Decimal,
    slug_by_condition: dict[str, str],
    token_by_condition_side: dict[tuple[str, str], str],
    pad_s: int = 3600,
) -> DictBookStore:
    """Load orderbooks for the given condition_ids in [t0_ns - pad, t1_ns + pad].

    Each (condition_id, side) row becomes a Book keyed on the corresponding
    token_id via ``token_by_condition_side``. Rows without a token mapping are
    skipped with a warning (counted once per condition).
    """
    if not condition_ids:
        return DictBookStore()
    lo = _ns_to_iso(t0_ns - pad_s * 1_000_000_000)
    hi = _ns_to_iso(t1_ns + pad_s * 1_000_000_000)
    conn = sqlite3.connect(f"file:{scrapes_db}?mode=ro", uri=True)
    try:
        placeholders = ",".join("?" for _ in condition_ids)
        q = (
            f"SELECT condition_id, side, bids, asks, snapshot_time "
            f"FROM orderbooks WHERE condition_id IN ({placeholders}) "
            f"AND snapshot_time BETWEEN ? AND ?"
        )
        rows = conn.execute(q, (*condition_ids, lo, hi)).fetchall()
    finally:
        conn.close()
    store = DictBookStore()
    extreme_prices_seen = False
    skipped_no_token: set[str] = set()
    for cid, side_label, bids_json, asks_json, snap_time in rows:
        token_id = token_by_condition_side.get((cid, side_label))
        if not token_id:
            skipped_no_token.add(f"{cid}:{side_label}")
            continue
        try:
            bids_raw = json.loads(bids_json) if bids_json else []
            asks_raw = json.loads(asks_json) if asks_json else []
        except json.JSONDecodeError:
            continue
        # sqlite stores bids [{price, size}] in ascending price order
        # (cheapest buyer first). We want descending (highest-price buyer first).
        bids = tuple(
            Level(Decimal(str(lvl["price"])), Decimal(str(lvl["size"])))
            for lvl in sorted(bids_raw, key=lambda x: -float(x["price"]))
        )
        asks = tuple(
            Level(Decimal(str(lvl["price"])), Decimal(str(lvl["size"])))
            for lvl in sorted(asks_raw, key=lambda x: float(x["price"]))
        )
        if bids and (bids[0].price < Decimal("0.02") or bids[0].price > Decimal("0.98")):
            extreme_prices_seen = True
        if asks and (asks[0].price < Decimal("0.02") or asks[0].price > Decimal("0.98")):
            extreme_prices_seen = True
        # Filter levels that violate the CLI tick regime (can happen near extremes
        # where the real book runs 0.001/0.0001 tick). Skip the whole snapshot
        # rather than silently lying.
        if any(
            (lvl.price < tick_size or lvl.price > (Decimal(1) - tick_size))
            for lvl in (*bids, *asks)
        ):
            continue
        ts_ns = _iso_to_ns_from_db(snap_time)
        store.add(
            Book(
                token_id=token_id,
                side_bids=bids,
                side_asks=asks,
                tick_size=tick_size,
                ts_ns=ts_ns,
            )
        )
    if skipped_no_token:
        warnings.warn(
            f"skipped {len(skipped_no_token)} (condition_id, side) pairs "
            f"with no token mapping",
            stacklevel=2,
        )
    if extreme_prices_seen and tick_size == Decimal("0.01"):
        warnings.warn(
            "observed best_bid<0.02 or best_ask>0.98 with --tick-size 0.01; "
            "real market may have been in 0.001/0.0001 tick regime",
            stacklevel=2,
        )
    store.freeze()
    return store


def _ns_to_iso(ns: int) -> str:
    return datetime.fromtimestamp(ns / 1e9, tz=timezone.utc).isoformat()


def load_events(events_path: Path, t0_ns: int, t1_ns: int, asset_prefix: str = "btc") -> dict[str, list[dict]]:
    """Return {'entries': [...], 'exits': [...]} within [t0_ns, t1_ns]."""
    entries: list[dict] = []
    exits: list[dict] = []
    with events_path.open("r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = row.get("ts")
            if ts is None:
                continue
            ts_ns = int(float(ts) * 1e9)
            if ts_ns < t0_ns or ts_ns > t1_ns:
                continue
            t = row.get("type")
            if t == "entry_filled":
                pos = row.get("position") or {}
                if not str(pos.get("slug", "")).lower().startswith(asset_prefix):
                    continue
                entries.append(row)
            elif t in ("exit_filled", "resolve"):
                trd = row.get("trade") or {}
                if not str(trd.get("slug", "")).lower().startswith(asset_prefix):
                    continue
                exits.append(row)
    return {"entries": entries, "exits": exits}


def pair_exit(exits: list[dict], slug: str, side: str, after_ts_ns: int) -> Optional[dict]:
    """Find the first exit for (slug, side) with ts_ns > after_ts_ns."""
    best: Optional[dict] = None
    best_ts: Optional[int] = None
    for ev in exits:
        trd = ev.get("trade") or {}
        if trd.get("slug") != slug or trd.get("side") != side:
            continue
        ts_ns = int(float(ev.get("ts", 0)) * 1e9)
        if ts_ns <= after_ts_ns:
            continue
        if best_ts is None or ts_ns < best_ts:
            best = ev
            best_ts = ts_ns
    return best


def _side_up_down_to_book_side(side: str) -> str:
    """Events use 'Up'/'Down' (token-label); the walk takes 'BUY'/'SELL'."""
    # Strategy always buys the token it thinks is mispriced (taker BUY of yes or no).
    return "BUY"


def _side_to_market_side(side: str) -> str:
    """'Up' -> 'yes' on the sqlite orderbooks.side label. 'Down' -> 'no'."""
    s = side.strip().lower()
    if s in ("up", "yes"):
        return "yes"
    if s in ("down", "no"):
        return "no"
    raise ValueError(f"unknown side {side!r}")


# ---------- book_feed (WS-scraped jsonl.gz) loader ----------


def _decimal(x: Any) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x))


@dataclasses.dataclass
class _RollingBookState:
    """Per-token mutable book state for delta application.

    Holds bids/asks as ``dict[Decimal price -> Decimal size]`` for O(1)
    delta application. ``to_book()`` serialises into an immutable
    ``Book`` with sorted Level tuples (bids descending, asks ascending)
    for emission as a per-event anchor.

    Reset semantics: ``apply_snapshot`` replaces both sides wholesale.
    ``apply_delta`` mutates a single level in place; ``size == 0``
    removes the level. ``apply_delta`` is permissive — calling it
    before any snapshot inserts a level into an otherwise-empty state.
    The caller (load_snapshots_from_feed_dir) enforces "skip price_change
    until baseline lands" via a state.get(aid) guard, not apply_delta
    itself. ``to_book`` always emits a Book — empty bids/asks indicate
    "book is known to be empty at this ts" rather than "no anchor".
    """

    tick_size: Decimal
    bids: dict = dataclasses.field(default_factory=dict)
    asks: dict = dataclasses.field(default_factory=dict)

    def apply_snapshot(
        self,
        bids_raw: list,
        asks_raw: list,
        tick_size: Optional[Decimal] = None,
    ) -> None:
        if tick_size is not None:
            self.tick_size = tick_size
        self.bids = {
            _decimal(lvl["price"]): _decimal(lvl["size"])
            for lvl in (bids_raw or [])
            if _decimal(lvl["size"]) > 0
        }
        self.asks = {
            _decimal(lvl["price"]): _decimal(lvl["size"])
            for lvl in (asks_raw or [])
            if _decimal(lvl["size"]) > 0
        }

    def apply_delta(self, delta: dict) -> None:
        side = (delta.get("side") or "").upper()
        if side == "BUY":
            target = self.bids
        elif side == "SELL":
            target = self.asks
        else:
            return
        try:
            price = _decimal(delta["price"])
            size = _decimal(delta["size"])
        except (KeyError, TypeError, ValueError):
            return
        if size <= 0:
            target.pop(price, None)
        else:
            target[price] = size

    def to_book(self, token_id: str, ts_ns: int) -> "Optional[Book]":
        bids_sorted = tuple(
            Level(p, s) for p, s in sorted(self.bids.items(), key=lambda x: -x[0])
        )
        asks_sorted = tuple(
            Level(p, s) for p, s in sorted(self.asks.items(), key=lambda x: x[0])
        )
        return Book(
            token_id=token_id,
            side_bids=bids_sorted,
            side_asks=asks_sorted,
            tick_size=self.tick_size,
            ts_ns=ts_ns,
        )


def load_snapshots_from_feed_dir(
    feed_dir: Path,
    wanted_tokens: set[str],
    t0_ns: int,
    t1_ns: int,
    tick_size_default: Decimal,
    *,
    pad_s: int = 600,
) -> tuple[DictBookStore, dict[str, Decimal]]:
    """Walk daemon_state/book_feed/{date}/{slug}.jsonl.gz and materialize
    Book anchors for the given tokens in [t0_ns - pad, t1_ns + pad].

    Anchor sources: every ``snapshot``, ``book``, and ``price_change`` record
    found in the feed, plus the running ``canonical_tick`` from the most recent
    record for that token (updated on ``tick_size_change`` events).

    A per-token ``_RollingBookState`` is maintained in memory. ``snapshot`` and
    ``book`` events replace both sides wholesale; ``price_change`` deltas mutate
    individual price levels in place (size=0 removes a level). Each of these
    three event types emits one ``Book`` anchor into the store, lifting anchor
    density from ~0.2 Hz (snapshot-only) to ~50+ Hz on active markets.

    Hash re-verification is skipped — the scraper already verified
    ``remote_hashes`` against ``local_hash_after`` at capture time. If state
    drifts, the next ``snapshot`` re-anchors cleanly.

    Returns (store, canonical_tick_by_token). Tick values come from the feed's
    own ``canonical_tick`` / ``effective_tick`` fields; ``tick_size_default`` is
    the fallback only when the feed does not carry a tick for the token.
    """
    lo_ns = t0_ns - pad_s * 1_000_000_000
    hi_ns = t1_ns + pad_s * 1_000_000_000
    lo_date = datetime.fromtimestamp(lo_ns / 1e9, tz=timezone.utc).date()
    hi_date = datetime.fromtimestamp(hi_ns / 1e9, tz=timezone.utc).date()

    store = DictBookStore()
    canonical_tick_by_token: dict[str, Decimal] = {}
    # Per-token rolling book state for delta application. Resets on
    # snapshot/book; mutated in place by price_change deltas; skipped
    # silently if a price_change arrives before the first snapshot
    # (next snapshot re-anchors).
    states: dict[str, _RollingBookState] = {}
    extreme_seen = False

    if not feed_dir.exists():
        return store, canonical_tick_by_token

    for date_dir in sorted(feed_dir.iterdir()):
        if not date_dir.is_dir():
            continue
        try:
            d = datetime.strptime(date_dir.name, "%Y-%m-%d").date()
        except ValueError:
            continue
        if d < lo_date or d > hi_date:
            continue
        for path in sorted(date_dir.glob("*.jsonl.gz")):
            try:
                with gzip.open(path, "rt") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except ValueError:
                            continue
                        aid = rec.get("asset_id")
                        if aid not in wanted_tokens:
                            continue
                        ts = int(rec.get("ts_ns", 0))
                        if ts < lo_ns or ts > hi_ns:
                            continue
                        t = rec.get("type")
                        # Track running canonical tick.
                        tick_str = rec.get("canonical_tick") or rec.get("effective_tick")
                        if tick_str:
                            try:
                                canonical_tick_by_token[aid] = _decimal(tick_str)
                            except (TypeError, ValueError):
                                pass
                        if t == "tick_size_change":
                            new_tick = rec.get("new_tick_size")
                            if new_tick:
                                try:
                                    nt = _decimal(new_tick)
                                except (TypeError, ValueError):
                                    nt = None
                                if nt is not None:
                                    canonical_tick_by_token[aid] = nt
                                    if aid in states:
                                        states[aid].tick_size = nt
                            continue

                        tick = canonical_tick_by_token.get(aid, tick_size_default)

                        if t == "snapshot":
                            state = states.setdefault(aid, _RollingBookState(tick))
                            state.apply_snapshot(
                                rec.get("bids", []),
                                rec.get("asks", []),
                                tick_size=tick,
                            )
                            book = state.to_book(aid, ts)
                        elif t == "book":
                            raw = rec.get("raw", {}) or {}
                            state = states.setdefault(aid, _RollingBookState(tick))
                            state.apply_snapshot(
                                raw.get("bids", []),
                                raw.get("asks", []),
                                tick_size=tick,
                            )
                            book = state.to_book(aid, ts)
                        elif t == "price_change":
                            state = states.get(aid)
                            if state is None:
                                # No baseline yet; next snapshot will re-anchor.
                                continue
                            for delta in rec.get("deltas", []) or []:
                                state.apply_delta(delta)
                            book = state.to_book(aid, ts)
                        else:
                            continue

                        if book is None:
                            continue
                        if book.side_bids and book.side_bids[0].price < Decimal("0.02"):
                            extreme_seen = True
                        if book.side_asks and book.side_asks[0].price > Decimal("0.98"):
                            extreme_seen = True
                        store.add(book)
            except OSError:
                continue

    if extreme_seen and tick_size_default == Decimal("0.01"):
        warnings.warn(
            "observed best_bid<0.02 or best_ask>0.98 in book_feed with "
            "--tick-size 0.01; real market may have been in 0.001/0.0001 "
            "tick regime",
            stacklevel=2,
        )
    store.freeze()
    return store, canonical_tick_by_token


# ---------- run-manifest ----------


def _git_sha() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            stderr=subprocess.DEVNULL,
            cwd=Path(__file__).resolve().parents[2],
        )
        return out.decode().strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


def _manifest_path_for(out: Path) -> Path:
    return out.with_suffix(out.suffix + ".manifest.json")


def _write_manifest(
    manifest_path: Path,
    *,
    run_id: str,
    started_ts: str,
    input_format: str,
    events_path: Path,
    scrapes_path: Path,
    window: str,
    tick_size: Decimal,
    latency_profile_name: str,
    latency_profile: LatencyProfile,
    fee_category: str,
    mode: str,
    staleness_hard_ms: int,
    staleness_soft_ms: int,
    staleness_policy: str,
    seed: int,
    rows_emitted: int,
    out_parquet: Path,
    skipped_no_market: int,
    skipped_no_token: int,
    classification_counts: dict[str, int],
) -> None:
    manifest = {
        "run_id": run_id,
        "started_at_utc": started_ts,
        "git_sha": _git_sha(),
        "input_format": input_format,
        "events_path": str(events_path),
        "scrapes_path": str(scrapes_path),
        "window": window,
        "tick_size": str(tick_size),
        "latency": {
            "name": latency_profile_name,
            "p50_ms": latency_profile.p50_ms,
            "p95_ms": latency_profile.p95_ms,
            "p99_ms": latency_profile.p99_ms,
            "p999_ms": latency_profile.p999_ms,
            "source": latency_profile.source,
        },
        "fee_schedule_version": FEE_SCHEDULE_VERSION,
        "fee_category": fee_category,
        "mode": mode,
        "staleness_hard_ms": staleness_hard_ms,
        "staleness_soft_ms": staleness_soft_ms,
        "staleness_policy": staleness_policy,
        "seed": seed,
        "rows_emitted": rows_emitted,
        "out_parquet": str(out_parquet),
        "skipped_no_market": skipped_no_market,
        "skipped_no_token": skipped_no_token,
        "classification_counts": classification_counts,
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")


def run(
    *,
    events: Path,
    scrapes: Path,
    out: Path,
    window: str,
    tick_size: Decimal,
    latency_profile: str = "sg_wg_prior",
    fee_category: str = "crypto",
    mode: str = "freeze_depleted",
    seed: int = 0,
    asset_prefix: str = "btc",
    staleness_hard_ms: int = 500,
    staleness_soft_ms: int = 200,
    input_format: str = "book_feed",
    feed_dir: Optional[Path] = None,
    staleness_policy: str = "strict",
) -> Path:
    t0_ns, t1_ns = parse_window(window)
    if latency_profile not in LATENCY_PROFILES:
        raise ValueError(f"unknown --latency-profile {latency_profile!r}")
    if fee_category not in CATEGORIES:
        raise ValueError(f"unknown --fee-category {fee_category!r}")
    profile = LATENCY_PROFILES[latency_profile]
    category: FeeCategory = CATEGORIES[fee_category]

    if input_format not in ("book_feed", "sqlite"):
        raise ValueError(f"unknown --input-format {input_format!r}")
    if input_format == "book_feed" and feed_dir is None:
        raise ValueError(
            "input_format=book_feed requires --feed-dir pointing at the "
            "scrape_book.py output (daemon_state/book_feed)"
        )

    markets = load_markets(scrapes)
    ev = load_events(events, t0_ns, t1_ns, asset_prefix=asset_prefix)
    entries = ev["entries"]
    exits = ev["exits"]

    # Build condition_id set and token-lookup tables.
    condition_ids: set[str] = set()
    token_by_condition_side: dict[tuple[str, str], str] = {}
    slug_by_condition: dict[str, str] = {}
    for slug, meta in markets.items():
        cid = meta["condition_id"]
        if not cid:
            continue
        if meta["yes_token_id"]:
            token_by_condition_side[(cid, "yes")] = meta["yes_token_id"]
        if meta["no_token_id"]:
            token_by_condition_side[(cid, "no")] = meta["no_token_id"]
        slug_by_condition[cid] = slug
    for e in entries:
        slug = (e.get("position") or {}).get("slug")
        if slug and slug in markets:
            cid = markets[slug]["condition_id"]
            if cid:
                condition_ids.add(cid)

    # Resolve the set of tokens the entries actually refer to so the book_feed
    # loader can ignore noise from unrelated markets.
    wanted_tokens: set[str] = set()
    for e in entries:
        pos = e.get("position") or {}
        slug = pos.get("slug")
        if slug not in markets:
            continue
        cid = markets[slug]["condition_id"]
        ms = _side_to_market_side(pos.get("side", ""))
        tok = token_by_condition_side.get((cid, ms))
        if tok:
            wanted_tokens.add(tok)

    if input_format == "sqlite":
        store = load_snapshots_for_window(
            scrapes_db=scrapes,
            condition_ids=condition_ids,
            t0_ns=t0_ns,
            t1_ns=t1_ns,
            tick_size=tick_size,
            slug_by_condition=slug_by_condition,
            token_by_condition_side=token_by_condition_side,
        )
    else:  # book_feed
        store, _ = load_snapshots_from_feed_dir(
            feed_dir=feed_dir,
            wanted_tokens=wanted_tokens,
            t0_ns=t0_ns,
            t1_ns=t1_ns,
            tick_size_default=tick_size,
        )

    run_id = uuid.uuid4().hex[:12]
    rng = random.Random(seed)
    executor = ReplayExecutor(
        books=store,
        latency_profile=profile,
        mode=mode,  # type: ignore[arg-type]
        staleness_hard_ms=staleness_hard_ms,
        staleness_soft_ms=staleness_soft_ms,
        rng=rng,
    )

    records: list[ExecutionRecord] = []
    skipped_no_market = 0
    skipped_no_token = 0
    for e in entries:
        pos = e.get("position") or {}
        slug = pos.get("slug")
        side_label = pos.get("side", "")
        if slug not in markets:
            skipped_no_market += 1
            continue
        market_meta = markets[slug]
        cid = market_meta["condition_id"]
        market_side = _side_to_market_side(side_label)
        token_id = token_by_condition_side.get((cid, market_side))
        if not token_id:
            skipped_no_token += 1
            continue

        t_zero = pos.get("t_zero")
        t_zero_ns = int(float(t_zero) * 1e9) if t_zero is not None else None
        t_end_ns = (
            (t_zero_ns + 300 * 1_000_000_000) if t_zero_ns is not None else None
        )
        decision_mid_raw = pos.get("market_at_entry")
        if decision_mid_raw is None:
            decision_mid_raw = pos.get("fair_at_entry")
        if decision_mid_raw is None or decision_mid_raw <= 0:
            continue
        decision_mid = Decimal(str(decision_mid_raw))
        shares = Decimal(str(pos.get("size_shares", 0)))
        if shares <= 0:
            continue

        order = OrderRequest(
            token_id=token_id,
            side=_side_up_down_to_book_side(side_label),  # always BUY (taker)
            requested_shares=shares,
            worst_price_limit=Decimal("0.99"),
            decision_mid=decision_mid,
            category=category,
            tick_size=tick_size,
            market_id=cid,
            strategy_id=str(e.get("strategy") or ""),
            trade_id=f"{slug}:{side_label}:{pos.get('entry_time')}",
            parent_order_id="",
            backtest_run_id=run_id,
            t_zero_ns=t_zero_ns,
            t_end_ns=t_end_ns,
            edge_at_signal=float(pos.get("edge") or 0.0),
        )
        decision_ts_ns = int(float(pos.get("entry_time", e["ts"])) * 1e9)
        rec = executor.post_fak(order, decision_ts_ns=decision_ts_ns)
        rec = _fill_exit_columns(rec, pos, side_label, decision_ts_ns, exits)
        # Stamp staleness_policy on every row so a downstream reader cannot
        # silently treat diagnostic-mode data as strict-mode data.
        rec = dataclasses.replace(rec, vol_regime=rec.vol_regime)  # no-op to ensure identity
        records.append(rec)

    _write_parquet(records, out, staleness_policy=staleness_policy)
    classification_counts = _classification_counts(records)
    _write_manifest(
        _manifest_path_for(out),
        run_id=run_id,
        started_ts=datetime.now(timezone.utc).isoformat(),
        input_format=input_format,
        events_path=events,
        scrapes_path=scrapes,
        window=window,
        tick_size=tick_size,
        latency_profile_name=latency_profile,
        latency_profile=profile,
        fee_category=fee_category,
        mode=mode,
        staleness_hard_ms=staleness_hard_ms,
        staleness_soft_ms=staleness_soft_ms,
        staleness_policy=staleness_policy,
        seed=seed,
        rows_emitted=len(records),
        out_parquet=out,
        skipped_no_market=skipped_no_market,
        skipped_no_token=skipped_no_token,
        classification_counts=classification_counts,
    )
    _print_summary(
        records,
        skipped_no_market=skipped_no_market,
        skipped_no_token=skipped_no_token,
        staleness_policy=staleness_policy,
    )
    return out


def _classification_counts(records: list[ExecutionRecord]) -> dict[str, int]:
    c: dict[str, int] = {}
    for r in records:
        c[r.classification] = c.get(r.classification, 0) + 1
    return c


def _fill_exit_columns(
    rec: ExecutionRecord,
    pos: dict,
    side_label: str,
    decision_ts_ns: int,
    exits: list[dict],
) -> ExecutionRecord:
    slug = pos.get("slug")
    exit_ev = pair_exit(exits, slug=slug, side=side_label, after_ts_ns=decision_ts_ns)
    if exit_ev is None:
        return rec
    trd = exit_ev.get("trade") or {}
    realised_pnl = trd.get("pnl")
    exit_price = trd.get("exit_price")
    shares = pos.get("size_shares")
    paper_pnl = None
    if exit_price is not None and shares is not None:
        paper_pnl = (float(exit_price) - 0.5) * float(shares)
    diff = None
    if realised_pnl is not None and paper_pnl is not None:
        diff = float(paper_pnl) - float(realised_pnl)
    edge_fill = None
    fair_at_entry = pos.get("fair_at_entry")
    if fair_at_entry is not None and rec.fill_vwap == rec.fill_vwap:  # not NaN
        edge_fill = float(fair_at_entry) - rec.fill_vwap
    return dataclasses.replace(
        rec,
        realised_pnl_at_close=float(realised_pnl) if realised_pnl is not None else float("nan"),
        paper_pnl_flat_0_5=float(paper_pnl) if paper_pnl is not None else float("nan"),
        diff_paper_minus_realised=float(diff) if diff is not None else float("nan"),
        edge_at_fill=float(edge_fill) if edge_fill is not None else float("nan"),
    )


def _write_parquet(
    records: list[ExecutionRecord],
    out: Path,
    *,
    staleness_policy: str,
) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    if not records:
        # Write an empty parquet with the expected schema so downstream tools
        # don't trip over a missing file.
        _write_empty(out, staleness_policy=staleness_policy)
        return
    import pyarrow as pa
    import pyarrow.parquet as pq

    rows = [asdict(r) for r in records]
    fields = list(records[0].__dataclass_fields__.keys())
    table_data = {k: [r[k] for r in rows] for k in fields}
    # staleness_policy column stamped on every row — report.py refuses the
    # paper-vs-realistic headline unless every input row has policy=strict.
    table_data["staleness_policy"] = [staleness_policy] * len(rows)
    table = pa.table(table_data)
    pq.write_table(table, out)


def _write_empty(out: Path, *, staleness_policy: str) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    from active_bots.execution.replay_executor import ExecutionRecord as _Rec

    fields = list(_Rec.__dataclass_fields__.keys())
    data: dict[str, list[Any]] = {k: [] for k in fields}
    data["staleness_policy"] = []
    _ = staleness_policy  # surfaces in manifest; empty parquet has no rows to stamp
    table = pa.table(data)
    pq.write_table(table, out)


def _pct(num: int, denom: int) -> str:
    if denom == 0:
        return "n/a"
    return f"{100.0 * num / denom:.1f}%"


def _print_summary(
    records: list[ExecutionRecord],
    *,
    skipped_no_market: int,
    skipped_no_token: int,
    staleness_policy: str = "strict",
) -> None:
    n = len(records)
    print(f"\n=== Replay summary ({n} rows, staleness_policy={staleness_policy}) ===")
    if n == 0:
        print("(no usable entries; skipped_no_market="
              f"{skipped_no_market}, skipped_no_token={skipped_no_token})")
        return
    stale = sum(1 for r in records if r.classification == "book_stale")
    full = sum(1 for r in records if r.classification == "full")
    partial = sum(1 for r in records if r.classification == "partial")
    unfilled = sum(1 for r in records if r.classification == "unfilled")
    stalenesses = sorted(r.book_staleness_ms for r in records if r.book_staleness_ms >= 0)
    median_stale = stalenesses[len(stalenesses) // 2] if stalenesses else -1
    print(
        f"full={full} partial={partial} unfilled={unfilled} book_stale={stale} "
        f"({_pct(stale, n)})"
    )
    print(f"skipped_no_market={skipped_no_market} skipped_no_token={skipped_no_token}")
    print(f"median book_staleness_ms={median_stale}")
    good = [r for r in records if r.classification in ("full", "partial")]
    if good:
        med = _median([r.total_IS for r in good if math.isfinite(r.total_IS)])
        med_hs = _median([r.half_spread_cost for r in good if math.isfinite(r.half_spread_cost)])
        med_lw = _median([r.latency_drift_cost for r in good if math.isfinite(r.latency_drift_cost)])
        med_bw = _median([r.book_walk_cost for r in good if math.isfinite(r.book_walk_cost)])
        med_fee = _median([r.fees_cost for r in good if math.isfinite(r.fees_cost)])
        print(
            f"attribution (median bps): half_spread={_fmt(med_hs)} "
            f"latency_drift={_fmt(med_lw)} book_walk={_fmt(med_bw)} "
            f"fees={_fmt(med_fee)} total_IS={_fmt(med)}"
        )
    # Regime breakdown.
    thin = sum(1 for r in records if r.thin_book_flag)
    extreme = sum(1 for r in records if r.price_extreme_flag)
    buckets: dict[str, int] = {}
    for r in records:
        buckets[r.time_in_market_bucket] = buckets.get(r.time_in_market_bucket, 0) + 1
    print(f"thin_book={thin} price_extreme={extreme} time_buckets={buckets}")


def _median(vals: list[float]) -> float:
    vals = sorted(v for v in vals if math.isfinite(v))
    if not vals:
        return float("nan")
    n = len(vals)
    if n % 2 == 1:
        return vals[n // 2]
    return 0.5 * (vals[n // 2 - 1] + vals[n // 2])


def _fmt(x: float) -> str:
    return "nan" if (x != x) else f"{x:.2f}"


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="experiments.backtest.harness")
    p.add_argument("--events", required=True, type=Path)
    p.add_argument("--scrapes", required=True, type=Path,
                   help="sqlite db with markets table (slug -> condition_id, "
                        "token_ids). Used as slug resolver regardless of "
                        "--input-format.")
    p.add_argument("--out", required=True, type=Path)
    p.add_argument("--window", required=True, help="ISO..ISO e.g. 2026-04-22T01:10Z..2026-04-22T03:10Z")
    p.add_argument("--tick-size", required=True, help="One of 0.1 / 0.01 / 0.001 / 0.0001 (per spec §2.2)")
    p.add_argument("--latency-profile", default="sg_wg_prior", choices=sorted(LATENCY_PROFILES))
    p.add_argument("--fee-category", default="crypto", choices=sorted(CATEGORIES))
    p.add_argument("--mode", default="freeze_depleted", choices=["freeze_depleted", "snap_back", "both"])
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--asset-prefix", default="btc")
    p.add_argument("--input-format", default="book_feed",
                   choices=["book_feed", "sqlite"],
                   help="book_feed: daemon_state/book_feed/{date}/{slug}.jsonl.gz "
                        "from scrape_book.py (primary). sqlite: legacy orderbooks "
                        "table from orderbook_monitor.py (plumbing only).")
    p.add_argument("--feed-dir", type=Path, default=None,
                   help="Path to daemon_state/book_feed (required when "
                        "--input-format=book_feed).")
    p.add_argument("--staleness-hard-ms", type=int, default=500,
                   help="Reject fills where the walked book is older than this (ms). "
                        "Spec default 500.")
    p.add_argument("--staleness-soft-ms", type=int, default=200)
    p.add_argument("--allow-stale", action="store_true",
                   help="Diagnostic only. Sets staleness_hard_ms very high so "
                        "coarse-cadence data does not classify every row as "
                        "book_stale. Stamps staleness_policy=allow_stale_diagnostic "
                        "on every output row and in the manifest; report.py refuses "
                        "paper-vs-realistic headlines on such outputs.")
    args = p.parse_args(argv)
    staleness_policy = "allow_stale_diagnostic" if args.allow_stale else "strict"
    staleness_hard_ms = args.staleness_hard_ms
    if args.allow_stale and staleness_hard_ms <= 500:
        staleness_hard_ms = 10 * 60 * 1000
    common = dict(
        events=args.events, scrapes=args.scrapes, window=args.window,
        tick_size=Decimal(args.tick_size), latency_profile=args.latency_profile,
        fee_category=args.fee_category, seed=args.seed,
        asset_prefix=args.asset_prefix,
        staleness_hard_ms=staleness_hard_ms,
        staleness_soft_ms=args.staleness_soft_ms,
        input_format=args.input_format,
        feed_dir=args.feed_dir,
        staleness_policy=staleness_policy,
    )
    if args.mode == "both":
        out_fd = args.out.with_suffix(".freeze_depleted.parquet")
        out_sb = args.out.with_suffix(".snap_back.parquet")
        run(out=out_fd, mode="freeze_depleted", **common)
        run(out=out_sb, mode="snap_back", **common)
    else:
        run(out=args.out, mode=args.mode, **common)
    return 0


if __name__ == "__main__":
    sys.exit(main())
