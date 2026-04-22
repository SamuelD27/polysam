"""Regime-conditional attribution table from a replay parquet.

Pure post-processing — reads the §7 parquet, groups by a small set of regime
tags, and emits a markdown table of median per-trade attribution in bps plus
trade counts. No plots, no HTML.
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path
from typing import Iterable

import pyarrow.parquet as pq

GROUP_COLS = ("thin_book_flag", "price_extreme_flag", "time_in_market_bucket")
METRIC_COLS = (
    "half_spread_cost",
    "latency_drift_cost",
    "book_walk_cost",
    "fees_cost",
    "total_IS",
    "diff_paper_minus_realised",
)


def _median(xs: list[float]) -> float:
    xs = sorted(x for x in xs if isinstance(x, float) and math.isfinite(x))
    if not xs:
        return float("nan")
    n = len(xs)
    return xs[n // 2] if n % 2 == 1 else 0.5 * (xs[n // 2 - 1] + xs[n // 2])


def _bucket_label(row: dict) -> str:
    thin = "thin" if row["thin_book_flag"] else "deep"
    extreme = "extreme" if row["price_extreme_flag"] else "mid"
    t = row["time_in_market_bucket"]
    return f"{thin}/{extreme}/{t}"


def attribute(parquet_path: Path) -> str:
    table = pq.read_table(str(parquet_path))
    cols = {name: table.column(name).to_pylist() for name in set(GROUP_COLS + METRIC_COLS + ("classification",))}
    n_rows = table.num_rows
    if n_rows == 0:
        return "_No rows in parquet._"

    by_bucket: dict[str, list[dict]] = {}
    for i in range(n_rows):
        row = {k: cols[k][i] for k in cols}
        if row["classification"] == "book_stale":
            continue
        label = _bucket_label(row)
        by_bucket.setdefault(label, []).append(row)

    if not by_bucket:
        return "_All rows classified book_stale; no attribution possible._"

    lines: list[str] = []
    header = ["bucket", "n"] + list(METRIC_COLS)
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "|".join(["---"] * len(header)) + "|")
    for label in sorted(by_bucket):
        rows = by_bucket[label]
        cells = [label, str(len(rows))]
        for m in METRIC_COLS:
            cells.append(f"{_median([r[m] for r in rows]):.2f}")
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="experiments.backtest.attribution")
    p.add_argument("--in", dest="inp", required=True, type=Path)
    args = p.parse_args(argv)
    print(attribute(args.inp))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
