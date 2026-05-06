#!/usr/bin/env bash
# launch_daemon.sh — capture-producing wrapper around daemon_base_v1.py.
#
# This script is preserved (in a slimmer form) BECAUSE the modular
# polyhustle.cli that the new `./launch` menu drives does not yet
# integrate the L2 book scraper, session manifest emission, or
# tunnel-verify pre-flight that capture-producing sessions require.
# The migration plan is at docs/POLYHUSTLE_CLI_ROADMAP.md; when those
# three items absorb into polyhustle.cli, this script becomes a thin
# shim over `launch --preset _legacy_*`.
#
# WHEN TO USE WHICH:
#   - launch_daemon.sh paper / live / live dryrun
#       Capture-producing sessions (R2.2-style analysis, sweep input,
#       reconcile-comparable). Spawns the L2 scraper, writes the session
#       manifest, runs tunnel verify on live, hard-fails on stale-daemon
#       or full disk. Runs daemon_base_v1.py (the legacy daemon).
#       Equivalent to pre-Phase-5 behaviour minus the auto-spawned TUI
#       dashboard and web GUI.
#   - ./launch (the new menu)
#       Interactive non-capture operator work. Runs polyhustle.cli (the
#       modular daemon). Does NOT auto-spawn the scraper, write a
#       manifest, or verify the tunnel. Suitable for comparison and
#       replay modes; presets save quick re-launch configs.
#   The two paths produce different on-disk artefacts. See
#   docs/LAUNCHER.md and CLAUDE.md §17 for the bright-line rule.
#
# Removed in the 2026-05-06 cleanup pass (preserved in git history):
#   - TUI dashboard auto-spawn (DASHBOARD=…). Run manually:
#       python tui/python/dashboard_legacy.py        (rich.Layout legacy)
#       python tui/python/dashboard.py               (Textual)
#       tui/target/release/polychart                 (ratatui, IN DEV)
#   - Web GUI auto-spawn (scripts/live_dashboard.py on port 3006).
#   - status / attach / preflight / refresh_cache subcommands. See
#     docs/LAUNCHER.md §"Migrating from launch_daemon.sh" for the
#     replacement workflow.
#
# Usage:
#   ./launch_daemon.sh             # paper mode (default)
#   ./launch_daemon.sh paper       # same
#   ./launch_daemon.sh live        # live mode (requires polybot netns + keys)
#   ./launch_daemon.sh live dryrun # live, signs orders, NO POST

set -euo pipefail

# PROJECT_DIR is overrideable for tests under tests/launcher/. Production
# callers never set the override; tests point it at a temp dir holding
# stub daemon_base_v1.py + scripts/scrape_book.py so the wrapper logic
# (preflight, manifest, traps) is exercised without actually spawning a
# trader. Single point of test-mode divergence; everything else runs
# the production code path.
PROJECT_DIR="${LAUNCH_DAEMON_PROJECT_DIR_OVERRIDE:-/home/samsam/polymarket-hustle}"
CONDA_ENV="polymarket-env"
NS="polybot"
POLY_DIR="/home/samsam/polymarket"
STATE_DIR="$PROJECT_DIR/daemon_state"
LOG_FILE="$STATE_DIR/daemon.log"
PID_FILE="$STATE_DIR/daemon.pid"
BOOK_FEED_DIR="$STATE_DIR/book_feed"
SCRAPES_ROOT="$STATE_DIR/scrapes"
MIN_FREE_MB=2048

MODE="${1:-paper}"
DRYRUN="${2:-}"

# ── Removed subcommand stubs ─────────────────────────────────────────────
case "$MODE" in
    status|attach|preflight|refresh_cache)
        cat >&2 <<EOM
[launch_daemon.sh] subcommand '$MODE' no longer dispatched here.
                   See docs/LAUNCHER.md §"Migrating from launch_daemon.sh"
                   for the replacement workflow.
EOM
        exit 1
        ;;
esac

