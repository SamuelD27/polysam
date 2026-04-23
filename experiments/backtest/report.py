"""Single-page markdown report from a replay parquet.

Emits (in strict mode) the paper-vs-book-walked ROI comparison plus bps-
level decomposition and a regime-conditional attribution table.

When the input parquet's ``staleness_policy`` column contains anything
other than ``strict`` on any row, the paper-vs-realistic headline is
SUPPRESSED (spec §8.1.2 footgun). Row counts, classification breakdown,
attribution-sum-to-total_IS invariant check, and manifest echo still
print — they are what plumbing smoke tests need.

Usage:

    python -m experiments.backtest.report --in runs/dryrun_01.parquet
"""

from __future__ import annotations

import argparse
import json
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


def _fmt_bps(x: float) -> str:
    if not math.isfinite(x):
        return "n/a"
    return f"{x:+.1f} bps"


def _fmt_pct(x: float) -> str:
    if not math.isfinite(x):
        return "n/a"
    return f"{x * 100:+.2f}%"


def _read_staleness_policy(table: pq.Table) -> str:
    """Return 'strict' if every row is 'strict', otherwise the first non-strict
    value found. Returns 'missing' if the column is absent from the parquet
    (legacy file from before R1.6). Legacy is treated as non-strict for safety."""
    names = set(table.column_names)
    if "staleness_policy" not in names:
        return "missing"
    vals = table.column("staleness_policy").to_pylist()
    if not vals:
        return "strict"
    uniq = set(vals)
    if uniq == {"strict"}:
        return "strict"
    # return the first non-strict value for readability
    for v in vals:
        if v != "strict":
            return str(v)
    return "strict"


def _sum_invariant_check(cols: dict, idxs: list[int]) -> tuple[int, int, int]:
    """Return (checked, matched, nan_skipped) for rows where total_IS should
    equal sum(half_spread + book_walk + latency_drift + fees) within 1e-9."""
    checked = 0
    matched = 0
    skipped = 0
    for i in idxs:
        parts = [
            cols.get("half_spread_cost", [])[i],
            cols.get("book_walk_cost", [])[i],
            cols.get("latency_drift_cost", [])[i],
            cols.get("fees_cost", [])[i],
        ]
        total = cols.get("total_IS", [])[i]
        if any(p is None or (isinstance(p, float) and math.isnan(p)) for p in parts):
            skipped += 1
            continue
        if total is None or (isinstance(total, float) and math.isnan(total)):
            skipped += 1
            continue
        checked += 1
        s = sum(float(p) for p in parts)
        if abs(float(total) - s) < 1e-9:
            matched += 1
    return checked, matched, skipped


def _manifest_for(parquet_path: Path) -> dict | None:
    candidate = parquet_path.with_suffix(parquet_path.suffix + ".manifest.json")
    if not candidate.exists():
        return None
    try:
        return json.loads(candidate.read_text())
    except (OSError, ValueError):
        return None


