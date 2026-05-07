"""Integration test for the scraper's resubscribe-on-discovery path.

Verifies the fix landed in commit `e3724dd`: when `_discovery_loop`
adds new tokens to ``token_states`` and sets the shared
``asyncio.Event``, the WS task closes and reopens its connection,
re-sending the subscribe with the full ``list(token_states.keys())``.

A local ``websockets.serve`` mock acts as the CLOB WS endpoint.
``WS_CLOB`` is monkey-patched at the module level so the scraper
connects to it. ``_prime_from_rest`` is monkey-patched to a no-op so
the test doesn't need to mock REST. ``_dispatch`` is also patched out
since this test is about subscription bookkeeping, not message handling.
"""

from __future__ import annotations

import asyncio
import json

import pytest
import websockets

from scripts import scrape_book


@pytest.mark.asyncio
async def test_subscribe_and_stream_resubscribes_when_discovery_adds_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Initial 2 tokens -> 1 subscribe; discovery adds 2 -> 2nd subscribe with all 4."""
    received_subs: list[list[str]] = []
    sub_observed = asyncio.Event()

    async def mock_handler(websocket):
        async for msg in websocket:
            try:
                payload = json.loads(msg)
            except (TypeError, ValueError):
                continue
            if payload.get("type") == "market" and "assets_ids" in payload:
                received_subs.append(list(payload["assets_ids"]))
                sub_observed.set()

    server = await websockets.serve(mock_handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    monkeypatch.setattr(scrape_book, "WS_CLOB", f"ws://127.0.0.1:{port}")

    # No-op REST prime + no-op dispatch keep the test focused on
    # subscribe bookkeeping. The mock server never sends messages, so
    # the WS task spends its time in the recv-timeout branch — exactly
    # where the discovery-event check fires.
    async def _noop_prime(*args, **kwargs):
        return None

    def _noop_dispatch(*args, **kwargs):
        return None

    monkeypatch.setattr(scrape_book, "_prime_from_rest", _noop_prime)
    monkeypatch.setattr(scrape_book, "_dispatch", _noop_dispatch)

    # Two initial tokens.
    def _ts(asset_id: str, slug: str, side: str) -> scrape_book.TokenState:
        return scrape_book.TokenState(
            asset_id=asset_id,
            slug=slug,
            side=side,
            condition_id="0xtest",
            canonical_tick="0.01",
        )

    token_states: dict[str, scrape_book.TokenState] = {
        "asset-1-yes": _ts("asset-1-yes", "btc-updown-5m-1700000000", "yes"),
        "asset-1-no": _ts("asset-1-no", "btc-updown-5m-1700000000", "no"),
    }

    class _NoopWriter:
        def write(self, *args, **kwargs):
            pass

        def close_all(self):
            pass

    writer = _NoopWriter()
    discovery_event = asyncio.Event()

    task = asyncio.create_task(
        scrape_book._subscribe_and_stream(
            token_states, writer, session=None, discovery_event=discovery_event,
        )
    )

    try:
        # Wait for first subscribe.
        await asyncio.wait_for(sub_observed.wait(), timeout=3.0)
        assert received_subs, "first subscribe never observed"
        assert sorted(received_subs[0]) == ["asset-1-no", "asset-1-yes"], (
            f"first subscribe wrong: {received_subs[0]}"
        )

        # Add two more tokens and signal discovery.
        sub_observed.clear()
        token_states["asset-2-yes"] = _ts(
            "asset-2-yes", "btc-updown-5m-1700000300", "yes",
        )
        token_states["asset-2-no"] = _ts(
            "asset-2-no", "btc-updown-5m-1700000300", "no",
        )
        discovery_event.set()

        # Wait for resubscribe (1s recv timeout + WS reconnect).
        await asyncio.wait_for(sub_observed.wait(), timeout=10.0)
        assert len(received_subs) >= 2, (
            f"resubscribe never observed; received={received_subs}"
        )
        assert sorted(received_subs[1]) == [
            "asset-1-no", "asset-1-yes", "asset-2-no", "asset-2-yes",
        ], f"resubscribe wrong: {received_subs[1]}"
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_discovery_loop_sets_event_only_when_tokens_added(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A discovery cycle that adds zero new tokens must NOT set the event."""
    # Stub discover_markets + _bulk_resolve_ticks so we don't hit the network.
    async def _no_new_markets(*args, **kwargs):
        return []

    monkeypatch.setattr(scrape_book, "discover_markets", lambda _s: [])
    monkeypatch.setattr(scrape_book, "_bulk_resolve_ticks", _no_new_markets)
    # Make the loop iterate fast.
    monkeypatch.setattr(scrape_book, "MARKET_DISCOVERY_EVERY_S", 0.05)

    token_states: dict[str, scrape_book.TokenState] = {
        "existing-asset": scrape_book.TokenState(
            asset_id="existing-asset",
            slug="btc-updown-5m-1700000000",
            side="yes",
            condition_id="0xtest",
            canonical_tick="0.01",
        ),
    }
    discovery_event = asyncio.Event()
    task = asyncio.create_task(
        scrape_book._discovery_loop(
            session=None, client=None,
            token_states=token_states, discovery_event=discovery_event,
        )
    )
    try:
        # Let the loop iterate a few times.
        await asyncio.sleep(0.25)
        assert not discovery_event.is_set(), (
            "discovery_event set despite no new tokens added"
        )
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def test_slug_t_zero_parses_canonical_slug() -> None:
    assert scrape_book._slug_t_zero("btc-updown-5m-1777986000") == 1777986000


def test_slug_t_zero_returns_none_on_unknown_prefix() -> None:
    assert scrape_book._slug_t_zero("eth-updown-5m-1700000000") is None


def test_slug_t_zero_returns_none_on_garbage_suffix() -> None:
    assert scrape_book._slug_t_zero("btc-updown-5m-not-a-number") is None