# ── Conda activation ─────────────────────────────────────────────────────
# Idempotent — skip if already inside the env. Tests that don't have
# conda available pre-export CONDA_DEFAULT_ENV=polymarket-env to skip.
if [[ "${CONDA_DEFAULT_ENV:-}" != "$CONDA_ENV" ]]; then
    CONDA_BASE="$(/home/samsam/miniconda3/bin/conda info --base 2>/dev/null \
        || echo /home/samsam/miniconda3)"
    # shellcheck source=/dev/null
    source "$CONDA_BASE/etc/profile.d/conda.sh"
    conda activate "$CONDA_ENV"
fi

cd "$PROJECT_DIR"
mkdir -p "$STATE_DIR" "$SCRAPES_ROOT"

# ── Sanity: deps ─────────────────────────────────────────────────────────
echo "[launch_daemon] python: $(which python3)"
python3 -c "import websockets, requests, dotenv" \
    || { echo "[launch_daemon] missing deps; run: uv pip install -r requirements.txt"; exit 2; }
if [[ "$MODE" == "live" ]]; then
    python3 -c "import py_clob_client" \
        || { echo "[launch_daemon] py-clob-client missing; run: uv pip install -r requirements.txt"; exit 2; }
fi

# ── netns helpers (live only) ────────────────────────────────────────────
ensure_ns() {
    if ip netns list 2>/dev/null | grep -qw "$NS"; then
        echo "[launch_daemon] namespace $NS already up"
        return 0
    fi
    echo "[launch_daemon] bringing up $NS via $POLY_DIR/setup-polybot-ns.sh (sudo)"
    sudo "$POLY_DIR/setup-polybot-ns.sh"
}

