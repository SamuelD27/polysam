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
