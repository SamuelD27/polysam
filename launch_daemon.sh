#!/usr/bin/env bash
# launch_daemon.sh — DEPRECATED. Forwards to `launch --preset _legacy_default`.
#
# The session B refactor (2026-05-06) replaced this script's body with a
# deprecation shim. The new entry point is the menu-driven `launch` at
# the repo root; saved presets reproduce specific configurations.
#
# Usage compat (cron / scripts):
#   ./launch_daemon.sh                — paper-mode legacy default (delegates to launch)
#   ./launch_daemon.sh paper          — same as above
#   ./launch_daemon.sh live           — live-real legacy default
#   ./launch_daemon.sh live dry       — live-dryrun legacy default
#
# Subcommands no longer dispatched here:
#   status / attach / preflight / refresh_cache — these used to bundle
#   utility shortcuts (one-line session summary, attach a dashboard,
#   onboard preflight, V2 balance-cache refresh). Their replacements
#   are documented in docs/LAUNCHER.md §"Migrating from launch_daemon.sh".
#
# The pre-deprecation script (full daemon orchestration: scraper, TUI,
# manifest, conda, kill-stale, netns, etc.) is preserved in the git
# history at the commit before feat/launch-menu's chore(launch)
# deprecation commit. `git log --follow launch_daemon.sh` walks back to
# it.

set -euo pipefail

PROJECT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PRESETS_DIR="$HOME/.polymarket-hustle/presets"

ensure_legacy_preset() {
    # Idempotently install the legacy preset matching the requested mode.
    # Operator's customisations (if any) under their preset name are not
    # touched — only the _legacy_* names are managed here.
    local preset_name="$1" exec_mode="$2"
    mkdir -p "$PRESETS_DIR"
    cat > "$PRESETS_DIR/$preset_name.json" <<EOF
{
  "execution_mode": "$exec_mode",
  "mode": "main",
  "main_strategy": "walked_vwap",
  "benchmarks": ["refined", "enhanced", "base"],
  "params": {},
  "tui_layout": "textual"
}
EOF
}

print_banner() {
    cat >&2 <<'EOM'
[launch_daemon.sh] DEPRECATED — forwarding to `launch --preset _legacy_*`.
                   The operator-facing entry point is now `./launch`.
                   See docs/LAUNCHER.md for the menu walkthrough.
EOM
}

print_subcommand_removed() {
    local sub="$1"
    cat >&2 <<EOM
[launch_daemon.sh] subcommand '$sub' no longer dispatched here.
                   See docs/LAUNCHER.md §"Migrating from launch_daemon.sh"
                   for the replacement workflow.
EOM
    exit 1
}

MODE="${1:-paper}"
DRYRUN="${2:-}"

case "$MODE" in
    status|attach|preflight|refresh_cache)
        print_subcommand_removed "$MODE"
        ;;
    paper|"")
        print_banner
        ensure_legacy_preset "_legacy_default" "paper"
        exec "$PROJECT_DIR/launch" --preset "_legacy_default"
        ;;
    live)
        if [[ "$DRYRUN" == "dry" || "$DRYRUN" == "dryrun" || "$DRYRUN" == "dry_run" ]]; then
            print_banner
            ensure_legacy_preset "_legacy_dryrun" "live_dryrun"
            exec "$PROJECT_DIR/launch" --preset "_legacy_dryrun"
        else
            print_banner
            ensure_legacy_preset "_legacy_live" "live_real"
            exec "$PROJECT_DIR/launch" --preset "_legacy_live"
        fi
        ;;
    *)
        print_banner
        echo "[launch_daemon.sh] unknown subcommand '$MODE'" >&2
        echo "[launch_daemon.sh] supported: paper / live / live dry" >&2
        exit 1
        ;;
esac
