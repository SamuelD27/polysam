"""Book-walked FAK replay executor.

Composes book.py, fees.py, latency.py into a single ``post_fak`` method that
takes an ``OrderRequest`` plus a decision timestamp and returns a §7
``ExecutionRecord`` with the §5.2 implementation-shortfall decomposition.

Decomposition (non-overlapping, sums to ``fill_VWAP - decision_mid`` * sign):

    half_spread_cost   = (best_opposite_at_decision - decision_mid)   * sign
    latency_drift_cost = (best_opposite_at_ack - best_opposite_at_dec) * sign
    book_walk_cost     = (fill_VWAP - best_opposite_at_ack)            * sign
    fees_cost          = fee_usdc(fill_VWAP, filled_qty, category)

All price-delta costs are emitted in basis points of ``decision_mid``. Fees
are emitted in basis points of filled notional. ``total_IS`` is the simple sum.

Two modes (spec §4.2):

- ``freeze_depleted`` (default): the book at ``t_ack`` is the latest snapshot
  with ``ts_ns <= t_ack``. Conservative on a single trade.
- ``snap_back``: the book at ``t_ack`` is the first snapshot with
  ``ts_ns >= t_ack``. Anti-conservative; reports the optimistic bound.

``book_staleness_ms`` is computed against the book used for the walk. When
``staleness_hard_ms`` is exceeded, classification is set to ``book_stale`` and
the walk is skipped; cost columns are NaN.

Adverse-selection columns are left NaN at this layer; the harness fills them
when a snapshot exists at ``t_fill + {1, 5, 30}s``. Same for
``opportunity_cost_unfilled`` (requires ``mid_at_close``) and
``paper_pnl_flat_0_5`` / ``realised_pnl_at_close`` (require EXIT pairing).
"""

from __future__ import annotations

import bisect
import math
import random
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Callable, Literal, Optional, Protocol

from .book import Book, FillResult, freeze_last_book, walk_book
from .fees import FeeCategory, fee_usdc as _default_fee
from .latency import (
    ConditionedSampler,
    LatencyProfile,
    SG_WG_PRIOR,
    sample as _sample_latency,
)

NAN = float("nan")
_ONE = Decimal("1")
_HALF = Decimal("0.5")
_10K = Decimal("10000")


class BookStore(Protocol):
    def snapshots_for(self, token_id: str) -> list[Book]: ...


@dataclass
class DictBookStore:
    """Minimal in-memory ``BookStore`` backed by a dict of sorted snapshot lists."""

    data: dict[str, list[Book]] = field(default_factory=dict)

    def snapshots_for(self, token_id: str) -> list[Book]:
        return self.data.get(token_id, [])

    def add(self, snap: Book) -> None:
        self.data.setdefault(snap.token_id, []).append(snap)

    def freeze(self) -> None:
        for k in self.data:
            self.data[k].sort(key=lambda s: s.ts_ns)


@dataclass(frozen=True)
class OrderRequest:
    token_id: str
    side: Literal["BUY", "SELL"]
    requested_shares: Decimal
    worst_price_limit: Decimal
    decision_mid: Decimal
    category: FeeCategory
    tick_size: Decimal
    # context passthrough — harness-supplied, no effect on walk/attribution
    market_id: str = ""
    strategy_id: str = ""
    trade_id: str = ""
    parent_order_id: str = ""
    backtest_run_id: str = ""
    t_zero_ns: Optional[int] = None
    t_end_ns: Optional[int] = None
    edge_at_signal: float = NAN
    # Latency-sampling context (spec §3.1 / §3.3): harness populates these
    # when it has signal; absent, default "unk" lets a ConditionedSampler
    # fall through to its configured fallback profile.
    tunnel_age_bucket: str = "unk"
    vol_regime: str = "unk"


@dataclass(frozen=True)
class ExecutionRecord:
    # identifiers / timestamps
    trade_id: str
    parent_order_id: str
    strategy_id: str
    backtest_run_id: str
    t_signal_ns: int
    t_send_ns: int
    t_ack_ns: int
    t_first_fill_ns: int
    t_last_fill_ns: int
    # context at decision
    token_id: str
    market_id: str
    side: str
    tick_size: float
    decision_mid: float
    best_bid: float
    best_ask: float
    spread: float
    top_of_book_size_bid: float
    top_of_book_size_ask: float
    cumulative_depth_5bps: float
    cumulative_depth_20bps: float
    book_staleness_ms: int
    market_age_s: float
    market_remaining_s: float
    # request and fill
    requested_qty_shares: float
    requested_notional_usdc: float
    worst_price_limit: float
    filled_qty: float
    residual_qty: float
    fill_vwap: float
    levels_consumed: int
    classification: str  # full | partial | unfilled | book_stale
    # latency
    sampled_latency_ms: float
    latency_source: str
    p_bucket_used: str
    # attribution (bps of notional)
    half_spread_cost: float
    book_walk_cost: float
    latency_drift_cost: float
    adverse_selection_1s: float
    adverse_selection_5s: float
    adverse_selection_30s: float
    fees_cost: float
    opportunity_cost_unfilled: float
    total_IS: float
    # PnL
    edge_at_signal: float
    edge_at_fill: float
    realised_pnl_at_close: float
    paper_pnl_flat_0_5: float
    diff_paper_minus_realised: float
    # regime tags
    thin_book_flag: bool
    price_extreme_flag: bool
    vol_regime: str
    tunnel_age_bucket: str
    time_in_market_bucket: str
    # run mode
    mode: str


