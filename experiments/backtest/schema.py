"""Schema declarations for backtest output artifacts.

Source of truth for the on-disk parquet column layout produced by
``experiments/backtest/reconcile.py``. Per spec §6.3, the golden-trace
parquet records — for every captured live fill — the joined live-side
/ replay-side / derivation columns required to score the
``|median diff_bps| < 2`` and ``p95 diff_bps < 10`` acceptance gate.

Scope of this commit: ENTRIES ONLY. SELL-side reconciliation (the leg
where the slippage analysis on N=39 found 2.36× book-walk asymmetry
in adaptive_sl) is the immediate followup before any real LIVE_MODE
capture — see TODO at the top of ``reconcile.py``.

The dataclass declarations embed ``ExecutionRecord`` for readability;
``dataclasses.asdict(rec)`` flattens the embed at write time so the
on-disk parquet exposes ExecutionRecord's full §7 schema (~40 cols)
inline alongside the live-fill overlay, derivations, trade context,
and gate flags.

Implementation logic (events.jsonl join, dual-pass replay, gate
evaluator, manifest writer) lives in ``reconcile.py``. Nothing here
performs IO or computation — these are declarations only.

Spec references throughout are to ``docs/book_walked_replay_backtester_spec.md``.
"""

from __future__ import annotations

from dataclasses import dataclass

from active_bots.execution.replay_executor import ExecutionRecord


# Partition boundary per spec §8.2 footnote: Polymarket removed the
# 500 ms taker delay on 2026-02-01. Pre/post windows have different
# effective latency distributions and MUST NOT be pooled in any
# replay or gate calculation. Stamped on every GoldenTraceRecord as
# ``partition`` so a downstream reader can split before aggregating.
PARTITION_BOUNDARY_NS: int = 1_769_904_000_000_000_000  # 2026-02-01T00:00:00Z