def report(parquet_path: Path) -> str:
    table = pq.read_table(str(parquet_path))
    n = table.num_rows
    policy = _read_staleness_policy(table)
    strict = (policy == "strict")

    lines: list[str] = []
    lines.append(f"# Book-walked replay report — {parquet_path.name}")
    lines.append("")
    manifest = _manifest_for(parquet_path)
    if manifest is not None:
        lat = manifest.get("latency", {})
        lines.append(
            f"run_id: `{manifest.get('run_id')}`  "
            f"git_sha: `{manifest.get('git_sha', '')[:12]}`  "
            f"input_format: `{manifest.get('input_format')}`  "
            f"mode: `{manifest.get('mode')}`"
        )
        lines.append(
            f"window: `{manifest.get('window')}`  "
            f"tick_size: `{manifest.get('tick_size')}`  "
            f"latency: `{lat.get('name')}` "
            f"(p50={lat.get('p50_ms')}ms, p95={lat.get('p95_ms')}ms, "
            f"p99={lat.get('p99_ms')}ms, p999={lat.get('p999_ms')}ms, "
            f"src={lat.get('source')})"
        )
        lines.append(
            f"fee: `{manifest.get('fee_schedule_version')}` "
            f"({manifest.get('fee_category')})  "
            f"staleness: `{manifest.get('staleness_policy')}` "
            f"(hard={manifest.get('staleness_hard_ms')}ms, "
            f"soft={manifest.get('staleness_soft_ms')}ms)"
        )
        lines.append("")
    lines.append(f"Rows: **{n}**  staleness_policy: **`{policy}`**")
    if n == 0:
        lines.append("")
        lines.append("_Empty parquet. Nothing to report._")
        return "\n".join(lines)

    cols = {c: table.column(c).to_pylist() for c in table.column_names}
    cls = cols.get("classification") or []

    # Classification breakdown.
    counts: dict[str, int] = {}
    for c in cls:
        counts[c] = counts.get(c, 0) + 1
    lines.append("Classifications: "
                 + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))

    # Book staleness.
    stale = [s for s in cols.get("book_staleness_ms", []) if s >= 0]
    if stale:
        stale_s = sorted(stale)
        med = stale_s[len(stale_s) // 2]
        p95 = stale_s[min(len(stale_s) - 1, int(len(stale_s) * 0.95))]
        lines.append(f"book_staleness_ms: median={med} p95={p95}")
    lines.append("")

    # Attribution-sum invariant check. This is what plumbing smoke tests need.
    good_idx = [i for i, c in enumerate(cls) if c in ("full", "partial")]
    checked, matched, skipped_nan = _sum_invariant_check(cols, good_idx)
    lines.append(
        f"Attribution-sum invariant (total_IS == Σ components): "
        f"checked={checked} matched={matched} nan_skipped={skipped_nan}"
    )
    if checked and checked != matched:
        lines.append(
            f"**WARNING**: attribution-sum mismatches on {checked - matched} rows. "
            f"This is a bug, not data — investigate replay_executor.post_fak."
        )
    lines.append("")

    # Paired rows for ROI.
    paper = cols.get("paper_pnl_flat_0_5", [])
    realised = cols.get("realised_pnl_at_close", [])
    notional = cols.get("requested_notional_usdc", [])
    paired_paper: list[float] = []
    paired_realised: list[float] = []
    paired_notional: list[float] = []
    for pp, rr, nn in zip(paper, realised, notional):
        if (
            isinstance(pp, float) and math.isfinite(pp)
            and isinstance(rr, float) and math.isfinite(rr)
            and isinstance(nn, float) and math.isfinite(nn) and nn > 0
        ):
            paired_paper.append(pp)
            paired_realised.append(rr)
            paired_notional.append(nn)

    if strict:
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
    else:
        lines.append(
            f"Paired (entry + exit) rows: **{len(paired_paper)}** "
            f"(ROI comparison suppressed: staleness_policy=`{policy}`)"
        )

    # Decomposition.
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
    if strict:
        lines.append(f"- diff_paper_minus_real:   {_fmt_bps(med_diff)}")

    # Regime table.
    lines.append("")
    lines.append("## Regime-conditional attribution")
    lines.append("")
    lines.append(attribute(parquet_path))

    # Headline — suppressed on non-strict runs.
    lines.append("")
    lines.append("## Headline")
    lines.append("")
    if not strict:
        lines.append(
            f"_Headline suppressed: parquet staleness_policy=`{policy}`. "
            f"Schema validation, row counts, attribution-sum invariant, "
            f"regime breakdown, and MinTRL above are still meaningful "
            f"for plumbing smoke checks._"
        )
    elif paired_paper:
        gross = sum(paired_notional)
        paper_roi = sum(paired_paper) / gross
        realised_roi = sum(paired_realised) / gross
        haircut = paper_roi - realised_roi
        headline = (
            f"Paper flat-0.5 ROI {_fmt_pct(paper_roi)} vs "
            f"book-walked-equivalent ROI {_fmt_pct(realised_roi)}, "
            f"haircut {_fmt_pct(haircut)} — decomposed as "
            f"{_fmt_bps(med_hs)} spread, {_fmt_bps(med_bw)} walk, "
            f"{_fmt_bps(med_ld)} latency, {_fmt_bps(med_fee)} fees."
        )
        lines.append(headline)
    else:
        lines.append("No paired entry/exit rows; cannot compute haircut yet.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="experiments.backtest.report")
    p.add_argument("--in", dest="inp", required=True, type=Path)
    args = p.parse_args(argv)
    print(report(args.inp))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
