# Strategy Fine-Tuning Session Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the orchestration harness + Round 1 variant specs so the main Claude session can run a multi-round, parallel, dry-run strategy fine-tuning session on the BTC 5m daemon tonight, ending with an overnight champion dashboard.

**Architecture:** All work lives in `experiments/` (git-ignored). Each variant is a self-contained copy of `daemon_base_v1.py` + `active_bots/` with its own `daemon_state/`. Harness tools under `experiments/_tools/` handle scaffolding, patch application, launch, stop, and metric parsing. One subagent per variant per round reads its `variant.json`, applies the tweak, runs the daemon for 30 min, reports metrics.

**Tech Stack:** Python 3.11 (conda env `polymarket-env`), bash, existing `daemon_base_v1.py` + `active_bots/` unchanged. `pytest` for harness tests.

**Spec reference:** `docs/superpowers/specs/2026-04-22-strategy-fine-tuning-design.md`

---

## File Structure

**Harness tools (created):**

- `experiments/_tools/__init__.py` — empty package marker
- `experiments/_tools/variant_spec.py` — `VariantSpec` dataclass, loads + validates `variant.json`
- `experiments/_tools/metrics.py` — parses `daemon_state/state.json` + `events.jsonl` into a metrics dict
- `experiments/_tools/patch_variant.py` — applies `VariantSpec.patches` + `new_files` to a variant dir
- `experiments/_tools/scaffold_variant.sh` — copies daemon + active_bots into variant dir, creates daemon_state
- `experiments/_tools/launch_variant.sh` — launches variant daemon in background, writes `runtime.json`
- `experiments/_tools/stop_variant.sh` — TERM + escalate kill, updates `runtime.json`
- `experiments/_tools/tests/test_variant_spec.py`
- `experiments/_tools/tests/test_metrics.py`
- `experiments/_tools/tests/test_patch_variant.py`

**Round 1 variant specs (created):**

- `experiments/_baseline/variant.json`
- `experiments/n1_mean_revert/variant.json` + `active_bots_overlay/pure_reversion_strategy.py`
- `experiments/n2_late_gamma/variant.json`
- `experiments/n3_momentum/variant.json` + `active_bots_overlay/momentum_strategy.py`
- `experiments/n4_market_maker/variant.json`
- `experiments/n5_no_squeeze/variant.json`
- `experiments/n6_vol_regime/variant.json`

**Analysis scaffolding (created):**

- `experiments/analysis/session_log.md` — append-only timeline
- `experiments/analysis/findings.md` — append-only findings log
- `experiments/analysis/champion.json` — initial empty champion
- `experiments/analysis/champion_history.jsonl` — touch-created empty

**Main codebase:** untouched. Only `.gitignore` already had `experiments/` added in the spec commit.

---

## Task 1: Harness package + variant_spec loader

**Files:**
- Create: `experiments/_tools/__init__.py`
- Create: `experiments/_tools/variant_spec.py`
- Test: `experiments/_tools/tests/test_variant_spec.py`

- [ ] **Step 1.1: Create package skeleton**

```bash
mkdir -p /home/samsam/polymarket-hustle/experiments/_tools/tests
touch /home/samsam/polymarket-hustle/experiments/_tools/__init__.py
touch /home/samsam/polymarket-hustle/experiments/_tools/tests/__init__.py
```

- [ ] **Step 1.2: Write the failing test**

File: `experiments/_tools/tests/test_variant_spec.py`

```python
"""Tests for VariantSpec loader."""
import json
from pathlib import Path

import pytest

from experiments._tools.variant_spec import VariantSpec


def _write(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "variant.json"
    p.write_text(json.dumps(data))
    return p


def test_load_env_only(tmp_path):
    p = _write(tmp_path, {
        "name": "v1",
        "hypothesis": "h",
        "tweak_type": "env_only",
        "env": {"X": "1"},
    })
    spec = VariantSpec.load(p)
    assert spec.name == "v1"
    assert spec.tweak_type == "env_only"
    assert spec.env == {"X": "1"}
    assert spec.patches == []
    assert spec.new_files == []


def test_load_code_patch(tmp_path):
    p = _write(tmp_path, {
        "name": "v2",
        "hypothesis": "h",
        "tweak_type": "code_patch",
        "patches": [{"file": "a.py", "find": "foo", "replace": "bar"}],
    })
    spec = VariantSpec.load(p)
    assert spec.tweak_type == "code_patch"
    assert spec.patches[0]["file"] == "a.py"


def test_load_new_module(tmp_path):
    p = _write(tmp_path, {
        "name": "v3",
        "hypothesis": "h",
        "tweak_type": "new_module",
        "new_files": [{"path": "x.py", "content": "pass\n"}],
        "patches": [{"file": "d.py", "find": "A", "replace": "B"}],
    })
    spec = VariantSpec.load(p)
    assert spec.new_files[0]["path"] == "x.py"


def test_invalid_tweak_type(tmp_path):
    p = _write(tmp_path, {"name": "v", "hypothesis": "h", "tweak_type": "nonsense"})
    with pytest.raises(ValueError, match="tweak_type"):
        VariantSpec.load(p)


def test_missing_required_field(tmp_path):
    p = _write(tmp_path, {"tweak_type": "env_only"})
    with pytest.raises((KeyError, TypeError, ValueError)):
        VariantSpec.load(p)
```

- [ ] **Step 1.3: Run test — expect failure**

```bash
cd /home/samsam/polymarket-hustle && \
  conda run -n polymarket-env pytest experiments/_tools/tests/test_variant_spec.py -v
```

Expected: ImportError (`variant_spec` module not found).

- [ ] **Step 1.4: Write implementation**

File: `experiments/_tools/variant_spec.py`

```python
"""Load + validate variant.json specs."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

VALID_TWEAK_TYPES = {"env_only", "code_patch", "new_module"}
ALLOWED_FIELDS = {"name", "hypothesis", "tweak_type", "env", "patches", "new_files"}


@dataclass
class VariantSpec:
    name: str
    hypothesis: str
    tweak_type: str
    env: dict[str, str] = field(default_factory=dict)
    patches: list[dict[str, Any]] = field(default_factory=list)
    new_files: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> "VariantSpec":
        data = json.loads(path.read_text())
        if "name" not in data or "hypothesis" not in data or "tweak_type" not in data:
            raise ValueError(f"variant.json missing required field (name/hypothesis/tweak_type): {path}")
        if data["tweak_type"] not in VALID_TWEAK_TYPES:
            raise ValueError(f"invalid tweak_type {data['tweak_type']!r} in {path}; allowed: {sorted(VALID_TWEAK_TYPES)}")
        filtered = {k: v for k, v in data.items() if k in ALLOWED_FIELDS}
        return cls(**filtered)
```

- [ ] **Step 1.5: Run test — expect pass**

```bash
cd /home/samsam/polymarket-hustle && \
  conda run -n polymarket-env pytest experiments/_tools/tests/test_variant_spec.py -v
```

Expected: 5 passed.

- [ ] **Step 1.6: Commit**

