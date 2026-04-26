"""Tests for the golden-trace stub (spec §6.3)."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from experiments.backtest.reconcile import (
    NoLiveFillsCaptured,
    count_live_fills,
    reconcile,
)


def test_iter_entry_fills_filters_by_strategy_window_predicate(tmp_path: Path) -> None:
    from experiments.backtest.reconcile import iter_entry_fills
    events = tmp_path / "events.jsonl"
    rows = [
        # In window, refined, predicate met → KEEP
        {"ts": 1.0, "type": "entry_filled", "strategy": "refined",
         "order_id": "0xA", "position": {"slug": "btc-updown-5m-1", "ack_ts": 1.001,
                                          "entry_price": 0.42, "size_shares": 24.0,
                                          "edge": 0.07}},
        # Out of window → DROP
        {"ts": 99.0, "type": "entry_filled", "strategy": "refined",
         "order_id": "0xB", "position": {"slug": "btc-updown-5m-2", "ack_ts": 99.001,
                                          "entry_price": 0.50, "size_shares": 20.0}},
        # Wrong strategy → DROP
        {"ts": 1.5, "type": "entry_filled", "strategy": "enhanced",
         "order_id": "0xC", "position": {"slug": "btc-updown-5m-3", "ack_ts": 1.501,
                                          "entry_price": 0.50, "size_shares": 20.0}},
        # Predicate fails (no order_id) → DROP
        {"ts": 1.7, "type": "entry_filled", "strategy": "refined",
         "position": {"slug": "btc-updown-5m-4", "ack_ts": 1.701,
                      "entry_price": 0.50, "size_shares": 20.0}},
        # Wrong asset prefix → DROP
        {"ts": 1.8, "type": "entry_filled", "strategy": "refined",
         "order_id": "0xE", "position": {"slug": "eth-updown-5m-1", "ack_ts": 1.801,
                                          "entry_price": 0.50, "size_shares": 20.0}},
        # Wrong type → DROP
        {"ts": 1.9, "type": "exit_filled", "strategy": "refined",
         "order_id": "0xF", "trade": {"slug": "btc-updown-5m-5", "ack_ts": 1.901,
                                       "exit_price": 0.55, "size_shares": 20.0}},
    ]
    events.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    out = list(iter_entry_fills(events, t0_ns=int(0.5 * 1e9),
                                 t1_ns=int(2.0 * 1e9),
                                 asset_prefix="btc",
                                 strategy_filter=("refined",)))
    assert len(out) == 1
    assert out[0]["order_id"] == "0xA"


def _write_manifest(scrapes_root: Path, session_id: str, mode: str,
                    launch_ns: int, stop_ns: int | None,
                    events_path: str, feed_dir: str) -> Path:
    d = scrapes_root / session_id
    d.mkdir(parents=True, exist_ok=True)
    m = d / "manifest.json"
    m.write_text(json.dumps({
        "session_id": session_id,
        "launch_ts_ns": launch_ns,
        "stop_ts_ns": stop_ns,
        "stop_ts_utc": None if stop_ns is None else "2026-04-26T07:00:00Z",
        "mode": mode,
        "events_jsonl_path": events_path,
        "scrape_canonical_dir": feed_dir,
    }))
    return m


def test_discover_session_latest_picks_most_recent_live_or_dryrun(tmp_path: Path) -> None:
    from experiments.backtest.reconcile import discover_session
    scrapes_root = tmp_path / "scrapes"
    _write_manifest(scrapes_root, "2026-04-24T07-10-19Z", "live_dryrun",
                    1_777_014_619_000_000_000, 1_777_200_000_000_000_000,
                    str(tmp_path / "events_old.jsonl"), str(tmp_path / "feed_old"))
    _write_manifest(scrapes_root, "2026-04-26T07-00-00Z", "live_dryrun",
                    1_777_180_800_000_000_000, None,
                    str(tmp_path / "events_new.jsonl"), str(tmp_path / "feed_new"))
    _write_manifest(scrapes_root, "2026-04-26T08-00-00Z", "paper",
                    1_777_184_400_000_000_000, None,
                    str(tmp_path / "events_paper.jsonl"), str(tmp_path / "feed_paper"))
    sess = discover_session("latest", scrapes_root=scrapes_root)
    assert sess["session_id"] == "2026-04-26T07-00-00Z"
    assert sess["mode"] == "live_dryrun"


def test_discover_session_explicit_id_loads_named_manifest(tmp_path: Path) -> None:
    from experiments.backtest.reconcile import discover_session
    scrapes_root = tmp_path / "scrapes"
    _write_manifest(scrapes_root, "S1", "live_dryrun",
                    1_777_000_000_000_000_000, 1_777_100_000_000_000_000,
                    "/x/events.jsonl", "/x/feed")
    sess = discover_session("S1", scrapes_root=scrapes_root)
    assert sess["session_id"] == "S1"
    assert sess["effective_stop_ns"] == 1_777_100_000_000_000_000


def test_discover_session_explicit_paper_id_rejected(tmp_path: Path) -> None:
    from experiments.backtest.reconcile import discover_session
    scrapes_root = tmp_path / "scrapes"
    _write_manifest(scrapes_root, "P1", "paper",
                    1_777_000_000_000_000_000, 1_777_100_000_000_000_000,
                    "/x/events.jsonl", "/x/feed")
    with pytest.raises(ValueError, match="reconcile only operates on"):
        discover_session("P1", scrapes_root=scrapes_root)


def test_discover_session_running_session_uses_now_for_stop(tmp_path: Path) -> None:
    from experiments.backtest.reconcile import discover_session
    scrapes_root = tmp_path / "scrapes"
    _write_manifest(scrapes_root, "S1", "live_dryrun",
                    1_777_000_000_000_000_000, None,
                    "/x/events.jsonl", "/x/feed")
    before = int(time.time() * 1e9)
    sess = discover_session("S1", scrapes_root=scrapes_root)
    after = int(time.time() * 1e9)
    assert sess["stop_ts_ns"] is None
    assert before <= sess["effective_stop_ns"] <= after


def _write_events(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def test_count_live_fills_zero_on_paper_only(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"
    _write_events(
        events,
        [
            {
                "ts": 1.0,
                "type": "entry_filled",
                "position": {
                    "slug": "btc-updown-5m-1",
                    "order_id": None,
                    "token_id": None,
                },
            },
            {"ts": 2.0, "type": "session_start"},
        ],
    )
    assert count_live_fills(events) == 0


def test_count_live_fills_requires_all_three_keys(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"
    _write_events(
        events,
        [
            {
                "ts": 1.0,
                "type": "entry_filled",
                "position": {"order_id": "abc", "ack_ts": 1.0},  # no fill_price
            },
            {
                "ts": 2.0,
                "type": "entry_filled",
                "position": {"order_id": "abc", "fill_price": 0.5},  # no ack_ts
            },
            {
                "ts": 3.0,
                "type": "entry_filled",
                "position": {"ack_ts": 2.0, "fill_price": 0.5},  # no order_id
            },
        ],
    )
    assert count_live_fills(events) == 0


def test_count_live_fills_accepts_top_level_fields(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"
    _write_events(
        events,
        [
            {
                "ts": 1.0,
                "type": "entry_filled",
                "order_id": "0xabc",
                "ack_ts": 1.00012,
                "fill_price": 0.48,
                "position": {"slug": "btc-updown-5m-1"},
            },
        ],
    )
    assert count_live_fills(events) == 1


def test_count_live_fills_accepts_filled_price_alias(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"
    _write_events(
        events,
        [
            {
                "ts": 1.0,
                "type": "entry_filled",
                "position": {
                    "order_id": "0xabc",
                    "ack_ts": 1.00012,
                    "filled_price": 0.48,  # alias
                },
            },
        ],
    )
    assert count_live_fills(events) == 1


def test_count_live_fills_accepts_entry_price_alias_for_live_fills(tmp_path: Path) -> None:
    # LiveExecutor.enter() persists the actual fill avg_price as 'entry_price';
    # the predicate must accept this. Paper mode lacks order_id/ack_ts so it
    # cannot accidentally slip through via this alias.
    events = tmp_path / "events.jsonl"
    _write_events(
        events,
        [
            {
                "ts": 1.0,
                "type": "entry_filled",
                "position": {
                    "order_id": "dry-run-1776910000001",
                    "ack_ts": 1.00035,
                    "entry_price": 0.48,  # LiveExecutor's fill_price alias
                },
            },
        ],
    )
    assert count_live_fills(events) == 1


def test_reconcile_raises_no_live_fills_on_paper(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"
    _write_events(
        events,
        [
            {
                "ts": 1.0,
                "type": "entry_filled",
                "position": {"order_id": None, "ack_ts": None},
            },
        ],
    )
    out = tmp_path / "golden.parquet"
    with pytest.raises(NoLiveFillsCaptured) as exc:
        reconcile(events_path=events, out=out, min_live_fills=100)
    msg = str(exc.value)
    # Exact predicate must be echoed so the operator sees the gate.
    assert "ack_ts NOT NULL" in msg
    assert "order_id NOT NULL" in msg
    assert "fill_price NOT NULL" in msg


def test_reconcile_under_min_fills_still_raises(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"
    _write_events(
        events,
        [
            {
                "ts": 1.0,
                "type": "entry_filled",
                "position": {
                    "order_id": "0x1",
                    "ack_ts": 1.00012,
                    "fill_price": 0.48,
                },
            },
        ],
    )
    out = tmp_path / "golden.parquet"
    # 1 live fill, default min=100 -> should still raise.
    with pytest.raises(NoLiveFillsCaptured) as exc:
        reconcile(events_path=events, out=out)
    assert "Found 1 live fills" in str(exc.value)


def test_fit_held_out_latency_returns_profile_with_30plus_samples(tmp_path: Path) -> None:
    from experiments.backtest.reconcile import fit_held_out_latency
    scrapes_root = tmp_path / "scrapes"
    events = tmp_path / "events_held_out.jsonl"
    rows = []
    for i in range(40):
        rows.append({
            "ts": 1000.0 + i,
            "type": "entry_filled",
            "strategy": "refined",
            "order_id": f"0x{i}",
            "position": {
                "slug": "btc-updown-5m-1",
                "entry_time": 1000.0 + i,
                "ack_ts": 1000.0 + i + 0.150,  # 150 ms gap
                "entry_price": 0.42,
            },
        })
    events.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    _write_manifest(scrapes_root, "HOLDOUT", "live_dryrun",
                    int(1000.0 * 1e9), int(2000.0 * 1e9),
                    str(events), str(tmp_path / "feed_unused"))
    profile, fit_source = fit_held_out_latency(["HOLDOUT"], scrapes_root=scrapes_root)
    assert profile is not None
    assert profile.source == "empirical"
    # 150 ms constant → all quantiles ≈ 150
    assert 145 < profile.p50_ms < 155
    assert fit_source["n_samples"] == 40
    assert fit_source["sessions"] == ["HOLDOUT"]


def test_fit_held_out_latency_returns_none_below_threshold(tmp_path: Path) -> None:
    from experiments.backtest.reconcile import fit_held_out_latency
    scrapes_root = tmp_path / "scrapes"
    events = tmp_path / "events_thin.jsonl"
    rows = [{
        "ts": 1000.0 + i, "type": "entry_filled", "strategy": "refined",
        "order_id": f"0x{i}", "position": {
            "slug": "btc-updown-5m-1", "entry_time": 1000.0 + i,
            "ack_ts": 1000.0 + i + 0.150, "entry_price": 0.42,
        },
    } for i in range(10)]  # only 10 rows
    events.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    _write_manifest(scrapes_root, "THIN", "live_dryrun",
                    int(1000.0 * 1e9), int(2000.0 * 1e9),
                    str(events), str(tmp_path / "feed_unused"))
    profile, fit_source = fit_held_out_latency(["THIN"], scrapes_root=scrapes_root)
    assert profile is None
    assert fit_source["n_samples"] == 10
    assert fit_source["reason"] == "below_threshold"


def test_fit_held_out_latency_no_sessions_returns_none(tmp_path: Path) -> None:
    from experiments.backtest.reconcile import fit_held_out_latency
    scrapes_root = tmp_path / "scrapes"
    scrapes_root.mkdir()
    profile, fit_source = fit_held_out_latency([], scrapes_root=scrapes_root)
    assert profile is None
    assert fit_source["n_samples"] == 0
    assert fit_source["reason"] == "no_sessions_provided"


def test_build_record_diff_bps_buy_taker_sign(tmp_path: Path) -> None:
    """live > replay → positive diff_bps (live overpaid). bps of decision_mid."""
    from experiments.backtest.reconcile import build_record
    from experiments.backtest.schema import GoldenTraceRecord
    from active_bots.execution.replay_executor import ExecutionRecord

    entry = {
        "ts": 1000.0, "strategy": "refined", "order_id": "0xA",
        "position": {
            "slug": "btc-updown-5m-1", "side": "Up", "entry_price": 0.55,
            "size_shares": 20.0, "ack_ts": 1000.150, "entry_time": 1000.000,
            "edge": 0.05, "fair_at_entry": 0.50, "market_at_entry": 0.50,
            "t_zero": 800,
        },
    }
    rec = ExecutionRecord(  # build a minimal stub
        trade_id="t", parent_order_id="", strategy_id="refined",
        backtest_run_id="r", t_signal_ns=int(1000.0 * 1e9),
        t_send_ns=0, t_ack_ns=0, t_first_fill_ns=0, t_last_fill_ns=0,
        token_id="", market_id="", side="BUY", tick_size=0.01,
        decision_mid=0.50, best_bid=0.49, best_ask=0.51, spread=0.02,
        top_of_book_size_bid=10.0, top_of_book_size_ask=10.0,
        cumulative_depth_5bps=0.0, cumulative_depth_20bps=0.0,
        book_staleness_ms=120, market_age_s=200.0, market_remaining_s=100.0,
        requested_qty_shares=20.0, requested_notional_usdc=10.0,
        worst_price_limit=0.99, filled_qty=20.0, residual_qty=0.0,
        fill_vwap=0.51, levels_consumed=1, classification="full",
        sampled_latency_ms=150.0, latency_source="empirical",
        p_bucket_used="base", half_spread_cost=20.0, book_walk_cost=0.0,
        latency_drift_cost=0.0, adverse_selection_1s=float("nan"),
        adverse_selection_5s=float("nan"), adverse_selection_30s=float("nan"),
        fees_cost=5.0, opportunity_cost_unfilled=float("nan"),
        total_IS=25.0, edge_at_signal=0.05, edge_at_fill=float("nan"),
        realised_pnl_at_close=float("nan"), paper_pnl_flat_0_5=float("nan"),
        diff_paper_minus_realised=float("nan"), thin_book_flag=False,
        price_extreme_flag=False, vol_regime="unk",
        tunnel_age_bucket="unk", time_in_market_bucket="mid_late",
        mode="freeze_depleted",
    )
    out = build_record(entry, rec, pass_name="empirical", mode_tag="live_dryrun")
    assert isinstance(out, GoldenTraceRecord)
    # live=0.55, replay=0.51, decision_mid=0.50 → (0.55-0.51)/0.50*1e4 = 800 bps
    assert abs(out.diff_bps - 800.0) < 1e-6
    # attribution_delta = (live - decision_mid)/dm*1e4 - total_IS = 1000 - 25 = 975
    assert abs(out.attribution_delta - 975.0) < 1e-6
    assert out.book_top_at_decision == 0.51   # best_ask for BUY taker
    assert out.t_decision_ns == int(1000.0 * 1e9)
    assert out.in_gate_window is True          # staleness 120 < 200, source empirical
    assert out.partition == "pre_2026-02-01"   # ack_ts 1000.150 s = epoch 1970, pre-2026
    assert out.live_fill_px == 0.55
    assert out.live_filled_qty == 20.0
    # ack_ts 1000.150 - entry_time 1000.000 = 0.150 s → 150 ms
    assert abs(out.live_latency_measured_ms - 150.0) < 1e-3
    assert out.live_ack_ts_ns == int(1000.150 * 1e9)
    assert out.mode_tag == "live_dryrun"
    assert out.strategy_name == "refined"
    assert out.edge_at_decision == 0.05
    assert out.fair_price_at_decision == 0.50
    # T+300 - market_age 200 = 100 s remaining
    assert out.time_remaining_at_decision_s == 100


def test_build_record_in_gate_window_predicate(tmp_path: Path) -> None:
    """Empirical pass + staleness < 200 → in_gate_window True; prior pass → False."""
    from experiments.backtest.reconcile import build_record
    from active_bots.execution.replay_executor import ExecutionRecord

    def make(staleness_ms: int, source: str) -> ExecutionRecord:
        return ExecutionRecord(
            trade_id="t", parent_order_id="", strategy_id="refined",
            backtest_run_id="r", t_signal_ns=int(1000.0 * 1e9),
            t_send_ns=0, t_ack_ns=0, t_first_fill_ns=0, t_last_fill_ns=0,
            token_id="", market_id="", side="BUY", tick_size=0.01,
            decision_mid=0.50, best_bid=0.49, best_ask=0.51, spread=0.02,
            top_of_book_size_bid=10.0, top_of_book_size_ask=10.0,
            cumulative_depth_5bps=0.0, cumulative_depth_20bps=0.0,
            book_staleness_ms=staleness_ms, market_age_s=200.0,
            market_remaining_s=100.0, requested_qty_shares=20.0,
            requested_notional_usdc=10.0, worst_price_limit=0.99,
            filled_qty=20.0, residual_qty=0.0, fill_vwap=0.51,
            levels_consumed=1, classification="full",
            sampled_latency_ms=150.0, latency_source=source,
            p_bucket_used="base", half_spread_cost=20.0, book_walk_cost=0.0,
            latency_drift_cost=0.0, adverse_selection_1s=float("nan"),
            adverse_selection_5s=float("nan"), adverse_selection_30s=float("nan"),
            fees_cost=5.0, opportunity_cost_unfilled=float("nan"),
            total_IS=25.0, edge_at_signal=0.05, edge_at_fill=float("nan"),
            realised_pnl_at_close=float("nan"), paper_pnl_flat_0_5=float("nan"),
            diff_paper_minus_realised=float("nan"), thin_book_flag=False,
            price_extreme_flag=False, vol_regime="unk",
            tunnel_age_bucket="unk", time_in_market_bucket="mid_late",
            mode="freeze_depleted",
        )
    entry = {"ts": 1000.0, "strategy": "refined", "order_id": "0xA",
             "position": {"slug": "btc-updown-5m-1", "side": "Up",
                          "entry_price": 0.51, "size_shares": 20.0,
                          "ack_ts": 1000.15, "entry_time": 1000.0, "edge": 0.05,
                          "fair_at_entry": 0.5, "market_at_entry": 0.5,
                          "t_zero": 800}}
    assert build_record(entry, make(150, "empirical"), "empirical", "live").in_gate_window is True
    assert build_record(entry, make(150, "prior"),     "prior",     "live").in_gate_window is False
    assert build_record(entry, make(250, "empirical"), "empirical", "live").in_gate_window is False


def test_build_record_partition_split_at_2026_02_01(tmp_path: Path) -> None:
    from experiments.backtest.reconcile import build_record
    from experiments.backtest.schema import PARTITION_BOUNDARY_NS
    from active_bots.execution.replay_executor import ExecutionRecord

    def make_rec(t_signal_ns: int) -> ExecutionRecord:
        return ExecutionRecord(
            trade_id="t", parent_order_id="", strategy_id="refined",
            backtest_run_id="r", t_signal_ns=t_signal_ns,
            t_send_ns=0, t_ack_ns=0, t_first_fill_ns=0, t_last_fill_ns=0,
            token_id="", market_id="", side="BUY", tick_size=0.01,
            decision_mid=0.50, best_bid=0.49, best_ask=0.51, spread=0.02,
            top_of_book_size_bid=10.0, top_of_book_size_ask=10.0,
            cumulative_depth_5bps=0.0, cumulative_depth_20bps=0.0,
            book_staleness_ms=120, market_age_s=200.0, market_remaining_s=100.0,
            requested_qty_shares=20.0, requested_notional_usdc=10.0,
            worst_price_limit=0.99, filled_qty=20.0, residual_qty=0.0,
            fill_vwap=0.51, levels_consumed=1, classification="full",
            sampled_latency_ms=150.0, latency_source="empirical",
            p_bucket_used="base", half_spread_cost=20.0, book_walk_cost=0.0,
            latency_drift_cost=0.0, adverse_selection_1s=float("nan"),
            adverse_selection_5s=float("nan"), adverse_selection_30s=float("nan"),
            fees_cost=5.0, opportunity_cost_unfilled=float("nan"),
            total_IS=25.0, edge_at_signal=0.05, edge_at_fill=float("nan"),
            realised_pnl_at_close=float("nan"), paper_pnl_flat_0_5=float("nan"),
            diff_paper_minus_realised=float("nan"), thin_book_flag=False,
            price_extreme_flag=False, vol_regime="unk",
            tunnel_age_bucket="unk", time_in_market_bucket="mid_late",
            mode="freeze_depleted",
        )
    pre_ack = (PARTITION_BOUNDARY_NS - 86_400_000_000_000) / 1e9   # 1 day before
    post_ack = (PARTITION_BOUNDARY_NS + 86_400_000_000_000) / 1e9  # 1 day after
    entry_pre = {"ts": pre_ack, "strategy": "refined", "order_id": "0xA",
                 "position": {"slug": "btc-x", "side": "Up", "entry_price": 0.51,
                              "size_shares": 20.0, "ack_ts": pre_ack,
                              "entry_time": pre_ack - 0.150,
                              "edge": 0.05, "fair_at_entry": 0.5,
                              "market_at_entry": 0.5, "t_zero": 0}}
    entry_post = {**entry_pre, "ts": post_ack,
                  "position": {**entry_pre["position"], "ack_ts": post_ack,
                               "entry_time": post_ack - 0.150}}
    assert build_record(entry_pre, make_rec(int(pre_ack * 1e9)),
                        "empirical", "live").partition == "pre_2026-02-01"
    assert build_record(entry_post, make_rec(int(post_ack * 1e9)),
                        "empirical", "live").partition == "post_2026-02-01"


def _mock_record_for_gate(diff_bps: float, in_window: bool, classification: str,
                          partition: str = "post_2026-02-01"):
    """Lightweight fake exposing only the fields evaluate_gate reads."""
    from dataclasses import dataclass

    @dataclass
    class _R:
        diff_bps: float
        in_gate_window: bool
        partition: str
        replay: object
    @dataclass
    class _Inner:
        classification: str
    return _R(diff_bps=diff_bps, in_gate_window=in_window,
              partition=partition, replay=_Inner(classification=classification))


def test_evaluate_gate_pass_when_within_thresholds() -> None:
    from experiments.backtest.reconcile import evaluate_gate
    rows = [
        _mock_record_for_gate(0.5, True, "full"),
        _mock_record_for_gate(-1.0, True, "full"),
        _mock_record_for_gate(8.0, True, "partial"),  # p95 contributor
    ] * 50  # 150 rows total
    out = evaluate_gate(rows, partition="post_2026-02-01")
    assert out["status"] == "PASS"
    assert out["n_rows"] == 150
    assert abs(out["median_abs_diff_bps"] - 1.0) < 1e-6  # median of |0.5|,|-1|,|8|
    assert out["p95_abs_diff_bps"] <= 8.0


def test_evaluate_gate_fail_when_p95_exceeds() -> None:
    from experiments.backtest.reconcile import evaluate_gate
    rows = [_mock_record_for_gate(0.5, True, "full")] * 95
    rows += [_mock_record_for_gate(15.0, True, "full")] * 5  # 5% at 15 bps
    out = evaluate_gate(rows, partition="post_2026-02-01")
    assert out["status"] == "FAIL"
    assert out["p95_abs_diff_bps"] >= 10.0


def test_evaluate_gate_fail_when_median_exceeds() -> None:
    from experiments.backtest.reconcile import evaluate_gate
    rows = [_mock_record_for_gate(3.0, True, "full")] * 100
    out = evaluate_gate(rows, partition="post_2026-02-01")
    assert out["status"] == "FAIL"
    assert abs(out["median_abs_diff_bps"] - 3.0) < 1e-6


def test_evaluate_gate_na_when_no_in_window_rows() -> None:
    from experiments.backtest.reconcile import evaluate_gate
    rows = [_mock_record_for_gate(0.5, False, "full"),
            _mock_record_for_gate(0.5, True, "unfilled")]  # excluded class
    out = evaluate_gate(rows, partition="post_2026-02-01")
    assert out["status"] == "n/a"
    assert out["n_rows"] == 0


def test_evaluate_gate_partition_isolation() -> None:
    """Records in OTHER partitions must NOT influence this gate."""
    from experiments.backtest.reconcile import evaluate_gate
    rows_post = [_mock_record_for_gate(0.5, True, "full",
                                       partition="post_2026-02-01")] * 100
    rows_pre = [_mock_record_for_gate(50.0, True, "full",
                                      partition="pre_2026-02-01")] * 100
    out = evaluate_gate(rows_post + rows_pre, partition="post_2026-02-01")
    assert out["status"] == "PASS"
    assert out["n_rows"] == 100


def test_evaluate_gate_na_with_eligible_but_all_nan_reports_count() -> None:
    """All eligible rows have NaN diff_bps → status n/a but n_rows reflects count."""
    from experiments.backtest.reconcile import evaluate_gate
    rows = [_mock_record_for_gate(float("nan"), True, "full")] * 7
    out = evaluate_gate(rows, partition="post_2026-02-01")
    assert out["status"] == "n/a"
    assert out["n_rows"] == 7   # not 0 — eligible count, just no usable diff_bps
    assert out["median_abs_diff_bps"] is None
    assert out["p95_abs_diff_bps"] is None


def test_write_parquet_round_trip_includes_projection_keys(tmp_path: Path) -> None:
    """Written parquet must contain every PROJECTION_KEYS_6_3 column
    plus the live overlay columns."""
    from experiments.backtest.reconcile import write_parquet
    from experiments.backtest.schema import (
        GoldenTraceRecord, PROJECTION_KEYS_6_3,
    )
    from active_bots.execution.replay_executor import ExecutionRecord

    rec_inner = ExecutionRecord(
        trade_id="t", parent_order_id="", strategy_id="refined",
        backtest_run_id="r", t_signal_ns=int(1000.0 * 1e9),
        t_send_ns=0, t_ack_ns=0, t_first_fill_ns=0, t_last_fill_ns=0,
        token_id="tok", market_id="cid", side="BUY", tick_size=0.01,
        decision_mid=0.50, best_bid=0.49, best_ask=0.51, spread=0.02,
        top_of_book_size_bid=10.0, top_of_book_size_ask=10.0,
        cumulative_depth_5bps=0.0, cumulative_depth_20bps=0.0,
        book_staleness_ms=120, market_age_s=200.0, market_remaining_s=100.0,
        requested_qty_shares=20.0, requested_notional_usdc=10.0,
        worst_price_limit=0.99, filled_qty=20.0, residual_qty=0.0,
        fill_vwap=0.51, levels_consumed=1, classification="full",
        sampled_latency_ms=150.0, latency_source="empirical",
        p_bucket_used="base", half_spread_cost=20.0, book_walk_cost=0.0,
        latency_drift_cost=0.0, adverse_selection_1s=float("nan"),
        adverse_selection_5s=float("nan"), adverse_selection_30s=float("nan"),
        fees_cost=5.0, opportunity_cost_unfilled=float("nan"),
        total_IS=25.0, edge_at_signal=0.05, edge_at_fill=float("nan"),
        realised_pnl_at_close=float("nan"), paper_pnl_flat_0_5=float("nan"),
        diff_paper_minus_realised=float("nan"), thin_book_flag=False,
        price_extreme_flag=False, vol_regime="unk",
        tunnel_age_bucket="unk", time_in_market_bucket="mid_late",
        mode="freeze_depleted",
    )
    rec = GoldenTraceRecord(
        replay=rec_inner, live_order_id="0xA",
        live_ack_ts_ns=int(1000.150 * 1e9), live_fill_px=0.55,
        live_filled_qty=20.0, live_latency_measured_ms=150.0,
        mode_tag="live_dryrun", t_decision_ns=int(1000.0 * 1e9),
        diff_bps=800.0, attribution_delta=975.0, book_top_at_decision=0.51,
        strategy_name="refined", edge_at_decision=0.05,
        fair_price_at_decision=0.50, time_remaining_at_decision_s=100,
        in_gate_window=True, partition="post_2026-02-01",
    )
    out = tmp_path / "golden.parquet"
    write_parquet([rec], out)
    import pyarrow.parquet as pq
    cols = set(pq.read_table(out).column_names)
    # Every projection key must be present after flatten
    for k in PROJECTION_KEYS_6_3:
        assert k in cols, f"missing projection col: {k}"
    # Overlay + derivations
    assert "live_order_id" in cols
    assert "live_ack_ts_ns" in cols
    assert "live_fill_px" in cols
    assert "mode_tag" in cols
    assert "diff_bps" in cols
    assert "in_gate_window" in cols
    assert "partition" in cols
    # R3 trade context
    assert "strategy_name" in cols
    assert "edge_at_decision" in cols


def test_write_manifest_emits_required_top_level_keys(tmp_path: Path) -> None:
    from experiments.backtest.reconcile import write_manifest
    out_parquet = tmp_path / "golden.parquet"
    out_parquet.touch()
    write_manifest(
        out_parquet,
        session_id="S1",
        session_mode="live_dryrun",
        asset_prefix="btc",
        strategy_filter=("refined",),
        latency_fit_source={"sessions": [], "n_samples": 0,
                            "fit_window_start_ns": 0, "fit_window_end_ns": 0,
                            "reason": "no_sessions_provided"},
        rows_emitted_total=0,
        rows_emitted_by_partition={"pre_2026-02-01": 0, "post_2026-02-01": 0},
        rows_emitted_by_pass={"empirical": 0, "prior": 0},
        gate={"pre_2026-02-01": {"status": "n/a"},
              "post_2026-02-01": {"status": "n/a"}},
        refusals=["..."],
    )
    m = json.loads((tmp_path / "golden.parquet.manifest.json").read_text())
    assert m["kind"] == "golden_trace"
    assert m["session_id"] == "S1"
    assert m["session_mode"] == "live_dryrun"
    assert m["scope"] == "entries_only"
    assert m["strategy_filter"] == ["refined"]
    assert "latency_fit_source" in m
    assert "gate" in m
    assert "refusals" in m


def test_reconcile_raises_not_implemented_when_gate_met(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"
    rows = [
        {
            "ts": float(i),
            "type": "entry_filled",
            "position": {
                "order_id": f"0x{i}",
                "ack_ts": i + 0.001,
                "fill_price": 0.5,
            },
        }
        for i in range(5)
    ]
    _write_events(events, rows)
    out = tmp_path / "golden.parquet"
    # With gate lowered to 5, we have enough — reconcile body is unimplemented.
    with pytest.raises(NotImplementedError):
        reconcile(events_path=events, out=out, min_live_fills=5)
