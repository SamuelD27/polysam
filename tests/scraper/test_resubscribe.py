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


# ── commit 5: snapshot-iter + RuntimeError catch ────────────────────────────


@pytest.mark.asyncio
async def test_prime_from_rest_snapshots_token_states_at_iteration_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_prime_from_rest must not crash when token_states grows mid-iteration.

    The fix is the ``list(token_states.items())`` snapshot at iteration
    start. Tokens added by a concurrent _discovery_loop after iteration
    starts are NOT primed in this call — they get caught by the next
    resubscribe cycle (initial prime is best-effort by design).
    """
    primed: list[str] = []

    def _flaky_fetch(_session, asset_id: str):
        primed.append(asset_id)
        # On the FIRST call, simulate a concurrent _discovery_loop add
        # — mutate the live dict that the iteration is walking.
        # Without the list() snapshot, the next iteration step raises
        # `RuntimeError: dictionary changed size during iteration`.
        if len(primed) == 1:
            token_states["asset-3-yes"] = scrape_book.TokenState(
                asset_id="asset-3-yes",
                slug="btc-updown-5m-1700000300",
                side="yes",
                condition_id="0xtest",
                canonical_tick="0.01",
            )
        return None  # empty REST → function takes the WARNING/DEBUG branch

    monkeypatch.setattr(scrape_book, "fetch_rest_book", _flaky_fetch)

    token_states: dict[str, scrape_book.TokenState] = {
        "asset-1-yes": scrape_book.TokenState(
            asset_id="asset-1-yes", slug="btc-updown-5m-1700000000",
            side="yes", condition_id="0xtest", canonical_tick="0.01",
        ),
        "asset-2-no": scrape_book.TokenState(
            asset_id="asset-2-no", slug="btc-updown-5m-1700000000",
            side="no", condition_id="0xtest", canonical_tick="0.01",
        ),
    }

    class _NoopWriter:
        def write(self, *a, **k):
            pass

        def close_all(self):
            pass

    # Must NOT raise RuntimeError despite mutation.
    await scrape_book._prime_from_rest(
        session=None, token_states=token_states, writer=_NoopWriter(),
    )

    # The two tokens present at iteration start were primed; the new
    # one added mid-iteration was NOT primed in this call (by design —
    # next resubscribe cycle catches it).
    assert sorted(primed) == ["asset-1-yes", "asset-2-no"]
    # And the new token is in token_states for the next cycle.
    assert "asset-3-yes" in token_states


@pytest.mark.asyncio
async def test_subscribe_and_stream_catches_runtime_error_and_reconnects(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A RuntimeError raised inside the WS block must be caught, logged at
    ERROR (with traceback), and trigger reconnect — not silently kill the
    task. Mirror of the failure pattern from the first 30-min smoke."""
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

    prime_calls = 0

    async def _flaky_prime(*args, **kwargs):
        nonlocal prime_calls
        prime_calls += 1
        if prime_calls == 1:
            # Simulate the exact failure shape from the first smoke:
            # mid-iteration mutation in the original _prime_from_rest.
            raise RuntimeError("simulated dict mutation during iteration")
        return None

    monkeypatch.setattr(scrape_book, "_prime_from_rest", _flaky_prime)
    monkeypatch.setattr(scrape_book, "_dispatch", lambda *a, **k: None)

    token_states = {
        "asset-1-yes": scrape_book.TokenState(
            asset_id="asset-1-yes", slug="btc-updown-5m-1700000000",
            side="yes", condition_id="0xtest", canonical_tick="0.01",
        ),
    }

    class _NoopWriter:
        def write(self, *a, **k):
            pass

        def close_all(self):
            pass

    discovery_event = asyncio.Event()

    task = asyncio.create_task(
        scrape_book._subscribe_and_stream(
            token_states, _NoopWriter(), session=None,
            discovery_event=discovery_event,
        )
    )

    try:
        with caplog.at_level("ERROR", logger="scrape_book"):
            # Wait long enough for: 1st subscribe → RuntimeError →
            # 3-s reconnect sleep → 2nd subscribe.
            await asyncio.wait_for(sub_observed.wait(), timeout=3.0)
            sub_observed.clear()
            await asyncio.wait_for(sub_observed.wait(), timeout=10.0)

        # Both subscribes happened — task survived the RuntimeError.
        assert len(received_subs) >= 2, (
            f"task likely died; got only {len(received_subs)} subscribes"
        )
        # First prime raised; second prime succeeded.
        assert prime_calls >= 2

        # ERROR log line emitted (with traceback via logger.exception).
        error_records = [
            r for r in caplog.records
            if r.levelname == "ERROR" and "RuntimeError" in r.getMessage()
        ]
        assert error_records, (
            "expected ERROR log line mentioning RuntimeError"
        )
        # logger.exception attaches exc_info — confirm traceback was recorded.
        assert any(r.exc_info is not None for r in error_records), (
            "expected exc_info on the ERROR record (logger.exception)"
        )
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        server.close()
        await server.wait_closed()
