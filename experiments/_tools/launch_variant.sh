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

ENV_LINES=$(python3 -c "
import json
spec = json.load(open('$DIR/variant.json'))
for k, v in (spec.get('env') or {}).items():
    print(f'{k}={v}')
")

source /home/samsam/miniconda3/etc/profile.d/conda.sh
conda activate polymarket-env

cd "$DIR"
mkdir -p daemon_state

# shellcheck disable=SC2086
env POLYMARKET_MODE=live POLYMARKET_DRY_RUN=1 $ENV_LINES \
    nohup python3 daemon_base_v1.py >daemon_state/stdout.log 2>&1 &
PID=$!

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
