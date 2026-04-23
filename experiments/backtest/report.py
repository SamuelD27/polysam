"""Single-page markdown report from a replay parquet.

Emits (in strict mode) the paper-vs-book-walked ROI comparison plus bps-
level decomposition, a regime-conditional attribution table, and MinTRL
per spec §6.4 (Bailey & López de Prado 2014) so the operator can see
how many trades per fold the observed sample would need for a defended
Sharpe separation.

When the input parquet's ``staleness_policy`` column contains anything
other than ``strict`` on any row, the paper-vs-realistic headline is
SUPPRESSED (spec §8.1.2 footgun). Row counts, classification breakdown,
attribution-sum-to-total_IS invariant check, MinTRL (always), and
manifest echo still print — they are what plumbing smoke tests need.

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


def _mintrl_estimate(
    per_trade_pnls: list[float],
    sr_nuisance: float = 1.0,
    alpha: float = 0.05,
) -> float | None:
    """Minimum Track Record Length per Bailey & López de Prado 2014 §6.4:

        MinTRL = 1 + (1 - γ₃·SR + ¼(γ₄-1)·SR²) · (Z_α / (SR - SR*))²

    where SR is the per-trade Sharpe of the sample, γ₃/γ₄ are skew and
    kurtosis (Fisher: kurtosis-excess), SR* is the nuisance Sharpe to
    beat, and Z_α is the normal 1-α quantile. Spec's alpha=0.05 fixes
    Z_α = 1.6448536269514722.

    The per-trade series is NOT annualised — for the 5-min BTC Up/Down
    strategy an "annualised Sharpe" is a fiction because the strategy
    trades in discrete 5-min windows and only N of them per day.
    Per-trade SR is defensibly comparable to the nuisance SR* of another
    paper-traded strategy variant at the same horizon.

    Returns None if fewer than 5 observations, zero variance, or
    SR == SR* (infinite MinTRL).
    """
    vals = [v for v in per_trade_pnls if isinstance(v, float) and math.isfinite(v)]
    n = len(vals)
    if n < 5:
        return None
    mean = sum(vals) / n
    var = sum((v - mean) ** 2 for v in vals) / (n - 1)
    if var <= 0:
        return None
    sd = math.sqrt(var)
    sr = mean / sd
    m3 = sum((v - mean) ** 3 for v in vals) / n
    m4 = sum((v - mean) ** 4 for v in vals) / n
    s3 = sd ** 3
    s4 = sd ** 4
    skew = m3 / s3 if s3 > 0 else 0.0
    kurt_excess = (m4 / s4 - 3.0) if s4 > 0 else 0.0
    z_alpha = 1.6448536269514722
    denom = sr - sr_nuisance
    if denom == 0:
        return None
    factor = 1.0 - skew * sr + 0.25 * kurt_excess * sr * sr
    if factor <= 0:
        # Pathological moments (very heavy tail with favourable skew) can
        # drive factor negative — the formula's normal-world correction
        # isn't valid there. Fall back to the gaussian term so the number
        # is a lower bound rather than nonsense.
        factor = 1.0
    return 1.0 + factor * (z_alpha / denom) ** 2


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

    # MinTRL per spec §6.4. Shown in both strict and non-strict modes —
    # a plumbing-smoke run still reveals whether the observed sample is
    # large enough to support the Sharpe claim we will eventually make.
    lines.append("")
    lines.append("## MinTRL (spec §6.4)")
    lines.append("")
    realised_for_mintrl = [
        v for v in realised if isinstance(v, float) and math.isfinite(v)
    ]
    paper_for_mintrl = [
        v for v in paper if isinstance(v, float) and math.isfinite(v)
    ]
    if realised_for_mintrl:
        m_real_0 = _mintrl_estimate(realised_for_mintrl, sr_nuisance=0.0)
        m_real_1 = _mintrl_estimate(realised_for_mintrl, sr_nuisance=1.0)
        lines.append(
            f"- realised_pnl_at_close series "
            f"(n={len(realised_for_mintrl)}): "
            f"MinTRL vs SR*=0.0 = "
            f"{('%.1f' % m_real_0) if m_real_0 else 'n/a'}, "
            f"MinTRL vs SR*=1.0 = "
            f"{('%.1f' % m_real_1) if m_real_1 else 'n/a'}"
        )
    if paper_for_mintrl:
        m_pp_0 = _mintrl_estimate(paper_for_mintrl, sr_nuisance=0.0)
        m_pp_1 = _mintrl_estimate(paper_for_mintrl, sr_nuisance=1.0)
        lines.append(
            f"- paper_pnl_flat_0_5 series "
            f"(n={len(paper_for_mintrl)}): "
            f"MinTRL vs SR*=0.0 = "
            f"{('%.1f' % m_pp_0) if m_pp_0 else 'n/a'}, "
            f"MinTRL vs SR*=1.0 = "
            f"{('%.1f' % m_pp_1) if m_pp_1 else 'n/a'}"
        )
    if not realised_for_mintrl and not paper_for_mintrl:
        lines.append("- (no paired PnL series yet; need >= 5 observations)")
    lines.append(
        "- Bailey & López de Prado 2014 per-trade formulation; "
        "α=0.05 (Z=1.645). Not annualised."
    )

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