verify_tunnel() {
    # Curl ifconfig.me through the netns. Fail-fast if unreachable; warn
    # if the visible IP looks APAC (likely Singapore — the CLOB geo-blocks
    # SG, so a live POST would silently 403 even though the daemon launches
    # cleanly).
    local ip
    ip=$(sudo ip netns exec "$NS" curl -s --max-time 10 https://ifconfig.me || echo FAILED)
    echo "[launch_daemon] tunnel external IP: $ip"
    if [[ "$ip" == "FAILED" ]]; then
        echo "[launch_daemon] tunnel not reachable — aborting"
        exit 3
    fi
    if [[ "$ip" =~ ^1\.|^27\.|^103\.|^118\.|^119\. ]]; then
        echo "[launch_daemon] WARNING: IP looks like APAC; verify this isn't a Singapore IP"
    fi
}

run_in_ns() {
    sudo -E ip netns exec "$NS" sudo -E -u samsam \
        --preserve-env=HOME,PATH,CONDA_PREFIX,CONDA_DEFAULT_ENV,VIRTUAL_ENV,POLYMARKET_MODE,POLYMARKET_DRY_RUN,WALKED_VWAP_EXIT_ENABLE,WALKED_VWAP_EXIT_STALENESS_S,WALKED_VWAP_EXIT_PARTIAL_OK,WALKED_VWAP_EXIT_FALLBACK_MID \
        env HOME=/home/samsam \
        "$@"
}

# ── Stale-daemon sweep ───────────────────────────────────────────────────
# pgrep returns 1 when no match — under `set -e` that would silently abort,
# so always append `|| true` and check the result string.
list_daemons() {
    pgrep -af "daemon_base_v1\.py" 2>/dev/null \
        | awk '/python/ && !/pgrep/ && !/awk/ && !/launch_daemon/ {print $1}' \
        || true
}

existing_pids=$(list_daemons | tr '\n' ' ')
if [[ -n "${existing_pids// /}" ]]; then
    echo "[launch_daemon] found existing daemon process(es): $existing_pids"
    for pid in $existing_pids; do
        if [[ "$pid" != "$$" ]]; then
            echo "[launch_daemon] stopping pid=$pid"
            kill "$pid" 2>/dev/null || echo "[launch_daemon]   (could not signal $pid; may need sudo)"
        fi
    done
    sleep 3
    still=$(list_daemons | tr '\n' ' ')
    if [[ -n "${still// /}" ]]; then
        echo "[launch_daemon] forcing kill on: $still"
        for pid in $still; do
            kill -9 "$pid" 2>/dev/null || true
        done
        sleep 1
    fi
fi
rm -f "$PID_FILE"
rm -f "$STATE_DIR/KILL"  # clear kill switch from any prior session

# ── Preflight: scraper PID + disk space + scraper deps ───────────────────
preflight_scraper_gates_now() {
    local pf pid
    if [[ -d "$SCRAPES_ROOT" ]]; then
        while IFS= read -r pf; do
            pid=$(cat "$pf" 2>/dev/null || true)
            if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
                echo "[launch_daemon] ABORT: existing scraper pid=$pid still running ($pf)" >&2
                exit 4
            fi
            if [[ -e "$pf" ]]; then
                echo "[launch_daemon] preflight: removing stale $pf (pid=${pid:-empty})"
                rm -f "$pf"
            fi
        done < <(find "$SCRAPES_ROOT" -name 'scraper.pid' -print 2>/dev/null || true)
    fi
    local legacy_pf="$BOOK_FEED_DIR/scrape_book.pid"
    if [[ -f "$legacy_pf" ]]; then
        pid=$(cat "$legacy_pf" 2>/dev/null || true)
        if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
            echo "[launch_daemon] ABORT: legacy scraper pid=$pid still running ($legacy_pf)" >&2
            exit 4
        fi
        rm -f "$legacy_pf"
    fi
    local free_mb
    free_mb=$(df -BM --output=avail "$STATE_DIR" | tail -1 | tr -dc '0-9')
    if [[ -z "$free_mb" ]] || [[ "$free_mb" -lt "$MIN_FREE_MB" ]]; then
        echo "[launch_daemon] ABORT: only ${free_mb:-0} MB free in $STATE_DIR; need >= $MIN_FREE_MB MB" >&2
        exit 5
    fi
    echo "[launch_daemon] disk check: ${free_mb} MB free (>= ${MIN_FREE_MB} MB required)"
    python3 -c "import websockets, requests, py_clob_client" 2>/dev/null \
        || { echo "[launch_daemon] ABORT: scraper deps missing" >&2; exit 6; }
}

preflight_scraper_gates_now

# ── Session bootstrap ────────────────────────────────────────────────────
SESSION_ID=$(date -u +"%Y-%m-%dT%H-%M-%SZ")
LAUNCH_TS_UTC=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
LAUNCH_TS_NS=$(date -u +"%s%N")
SCRAPE_SESSION_DIR="$SCRAPES_ROOT/$SESSION_ID"
mkdir -p "$SCRAPE_SESSION_DIR"
if [[ ! -e "$SCRAPE_SESSION_DIR/book_feed" ]]; then
    ln -s "../../book_feed" "$SCRAPE_SESSION_DIR/book_feed"
fi
echo "[launch_daemon] session_id=$SESSION_ID"

# ── Daemon launch ────────────────────────────────────────────────────────
export POLYMARKET_MODE="$MODE"
if [[ "$DRYRUN" == "dryrun" || "$DRYRUN" == "dry_run" ]]; then
    export POLYMARKET_DRY_RUN=1
    echo "[launch_daemon] DRY_RUN=1 — orders will be logged, NOT posted"
fi
echo "[launch_daemon] mode=$MODE dry_run=${POLYMARKET_DRY_RUN:-0}"

if [[ "$MODE" == "live" ]]; then
    ensure_ns
    verify_tunnel
    echo "[launch_daemon] starting daemon inside ns=$NS"
    run_in_ns "$(which python3)" "$PROJECT_DIR/daemon_base_v1.py" &
    DAEMON_PID=$!
else
    python3 "$PROJECT_DIR/daemon_base_v1.py" &
    DAEMON_PID=$!
fi
echo "[launch_daemon] daemon pid=$DAEMON_PID"

# ── Scraper auto-spawn ───────────────────────────────────────────────────
SCRAPER_PID=""
DAEMON_EXIT_CODE=""
SCRAPER_EXIT_CODE=""

start_scraper() {
    local scraper_log="$SCRAPE_SESSION_DIR/scraper.log"
    local scraper_pidfile="$SCRAPE_SESSION_DIR/scraper.pid"
    if [[ "$MODE" == "live" ]]; then
        run_in_ns "$(which python3)" "$PROJECT_DIR/scripts/scrape_book.py" \
            > "$scraper_log" 2>&1 &
    else
        python3 "$PROJECT_DIR/scripts/scrape_book.py" \
            > "$scraper_log" 2>&1 &
    fi
    SCRAPER_PID=$!
    echo "$SCRAPER_PID" > "$scraper_pidfile"
    echo "[launch_daemon] scraper pid=$SCRAPER_PID  log=$scraper_log"
    sleep 5
    if ! kill -0 "$SCRAPER_PID" 2>/dev/null \
       && ! pgrep -f "python.*scripts/scrape_book\.py" >/dev/null 2>&1; then
        echo "[launch_daemon] ERROR scraper failed to stay up for 5s; capture degraded" >&2
        SCRAPER_PID=""
    fi
}

start_scraper

# ── Manifest emit ────────────────────────────────────────────────────────
write_manifest_launch() {
    local git_sha git_branch
    git_sha=$(git -C "$PROJECT_DIR" rev-parse --short HEAD 2>/dev/null || echo "unknown")
    git_branch=$(git -C "$PROJECT_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")
    local netns_val="null"
    [[ "$MODE" == "live" ]] && netns_val="\"$NS\""
    local manifest_mode="$MODE"
    if [[ "$MODE" == "live" && "${POLYMARKET_DRY_RUN:-0}" == "1" ]]; then
        manifest_mode="live_dryrun"
    fi
    local scraper_pid_val="${SCRAPER_PID:-null}"
    [[ -z "$scraper_pid_val" ]] && scraper_pid_val="null"
    local max_size_val="${MAX_TRADE_SIZE_USDC:-null}"
    [[ "$max_size_val" == "null" ]] || max_size_val="\"$max_size_val\""
    cat > "$SCRAPE_SESSION_DIR/manifest.json" <<EOF
{
  "session_id": "$SESSION_ID",
  "launch_ts_utc": "$LAUNCH_TS_UTC",
  "launch_ts_ns": $LAUNCH_TS_NS,
  "stop_ts_utc": null,
  "stop_ts_ns": null,
  "daemon_exit_code": null,
  "scraper_exit_code": null,
  "mode": "$manifest_mode",
  "max_trade_size_usdc": $max_size_val,
  "daemon_pid": $DAEMON_PID,
  "scraper_pid": $scraper_pid_val,
  "tokens_scraped": "auto:gamma-5m-active-window",
  "netns": $netns_val,
  "scrape_output_dir": "$SCRAPE_SESSION_DIR",
  "scrape_canonical_dir": "$BOOK_FEED_DIR",
  "scrape_output_format": "per-slug gzipped JSONL: {YYYY-MM-DD}/{slug}.jsonl.gz",
  "events_jsonl_path": "$STATE_DIR/events.jsonl",
  "daemon_log_path": "$LOG_FILE",
  "git_sha": "$git_sha",
  "git_branch": "$git_branch"
}
EOF
    echo "[launch_daemon] manifest: $SCRAPE_SESSION_DIR/manifest.json"
}

update_manifest_stop() {
    local m="$SCRAPE_SESSION_DIR/manifest.json"
    [[ ! -f "$m" ]] && return 0
    local stop_utc stop_ns
    stop_utc=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
    stop_ns=$(date -u +"%s%N")
    python3 - "$m" "$stop_utc" "$stop_ns" "${DAEMON_EXIT_CODE:-}" "${SCRAPER_EXIT_CODE:-}" <<'PY' || true
import json, sys
path, stop_utc, stop_ns, dec, sec = sys.argv[1:6]
with open(path) as f: m = json.load(f)
m["stop_ts_utc"] = stop_utc
m["stop_ts_ns"]  = int(stop_ns)
m["daemon_exit_code"]  = dec if dec else None
m["scraper_exit_code"] = sec if sec else None
with open(path, "w") as f: json.dump(m, f, indent=2)
PY
}

write_manifest_launch

# ── Cleanup ──────────────────────────────────────────────────────────────
stop_daemon() {
    if kill -0 "$DAEMON_PID" 2>/dev/null; then
        echo "[launch_daemon] stopping daemon pid=$DAEMON_PID"
        kill "$DAEMON_PID" 2>/dev/null || true
        pkill -TERM -f "python.*daemon_base_v1\.py" 2>/dev/null || true
        for _ in 1 2 3 4 5 6 7 8 9 10; do
            if ! kill -0 "$DAEMON_PID" 2>/dev/null \
               && ! pgrep -f "python.*daemon_base_v1\.py" >/dev/null 2>&1; then
                break
            fi
            sleep 1
        done
        if kill -0 "$DAEMON_PID" 2>/dev/null \
           || pgrep -f "python.*daemon_base_v1\.py" >/dev/null 2>&1; then
            echo "[launch_daemon] daemon did not exit in 10s; SIGKILL"
            kill -9 "$DAEMON_PID" 2>/dev/null || true
            pkill -KILL -f "python.*daemon_base_v1\.py" 2>/dev/null || true
            DAEMON_EXIT_CODE="sigkill"
        else
            DAEMON_EXIT_CODE="sigterm"
        fi
        wait "$DAEMON_PID" 2>/dev/null || true
    else
        DAEMON_EXIT_CODE="already_dead"
    fi
}

stop_scraper() {
    if [[ -z "$SCRAPER_PID" ]] && [[ -f "$SCRAPE_SESSION_DIR/scraper.pid" ]]; then
        SCRAPER_PID=$(cat "$SCRAPE_SESSION_DIR/scraper.pid" 2>/dev/null || true)
    fi
    if [[ -z "$SCRAPER_PID" ]]; then
        SCRAPER_EXIT_CODE="none"
        return 0
    fi
    if kill -0 "$SCRAPER_PID" 2>/dev/null; then
        echo "[launch_daemon] stopping scraper pid=$SCRAPER_PID"
        kill "$SCRAPER_PID" 2>/dev/null || true
        pkill -TERM -f "python.*scripts/scrape_book\.py" 2>/dev/null || true
        for _ in 1 2 3 4 5 6 7 8 9 10; do
            if ! kill -0 "$SCRAPER_PID" 2>/dev/null \
               && ! pgrep -f "python.*scripts/scrape_book\.py" >/dev/null 2>&1; then
                break
            fi
            sleep 1
        done
        if kill -0 "$SCRAPER_PID" 2>/dev/null \
           || pgrep -f "python.*scripts/scrape_book\.py" >/dev/null 2>&1; then
            echo "[launch_daemon] scraper did not exit in 10s; SIGKILL (gzip tail may be short)"
            kill -9 "$SCRAPER_PID" 2>/dev/null || true
            pkill -KILL -f "python.*scripts/scrape_book\.py" 2>/dev/null || true
            SCRAPER_EXIT_CODE="sigkill"
        else
            SCRAPER_EXIT_CODE="sigterm"
        fi
        wait "$SCRAPER_PID" 2>/dev/null || true
    else
        SCRAPER_EXIT_CODE="already_dead"
    fi
    rm -f "$SCRAPE_SESSION_DIR/scraper.pid"
}

CLEANUP_DONE=0
cleanup_session() {
    # Idempotent — second call returns immediately so manifest stop_ts /
    # exit_code values from the first call survive.
    if [[ "$CLEANUP_DONE" == "1" ]]; then
        return 0
    fi
    CLEANUP_DONE=1
    stop_daemon
    stop_scraper
    update_manifest_stop
    echo "[launch_daemon] session stopped"
}

trap 'cleanup_session' EXIT HUP INT TERM

# ── Foreground: wait on daemon (no auto TUI) ─────────────────────────────
echo "[launch_daemon] running. Ctrl-C stops daemon + scraper."
echo "[launch_daemon] no TUI is auto-spawned. To attach a dashboard manually:"
echo "  python tui/python/dashboard_legacy.py     # legacy rich.Layout"
echo "  python tui/python/dashboard.py            # Textual"
wait "$DAEMON_PID" || true
