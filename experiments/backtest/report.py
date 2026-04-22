"""Single-page markdown report from a replay parquet.

Emits the headline paper-vs-book-walked ROI comparison plus a bps-level
decomposition and the regime-conditional attribution table.

Usage:

    python -m experiments.backtest.report --in runs/dryrun_01.parquet
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path

import pyarrow.parquet as pq

from .attribution import attribute


def _finite(xs):
    return [x for x in xs if isinstance(x, float) and math.isfinite(x)]


def _median(xs):
    xs = sorted(_finite(xs))
    if not xs:
        return float("nan")
    n = len(xs)
    return xs[n // 2] if n % 2 == 1 else 0.5 * (xs[n // 2 - 1] + xs[n // 2])


def _sum(xs):
    return sum(_finite(xs))


def _fmt_bps(x: float) -> str:
    if not math.isfinite(x):
        return "n/a"
    return f"{x:+.1f} bps"


def _fmt_pct(x: float) -> str:
    if not math.isfinite(x):
        return "n/a"
    return f"{x * 100:+.2f}%"


def report(parquet_path: Path) -> str:
    table = pq.read_table(str(parquet_path))
    n = table.num_rows
    lines: list[str] = []
    lines.append(f"# Book-walked replay report — {parquet_path.name}")
    lines.append("")
    lines.append(f"Rows: **{n}**")
    if n == 0:
        lines.append("")
        lines.append("_Empty parquet. Nothing to report._")
        return "\n".join(lines)

    cols = {c: table.column(c).to_pylist() for c in table.column_names}

    # Classification breakdown.
    cls = cols.get("classification") or []
    counts: dict[str, int] = {}
    for c in cls:
        counts[c] = counts.get(c, 0) + 1
    lines.append(
        "Classifications: "
        + ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    )

    # Book staleness.
    stale = [s for s in cols.get("book_staleness_ms", []) if s >= 0]
    if stale:
        stale_s = sorted(stale)
        med = stale_s[len(stale_s) // 2]
        p95 = stale_s[min(len(stale_s) - 1, int(len(stale_s) * 0.95))]
        lines.append(f"book_staleness_ms: median={med} p95={p95}")
    lines.append("")

    # ROI comparison — paired rows only (both paper and realised present).
    paper = cols.get("paper_pnl_flat_0_5", [])
    realised = cols.get("realised_pnl_at_close", [])
    notional = cols.get("requested_notional_usdc", [])
    paired_paper = []
    paired_realised = []
    paired_notional = []
    for pp, rr, nn in zip(paper, realised, notional):
        if (
            isinstance(pp, float) and math.isfinite(pp)
            and isinstance(rr, float) and math.isfinite(rr)
            and isinstance(nn, float) and math.isfinite(nn) and nn > 0
        ):
            paired_paper.append(pp)
            paired_realised.append(rr)
            paired_notional.append(nn)
    lines.append(f"Paired (entry + exit) rows: **{len(paired_paper)}**")
    if paired_paper:
        gross = sum(paired_notional)
        paper_roi = sum(paired_paper) / gross
        realised_roi = sum(paired_realised) / gross
        haircut = paper_roi - realised_roi
        lines.append(
            f"Paper flat-0.5 ROI: **{_fmt_pct(paper_roi)}** vs "
            f"realised ROI: **{_fmt_pct(realised_roi)}** "
            f"(haircut: **{_fmt_pct(haircut)}**)"
        )

    # Decomposition.
    good_idx = [i for i, c in enumerate(cls) if c in ("full", "partial")]

    def _col(name):
        return [cols[name][i] for i in good_idx] if name in cols else []

    med_hs = _median(_col("half_spread_cost"))
    med_ld = _median(_col("latency_drift_cost"))
    med_bw = _median(_col("book_walk_cost"))
    med_fee = _median(_col("fees_cost"))
    med_total = _median(_col("total_IS"))
    med_diff = _median(_col("diff_paper_minus_realised"))
    lines.append("")
    lines.append("## Median per-trade attribution (bps of notional)")
    lines.append("")
    lines.append(f"- half_spread_cost:        {_fmt_bps(med_hs)}")
    lines.append(f"- latency_drift_cost:      {_fmt_bps(med_ld)}")
    lines.append(f"- book_walk_cost:          {_fmt_bps(med_bw)}")
    lines.append(f"- fees_cost:               {_fmt_bps(med_fee)}")
    lines.append(f"- total_IS (sum):          {_fmt_bps(med_total)}")
    lines.append(f"- diff_paper_minus_real:   {_fmt_bps(med_diff)}")

    # Regime table.
    lines.append("")
    lines.append("## Regime-conditional attribution")
    lines.append("")
    lines.append(attribute(parquet_path))

    # Narrative bullet.
    lines.append("")
    lines.append("## Headline")
    lines.append("")
    if paired_paper:
        headline = (
            f"Paper flat-0.5 ROI {_fmt_pct(paper_roi)} vs "
            f"book-walked-equivalent ROI {_fmt_pct(realised_roi)}, "
            f"haircut {_fmt_pct(haircut)} — decomposed as "
            f"{_fmt_bps(med_hs)} spread, {_fmt_bps(med_bw)} walk, "
            f"{_fmt_bps(med_ld)} latency, {_fmt_bps(med_fee)} fees."
        )
    else:
        headline = "No paired entry/exit rows; cannot compute haircut yet."
    lines.append(headline)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="experiments.backtest.report")
    p.add_argument("--in", dest="inp", required=True, type=Path)
    args = p.parse_args(argv)
    print(report(args.inp))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
