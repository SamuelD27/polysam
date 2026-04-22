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
import json
import math
import random
import sqlite3
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
) -> Path:
    t0_ns, t1_ns = parse_window(window)
    if latency_profile not in LATENCY_PROFILES:
        raise ValueError(f"unknown --latency-profile {latency_profile!r}")
    if fee_category not in CATEGORIES:
        raise ValueError(f"unknown --fee-category {fee_category!r}")
    profile = LATENCY_PROFILES[latency_profile]
    category: FeeCategory = CATEGORIES[fee_category]

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

    store = load_snapshots_for_window(
        scrapes_db=scrapes,
        condition_ids=condition_ids,
        t0_ns=t0_ns,
        t1_ns=t1_ns,
        tick_size=tick_size,
        slug_by_condition=slug_by_condition,
        token_by_condition_side=token_by_condition_side,
    )

    run_id = uuid.uuid4().hex[:12]
    rng = random.Random(seed)
    executor = ReplayExecutor(
        books=store,
        latency_profile=profile,
        mode=mode,  # type: ignore[arg-type]
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
        records.append(rec)

    _write_parquet(records, out)
    _print_summary(records, skipped_no_market=skipped_no_market, skipped_no_token=skipped_no_token)
    return out


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


def _write_parquet(records: list[ExecutionRecord], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    if not records:
        # Write an empty parquet with the expected schema so downstream tools
        # don't trip over a missing file.
        _write_empty(out)
        return
    import pyarrow as pa
    import pyarrow.parquet as pq

    rows = [asdict(r) for r in records]
    fields = list(records[0].__dataclass_fields__.keys())
    table_data = {k: [r[k] for r in rows] for k in fields}
    table = pa.table(table_data)
    pq.write_table(table, out)


def _write_empty(out: Path) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    from active_bots.execution.replay_executor import ExecutionRecord as _Rec

    fields = list(_Rec.__dataclass_fields__.keys())
    table = pa.table({k: [] for k in fields})
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
) -> None:
    n = len(records)
    print(f"\n=== Replay summary ({n} rows) ===")
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
    p.add_argument("--scrapes", required=True, type=Path)
    p.add_argument("--out", required=True, type=Path)
    p.add_argument("--window", required=True, help="ISO..ISO e.g. 2026-04-22T01:10Z..2026-04-22T03:10Z")
    p.add_argument("--tick-size", required=True, help="One of 0.1 / 0.01 / 0.001 / 0.0001 (per spec §2.2)")
    p.add_argument("--latency-profile", default="sg_wg_prior", choices=sorted(LATENCY_PROFILES))
    p.add_argument("--fee-category", default="crypto", choices=sorted(CATEGORIES))
    p.add_argument("--mode", default="freeze_depleted", choices=["freeze_depleted", "snap_back", "both"])
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--asset-prefix", default="btc")
    args = p.parse_args(argv)
    if args.mode == "both":
        out_fd = args.out.with_suffix(".freeze_depleted.parquet")
        out_sb = args.out.with_suffix(".snap_back.parquet")
        run(
            events=args.events, scrapes=args.scrapes, out=out_fd, window=args.window,
            tick_size=Decimal(args.tick_size), latency_profile=args.latency_profile,
            fee_category=args.fee_category, mode="freeze_depleted", seed=args.seed,
            asset_prefix=args.asset_prefix,
        )
        run(
            events=args.events, scrapes=args.scrapes, out=out_sb, window=args.window,
            tick_size=Decimal(args.tick_size), latency_profile=args.latency_profile,
            fee_category=args.fee_category, mode="snap_back", seed=args.seed,
            asset_prefix=args.asset_prefix,
        )
    else:
        run(
            events=args.events, scrapes=args.scrapes, out=args.out, window=args.window,
            tick_size=Decimal(args.tick_size), latency_profile=args.latency_profile,
            fee_category=args.fee_category, mode=args.mode, seed=args.seed,
            asset_prefix=args.asset_prefix,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