```bash
cd /home/samsam/polymarket-hustle && \
  git add -f experiments/_tools/__init__.py experiments/_tools/variant_spec.py experiments/_tools/tests/ && \
  git commit -m "feat(experiments): add VariantSpec loader for variant.json

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

Note: `-f` is required because `experiments/` is in .gitignore. We commit harness tools (they are reusable infra), but NOT variant daemon copies or daemon_state.

---

## Task 2: Metrics parser

**Files:**
- Create: `experiments/_tools/metrics.py`
- Test: `experiments/_tools/tests/test_metrics.py`

- [ ] **Step 2.1: Write the failing test**

File: `experiments/_tools/tests/test_metrics.py`

```python
"""Tests for metrics parsing."""
import json
from pathlib import Path

from experiments._tools.metrics import parse_metrics


def _write_state(dir: Path, strategy_stats: dict) -> None:
    (dir / "state.json").write_text(json.dumps({
        "enhanced": {
            "stats": strategy_stats,
            "extra": {},
            "closed_trades": [],
            "open_position": None,
            "fair_price": None,
        },
        "base": {"stats": {}, "closed_trades": [], "open_position": None, "fair_price": None},
    }))


def _write_events(dir: Path, events: list[dict]) -> None:
    (dir / "events.jsonl").write_text("\n".join(json.dumps(e) for e in events))


def test_no_state_file_is_crashed(tmp_path):
    m = parse_metrics(tmp_path, strategy="enhanced")
    assert m["crashed"] is True


def test_empty_stats(tmp_path):
    _write_state(tmp_path, {"total_pnl": 0, "total_risked": 0, "total_trades": 0,
                             "wins": 0, "losses": 0, "max_drawdown": 0})
    m = parse_metrics(tmp_path, strategy="enhanced")
    assert m["crashed"] is False
    assert m["roi"] == 0.0
    assert m["trade_count"] == 0
    assert m["win_rate"] == 0.0
    assert m["composite"] == 0.0


def test_roi_and_composite(tmp_path):
    _write_state(tmp_path, {"total_pnl": 20.0, "total_risked": 100.0,
                             "total_trades": 10, "wins": 7, "losses": 3,
                             "max_drawdown": 5.0})
    m = parse_metrics(tmp_path, strategy="enhanced")
    assert m["roi"] == 0.2
    assert m["trade_count"] == 10
    assert m["win_rate"] == 0.7
    assert m["max_dd_pct"] == 0.05
    # composite = 0.2 - 0.5*0.05 + 0.2*0.7 = 0.2 - 0.025 + 0.14 = 0.315
    assert abs(m["composite"] - 0.315) < 1e-9


def test_per_exit_and_source_counts(tmp_path):
    _write_state(tmp_path, {"total_pnl": 1, "total_risked": 1, "total_trades": 3,
                             "wins": 2, "losses": 1, "max_drawdown": 0})
    _write_events(tmp_path, [
        {"strategy": "enhanced", "type": "entry_filled", "source": "edge"},
        {"strategy": "enhanced", "type": "entry_filled", "source": "squeeze"},
        {"strategy": "enhanced", "type": "entry_filled", "source": "edge"},
        {"strategy": "enhanced", "type": "exit_filled", "trade": {"exit_type": "TP"}},
        {"strategy": "enhanced", "type": "exit_filled", "trade": {"exit_type": "SL"}},
        {"strategy": "enhanced", "type": "resolve", "trade": {}},
        {"strategy": "base", "type": "entry_filled", "source": "base"},  # ignored
    ])
    m = parse_metrics(tmp_path, strategy="enhanced")
    assert m["per_exit_type"] == {"TP": 1, "SL": 1, "RESOLUTION": 1}
    assert m["per_source"] == {"edge": 2, "squeeze": 1}


def test_malformed_event_lines_are_skipped(tmp_path):
    _write_state(tmp_path, {"total_pnl": 0, "total_risked": 0, "total_trades": 0,
                             "wins": 0, "losses": 0, "max_drawdown": 0})
    (tmp_path / "events.jsonl").write_text('not json\n{"strategy":"enhanced","type":"resolve"}\n')
    m = parse_metrics(tmp_path, strategy="enhanced")
    assert m["per_exit_type"]["RESOLUTION"] == 1
```

- [ ] **Step 2.2: Run test — expect failure**

```bash
cd /home/samsam/polymarket-hustle && \
  conda run -n polymarket-env pytest experiments/_tools/tests/test_metrics.py -v
```

Expected: ImportError.

- [ ] **Step 2.3: Write implementation**

File: `experiments/_tools/metrics.py`

```python
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
```

- [ ] **Step 2.4: Run test — expect pass**

```bash
cd /home/samsam/polymarket-hustle && \
  conda run -n polymarket-env pytest experiments/_tools/tests/test_metrics.py -v
```

Expected: 5 passed.

- [ ] **Step 2.5: Commit**

```bash
cd /home/samsam/polymarket-hustle && \
  git add -f experiments/_tools/metrics.py experiments/_tools/tests/test_metrics.py && \
  git commit -m "feat(experiments): add metrics parser for variant state

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Variant patch applier

**Files:**
- Create: `experiments/_tools/patch_variant.py`
- Test: `experiments/_tools/tests/test_patch_variant.py`

- [ ] **Step 3.1: Write the failing test**

File: `experiments/_tools/tests/test_patch_variant.py`

```python
"""Tests for patch applier."""
import json
from pathlib import Path

import pytest

from experiments._tools.patch_variant import apply_patches
from experiments._tools.variant_spec import VariantSpec


def _spec(tmp_path: Path, data: dict) -> VariantSpec:
    p = tmp_path / "variant.json"
    p.write_text(json.dumps(data))
    return VariantSpec.load(p)


def test_env_only_is_noop_on_files(tmp_path):
    (tmp_path / "daemon_base_v1.py").write_text("hello\n")
    spec = _spec(tmp_path, {"name": "v", "hypothesis": "h", "tweak_type": "env_only", "env": {"X": "1"}})
    apply_patches(tmp_path, spec)
    assert (tmp_path / "daemon_base_v1.py").read_text() == "hello\n"


def test_code_patch_replaces_find_block(tmp_path):
    target = tmp_path / "daemon_base_v1.py"
    target.write_text("line1\nFIND_ME\nline3\n")
    spec = _spec(tmp_path, {
        "name": "v", "hypothesis": "h", "tweak_type": "code_patch",
        "patches": [{"file": "daemon_base_v1.py", "find": "FIND_ME", "replace": "REPLACED"}],
    })
    apply_patches(tmp_path, spec)
    assert target.read_text() == "line1\nREPLACED\nline3\n"


def test_code_patch_missing_find_raises(tmp_path):
    (tmp_path / "a.py").write_text("nope\n")
    spec = _spec(tmp_path, {
        "name": "v", "hypothesis": "h", "tweak_type": "code_patch",
        "patches": [{"file": "a.py", "find": "MISSING", "replace": "X"}],
    })
    with pytest.raises(RuntimeError, match="not found"):
        apply_patches(tmp_path, spec)


def test_new_module_writes_files_and_applies_patches(tmp_path):
    (tmp_path / "daemon_base_v1.py").write_text("from active_bots.enhanced_strategy import EnhancedStrategy\n")
    spec = _spec(tmp_path, {
        "name": "v", "hypothesis": "h", "tweak_type": "new_module",
        "new_files": [{"path": "active_bots/pure_x.py", "content": "class PureX:\n    pass\n"}],
        "patches": [{
            "file": "daemon_base_v1.py",
            "find": "from active_bots.enhanced_strategy import EnhancedStrategy",
            "replace": "from active_bots.pure_x import PureX as EnhancedStrategy",
        }],
    })
    apply_patches(tmp_path, spec)
    assert (tmp_path / "active_bots/pure_x.py").read_text().startswith("class PureX:")
    assert "from active_bots.pure_x" in (tmp_path / "daemon_base_v1.py").read_text()


def test_patch_is_single_replacement(tmp_path):
    """Guard: if find-block appears twice, replace only the first (explicit count=1)."""
    target = tmp_path / "a.py"
    target.write_text("X\nX\n")
    spec = _spec(tmp_path, {
        "name": "v", "hypothesis": "h", "tweak_type": "code_patch",
        "patches": [{"file": "a.py", "find": "X", "replace": "Y"}],
    })
    apply_patches(tmp_path, spec)
    assert target.read_text() == "Y\nX\n"
```