def _freeze_or_snap(
    snapshots: list[Book],
    t_query_ns: int,
    mode: Literal["freeze_depleted", "snap_back"],
) -> tuple[Optional[Book], Optional[int]]:
    """Return (book, staleness_ms) per mode, or (None, None) if unavailable."""
    if not snapshots:
        return None, None
    if mode == "freeze_depleted":
        try:
            snap, stale = freeze_last_book(snapshots, t_query_ns)
            return snap, stale
        except LookupError:
            return None, None
    elif mode == "snap_back":
        ts_list = [s.ts_ns for s in snapshots]
        idx = bisect.bisect_left(ts_list, t_query_ns)
        if idx >= len(snapshots):
            return None, None
        snap = snapshots[idx]
        stale_ms = (snap.ts_ns - t_query_ns) // 1_000_000
        return snap, stale_ms
    raise ValueError(f"invalid mode {mode!r}")


def _best_opposite(book: Book, side: str) -> Optional[Decimal]:
    if side == "BUY":
        return book.side_asks[0].price if book.side_asks else None
    if side == "SELL":
        return book.side_bids[0].price if book.side_bids else None
    raise ValueError(f"invalid side {side!r}")


def _top_size(levels: tuple) -> Decimal:
    return levels[0].size if levels else Decimal(0)


def _cumulative_depth(levels: tuple, ref_price: Decimal, width_bps: Decimal) -> Decimal:
    """Sum of size at levels within ``width_bps`` of ``ref_price`` on the same
    side as ``levels``. Works for either side; caller passes the opposite-side
    best as ``ref_price`` for book-walk context."""
    if not levels or ref_price <= 0:
        return Decimal(0)
    width = ref_price * width_bps / _10K
    total = Decimal(0)
    for lvl in levels:
        if abs(lvl.price - ref_price) <= width:
            total += lvl.size
    return total


def _bps_of(delta: Decimal, ref: Decimal) -> float:
    if ref <= 0:
        return NAN
    return float(delta / ref * _10K)


def _mid(book: Book) -> Optional[Decimal]:
    bb = book.side_bids[0].price if book.side_bids else None
    ba = book.side_asks[0].price if book.side_asks else None
    if bb is None and ba is None:
        return None
    if bb is None:
        return ba
    if ba is None:
        return bb
    return (bb + ba) / 2


def _time_in_market_bucket(
    decision_ts_ns: int, t_zero_ns: Optional[int], t_end_ns: Optional[int]
) -> str:
    if t_zero_ns is None or t_end_ns is None or t_end_ns <= t_zero_ns:
        return "unk"
    if decision_ts_ns < t_zero_ns:
        return "pre"
    frac = (decision_ts_ns - t_zero_ns) / (t_end_ns - t_zero_ns)
    age_s = (decision_ts_ns - t_zero_ns) / 1e9
    remaining_s = (t_end_ns - decision_ts_ns) / 1e9
    if remaining_s <= 30.0:
        return "last_30s"
    if age_s <= 60.0:
        return "first_60s"
    if frac < 0.5:
        return "mid_early"
    return "mid_late"