@dataclass(frozen=True)
class GoldenTraceRecord:
    """One row of ``golden_trace.parquet`` — joins one captured live
    fill to its book-walked replay.

    Field groups:
      replay         — the §7 ``ExecutionRecord`` from ReplayExecutor
                       (embedded; flattened at parquet-write time)
      live overlay   — from events.jsonl ``entry_filled`` rows (per-fill)
      derivations    — computed by reconcile.py, named per spec §6.3
      trade context  — strategy + decision identifiers, load-bearing
                       for R3 regime decomposition
      gate inputs    — flags consumed by the §6.3 acceptance evaluator

    The 11-column §6.3 view is exposed as a projection over the
    flattened parquet; see ``PROJECTION_KEYS_6_3`` below.
    """

    # ── Replay side: §7 ExecutionRecord embed ─────────────────────────
    # ``replay`` carries every column from ExecutionRecord verbatim —
    # identifiers (trade_id, parent_order_id, strategy_id, backtest_run_id),
    # timestamps (t_signal_ns through t_last_fill_ns), decision context
    # (token_id, side, tick_size, decision_mid, best_bid/ask, spread,
    # top_of_book sizes, cumulative_depth, book_staleness_ms,
    # market_age/remaining), request/fill (requested_qty_shares,
    # worst_price_limit, filled_qty, residual_qty, fill_vwap,
    # levels_consumed, classification), latency (sampled_latency_ms,
    # latency_source, p_bucket_used), attribution bps (half_spread_cost,
    # book_walk_cost, latency_drift_cost, adverse_selection_*, fees_cost,
    # opportunity_cost_unfilled, total_IS), PnL (edge_at_signal,
    # edge_at_fill, realised_pnl_at_close, paper_pnl_flat_0_5,
    # diff_paper_minus_realised), regime tags (thin_book_flag,
    # price_extreme_flag, vol_regime, tunnel_age_bucket,
    # time_in_market_bucket), mode.
    # See active_bots/execution/replay_executor.py:104-168 for the
    # canonical declaration.
    replay: ExecutionRecord

    # ── Live-fill overlay (events.jsonl entry_filled, R2.1 predicate) ─
    # CLOB orderID for real LIVE_MODE; "dry-run-{ms}" for live_dryrun.
    # Required by the predicate in reconcile.PREDICATE_SQL.
    live_order_id: str
    # Daemon-stamped ack moment (R2.1 ``ack_ts``). Nanoseconds for
    # parquet uniformity with replay.t_signal_ns; daemon writes seconds
    # so reconcile.py multiplies × 1e9 at ingest.
    live_ack_ts_ns: int
    # Actual avg fill price. LiveExecutor persists this as
    # ``entry_price`` (NOT the strategy's pre-call estimate); reconcile
    # reads via the ``entry_price`` alias documented in
    # reconcile._matches_live_predicate.
    live_fill_px: float
    # Actual shares filled — equals position.size_shares for entries.
    live_filled_qty: float
    # Real per-fill latency: (live_ack_ts - decision_ts) × 1000.
    # Distinct from replay.sampled_latency_ms which is a *draw* from a
    # latency profile. The §6.3 column ``live_latency_measured`` maps
    # here; ``paper_latency_sample`` maps to replay.sampled_latency_ms.
    live_latency_measured_ms: float
    # "live" (real LiveExecutor fill) | "live_dryrun" (DryRunExecutor
    # fill — paper math wearing live metadata). Drives the report.py
    # refusal logic: when mode_tag != "live", the headline must say
    # "PLUMBING-VALIDATION ONLY — real paper-vs-live haircut not
    # measured". Same pattern as the existing staleness_policy refusal.
    mode_tag: str

    # ── Reconcile derivations (named verbatim per §6.3 column list) ───
    # Spec §6.3 names these as the 11 per-trade columns. Some are
    # aliases over replay/live fields, kept as explicit columns so the
    # §6.3 projection is a pure column-select with no rename logic.
    #
    # ``t_decision`` per spec → t_decision_ns (alias of replay.t_signal_ns)
    t_decision_ns: int
    # diff_bps = sign × (live_fill_px − replay.fill_vwap) / decision_mid × 10_000
    # Sign convention: BUY → +1 (a higher live price than replay means
    # live overpaid). Spec §6.3 acceptance gate is over |diff_bps|.
    diff_bps: float
    # attribution_delta = (live_fill_px − decision_mid) bps − replay.total_IS
    # Residual the harness's per-component attribution (half_spread +
    # book_walk + latency_drift + fees) cannot explain. Should be ~0
    # when the harness model is well-calibrated; non-zero magnitude
    # points to missing attribution components (adverse selection,
    # fee mis-categorisation, etc).
    attribution_delta: float
    # ``book_top_at_decision`` per spec → best opposite at decision time:
    # best_ask for a BUY taker (entries — always BUY in this commit),
    # best_bid for a SELL taker (added in the SELL-side followup).
    # Explicit column for §6.3 projection rather than computed view.
    book_top_at_decision: float

    # ── Trade context (R3 regime decomposition; not §7 regime tags) ───
    # First-class trade identifiers lifted from events.jsonl
    # entry_filled without new instrumentation. R3 will groupby on
    # these to produce haircut-by-strategy / by-edge-bucket /
    # by-time-in-market tables.
    strategy_name: str           # "refined" | "enhanced" | "base"
    edge_at_decision: float      # strategy's edge claim at signal time (|fair − market|)
    fair_price_at_decision: float  # model fair price Phi(d2) at signal time
    time_remaining_at_decision_s: int  # seconds until T+300 resolution

    # ── Gate inputs (§6.3 acceptance-gate evaluator) ──────────────────
    # in_gate_window = (replay.book_staleness_ms < 200) AND
    #                  (replay.latency_source == "empirical")
    # The conjunction is verbatim per spec §6.3: the |median diff_bps|<2
    # / p95<10 acceptance gate is *conditional* on this flag. Rows with
    # in_gate_window=False contribute to the parquet for inspection but
    # are excluded from the gate calculation.
    in_gate_window: bool
    # "pre_2026-02-01" | "post_2026-02-01" per spec §8.2 footnote
    # (taker-delay change). Determined by the live_ack_ts_ns position
    # relative to PARTITION_BOUNDARY_NS above. Partitions MUST NOT be
    # pooled by the gate evaluator.
    partition: str


# Spec §6.3 names 11 per-trade columns as the canonical acceptance-
# summary subset. After ``dataclasses.asdict`` flattens the embed,
# downstream gate code (and report.py) projects exactly these to
# print the per-partition |median diff_bps| / p95 numbers.
#
# Names map between spec text and on-disk columns:
#   spec "t_decision"             → t_decision_ns
#   spec "live_fill_px"           → live_fill_px
#   spec "paper_fill_px"          → fill_vwap (from embedded replay)
#   spec "diff_bps"               → diff_bps
#   spec "live_filled_qty"        → live_filled_qty
#   spec "paper_filled_qty"       → filled_qty (from embedded replay)
#   spec "book_top_at_decision"   → book_top_at_decision
#   spec "book_staleness_ms"      → book_staleness_ms (from embedded replay)
#   spec "paper_latency_sample"   → sampled_latency_ms (from embedded replay)
#   spec "live_latency_measured"  → live_latency_measured_ms
#   spec "attribution_delta"      → attribution_delta
PROJECTION_KEYS_6_3: tuple[str, ...] = (
    "t_decision_ns",
    "live_fill_px",
    "fill_vwap",
    "diff_bps",
    "live_filled_qty",
    "filled_qty",
    "book_top_at_decision",
    "book_staleness_ms",
    "sampled_latency_ms",
    "live_latency_measured_ms",
    "attribution_delta",
)


__all__ = [
    "PARTITION_BOUNDARY_NS",
    "PROJECTION_KEYS_6_3",
    "GoldenTraceRecord",
]
