"""Parse a variant's daemon_state/ into a metrics dict."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def parse_metrics(state_dir: Path, strategy: str = "enhanced") -> dict[str, Any]:
    """Parse state.json + events.jsonl into a metrics dict.

    Returns {"crashed": True, "reason": ...} if state.json is missing.
    """
    state_path = state_dir / "state.json"
    events_path = state_dir / "events.jsonl"

    if not state_path.exists():
        return {"crashed": True, "reason": "state.json missing"}

    try:
        state = json.loads(state_path.read_text())
    except json.JSONDecodeError as exc:
        return {"crashed": True, "reason": f"state.json malformed: {exc}"}

    strat = state.get(strategy, {}) or {}
    stats = strat.get("stats", {}) or {}

    total_pnl = float(stats.get("total_pnl", 0.0))
    total_risked = float(stats.get("total_risked", 0.0))
    trades = int(stats.get("total_trades", 0))
    wins = int(stats.get("wins", 0))
    max_dd = float(stats.get("max_drawdown", 0.0))

    roi = (total_pnl / total_risked) if total_risked > 0 else 0.0
    win_rate = (wins / trades) if trades > 0 else 0.0
    max_dd_pct = (max_dd / total_risked) if total_risked > 0 else 0.0
    composite = roi - 0.5 * max_dd_pct + 0.2 * win_rate

    per_exit = {"TP": 0, "SL": 0, "RESOLUTION": 0}
    per_source = {"edge": 0, "squeeze": 0}

    if events_path.exists():
        for line in events_path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("strategy") != strategy:
                continue
            etype = e.get("type")
            if etype == "resolve":
                per_exit["RESOLUTION"] += 1
            elif etype == "exit_filled":
                xt = (e.get("trade") or {}).get("exit_type")
                if xt in per_exit:
                    per_exit[xt] += 1
            elif etype == "entry_filled":
                src = e.get("source")
                if src in per_source:
                    per_source[src] += 1

    return {
        "crashed": False,
        "roi": roi,
        "total_pnl": total_pnl,
        "total_risked": total_risked,
        "trade_count": trades,
        "win_rate": win_rate,
        "max_dd_pct": max_dd_pct,
        "composite": composite,
        "per_exit_type": per_exit,
        "per_source": per_source,
    }


if __name__ == "__main__":
    import sys
    d = Path(sys.argv[1])
    strat = sys.argv[2] if len(sys.argv) > 2 else "enhanced"
    print(json.dumps(parse_metrics(d, strategy=strat), indent=2))