class ReplayExecutor:
    def __init__(
        self,
        books: BookStore,
        fees_fn: Callable[[Decimal, Decimal, FeeCategory], Decimal] = _default_fee,
        latency_profile: LatencyProfile = SG_WG_PRIOR,
        latency_sampler: Optional[ConditionedSampler] = None,
        mode: Literal["freeze_depleted", "snap_back"] = "freeze_depleted",
        staleness_hard_ms: int = 500,
        staleness_soft_ms: int = 200,
        rng: Optional[random.Random] = None,
        p_bucket_used: str = "base",
    ):
        self._books = books
        self._fees_fn = fees_fn
        self._profile = latency_profile
        # When ``latency_sampler`` is supplied, per-order latency is drawn
        # from it using (tunnel_age_bucket, vol_regime) context from the
        # OrderRequest. Otherwise a single LatencyProfile is used (back-compat).
        self._sampler = latency_sampler
        self._mode = mode
        self._staleness_hard_ms = int(staleness_hard_ms)
        self._staleness_soft_ms = int(staleness_soft_ms)
        self._rng = rng if rng is not None else random.Random(0)
        self._p_bucket_used = p_bucket_used

    @property
    def mode(self) -> str:
        return self._mode

    def post_fak(self, order: OrderRequest, decision_ts_ns: int) -> ExecutionRecord:
        sign = _ONE if order.side == "BUY" else -_ONE
        snaps = self._books.snapshots_for(order.token_id)

        if self._sampler is not None:
            # Context is a dict so ConditionedSampler's key_fn can pick the
            # field(s) relevant to its bucketing strategy.
            ctx = {
                "tunnel_age_bucket": order.tunnel_age_bucket,
                "vol_regime": order.vol_regime,
            }
            latency_ms, bucket_used, profile_used = self._sampler.sample(ctx, self._rng)
            latency_source = profile_used.source
        else:
            latency_ms = _sample_latency(self._profile, self._rng)
            bucket_used = self._p_bucket_used
            latency_source = self._profile.source
        t_ack_ns = decision_ts_ns + int(latency_ms * 1e6)

        book_dec, stale_dec_ms = _freeze_or_snap(snaps, decision_ts_ns, "freeze_depleted")
        book_ack, stale_ack_ms = _freeze_or_snap(snaps, t_ack_ns, self._mode)

        base = _empty_record(
            order=order,
            decision_ts_ns=decision_ts_ns,
            t_ack_ns=t_ack_ns,
            latency_ms=latency_ms,
            latency_source=latency_source,
            p_bucket_used=bucket_used,
            mode=self._mode,
        )

        if book_ack is None or stale_ack_ms is None:
            return _with(base, classification="book_stale", book_staleness_ms=_nan_to_int(stale_ack_ms))

        if stale_ack_ms > self._staleness_hard_ms:
            dec_mid_f = float(order.decision_mid)
            return _with(
                base,
                classification="book_stale",
                book_staleness_ms=int(stale_ack_ms),
                best_bid=float(book_ack.side_bids[0].price) if book_ack.side_bids else NAN,
                best_ask=float(book_ack.side_asks[0].price) if book_ack.side_asks else NAN,
                spread=_safe_spread(book_ack),
                tick_size=float(book_ack.tick_size),
            )

        fill: FillResult = walk_book(
            book_ack, order.side, order.requested_shares, order.worst_price_limit
        )

        dec_mid = order.decision_mid
        best_opp_dec = _best_opposite(book_dec, order.side) if book_dec is not None else None
        best_opp_ack = _best_opposite(book_ack, order.side)
        mid_ack = _mid(book_ack)
        if best_opp_dec is None:
            best_opp_dec = best_opp_ack  # fallback: no pre-decision book

        # Attribution in price units (signed so positive = cost to trader).
        half_spread = (best_opp_dec - dec_mid) * sign if best_opp_dec is not None else None
        latency_drift = (best_opp_ack - best_opp_dec) * sign if (best_opp_ack is not None and best_opp_dec is not None) else None
        book_walk = (fill.vwap - best_opp_ack) * sign if (fill.vwap is not None and best_opp_ack is not None) else None

        half_spread_bps = _bps_of(half_spread, dec_mid) if half_spread is not None else NAN
        latency_drift_bps = _bps_of(latency_drift, dec_mid) if latency_drift is not None else NAN
        book_walk_bps = _bps_of(book_walk, dec_mid) if book_walk is not None else NAN

        if fill.vwap is not None and fill.filled_qty > 0:
            fees_usdc = self._fees_fn(fill.vwap, fill.filled_qty, order.category)
            notional = fill.vwap * fill.filled_qty
            fees_bps = float(fees_usdc / notional * _10K) if notional > 0 else NAN
        else:
            fees_usdc = Decimal(0)
            fees_bps = 0.0 if fill.classification == "unfilled" else NAN

        total_is = _sum_nan_propagating(
            half_spread_bps, latency_drift_bps, book_walk_bps, fees_bps
        )

        # Regime flags
        top_bid = _top_size(book_ack.side_bids)
        top_ask = _top_size(book_ack.side_asks)
        top_opp = top_ask if order.side == "BUY" else top_bid
        thin = bool(top_opp < order.requested_shares * 2)

        price_extreme = False
        if dec_mid < Decimal("0.05") or dec_mid > Decimal("0.95"):
            price_extreme = True

        return _with(
            base,
            classification=fill.classification,
            tick_size=float(book_ack.tick_size),
            best_bid=float(book_ack.side_bids[0].price) if book_ack.side_bids else NAN,
            best_ask=float(book_ack.side_asks[0].price) if book_ack.side_asks else NAN,
            spread=_safe_spread(book_ack),
            top_of_book_size_bid=float(top_bid),
            top_of_book_size_ask=float(top_ask),
            cumulative_depth_5bps=float(
                _cumulative_depth(
                    book_ack.side_asks if order.side == "BUY" else book_ack.side_bids,
                    best_opp_ack if best_opp_ack is not None else Decimal(0),
                    Decimal(5),
                )
            ),
            cumulative_depth_20bps=float(
                _cumulative_depth(
                    book_ack.side_asks if order.side == "BUY" else book_ack.side_bids,
                    best_opp_ack if best_opp_ack is not None else Decimal(0),
                    Decimal(20),
                )
            ),
            book_staleness_ms=int(stale_ack_ms),
            filled_qty=float(fill.filled_qty),
            residual_qty=float(fill.residual_qty),
            fill_vwap=float(fill.vwap) if fill.vwap is not None else NAN,
            levels_consumed=fill.levels_consumed,
            half_spread_cost=half_spread_bps,
            book_walk_cost=book_walk_bps,
            latency_drift_cost=latency_drift_bps,
            fees_cost=fees_bps,
            total_IS=total_is,
            thin_book_flag=thin,
            price_extreme_flag=price_extreme,
            time_in_market_bucket=_time_in_market_bucket(
                decision_ts_ns, order.t_zero_ns, order.t_end_ns
            ),
        )