- [ ] **Step 3.2: Run test — expect failure**

```bash
cd /home/samsam/polymarket-hustle && \
  conda run -n polymarket-env pytest experiments/_tools/tests/test_patch_variant.py -v
```

Expected: ImportError.

- [ ] **Step 3.3: Write implementation**

File: `experiments/_tools/patch_variant.py`

```python
"""Apply a VariantSpec's new_files + patches to a variant directory."""
from __future__ import annotations

import sys
from pathlib import Path

from experiments._tools.variant_spec import VariantSpec


def apply_patches(variant_dir: Path, spec: VariantSpec) -> None:
    """Write new_files and apply string-replace patches, in that order."""
    for nf in spec.new_files:
        target = variant_dir / nf["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(nf["content"])

    for p in spec.patches:
        target = variant_dir / p["file"]
        if not target.exists():
            raise RuntimeError(f"patch target not found: {target}")
        text = target.read_text()
        find = p["find"]
        if find not in text:
            raise RuntimeError(
                f"patch find-block not found in {target}: {find[:120]!r}"
            )
        new_text = text.replace(find, p["replace"], 1)
        target.write_text(new_text)


if __name__ == "__main__":
    variant_dir = Path(sys.argv[1])
    spec = VariantSpec.load(variant_dir / "variant.json")
    apply_patches(variant_dir, spec)
    print(f"applied {len(spec.patches)} patch(es) and {len(spec.new_files)} new file(s) to {variant_dir}")
```

- [ ] **Step 3.4: Run test — expect pass**

```bash
cd /home/samsam/polymarket-hustle && \
  conda run -n polymarket-env pytest experiments/_tools/tests/test_patch_variant.py -v
```

Expected: 5 passed.

- [ ] **Step 3.5: Commit**

```bash
cd /home/samsam/polymarket-hustle && \
  git add -f experiments/_tools/patch_variant.py experiments/_tools/tests/test_patch_variant.py && \
  git commit -m "feat(experiments): add variant patch applier

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: scaffold_variant.sh

**Files:**
- Create: `experiments/_tools/scaffold_variant.sh`

- [ ] **Step 4.1: Write the script**

File: `experiments/_tools/scaffold_variant.sh`

```bash
#!/usr/bin/env bash
# Usage: scaffold_variant.sh <variant_dir>
# Idempotent: copies daemon_base_v1.py + active_bots/ into the variant dir,
# creates daemon_state/. Requires variant.json to already exist.
set -euo pipefail

REPO="/home/samsam/polymarket-hustle"

