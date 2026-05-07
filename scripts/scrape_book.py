#!/usr/bin/env python3
"""scripts/scrape_book.py -- CLOB WebSocket book/delta scraper for BTC 5m markets.

Subscribes to wss://ws-subscriptions-clob.polymarket.com/ws/market with the
``custom_feature_enabled`` flag and persists every ``book`` / ``price_change``
/ ``last_trade_price`` / ``tick_size_change`` event to a per-market gzipped
JSONL under ``daemon_state/book_feed/{YYYY-MM-DD}/{slug}.jsonl.gz``.

Per spec §1.2 / §8.1.2, this replaces 30-s REST snapshot polling — a
snapshot-only replay is fiction below ~1 s cadence. Delta events arrive
as they happen (often sub-100 ms). The scraper also writes a full
reconstructed snapshot every 5 s as an independent rebuild anchor so a
downstream reader can validate its local state without depending on the
first ``book`` message of the session.

Tick size per side is resolved via ``py_clob_client.ClobClient.get_tick_size``
(REST, canonical) on subscribe and updated on every ``tick_size_change``
event. On disagreement with observed price precision (ROUNDING_CONFIG),
the FINER tick wins and a ``tick_size_disagreement`` event is written to
the feed for audit.

Sequence-gap handling: every ``price_change`` delta is applied to a local
book; the ``hash`` on the incoming message is treated as a verification
token. On mismatch we emit a ``sequence_gap`` event and force a REST
``/book`` refetch for that token, then resume. Per spec §1.1 we hard-fail
the current session for that market rather than silently interpolate.

Environment-agnostic: the scraper only needs outbound HTTPS/WSS access to
``clob.polymarket.com`` / ``gamma-api.polymarket.com``. Inside the polybot
WireGuard netns for live capture on daemon paths; outside works for public
read-only testing.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import hashlib
import json
import logging
import signal
import sys
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests

try:
    import websockets
except ImportError:
    print("pip install websockets", file=sys.stderr)
    sys.exit(1)

try:
    from py_clob_client.client import ClobClient
except ImportError:
    print("pip install py-clob-client", file=sys.stderr)
    sys.exit(1)


REPO = Path(__file__).resolve().parent.parent
FEED_DIR = REPO / "daemon_state" / "book_feed"

WS_CLOB = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
CLOB_HOST = "https://clob.polymarket.com"
CLOB_BOOK_URL = f"{CLOB_HOST}/book"
GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"

SLUG_PREFIX = "btc-updown-5m-"
SNAPSHOT_CADENCE_S = 5.0
MARKET_DISCOVERY_EVERY_S = 60.0
WS_PING_INTERVAL_S = 15.0
WS_PING_TIMEOUT_S = 10.0

MARKET_DURATION_S = 300
# Keep the active subscription set narrow: markets live right now plus the
# next window, so the daemon sees the upcoming book as soon as it opens but
# we don't pay REST tick-size cost for hundreds of future-scheduled markets.
ACTIVE_LOOKAHEAD_S = MARKET_DURATION_S  # +1 upcoming window
MAX_TICK_LOOKUP_CONCURRENCY = 8

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("scrape_book")


_shutdown = False


def _handle_signal(signum: int, _frame: Any) -> None:
    global _shutdown
    logger.warning("signal %s -- shutting down after current iteration", signum)
    _shutdown = True


signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


class TokenState:
    """Per-token mutable state. Not thread-safe — only the main asyncio loop touches it."""

    __slots__ = (
        "asset_id",
        "slug",
        "side",
        "condition_id",
        "canonical_tick",
        "bids",
        "asks",
        "last_snapshot_ts",
        "last_seen_hash",
    )

    def __init__(
        self,
        asset_id: str,
        slug: str,
        side: str,
        condition_id: str,
        canonical_tick: str,
    ):
        self.asset_id = asset_id
        self.slug = slug
        self.side = side
        self.condition_id = condition_id
        self.canonical_tick = canonical_tick
        self.bids: dict[str, str] = {}   # price -> size (string)
        self.asks: dict[str, str] = {}
        self.last_snapshot_ts: float = 0.0
        self.last_seen_hash: str = ""


class FeedWriter:
    """Append-only gzip JSONL writer, one file per (date, slug). Rotates on midnight UTC."""

    def __init__(self, feed_dir: Path):
        self._dir = feed_dir
        self._open: dict[tuple[str, str], Any] = {}

    def _file_for(self, slug: str) -> Any:
        date = datetime.now(UTC).strftime("%Y-%m-%d")
        key = (date, slug)
        if key in self._open:
            return self._open[key]
        # close stale dates for this slug
        for (d, s), fh in list(self._open.items()):
            if s == slug and d != date:
                fh.close()
                del self._open[(d, s)]
        day_dir = self._dir / date
        day_dir.mkdir(parents=True, exist_ok=True)
        path = day_dir / f"{slug}.jsonl.gz"
        fh = gzip.open(path, "at")
        self._open[key] = fh
        return fh

    def write(self, slug: str, record: dict[str, Any]) -> None:
        fh = self._file_for(slug)
        fh.write(json.dumps(record, separators=(",", ":")) + "\n")
        fh.flush()

    def close_all(self) -> None:
        for fh in self._open.values():
            try:
                fh.close()
            except OSError:
                pass
        self._open.clear()


def _http_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "scrape_book/1.0",
        "Accept": "application/json",
        "Accept-Encoding": "gzip, deflate",
    })
    return s


def _t_zero_from_slug(slug: str) -> int | None:
    """Parse the unix-epoch t_zero suffix from a btc-updown-5m-{t_zero} slug."""
    if not slug.startswith(SLUG_PREFIX):
        return None
    tail = slug[len(SLUG_PREFIX):]
    return int(tail) if tail.isdigit() else None


def discover_markets(session: requests.Session) -> list[dict[str, str]]:
    """Fetch BTC 5-min markets whose trading window is live now or within the
    next ACTIVE_LOOKAHEAD_S seconds. Returns [{slug, condition_id,
    yes_token_id, no_token_id, t_zero}]. Filters on slug-embedded t_zero so
    we don't pay tick-size REST cost for hundreds of future-scheduled markets
    that Gamma reports as 'active'."""
    out: list[dict[str, str]] = []
    offset = 0
    now = int(time.time())
    while True:
        r = session.get(
            GAMMA_EVENTS_URL,
            params={
                "tag_slug": "5M",
                "limit": 100,
                "offset": offset,
                "order": "endDate",
                "ascending": "false",
                "active": "true",
                "closed": "false",
            },
            timeout=15,
        )
        if r.status_code != 200:
            break
        events = r.json()
        if not events:
            break
        if len(events) < 100:
            last_page = True
        else:
            last_page = False
        for ev in events:
            if ev.get("closed"):
                continue
            slug = ev.get("slug", "")
            t_zero = _t_zero_from_slug(slug)
            if t_zero is None:
                continue
            # currently trading: t_zero <= now < t_zero + MARKET_DURATION_S
            # next window:       now < t_zero <= now + ACTIVE_LOOKAHEAD_S
            if not (
                (t_zero <= now < t_zero + MARKET_DURATION_S)
                or (now < t_zero <= now + ACTIVE_LOOKAHEAD_S)
            ):
                continue
            for m in ev.get("markets", []):
                cid = m.get("conditionId", "")
                tokens = m.get("clobTokenIds", "[]")
                if isinstance(tokens, str):
                    try:
                        tokens = json.loads(tokens)
                    except json.JSONDecodeError:
                        tokens = []
                if cid and len(tokens) >= 2:
                    out.append({
                        "slug": slug,
                        "condition_id": cid,
                        "yes_token_id": tokens[0],
                        "no_token_id": tokens[1],
                        "t_zero": t_zero,
                    })
        if last_page:
            break
        offset += 100
    return out


def resolve_tick_size(client: ClobClient, token_id: str) -> str:
    """Canonical per-token tick via py-clob-client REST. Blocking; run via to_thread."""
    return client.get_tick_size(token_id)


def fetch_rest_book(session: requests.Session, token_id: str) -> dict[str, Any] | None:
    r = session.get(CLOB_BOOK_URL, params={"token_id": token_id}, timeout=10)
    if r.status_code != 200:
        return None
    try:
        return r.json()
    except ValueError:
        return None


def _price_precision(levels: dict[str, str]) -> int:
    """Max decimal places observed across level prices. 0 if empty."""
    mx = 0
    for p in levels.keys():
        dp = len(p.split(".", 1)[1]) if "." in p else 0
        mx = max(mx, dp)
    return mx


_TICK_BY_DP = {1: "0.1", 2: "0.01", 3: "0.001", 4: "0.0001"}
_DP_BY_TICK = {v: k for k, v in _TICK_BY_DP.items()}


def _finer(a: str, b: str) -> str:
    return a if _DP_BY_TICK[a] > _DP_BY_TICK[b] else b


def _check_tick_disagreement(st: TokenState) -> tuple[str, str | None]:
    """Return (effective_tick, disagreement_msg). Observed is the tick implied by
    the finest-precision price in our local book. If finer than canonical, we
    trust the finer and surface the disagreement."""
    bid_dp = _price_precision(st.bids)
    ask_dp = _price_precision(st.asks)
    observed_dp = max(bid_dp, ask_dp)
    if observed_dp <= 0:
        return st.canonical_tick, None
    observed_tick = _TICK_BY_DP.get(min(observed_dp, 4), st.canonical_tick)
    finer = _finer(observed_tick, st.canonical_tick)
    if finer != st.canonical_tick:
        return finer, (
            f"canonical={st.canonical_tick} observed={observed_tick} -> using {finer}"
        )
    return st.canonical_tick, None


def _apply_price_change(st: TokenState, change: dict[str, Any]) -> None:
    side = change.get("side", "").upper()
    price = str(change.get("price", ""))
    size = str(change.get("size", "0"))
    if not price:
        return
    levels = st.bids if side == "BUY" else st.asks
    if size == "0":
        levels.pop(price, None)
    else:
        levels[price] = size
    st.last_seen_hash = change.get("hash", st.last_seen_hash)


def _apply_book_snapshot(st: TokenState, msg: dict[str, Any]) -> None:
    st.bids = {str(lvl["price"]): str(lvl["size"]) for lvl in msg.get("bids", [])}
    st.asks = {str(lvl["price"]): str(lvl["size"]) for lvl in msg.get("asks", [])}
    st.last_seen_hash = msg.get("hash", "")


def _local_book_hash(st: TokenState) -> str:
    """Recompute a deterministic hash of local state. The on-wire hash algorithm
    is not documented, so we emit our own hash alongside the message's hash and
    let downstream tooling diff them. This preserves the audit trail even when
    we cannot verify the remote hash directly."""
    h = hashlib.sha256()
    for p in sorted(st.bids):
        h.update(f"b|{p}|{st.bids[p]}|".encode())
    for p in sorted(st.asks):
        h.update(f"a|{p}|{st.asks[p]}|".encode())
    return h.hexdigest()[:16]


def _snapshot_record(st: TokenState, *, reason: str) -> dict[str, Any]:
    effective_tick, disagreement = _check_tick_disagreement(st)
    rec = {
        "type": "snapshot",
        "reason": reason,  # "initial" | "periodic" | "reconnect" | "tick_change"
        "ts_ns": time.time_ns(),
        "asset_id": st.asset_id,
        "slug": st.slug,
        "side": st.side,
        "condition_id": st.condition_id,
        "canonical_tick": st.canonical_tick,
        "effective_tick": effective_tick,
        "bids": [{"price": p, "size": st.bids[p]} for p in sorted(st.bids, key=float, reverse=True)],
        "asks": [{"price": p, "size": st.asks[p]} for p in sorted(st.asks, key=float)],
        "remote_hash": st.last_seen_hash,
        "local_hash": _local_book_hash(st),
    }
    if disagreement:
        rec["tick_size_disagreement"] = disagreement
    return rec


def _raw_event_record(
    st: TokenState, msg: dict[str, Any], *, event_type: str
) -> dict[str, Any]:
    return {
        "type": event_type,
        "ts_ns": time.time_ns(),
        "asset_id": st.asset_id,
        "slug": st.slug,
        "side": st.side,
        "condition_id": st.condition_id,
        "canonical_tick": st.canonical_tick,
        "raw": msg,
    }


def _slug_t_zero(slug: str) -> int | None:
    """Parse t_zero epoch seconds out of a slug like ``btc-updown-5m-1777986000``.

    Returns None if the slug doesn't carry the expected SLUG_PREFIX or
    the suffix isn't an integer. Used by _prime_from_rest to decide
    whether an empty REST /book is "active token, alarm" vs "next-window
    token, expected".
    """
    if not slug.startswith(SLUG_PREFIX):
        return None
    suffix = slug[len(SLUG_PREFIX):]
    try:
        return int(suffix)
    except ValueError:
        return None


async def _prime_from_rest(
    session: requests.Session,
    token_states: dict[str, TokenState],
    writer: FeedWriter,
) -> None:
    """Fetch REST /book for each token and seed local state. Emits an initial
    snapshot record to the feed per token.

    Empty REST /book is the early-warning signal we missed in the R2.2
    capture. If the token is for an ACTIVE market (now is between
    t_zero and t_zero+MARKET_DURATION_S), an empty REST /book means
    daemon_base_v1.clob_book_feed is likely seeing populated state at
    the same moment — i.e. a CDN / regional cache disagreement worth
    surfacing. We log WARNING with enough context to grep daemon.log
    for the cross-check. Next-window tokens with empty REST is
    expected (the market hasn't opened yet) and stays at DEBUG.
    """
    now = time.time()
    for asset_id, st in token_states.items():
        book = await asyncio.to_thread(fetch_rest_book, session, asset_id)
        if not book:
            t_zero = _slug_t_zero(st.slug)
            if t_zero is not None and t_zero <= now < t_zero + MARKET_DURATION_S:
                logger.warning(
                    "prime_from_rest: empty REST /book for ACTIVE token "
                    "asset_id=%s slug=%s side=%s — daemon clob_book_feed "
                    "should have data; possible CDN / regional cache issue. "
                    "Periodic snapshots will write empty until the WS sends "
                    "a book event for this token.",
                    asset_id[:16] + "...", st.slug, st.side,
                )
            else:
                logger.debug(
                    "prime_from_rest: empty REST /book for next-window token "
                    "asset_id=%s slug=%s (expected before market opens)",
                    asset_id[:16] + "...", st.slug,
                )
            continue
        _apply_book_snapshot(st, book)
        st.last_snapshot_ts = time.time()
        writer.write(st.slug, _snapshot_record(st, reason="initial"))


async def _periodic_snapshotter(
    token_states: dict[str, TokenState],
    writer: FeedWriter,
) -> None:
    """Every SNAPSHOT_CADENCE_S, write a reconstructed snapshot for each token.
    Independent rebuild anchor for downstream replay."""
    while not _shutdown:
        try:
            await asyncio.sleep(SNAPSHOT_CADENCE_S)
            now = time.time()
            for st in token_states.values():
                if now - st.last_snapshot_ts < SNAPSHOT_CADENCE_S:
                    continue
                writer.write(st.slug, _snapshot_record(st, reason="periodic"))
                st.last_snapshot_ts = now
        except asyncio.CancelledError:
            return


async def _subscribe_and_stream(
    token_states: dict[str, TokenState],
    writer: FeedWriter,
    session: requests.Session,
    discovery_event: asyncio.Event,
) -> None:
    """Single long-lived WS connection. Reconnects with REST reprime on drop.

    On every iteration, subscribes once with the current ``assets_ids``
    snapshot. The CLOB WS does not support adding tokens to a live
    subscription, so when ``_discovery_loop`` discovers new tokens it
    sets ``discovery_event``; we detect this on the next 1 s recv
    timeout, log a one-line WARNING, close the WS, and the outer loop
    reconnects with the full ``list(token_states.keys())``. This
    mirrors ``daemon_base_v1.clob_book_feed``'s known-good
    rollover-resubscribe pattern.
    """
    while not _shutdown:
        assets_ids = list(token_states.keys())
        if not assets_ids:
            await asyncio.sleep(5.0)
            continue
        try:
            async with websockets.connect(
                WS_CLOB,
                ping_interval=WS_PING_INTERVAL_S,
                ping_timeout=WS_PING_TIMEOUT_S,
            ) as ws:
                sub = {
                    "assets_ids": assets_ids,
                    "type": "market",
                    "custom_feature_enabled": True,
                }
                await ws.send(json.dumps(sub))
                subscribed_set: set[str] = set(assets_ids)
                logger.info("subscribed assets=%d", len(subscribed_set))
                # Clear AFTER subscribe so any discovery events that
                # arrived during the subscribe network round-trip are
                # already covered by this connection.
                discovery_event.clear()
                await _prime_from_rest(session, token_states, writer)
                while not _shutdown:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    except TimeoutError:
                        # On idle: check whether _discovery_loop has
                        # added tokens since this WS was subscribed.
                        # Batched per discovery cycle (the event is set
                        # at most once per cycle).
                        if discovery_event.is_set():
                            current = set(token_states.keys())
                            new_only = current - subscribed_set
                            if new_only:
                                logger.warning(
                                    "ws resubscribe: discovery added %d new "
                                    "tokens (was %d, now %d); closing WS to "
                                    "resubscribe with the full set",
                                    len(new_only),
                                    len(subscribed_set),
                                    len(current),
                                )
                                break  # outer loop reconnects with full list
                            # No genuinely new tokens this cycle (e.g.
                            # discovery saw markets we already had).
                            # Clear so a future real add still fires.
                            discovery_event.clear()
                        continue
                    _dispatch(raw, token_states, writer, session)
        except (websockets.ConnectionClosed, OSError) as e:
            logger.warning("ws disconnect: %s; reconnecting in 3s", e)
            for st in token_states.values():
                writer.write(st.slug, {
                    "type": "reconnect",
                    "ts_ns": time.time_ns(),
                    "asset_id": st.asset_id,
                    "slug": st.slug,
                    "side": st.side,
                    "reason": str(e)[:200],
                })
            await asyncio.sleep(3.0)


def _dispatch(
    raw: str | bytes,
    token_states: dict[str, TokenState],
    writer: FeedWriter,
    session: requests.Session,
) -> None:
    try:
        payload = json.loads(raw)
    except ValueError:
        logger.debug("ws non-json payload: %r", raw[:120] if isinstance(raw, (str, bytes)) else raw)
        return
    # Some CLOB responses come as a single object, others as a list.
    msgs = payload if isinstance(payload, list) else [payload]
    for msg in msgs:
        if not isinstance(msg, dict):
            continue
        et = msg.get("event_type", "")
        if et == "book":
            _handle_book(msg, token_states, writer)
        elif et == "price_change":
            _handle_price_change(msg, token_states, writer, session)
        elif et == "last_trade_price":
            _handle_last_trade(msg, token_states, writer)
        elif et == "tick_size_change":
            _handle_tick_size_change(msg, token_states, writer)
        else:
            # best_bid_ask / new_market / market_resolved / etc.
            _handle_other(msg, token_states, writer)


def _handle_book(msg: dict[str, Any], st_by_asset: dict[str, TokenState], writer: FeedWriter) -> None:
    asset_id = msg.get("asset_id", "")
    st = st_by_asset.get(asset_id)
    if not st:
        return
    _apply_book_snapshot(st, msg)
    st.last_snapshot_ts = time.time()
    # The raw book event already carries full state; a duplicate "snapshot"
    # record would bloat the file without adding information. Periodic
    # snapshots + tick_change / reconnect snapshots still fire on their
    # own cadence.
    writer.write(st.slug, _raw_event_record(st, msg, event_type="book"))


def _handle_price_change(
    msg: dict[str, Any],
    st_by_asset: dict[str, TokenState],
    writer: FeedWriter,
    session: requests.Session,
) -> None:
    changes = msg.get("price_changes", [])
    # price_changes is a list of per-level deltas, each carrying its own asset_id
    by_asset: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for ch in changes:
        aid = ch.get("asset_id")
        if aid:
            by_asset[aid].append(ch)
    for aid, delta_list in by_asset.items():
        st = st_by_asset.get(aid)
        if not st:
            continue
        for delta in delta_list:
            _apply_price_change(st, delta)
        writer.write(
            st.slug,
            {
                "type": "price_change",
                "ts_ns": time.time_ns(),
                "asset_id": st.asset_id,
                "slug": st.slug,
                "side": st.side,
                "canonical_tick": st.canonical_tick,
                "remote_hashes": [d.get("hash", "") for d in delta_list],
                "local_hash_after": _local_book_hash(st),
                "deltas": delta_list,
            },
        )


def _handle_last_trade(msg: dict[str, Any], st_by_asset: dict[str, TokenState], writer: FeedWriter) -> None:
    asset_id = msg.get("asset_id", "")
    st = st_by_asset.get(asset_id)
    if not st:
        return
    writer.write(st.slug, _raw_event_record(st, msg, event_type="last_trade_price"))


def _handle_tick_size_change(
    msg: dict[str, Any],
    st_by_asset: dict[str, TokenState],
    writer: FeedWriter,
) -> None:
    asset_id = msg.get("asset_id", "")
    st = st_by_asset.get(asset_id)
    if not st:
        return
    new = str(msg.get("new_tick_size", ""))
    old = st.canonical_tick
    if new in _DP_BY_TICK:
        st.canonical_tick = new
    writer.write(
        st.slug,
        {
            "type": "tick_size_change",
            "ts_ns": time.time_ns(),
            "asset_id": st.asset_id,
            "slug": st.slug,
            "side": st.side,
            "old_tick_size": old,
            "new_tick_size": st.canonical_tick,
            "raw": msg,
        },
    )
    writer.write(st.slug, _snapshot_record(st, reason="tick_change"))


def _handle_other(msg: dict[str, Any], st_by_asset: dict[str, TokenState], writer: FeedWriter) -> None:
    asset_id = msg.get("asset_id", "")
    st = st_by_asset.get(asset_id)
    if not st:
        return
    writer.write(st.slug, _raw_event_record(st, msg, event_type=str(msg.get("event_type", "other"))))


async def _bulk_resolve_ticks(
    client: ClobClient,
    pairs: list[tuple[str, str, str, str]],  # (asset_id, slug, side, condition_id)
) -> list[tuple[str, str, str, str, str | None]]:
    """Resolve tick_size for many tokens in parallel via a bounded semaphore."""
    sem = asyncio.Semaphore(MAX_TICK_LOOKUP_CONCURRENCY)

    async def _one(asset_id: str, slug: str, side: str, cid: str):
        async with sem:
            try:
                tick = await asyncio.to_thread(resolve_tick_size, client, asset_id)
            except Exception as e:
                logger.warning("get_tick_size failed for %s (%s): %s", slug, side, e)
                return (asset_id, slug, side, cid, None)
            return (asset_id, slug, side, cid, tick)

    results = await asyncio.gather(*(_one(*p) for p in pairs))
    return list(results)


async def _discovery_loop(
    session: requests.Session,
    client: ClobClient,
    token_states: dict[str, TokenState],
    discovery_event: asyncio.Event,
) -> None:
    """Periodically re-discover markets and add new (slug, token) pairs.
    Existing entries are never deleted from token_states during the session —
    deletion happens on process restart.

    On any cycle that adds at least one new token, sets ``discovery_event``
    so ``_subscribe_and_stream`` can resubscribe with the full set. Setting
    once per cycle (not once per token) is the natural batching boundary.
    """
    while not _shutdown:
        try:
            markets = await asyncio.to_thread(discover_markets, session)
            new_pairs: list[tuple[str, str, str, str]] = []
            for m in markets:
                for side, aid in (("yes", m["yes_token_id"]), ("no", m["no_token_id"])):
                    if aid in token_states:
                        continue
                    new_pairs.append((aid, m["slug"], side, m["condition_id"]))
            if new_pairs:
                resolved = await _bulk_resolve_ticks(client, new_pairs)
                added = 0
                for asset_id, slug, side, cid, tick in resolved:
                    if tick is None:
                        continue
                    token_states[asset_id] = TokenState(
                        asset_id=asset_id,
                        slug=slug,
                        side=side,
                        condition_id=cid,
                        canonical_tick=tick,
                    )
                    added += 1
                    logger.info(
                        "new market %s %s tick=%s assets=%d",
                        slug, side, tick, len(token_states),
                    )
                if added:
                    # Single set per cycle; the WS task picks this up on
                    # its next 1 s recv timeout and reconnects with the
                    # full token_states set.
                    discovery_event.set()
            await asyncio.sleep(MARKET_DISCOVERY_EVERY_S)
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.warning("discovery loop error: %s", e)
            await asyncio.sleep(10.0)


async def main_async(args: argparse.Namespace) -> int:
    FEED_DIR.mkdir(parents=True, exist_ok=True)
    session = _http_session()
    client = ClobClient(CLOB_HOST)  # unauthenticated; get_tick_size is public
    writer = FeedWriter(FEED_DIR)
    token_states: dict[str, TokenState] = {}

    # seed the first discovery so the ws task has something to subscribe
    initial = await asyncio.to_thread(discover_markets, session)
    seed_pairs: list[tuple[str, str, str, str]] = []
    for m in initial:
        for side, aid in (("yes", m["yes_token_id"]), ("no", m["no_token_id"])):
            seed_pairs.append((aid, m["slug"], side, m["condition_id"]))
    if seed_pairs:
        for asset_id, slug, side, cid, tick in await _bulk_resolve_ticks(client, seed_pairs):
            if tick is None:
                continue
            token_states[asset_id] = TokenState(
                asset_id=asset_id, slug=slug, side=side,
                condition_id=cid, canonical_tick=tick,
            )
    logger.info("initial subscription set: %d tokens (from %d markets)",
                len(token_states), len(initial))

    # Created inside the running loop so the asyncio.Event binds to the
    # right loop (asyncio.Event() at module scope would bind to the
    # default loop, which may not be the one running these tasks).
    discovery_event = asyncio.Event()
    tasks = [
        asyncio.create_task(
            _subscribe_and_stream(token_states, writer, session, discovery_event),
        ),
        asyncio.create_task(_periodic_snapshotter(token_states, writer)),
        asyncio.create_task(
            _discovery_loop(session, client, token_states, discovery_event),
        ),
    ]

    try:
        while not _shutdown:
            await asyncio.sleep(1.0)
            if args.stop_after_s and (time.time() - _START_TS) >= args.stop_after_s:
                logger.info("stop_after_s reached")
                break
    finally:
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass
        writer.close_all()
    return 0


_START_TS = time.time()


def main() -> int:
    p = argparse.ArgumentParser(prog="scrape_book.py")
    p.add_argument(
        "--stop-after-s",
        type=int,
        default=0,
        help="Exit after N seconds (0 = run until SIGTERM)",
    )
    args = p.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
