"""Polymarket CLOB /book poller with backoff."""
from unittest.mock import MagicMock

import pytest

import dashboard as d


class FakeResponse:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            err = d.requests.HTTPError(f"{self.status_code}")
            err.response = self
            raise err


def test_poller_success_parses_bids_and_asks():
    sess = MagicMock()
    sess.get.return_value = FakeResponse(200, {
        "bids": [{"price": "0.42", "size": "10"},
                 {"price": "0.40", "size": "15"}],
        "asks": [{"price": "0.45", "size": "8"},
                 {"price": "0.47", "size": "12"}],
    })
    p = d.OrderbookPoller("tokenid", session=sess)
    snap = p.poll_once()
    assert snap.status == "ok"
    assert snap.best_bid == 0.42
    assert snap.best_ask == 0.45
    assert snap.spread == pytest.approx(0.03)
    assert snap.mid == pytest.approx(0.435)
    # Bids sorted descending, asks ascending
    assert [px for px, _ in snap.bids] == [0.42, 0.40]
    assert [px for px, _ in snap.asks] == [0.45, 0.47]
    assert snap.error is None


def test_poller_403_records_status_code():
    sess = MagicMock()
    sess.get.return_value = FakeResponse(403, None)
    p = d.OrderbookPoller("tokenid", session=sess)
    snap = p.poll_once()
    assert snap.status == "error"
    assert "403" in (snap.error or "")


def test_poller_timeout_records_timeout():
    sess = MagicMock()
    sess.get.side_effect = d.requests.Timeout()
    p = d.OrderbookPoller("tokenid", session=sess)
    snap = p.poll_once()
    assert snap.status == "error"
    assert snap.error == "timeout"


def test_poller_backs_off_after_three_failures():
    sess = MagicMock()
    sess.get.side_effect = d.requests.Timeout()
    p = d.OrderbookPoller("tokenid", session=sess,
                         base_interval=2.0, backoff_interval=10.0,
                         failure_threshold=3)
    assert p.current_interval == 2.0
    p.poll_once(); assert p.current_interval == 2.0
    p.poll_once(); assert p.current_interval == 2.0
    p.poll_once(); assert p.current_interval == 10.0


def test_poller_recovers_to_base_interval_on_success():
    sess = MagicMock()
    sess.get.side_effect = (
        [d.requests.Timeout()] * 3 +
        [FakeResponse(200, {"bids": [], "asks": []})]
    )
    p = d.OrderbookPoller("tokenid", session=sess,
                         base_interval=2.0, backoff_interval=10.0,
                         failure_threshold=3)
    for _ in range(3):
        p.poll_once()
    assert p.current_interval == 10.0
    p.poll_once()  # 4th call returns success
    assert p.current_interval == 2.0


def test_poller_empty_book_marked_empty_not_error():
    sess = MagicMock()
    sess.get.return_value = FakeResponse(200, {"bids": [], "asks": []})
    p = d.OrderbookPoller("tokenid", session=sess)
    snap = p.poll_once()
    assert snap.status == "empty"
    assert snap.error is None


def test_ensure_poller_does_not_busy_loop_on_resolve_failure(monkeypatch):
    """If TokenResolver returns None, we must not re-resolve every tick."""
    import dashboard as d

    # Bypass full app init.
    app = d.DashboardApp.__new__(d.DashboardApp)
    app._resolver = MagicMock()
    app._resolver.resolve.return_value = None
    app._poller = None
    app._poller_slug = None

    app._ensure_poller("slug-X")
    app._ensure_poller("slug-X")
    app._ensure_poller("slug-X")

    # Only the first call should hit the resolver.
    assert app._resolver.resolve.call_count == 1
    assert app._poller is None  # confirmed unresolvable
    assert app._poller_slug == "slug-X"


def test_ensure_poller_retries_on_slug_change(monkeypatch):
    """A new slug must trigger a fresh resolve, even if the previous failed."""
    import dashboard as d

    app = d.DashboardApp.__new__(d.DashboardApp)
    app._resolver = MagicMock()
    app._resolver.resolve.side_effect = [None, MagicMock(yes_token_id="tok-Y")]
    app._poller = None
    app._poller_slug = None

    app._ensure_poller("slug-X")  # first slug fails
    app._ensure_poller("slug-Y")  # second slug succeeds

    assert app._resolver.resolve.call_count == 2
    assert isinstance(app._poller, d.OrderbookPoller)
    assert app._poller.token_id == "tok-Y"