if [[ $# -ne 1 ]]; then
    echo "usage: $0 <variant_dir>" >&2
    exit 2
fi

DIR="$(cd "$1" && pwd)"
if [[ ! -f "$DIR/variant.json" ]]; then
    echo "missing variant.json in $DIR" >&2
    exit 2
fi

mkdir -p "$DIR/daemon_state"

cp "$REPO/daemon_base_v1.py" "$DIR/daemon_base_v1.py"
rm -rf "$DIR/active_bots"
cp -r "$REPO/active_bots" "$DIR/active_bots"

# Strip pycache / plots / backtests from the copy
find "$DIR/active_bots" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
rm -rf "$DIR/active_bots/plots" 2>/dev/null || true
find "$DIR/active_bots" -name "backtest_*.csv" -delete 2>/dev/null || true

echo "scaffolded: $DIR"
```

- [ ] **Step 4.2: Make it executable**

```bash
chmod +x /home/samsam/polymarket-hustle/experiments/_tools/scaffold_variant.sh
```

- [ ] **Step 4.3: Smoke test**

```bash
cd /home/samsam/polymarket-hustle && \
  mkdir -p /tmp/scaffold_test && \
  echo '{"name":"t","hypothesis":"h","tweak_type":"env_only"}' > /tmp/scaffold_test/variant.json && \
  experiments/_tools/scaffold_variant.sh /tmp/scaffold_test && \
  ls /tmp/scaffold_test && \
  rm -rf /tmp/scaffold_test
```

Expected: lists `daemon_base_v1.py  active_bots  daemon_state  variant.json`.

- [ ] **Step 4.4: Commit**

```bash
cd /home/samsam/polymarket-hustle && \
  git add -f experiments/_tools/scaffold_variant.sh && \
  git commit -m "feat(experiments): add scaffold_variant.sh

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: launch_variant.sh

**Files:**
- Create: `experiments/_tools/launch_variant.sh`

- [ ] **Step 5.1: Write the script**

File: `experiments/_tools/launch_variant.sh`

```bash
#!/usr/bin/env bash
# Usage: launch_variant.sh <variant_dir>
# Reads variant.json env, launches daemon in background with dry-run forced,
# writes runtime.json with pid/start_ts/status=running.
set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo "usage: $0 <variant_dir>" >&2
    exit 2
fi

DIR="$(cd "$1" && pwd)"
if [[ ! -f "$DIR/variant.json" ]]; then
    echo "missing variant.json in $DIR" >&2
    exit 2
fi
if [[ ! -f "$DIR/daemon_base_v1.py" ]]; then
    echo "missing daemon_base_v1.py in $DIR (run scaffold first)" >&2
    exit 2
fi

# Extract env vars from variant.json as KEY=VALUE lines
ENV_LINES=$(python3 -c "
import json
spec = json.load(open('$DIR/variant.json'))
for k, v in (spec.get('env') or {}).items():
    print(f'{k}={v}')
")

# Activate conda
source /home/samsam/miniconda3/etc/profile.d/conda.sh
conda activate polymarket-env

cd "$DIR"
mkdir -p daemon_state

# Build env and launch. POLYMARKET_MODE=live + POLYMARKET_DRY_RUN=1 → PaperExecutor.
# shellcheck disable=SC2086
env POLYMARKET_MODE=live POLYMARKET_DRY_RUN=1 $ENV_LINES \
    nohup python3 daemon_base_v1.py >daemon_state/stdout.log 2>&1 &
PID=$!

# Confirm alive
sleep 3
if ! kill -0 "$PID" 2>/dev/null; then
    echo "FAIL: daemon died; see $DIR/daemon_state/stdout.log" >&2
    tail -20 "$DIR/daemon_state/stdout.log" >&2 || true
    exit 1
fi

python3 -c "
import json, time
from pathlib import Path
Path('$DIR/runtime.json').write_text(json.dumps({
    'pid': $PID,
    'start_ts': time.time(),
    'status': 'running',
}, indent=2))
"

echo "launched $DIR pid=$PID"
```

- [ ] **Step 5.2: Make executable**

```bash
chmod +x /home/samsam/polymarket-hustle/experiments/_tools/launch_variant.sh
```

- [ ] **Step 5.3: Commit**

```bash
cd /home/samsam/polymarket-hustle && \
  git add -f experiments/_tools/launch_variant.sh && \
  git commit -m "feat(experiments): add launch_variant.sh (dry-run forced)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: stop_variant.sh

**Files:**
- Create: `experiments/_tools/stop_variant.sh`

- [ ] **Step 6.1: Write the script**

File: `experiments/_tools/stop_variant.sh`

```bash
#!/usr/bin/env bash
# Usage: stop_variant.sh <variant_dir>
# TERM then escalate KILL, updates runtime.json.
set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo "usage: $0 <variant_dir>" >&2
    exit 2
fi

DIR="$(cd "$1" && pwd)"
if [[ ! -f "$DIR/runtime.json" ]]; then
    echo "no runtime.json in $DIR; nothing to stop" >&2
    exit 0
fi

PID=$(python3 -c "import json; print(json.load(open('$DIR/runtime.json')).get('pid', ''))")
if [[ -z "$PID" ]]; then
    echo "runtime.json has no pid; skipping kill" >&2
else
    if kill -0 "$PID" 2>/dev/null; then
        kill "$PID" 2>/dev/null || true
        for _ in 1 2 3 4 5 6 7 8 9 10; do
            if ! kill -0 "$PID" 2>/dev/null; then break; fi
            sleep 1
        done
        if kill -0 "$PID" 2>/dev/null; then
            echo "forcing SIGKILL pid=$PID" >&2
            kill -9 "$PID" 2>/dev/null || true
        fi
    fi
    # Belt-and-braces: kill any python daemon still referencing this dir
    pkill -f "python.*$DIR.*daemon_base_v1\.py" 2>/dev/null || true
fi

python3 -c "
import json, time
from pathlib import Path
p = Path('$DIR/runtime.json')
rt = json.loads(p.read_text())
rt['status'] = 'stopped'
rt['end_ts'] = time.time()
p.write_text(json.dumps(rt, indent=2))
"

echo "stopped $DIR pid=$PID"
```

- [ ] **Step 6.2: Make executable**

```bash
chmod +x /home/samsam/polymarket-hustle/experiments/_tools/stop_variant.sh
```

- [ ] **Step 6.3: Commit**

```bash
cd /home/samsam/polymarket-hustle && \
  git add -f experiments/_tools/stop_variant.sh && \
  git commit -m "feat(experiments): add stop_variant.sh (escalating kill)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: Analysis scaffolding

**Files:**
- Create: `experiments/analysis/session_log.md`
- Create: `experiments/analysis/findings.md`
- Create: `experiments/analysis/champion.json`
- Create: `experiments/analysis/champion_history.jsonl`

- [ ] **Step 7.1: Create analysis dir**

```bash
mkdir -p /home/samsam/polymarket-hustle/experiments/analysis/variants
```

- [ ] **Step 7.2: Initial session_log.md**

File: `experiments/analysis/session_log.md`

```markdown
# Strategy Fine-Tuning Session Log

Append-only timeline. Each entry: `YYYY-MM-DD HH:MM UTC — <event> — <detail>`.

## 2026-04-22

- Session initialized. Spec: `docs/superpowers/specs/2026-04-22-strategy-fine-tuning-design.md`. Plan: `docs/superpowers/plans/2026-04-22-strategy-fine-tuning.md`.
```

- [ ] **Step 7.3: Initial findings.md**

File: `experiments/analysis/findings.md`

```markdown
# Findings Log

Append-only. One `###` section per finding. See spec §Findings Log for format.
```

- [ ] **Step 7.4: Initial champion.json**

File: `experiments/analysis/champion.json`

```json
{
  "variant": null,
  "round": 0,
  "metrics": null,
  "created_at": null,
  "note": "No champion yet — will be set after Round 1."
}
```

- [ ] **Step 7.5: Touch champion_history.jsonl**

```bash
touch /home/samsam/polymarket-hustle/experiments/analysis/champion_history.jsonl
```

- [ ] **Step 7.6: Commit the scaffolding (leave runtime files ignored)**

Analysis files are worth checking in (history is small, survives sessions). Use `-f` since `experiments/` is gitignored.

```bash
cd /home/samsam/polymarket-hustle && \
  git add -f experiments/analysis/session_log.md \
            experiments/analysis/findings.md \
            experiments/analysis/champion.json \
            experiments/analysis/champion_history.jsonl && \
  git commit -m "feat(experiments): scaffold analysis dir with initial logs

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: _baseline variant.json

**Files:**
- Create: `experiments/_baseline/variant.json`

- [ ] **Step 8.1: Write variant.json**

File: `experiments/_baseline/variant.json`

```json
{
  "name": "_baseline",
  "hypothesis": "Reference run of unmodified daemon (BASE + ENHANCED) for cross-variant comparison.",
  "tweak_type": "env_only",
  "env": {}
}
```

- [ ] **Step 8.2: Scaffold and validate**

```bash
cd /home/samsam/polymarket-hustle && \
  experiments/_tools/scaffold_variant.sh experiments/_baseline && \
  conda run -n polymarket-env python3 -c "
from pathlib import Path
from experiments._tools.variant_spec import VariantSpec
from experiments._tools.patch_variant import apply_patches
d = Path('experiments/_baseline')
spec = VariantSpec.load(d/'variant.json')
apply_patches(d, spec)
print('OK', spec.name, spec.tweak_type)
"
```

Expected: `OK _baseline env_only`.

- [ ] **Step 8.3: Commit variant.json (not the scaffolded daemon/active_bots)**

```bash
cd /home/samsam/polymarket-hustle && \
  git add -f experiments/_baseline/variant.json && \
  git commit -m "feat(experiments): add _baseline variant spec

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 9: n5_no_squeeze variant.json

Simplest code_patch: flip one arg.

**Files:**
- Create: `experiments/n5_no_squeeze/variant.json`

- [ ] **Step 9.1: Write variant.json**

File: `experiments/n5_no_squeeze/variant.json`

```json
{
  "name": "n5_no_squeeze",
  "hypothesis": "Ablation of squeeze pillar. Tests whether squeeze is additive, neutral, or dilutive.",
  "tweak_type": "code_patch",
  "patches": [
    {
      "file": "daemon_base_v1.py",
      "find": "    enh = EnhancedStrategy(max_risk=max_risk)",
      "replace": "    enh = EnhancedStrategy(enable_squeeze=False, max_risk=max_risk)"
    }
  ]
}
```

- [ ] **Step 9.2: Scaffold + apply + verify patch landed**

```bash
cd /home/samsam/polymarket-hustle && \
  experiments/_tools/scaffold_variant.sh experiments/n5_no_squeeze && \
  conda run -n polymarket-env python3 -m experiments._tools.patch_variant experiments/n5_no_squeeze && \
  grep "enable_squeeze=False" experiments/n5_no_squeeze/daemon_base_v1.py
```

Expected: one matching line echoed.

- [ ] **Step 9.3: Commit**

```bash
cd /home/samsam/polymarket-hustle && \
  git add -f experiments/n5_no_squeeze/variant.json && \
  git commit -m "feat(experiments): add n5_no_squeeze variant (squeeze ablation)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 10: n2_late_gamma variant.json

Env + two patches: shrink TIME_ZONES, disable squeeze.

**Files:**
- Create: `experiments/n2_late_gamma/variant.json`

- [ ] **Step 10.1: Write variant.json**

File: `experiments/n2_late_gamma/variant.json`

```json
{
  "name": "n2_late_gamma",
  "hypothesis": "Peak model accuracy is at T+260-285; a large-edge-only strategy in that window with aggressive sizing dominates.",
  "tweak_type": "code_patch",
  "env": {"MAX_BET_PCT": "0.40"},
  "patches": [
    {
      "file": "active_bots/enhanced_strategy.py",
      "find": "TIME_ZONES: list[tuple[int, int, float, str]] = [\n    (60, 120, 0.20, \"early\"),\n    (120, 210, 0.12, \"mid\"),\n    (210, 260, 0.08, \"sweet_spot\"),\n    (260, 285, 0.15, \"late_gamma\"),\n]",
      "replace": "TIME_ZONES: list[tuple[int, int, float, str]] = [\n    (260, 285, 0.20, \"late_gamma_only\"),\n]"
    },
    {
      "file": "daemon_base_v1.py",
      "find": "    enh = EnhancedStrategy(max_risk=max_risk)",
      "replace": "    enh = EnhancedStrategy(enable_squeeze=False, max_risk=max_risk)"
    }
  ]
}
```

- [ ] **Step 10.2: Scaffold + apply + verify**

```bash
cd /home/samsam/polymarket-hustle && \
  experiments/_tools/scaffold_variant.sh experiments/n2_late_gamma && \
  conda run -n polymarket-env python3 -m experiments._tools.patch_variant experiments/n2_late_gamma && \
  grep "late_gamma_only" experiments/n2_late_gamma/active_bots/enhanced_strategy.py && \
  grep "enable_squeeze=False" experiments/n2_late_gamma/daemon_base_v1.py
```

Expected: both grep matches.

- [ ] **Step 10.3: Commit**

```bash
cd /home/samsam/polymarket-hustle && \
  git add -f experiments/n2_late_gamma/variant.json && \
  git commit -m "feat(experiments): add n2_late_gamma variant (late-gamma sniper)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 11: n6_vol_regime variant.json

Small patch into `TimeBasedStrategy.on_tick` to skip entries below sigma floor.

**Files:**
- Create: `experiments/n6_vol_regime/variant.json`

- [ ] **Step 11.1: Write variant.json**

File: `experiments/n6_vol_regime/variant.json`

```json
{
  "name": "n6_vol_regime",
  "hypothesis": "Edge model is only trustworthy in elevated vol regimes; low-vol markets are noise.",
  "tweak_type": "code_patch",
  "env": {"SIGMA_MIN": "0.60"},
  "patches": [
    {
      "file": "active_bots/enhanced_strategy.py",
      "find": "        # ── Entry check: iterate time zones ──\n        if not self.has_position:\n            for offset_min, offset_max, zone_edge_min, label in TIME_ZONES:",
      "replace": "        # ── Entry check: iterate time zones ──\n        # n6: vol-regime gate — skip entries below annualized-sigma floor\n        if sigma < float(os.environ.get(\"SIGMA_MIN\", \"0.0\")):\n            return None\n        if not self.has_position:\n            for offset_min, offset_max, zone_edge_min, label in TIME_ZONES:"
    }
  ]
}
```

- [ ] **Step 11.2: Scaffold + apply + verify**

```bash
cd /home/samsam/polymarket-hustle && \
  experiments/_tools/scaffold_variant.sh experiments/n6_vol_regime && \
  conda run -n polymarket-env python3 -m experiments._tools.patch_variant experiments/n6_vol_regime && \
  grep "SIGMA_MIN" experiments/n6_vol_regime/active_bots/enhanced_strategy.py
```

Expected: patched line found.

- [ ] **Step 11.3: Commit**

```bash
cd /home/samsam/polymarket-hustle && \
  git add -f experiments/n6_vol_regime/variant.json && \
  git commit -m "feat(experiments): add n6_vol_regime variant (sigma-gated entries)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 12: n1_mean_revert variant.json + pure_reversion_strategy.py

New module + import swap in the copied daemon.

**Files:**
- Create: `experiments/n1_mean_revert/variant.json`

- [ ] **Step 12.1: Write variant.json**

File: `experiments/n1_mean_revert/variant.json`

The `content` field for the new module is long — use the file content shown below, embedded as a JSON string with `\n` escaping. To keep the spec maintainable, put the strategy code in a sibling file and inline it via a here-doc when writing variant.json.

Run this command to generate `variant.json` from the embedded strategy source:

```bash
cd /home/samsam/polymarket-hustle
mkdir -p experiments/n1_mean_revert
cat > /tmp/pure_reversion_strategy.py <<'PYEOF'
"""Pure mean-reversion strategy — no edge model, no GBM fair price.

Tracks the largest normalized deviation from strike during T+0..T+120, then
enters counter-trend when price starts reverting.
"""
from __future__ import annotations

import math
import time
from typing import Any

from .base_strategy import MARKET_DURATION, MAX_RISK, SPREAD_COST
from .pricing.constants import SECONDS_PER_YEAR

SPIKE_SCORE_MIN = 1.0
REVERSION_FRAC = 0.30
ENTRY_WINDOW = (60, 180)
FORCE_EXIT_S = 30
MIN_ENTRY_PRICE = 0.05
MAX_ENTRY_PRICE = 0.95
EXIT_REVERSION_FRAC = 0.50  # exit when deviation drops 50% further from entry-time peak


class PureReversionStrategy:
    """Mean-reversion only; exposes the same interface the daemon calls on EnhancedStrategy."""

    def __init__(self, max_risk: float = MAX_RISK, **_: Any):
        self.max_risk = max_risk
        self.squeeze = None  # daemon checks `enh.squeeze is not None` — keep attr
        self._t_zero: float | None = None
        self._strike: float | None = None
        self._max_deviation = 0.0
        self._spike_direction: str | None = None
        self._spike_score = 0.0
        self._candidate = False
        self._position: dict[str, Any] | None = None
        self._resolved = False

    def reset(self, t_zero: float | None = None, strike: float | None = None) -> None:
        self._t_zero = t_zero
        self._strike = strike
        self._max_deviation = 0.0
        self._spike_direction = None
        self._spike_score = 0.0
        self._candidate = False
        self._position = None
        self._resolved = False

    @property
    def has_position(self) -> bool:
        return self._position is not None

    def on_tick(
        self,
        btc_price: float,
        market_price_up: float,
        sigma: float,
        t_zero: float,
        market_price_ts: float = 0.0,
    ) -> dict[str, Any] | None:
        now = time.time()
        if self._t_zero != t_zero:
            self.reset(t_zero=t_zero, strike=btc_price)
        elapsed = now - self._t_zero if self._t_zero else 0.0
        time_remaining = MARKET_DURATION - elapsed

        # Track deviation
        if self._strike and self._strike > 0:
            deviation = abs(math.log(btc_price / self._strike))
            if deviation > self._max_deviation:
                self._max_deviation = deviation
                self._spike_direction = "up" if btc_price > self._strike else "down"
            if sigma and sigma > 1e-12 and elapsed > 0:
                expected_std = sigma * math.sqrt(elapsed / SECONDS_PER_YEAR)
                self._spike_score = (self._max_deviation / expected_std) if expected_std > 0 else 0.0
                if self._spike_score >= SPIKE_SCORE_MIN:
                    self._candidate = True
        else:
            deviation = 0.0

        # Exits on open position
        if self._position is not None:
            force = time_remaining <= FORCE_EXIT_S
            revert_ok = (
                self._max_deviation > 0
                and deviation / self._max_deviation <= (1.0 - EXIT_REVERSION_FRAC)
            )
            if force or revert_ok:
                if market_price_up is None:
                    return None
                side = self._position["side"]
                realizable = market_price_up if side == "Up" else 1.0 - market_price_up
                entry_price = self._position["entry_price"]
                size_shares = self._position["size_shares"]
                pnl = (realizable - entry_price) * size_shares - SPREAD_COST * size_shares
                exit_action = {
                    "action": "EXIT_TP" if pnl >= 0 else "EXIT_SL",
                    "exit_price": max(0.0, min(1.0, realizable)),
                    "pnl": pnl,
                    "hold_time_s": now - self._position["entry_time"],
                    "side": side,
                    "forced": force,
                    "strike": self._strike,
                    "size_usdc": self._position["size_usdc"],
                    "size_shares": size_shares,
                }
                self._position = None
                self._resolved = True
                return exit_action

        # Entry — candidate, reverting, in window, fresh quote
        if self._position is None and self._candidate and market_price_up is not None:
            lo, hi = ENTRY_WINDOW
            if lo <= elapsed <= hi and self._max_deviation > 0:
                reversion = 1.0 - (deviation / self._max_deviation)
                if reversion >= REVERSION_FRAC:
                    side = "Down" if self._spike_direction == "up" else "Up"
                    entry_price = market_price_up if side == "Up" else 1.0 - market_price_up
                    if MIN_ENTRY_PRICE <= entry_price <= MAX_ENTRY_PRICE:
                        size_usdc = self.max_risk
                        size_shares = size_usdc / entry_price
                        self._position = {
                            "side": side,
                            "entry_price": entry_price,
                            "size_usdc": size_usdc,
                            "size_shares": size_shares,
                            "strike": self._strike,
                            "entry_time": now,
                            "entry_elapsed": elapsed,
                            "edge": min(self._spike_score / 4.0, 1.0),
                            "fair_price": 0.5,
                            "market_price_up": market_price_up,
                            "market_price_ts": market_price_ts,
                        }
                        return {
                            "action": "ENTER",
                            "side": side,
                            "entry_price": entry_price,
                            "edge": min(self._spike_score / 4.0, 1.0),
                            "size_usdc": size_usdc,
                            "size_shares": size_shares,
                            "time_zone": "pure_reversion",
                            "spike_score": self._spike_score,
                        }
        return None
PYEOF

conda run -n polymarket-env python3 <<'PYEOF'
import json
from pathlib import Path
content = Path("/tmp/pure_reversion_strategy.py").read_text()
spec = {
    "name": "n1_mean_revert",
    "hypothesis": "The edge model adds no value; all real alpha is in mean-reversion. No GBM fair-price, pure counter-trend on 1-sigma moves.",
    "tweak_type": "new_module",
    "new_files": [
        {"path": "active_bots/pure_reversion_strategy.py", "content": content},
    ],
    "patches": [
        {
            "file": "daemon_base_v1.py",
            "find": "from active_bots.enhanced_strategy import EnhancedStrategy",
            "replace": "from active_bots.pure_reversion_strategy import PureReversionStrategy as EnhancedStrategy",
        },
    ],
}
Path("experiments/n1_mean_revert/variant.json").write_text(json.dumps(spec, indent=2))
print("variant.json written")
PYEOF
```

- [ ] **Step 12.2: Scaffold + apply + verify**

```bash
cd /home/samsam/polymarket-hustle && \
  experiments/_tools/scaffold_variant.sh experiments/n1_mean_revert && \
  conda run -n polymarket-env python3 -m experiments._tools.patch_variant experiments/n1_mean_revert && \
  grep "PureReversionStrategy as EnhancedStrategy" experiments/n1_mean_revert/daemon_base_v1.py && \
  test -f experiments/n1_mean_revert/active_bots/pure_reversion_strategy.py
```

Expected: grep match + file exists.

- [ ] **Step 12.3: Smoke-import the patched daemon**

```bash
cd /home/samsam/polymarket-hustle/experiments/n1_mean_revert && \
  conda run -n polymarket-env python3 -c "
import sys
sys.path.insert(0, '.')
import importlib.util
spec = importlib.util.spec_from_file_location('d', 'daemon_base_v1.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
s = m.EnhancedStrategy(max_risk=50.0)
print('imports OK; class:', type(s).__name__)
"
```

Expected: `imports OK; class: PureReversionStrategy`.

- [ ] **Step 12.4: Commit**

```bash
cd /home/samsam/polymarket-hustle && \
  git add -f experiments/n1_mean_revert/variant.json && \
  git commit -m "feat(experiments): add n1_mean_revert variant (pure reversion, no edge model)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 13: n3_momentum variant.json + momentum_strategy.py

Symmetric to n1 but enters *with* the spike when no reversion by T+90.

**Files:**
- Create: `experiments/n3_momentum/variant.json`

- [ ] **Step 13.1: Write variant.json (strategy source + JSON wrap)**

```bash
cd /home/samsam/polymarket-hustle
mkdir -p experiments/n3_momentum
cat > /tmp/momentum_strategy.py <<'PYEOF'
"""Momentum-follow strategy — bets on sustained early spikes.

Mirrors SqueezeDetector's detection phase but reverses the entry condition:
if at T+90 the deviation is still ≥ 85% of its peak (spike is *not* reverting),
enter WITH the spike direction.
"""
from __future__ import annotations

import math
import time
from typing import Any

from .base_strategy import MARKET_DURATION, MAX_RISK, SPREAD_COST
from .pricing.constants import SECONDS_PER_YEAR

SPIKE_THRESHOLD = 2.0
SPIKE_WINDOW = 60
ENTRY_CHECK_T = 90
SUSTAIN_FRAC = 0.85
TP_DELTA = 0.08
SL_DELTA = 0.10
FORCE_EXIT_S = 30
MIN_ENTRY_PRICE = 0.05
MAX_ENTRY_PRICE = 0.95


class MomentumStrategy:
    """Enters with spike direction when spike persists past T+90."""

    def __init__(self, max_risk: float = MAX_RISK, **_: Any):
        self.max_risk = max_risk
        self.squeeze = None
        self._t_zero: float | None = None
        self._strike: float | None = None
        self._max_deviation = 0.0
        self._spike_direction: str | None = None
        self._spike_score = 0.0
        self._candidate = False
        self._checked_entry = False
        self._position: dict[str, Any] | None = None
        self._resolved = False

    def reset(self, t_zero: float | None = None, strike: float | None = None) -> None:
        self._t_zero = t_zero
        self._strike = strike
        self._max_deviation = 0.0
        self._spike_direction = None
        self._spike_score = 0.0
        self._candidate = False
        self._checked_entry = False
        self._position = None
        self._resolved = False

    @property
    def has_position(self) -> bool:
        return self._position is not None

    def on_tick(
        self,
        btc_price: float,
        market_price_up: float,
        sigma: float,
        t_zero: float,
        market_price_ts: float = 0.0,
    ) -> dict[str, Any] | None:
        now = time.time()
        if self._t_zero != t_zero:
            self.reset(t_zero=t_zero, strike=btc_price)
        elapsed = now - self._t_zero if self._t_zero else 0.0
        time_remaining = MARKET_DURATION - elapsed

        # Detect spike
        if self._strike and self._strike > 0:
            deviation = abs(math.log(btc_price / self._strike))
            if elapsed <= SPIKE_WINDOW and deviation > self._max_deviation:
                self._max_deviation = deviation
                self._spike_direction = "up" if btc_price > self._strike else "down"
            if sigma and sigma > 1e-12 and elapsed > 0:
                expected_std = sigma * math.sqrt(min(elapsed, SPIKE_WINDOW) / SECONDS_PER_YEAR)
                self._spike_score = (self._max_deviation / expected_std) if expected_std > 0 else 0.0
                if self._spike_score >= SPIKE_THRESHOLD:
                    self._candidate = True
        else:
            deviation = 0.0

        # Exits
        if self._position is not None and market_price_up is not None:
            side = self._position["side"]
            realizable = market_price_up if side == "Up" else 1.0 - market_price_up
            entry_price = self._position["entry_price"]
            favor = realizable - entry_price
            force = time_remaining <= FORCE_EXIT_S
            action = None
            if force:
                action = "EXIT_TP" if favor >= 0 else "EXIT_SL"
            elif favor >= TP_DELTA:
                action = "EXIT_TP"
            elif -favor >= SL_DELTA:
                action = "EXIT_SL"
            if action is not None:
                size_shares = self._position["size_shares"]
                pnl = (realizable - entry_price) * size_shares - SPREAD_COST * size_shares
                out = {
                    "action": action,
                    "exit_price": max(0.0, min(1.0, realizable)),
                    "pnl": pnl,
                    "hold_time_s": now - self._position["entry_time"],
                    "side": side,
                    "forced": force,
                    "strike": self._strike,
                    "size_usdc": self._position["size_usdc"],
                    "size_shares": size_shares,
                }
                self._position = None
                self._resolved = True
                return out

        # One-shot entry check at T+ENTRY_CHECK_T
        if (
            self._position is None
            and self._candidate
            and not self._checked_entry
            and elapsed >= ENTRY_CHECK_T
            and market_price_up is not None
            and self._max_deviation > 0
        ):
            self._checked_entry = True
            sustain_ratio = deviation / self._max_deviation
            if sustain_ratio >= SUSTAIN_FRAC:
                side = "Up" if self._spike_direction == "up" else "Down"
                entry_price = market_price_up if side == "Up" else 1.0 - market_price_up
                if MIN_ENTRY_PRICE <= entry_price <= MAX_ENTRY_PRICE:
                    size_usdc = self.max_risk
                    size_shares = size_usdc / entry_price
                    self._position = {
                        "side": side,
                        "entry_price": entry_price,
                        "size_usdc": size_usdc,
                        "size_shares": size_shares,
                        "strike": self._strike,
                        "entry_time": now,
                        "entry_elapsed": elapsed,
                        "edge": min(self._spike_score / 4.0, 1.0),
                        "fair_price": 0.5,
                        "market_price_up": market_price_up,
                        "market_price_ts": market_price_ts,
                    }
                    return {
                        "action": "ENTER",
                        "side": side,
                        "entry_price": entry_price,
                        "edge": min(self._spike_score / 4.0, 1.0),
                        "size_usdc": size_usdc,
                        "size_shares": size_shares,
                        "time_zone": "momentum",
                        "spike_score": self._spike_score,
                    }
        return None
PYEOF

conda run -n polymarket-env python3 <<'PYEOF'
import json
from pathlib import Path
content = Path("/tmp/momentum_strategy.py").read_text()
spec = {
    "name": "n3_momentum",
    "hypothesis": "Early moves that don't revert by T+90 are momentum signals. Enter with spike direction.",
    "tweak_type": "new_module",
    "new_files": [
        {"path": "active_bots/momentum_strategy.py", "content": content},
    ],
    "patches": [
        {
            "file": "daemon_base_v1.py",
            "find": "from active_bots.enhanced_strategy import EnhancedStrategy",
            "replace": "from active_bots.momentum_strategy import MomentumStrategy as EnhancedStrategy",
        },
    ],
}
Path("experiments/n3_momentum/variant.json").write_text(json.dumps(spec, indent=2))
print("variant.json written")
PYEOF
```

- [ ] **Step 13.2: Scaffold + apply + verify import**

```bash
cd /home/samsam/polymarket-hustle && \
  experiments/_tools/scaffold_variant.sh experiments/n3_momentum && \
  conda run -n polymarket-env python3 -m experiments._tools.patch_variant experiments/n3_momentum && \
  cd experiments/n3_momentum && \
  conda run -n polymarket-env python3 -c "
import sys; sys.path.insert(0,'.')
import importlib.util
s = importlib.util.spec_from_file_location('d','daemon_base_v1.py')
m = importlib.util.module_from_spec(s); s.loader.exec_module(m)
print('OK', type(m.EnhancedStrategy(max_risk=10.0)).__name__)
"
```

Expected: `OK MomentumStrategy`.

- [ ] **Step 13.3: Commit**

```bash
cd /home/samsam/polymarket-hustle && \
  git add -f experiments/n3_momentum/variant.json && \
  git commit -m "feat(experiments): add n3_momentum variant (momentum-follow)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 14: n4_market_maker variant.json

Patch into `TimeBasedStrategy.on_tick` to override entry_price with a passive target, and refuse entries until market crosses it.

**Files:**
- Create: `experiments/n4_market_maker/variant.json`

- [ ] **Step 14.1: Write variant.json**

File: `experiments/n4_market_maker/variant.json`

```json
{
  "name": "n4_market_maker",
  "hypothesis": "FAK market orders overpay the spread; passive posting at fair ± 0.03 captures the spread when the market overreacts.",
  "tweak_type": "code_patch",
  "patches": [
    {
      "file": "active_bots/enhanced_strategy.py",
      "find": "                    if edge >= zone_edge_min and MIN_ENTRY_PRICE <= entry_price <= MAX_ENTRY_PRICE:",
      "replace": "                    # n4 market-maker: replace entry_price with a passive target; skip until market crosses it.\n                    _mm_offset = 0.03\n                    if side == \"Up\":\n                        _mm_target_up = max(fair - _mm_offset, 0.01)\n                        if market_price_up > _mm_target_up:\n                            break\n                        entry_price = _mm_target_up\n                    else:\n                        _mm_target_up = min(fair + _mm_offset, 0.99)\n                        if market_price_up < _mm_target_up:\n                            break\n                        entry_price = 1.0 - _mm_target_up\n                    if edge >= zone_edge_min and MIN_ENTRY_PRICE <= entry_price <= MAX_ENTRY_PRICE:"
    }
  ]
}
```

- [ ] **Step 14.2: Scaffold + apply + verify**

```bash
cd /home/samsam/polymarket-hustle && \
  experiments/_tools/scaffold_variant.sh experiments/n4_market_maker && \
  conda run -n polymarket-env python3 -m experiments._tools.patch_variant experiments/n4_market_maker && \
  grep "market-maker: replace entry_price" experiments/n4_market_maker/active_bots/enhanced_strategy.py
```

Expected: patched comment found.

- [ ] **Step 14.3: Commit**

```bash
cd /home/samsam/polymarket-hustle && \
  git add -f experiments/n4_market_maker/variant.json && \
  git commit -m "feat(experiments): add n4_market_maker variant (passive posting sim)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 15: Harness end-to-end smoke test

Prove the harness actually works on live data before launching round 1. Uses `_baseline` for 90 seconds.

- [ ] **Step 15.1: Ensure _baseline is scaffolded + patched**

```bash
cd /home/samsam/polymarket-hustle && \
  experiments/_tools/scaffold_variant.sh experiments/_baseline && \
  conda run -n polymarket-env python3 -m experiments._tools.patch_variant experiments/_baseline
```

- [ ] **Step 15.2: Make sure no other daemon is running in the project**

```bash
pgrep -af "daemon_base_v1.py" || echo "no daemons running"
```

If any are listed, stop them first (`kill <pid>`). The _baseline daemon would collide with any other instance in the main project, because the PID guard matches on `daemon_base_v1.py` + parent-dir name.

- [ ] **Step 15.3: Launch _baseline, wait 90s, stop, parse metrics**

```bash
cd /home/samsam/polymarket-hustle && \
  experiments/_tools/launch_variant.sh experiments/_baseline && \
  sleep 90 && \
  experiments/_tools/stop_variant.sh experiments/_baseline && \
  conda run -n polymarket-env python3 -m experiments._tools.metrics experiments/_baseline/daemon_state enhanced
```

Expected: `crashed: false`, `trade_count: 0` (unlikely to get a trade in 90s), ROI 0. Most importantly, the state.json parses and no crash in stdout.log.

- [ ] **Step 15.4: Sanity check the stdout log**

```bash
tail -20 /home/samsam/polymarket-hustle/experiments/_baseline/daemon_state/stdout.log
```

Expected: "daemon_base_v1 starting", "Binance connected", "RTDS connected", and no tracebacks.

- [ ] **Step 15.5: Log smoke test to session_log.md**

```bash
cd /home/samsam/polymarket-hustle && \
  python3 -c "
from pathlib import Path
from datetime import datetime, timezone
p = Path('experiments/analysis/session_log.md')
line = f'- {datetime.now(timezone.utc).strftime(\"%Y-%m-%d %H:%M UTC\")} — Harness smoke test passed — _baseline 90s dry run, metrics parsed, no crash.\n'
p.write_text(p.read_text() + line)
print('logged')
"
```

- [ ] **Step 15.6: Commit the log update**

```bash
cd /home/samsam/polymarket-hustle && \
  git add -f experiments/analysis/session_log.md && \
  git commit -m "chore(experiments): log harness smoke-test pass

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 16: Round 1 launch kickoff

This is the operational handoff: the plan's build phase is complete, and the main Claude session now drives rounds per the spec.

- [ ] **Step 16.1: Verify all 7 variants are scaffolded + patched**

```bash
cd /home/samsam/polymarket-hustle && \
  for v in _baseline n1_mean_revert n2_late_gamma n3_momentum n4_market_maker n5_no_squeeze n6_vol_regime; do
    test -f "experiments/$v/variant.json" || { echo "MISSING $v/variant.json"; exit 1; }
    test -f "experiments/$v/daemon_base_v1.py" || { echo "MISSING $v/daemon_base_v1.py"; exit 1; }
    echo "OK $v"
  done
```

Expected: 7 `OK` lines.

- [ ] **Step 16.2: Launch all 7 variants in parallel**

```bash
cd /home/samsam/polymarket-hustle && \
  for v in _baseline n1_mean_revert n2_late_gamma n3_momentum n4_market_maker n5_no_squeeze n6_vol_regime; do
    experiments/_tools/launch_variant.sh "experiments/$v"
  done && \
  sleep 5 && \
  pgrep -af "daemon_base_v1.py" | wc -l
```

Expected: 7 python processes alive.

- [ ] **Step 16.3: Record round 1 start in session_log.md**

```bash
cd /home/samsam/polymarket-hustle && \
  python3 -c "
from pathlib import Path
from datetime import datetime, timezone
p = Path('experiments/analysis/session_log.md')
line = f'- {datetime.now(timezone.utc).strftime(\"%Y-%m-%d %H:%M UTC\")} — Round 1 started — 7 variants launched in parallel: _baseline, n1-n6.\n'
p.write_text(p.read_text() + line)
print('logged')
"
```

- [ ] **Step 16.4: HANDOFF — main session takes over**

From here on, the main Claude session:

1. Waits 30 minutes (use ScheduleWakeup or a long Bash sleep).
2. Stops all 7 variants via `experiments/_tools/stop_variant.sh <dir>` for each.
3. For each variant, parses metrics via `metrics.py`.
4. Synthesizes Round 1 report → writes `experiments/analysis/round_01.md`.
5. Updates `champion.json` and appends to `champion_history.jsonl`.
6. Decides Round 2 composition per spec §Round ≥ 2 Strategy (keep top-2 survivors, generate fine-tuning variants).
7. Loops until convergence rule triggers or 10 rounds hit.
8. Overnight handoff per spec §Overnight Handoff.

The plan does not encode rounds 2+, since their composition depends on Round 1 results. All tools needed are built.

---

## Self-Review

**Spec coverage:**

- §Purpose — covered by Tasks 7–16 (analysis scaffolding + variants + launch).
- §Success Criteria — covered by Task 2 (metrics.py computes ROI + composite).
- §Architecture/Isolation — covered by Task 4 (scaffold), Task 3 (patches), Task 8–14 (variants).
- §Data feeds / Execution mode — Task 5 (launch_variant.sh sets `POLYMARKET_MODE=live POLYMARKET_DRY_RUN=1`).
- §Orchestration — documented in Task 16 handoff + spec; lives in the main session, not in plan code.
- §Round 1 variant specs — Tasks 8–14 (7 variants including baseline).
- §Round ≥ 2 Strategy — documented in Task 16; orchestrator decides at synthesis time.
- §Per-Variant Subagent Lifecycle — harness (Tasks 1–6) is what subagents will call.
- §Orchestrator Responsibilities — runs in main session; all required tools (metrics, champion.json, session_log) exist post-Task 7.
- §Overnight Handoff — operational step in Task 16; uses existing `./launch_daemon.sh live dryrun`.
- §Safety — `POLYMARKET_DRY_RUN=1` enforced in Task 5 launch script, Kill switches available via daemon's existing kill-switch file.
- §Findings Log — Task 7 creates empty findings.md; orchestrator appends.
- §Documentation Artifacts — Task 7 creates all listed files.

**Placeholder scan:** No TBD/TODO. All code steps have literal code. All commands have literal arguments.

**Type consistency:**
- `VariantSpec.load()` signature matches Task 1 test + Task 3 usage.
- `apply_patches(variant_dir, spec)` matches Task 3 test + Tasks 9–14 CLI invocations via `python3 -m experiments._tools.patch_variant`.
- `parse_metrics(state_dir, strategy="enhanced")` matches Task 2 test + Task 15 CLI.
- Variant JSON keys (`name`, `hypothesis`, `tweak_type`, `env`, `patches`, `new_files`) used identically across Tasks 1, 3, 8–14.
- Variant dirs naming convention (`experiments/<name>/`) consistent.
- `runtime.json` schema (`pid`, `start_ts`, `status`, `end_ts`) consistent between launch (Task 5) and stop (Task 6).
