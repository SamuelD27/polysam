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

find "$DIR/active_bots" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
rm -rf "$DIR/active_bots/plots" 2>/dev/null || true
find "$DIR/active_bots" -name "backtest_*.csv" -delete 2>/dev/null || true

echo "scaffolded: $DIR"