def _safe_spread(book: Book) -> float:
    if not book.side_bids or not book.side_asks:
        return NAN
    return float(book.side_asks[0].price - book.side_bids[0].price)


def _sum_nan_propagating(*vals: float) -> float:
    total = 0.0
    for v in vals:
        if math.isnan(v):
            return NAN
        total += v
    return total


def _nan_to_int(v: Optional[int]) -> int:
    return -1 if v is None else int(v)


def _empty_record(
    *,
    order: OrderRequest,
    decision_ts_ns: int,
    t_ack_ns: int,
    latency_ms: float,
    latency_source: str,
    p_bucket_used: str,
    mode: str,
) -> ExecutionRecord:
    return ExecutionRecord(
        trade_id=order.trade_id,
        parent_order_id=order.parent_order_id,
        strategy_id=order.strategy_id,
        backtest_run_id=order.backtest_run_id,
        t_signal_ns=decision_ts_ns,
        t_send_ns=decision_ts_ns,
        t_ack_ns=t_ack_ns,
        t_first_fill_ns=t_ack_ns,
        t_last_fill_ns=t_ack_ns,
        token_id=order.token_id,
        market_id=order.market_id,
        side=order.side,
        tick_size=float(order.tick_size),
        decision_mid=float(order.decision_mid),
        best_bid=NAN,
        best_ask=NAN,
        spread=NAN,
        top_of_book_size_bid=NAN,
        top_of_book_size_ask=NAN,
        cumulative_depth_5bps=NAN,
        cumulative_depth_20bps=NAN,
        book_staleness_ms=-1,
        market_age_s=NAN,
        market_remaining_s=NAN,
        requested_qty_shares=float(order.requested_shares),
        requested_notional_usdc=float(order.requested_shares * order.decision_mid),
        worst_price_limit=float(order.worst_price_limit),
        filled_qty=0.0,
        residual_qty=float(order.requested_shares),
        fill_vwap=NAN,
        levels_consumed=0,
        classification="unfilled",
        sampled_latency_ms=float(latency_ms),
        latency_source=latency_source,
        p_bucket_used=p_bucket_used,
        half_spread_cost=NAN,
        book_walk_cost=NAN,
        latency_drift_cost=NAN,
        adverse_selection_1s=NAN,
        adverse_selection_5s=NAN,
        adverse_selection_30s=NAN,
        fees_cost=NAN,
        opportunity_cost_unfilled=NAN,
        total_IS=NAN,
        edge_at_signal=order.edge_at_signal,
        edge_at_fill=NAN,
        realised_pnl_at_close=NAN,
        paper_pnl_flat_0_5=NAN,
        diff_paper_minus_realised=NAN,
        thin_book_flag=False,
        price_extreme_flag=False,
        vol_regime=order.vol_regime,
        tunnel_age_bucket=order.tunnel_age_bucket,
        time_in_market_bucket="unk",
        mode=mode,
    )


def _with(rec: ExecutionRecord, **overrides) -> ExecutionRecord:
    from dataclasses import replace as _replace
    return _replace(rec, **overrides)
