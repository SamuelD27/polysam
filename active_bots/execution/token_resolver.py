"""Resolve a Polymarket market slug -> (yes_token_id, no_token_id).

Polymarket web traders see slugs like 'btc-updown-5m-1713638400'. To place
a CLOB order we need the ERC-1155 token id for each outcome, which lives in
the `clobTokenIds` field of the Gamma markets API response.

The daemon calls resolve(slug) each time a new 5-minute market starts.
Results are cached forever (slugs are time-unique). Lookups are synchronous
HTTP (blocking the strategy loop for a few hundred ms) since entry windows
don't open until T+60 at the earliest.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass

import requests

GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"

logger = logging.getLogger("execution.token_resolver")


@dataclass(frozen=True)
class TokenIds:
    yes_token_id: str
    no_token_id: str
    tick_size: float = 0.01


class TokenResolver:
    def __init__(self, session: requests.Session | None = None, timeout: float = 5.0):
        self._session = session or requests.Session()
        self._timeout = timeout
        self._cache: dict[str, TokenIds] = {}
        self._lock = threading.Lock()

    def resolve(self, slug: str) -> TokenIds | None:
        """Look up YES/NO token ids for the given slug. Cached on hit.

        Returns None on error; caller is expected to skip the entry and try
        again on the next tick (the cache is empty so the next call retries).
        """
        with self._lock:
            cached = self._cache.get(slug)
        if cached is not None:
            return cached

        tokens = self._fetch_via_events(slug) or self._fetch_via_markets(slug)
        if tokens is None:
            return None

        with self._lock:
            self._cache[slug] = tokens
        logger.info(
            "resolved slug=%s yes=%s… no=%s…",
            slug, tokens.yes_token_id[:10], tokens.no_token_id[:10],
        )
        return tokens

    # ── Two lookup paths: by event slug (preferred) and by market slug ──

    def _fetch_via_events(self, slug: str) -> TokenIds | None:
        """5-minute markets live inside an event whose slug equals the market slug.
        The events endpoint embeds the markets list directly -- 1 roundtrip."""
        try:
            resp = self._session.get(
                GAMMA_EVENTS_URL,
                params={"slug": slug},
                timeout=self._timeout,
            )
            resp.raise_for_status()
            events = resp.json()
        except (requests.RequestException, ValueError) as e:
            logger.warning("gamma /events lookup failed for %s: %s", slug, e)
            return None

        if not events:
            return None
        markets = events[0].get("markets") or []
        for market in markets:
            parsed = _parse_market(market)
            if parsed is not None:
                return parsed
        return None

    def _fetch_via_markets(self, slug: str) -> TokenIds | None:
        """Fallback: the /markets endpoint accepts the same slug directly."""
        try:
            resp = self._session.get(
                GAMMA_MARKETS_URL,
                params={"slug": slug},
                timeout=self._timeout,
            )
            resp.raise_for_status()
            markets = resp.json()
        except (requests.RequestException, ValueError) as e:
            logger.warning("gamma /markets lookup failed for %s: %s", slug, e)
            return None

        if not markets:
            return None
        for market in markets:
            parsed = _parse_market(market)
            if parsed is not None:
                return parsed
        return None


def _parse_market(market: dict) -> TokenIds | None:
    """Extract YES/NO token ids from a Gamma market object.

    clobTokenIds may be a JSON-encoded string or already a list; both forms
    show up in the wild (matches scrap/01_markets.py:_parse_event_markets).
    """
    raw = market.get("clobTokenIds", "[]")
    if isinstance(raw, str):
        try:
            tokens = json.loads(raw)
        except json.JSONDecodeError:
            return None
    else:
        tokens = raw

    if not isinstance(tokens, list) or len(tokens) < 2:
        return None
    yes, no = str(tokens[0]), str(tokens[1])
    if not yes or not no:
        return None

    tick = market.get("orderPriceMinTickSize") or market.get("minimumTickSize")
    try:
        tick_size = float(tick) if tick is not None else 0.01
    except (TypeError, ValueError):
        tick_size = 0.01

    return TokenIds(yes_token_id=yes, no_token_id=no, tick_size=tick_size)
