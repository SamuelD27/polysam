#!/usr/bin/env bash
# launch_daemon.sh — Run daemon_base_v1 inside the polybot VPN namespace
# with conda polymarket-env activated, and tail state.json in a parallel pane.
#
# Usage:
#   ./launch_daemon.sh              # paper mode (default, no tunnel needed)
#   ./launch_daemon.sh live         # live mode (requires tunnel + keys)
#   ./launch_daemon.sh live dryrun  # live mode with DRY_RUN=1 (logs orders, no posts)
#   ./launch_daemon.sh preflight    # run scripts/onboard_check.py inside tunnel, exit
#   ./launch_daemon.sh status       # attach dashboard to an already-running daemon
#
# The Polymarket CLOB geo-blocks Singapore IPs. Live mode runs inside the
# 'polybot' netns (WireGuard -> ProtonVPN Ireland) set up by
# /home/samsam/polymarket/setup-polybot-ns.sh. Paper mode needs no tunnel.

set -euo pipefail

PROJECT_DIR="/home/samsam/polymarket-hustle"
CONDA_ENV="polymarket-env"
NS="polybot"
POLY_DIR="/home/samsam/polymarket"
STATE_DIR="$PROJECT_DIR/daemon_state"
STATE_FILE="$STATE_DIR/state.json"
LOG_FILE="$STATE_DIR/daemon.log"
PID_FILE="$STATE_DIR/daemon.pid"

MODE="${1:-paper}"
DRYRUN="${2:-}"

# ── Conda activation ─────────────────────────────────────────────────────
CONDA_BASE="$(/home/samsam/miniconda3/bin/conda info --base 2>/dev/null || echo /home/samsam/miniconda3)"
# shellcheck source=/dev/null
source "$CONDA_BASE/etc/profile.d/conda.sh"
conda activate "$CONDA_ENV"

cd "$PROJECT_DIR"
mkdir -p "$STATE_DIR"

# ── Sanity: deps ─────────────────────────────────────────────────────────
echo "[launch] python: $(which python3)"
python3 -c "import websockets, requests, dotenv" \
    || { echo "[launch] missing deps; run: uv pip install -r requirements.txt"; exit 2; }

if [[ "$MODE" == "live" ]]; then
    python3 -c "import py_clob_client" \
        || { echo "[launch] py-clob-client missing; run: uv pip install -r requirements.txt"; exit 2; }
fi

# ── VPN namespace (live only) ────────────────────────────────────────────
ensure_ns() {
    if ip netns list 2>/dev/null | grep -qw "$NS"; then
        echo "[launch] namespace $NS already up"
        return 0
    fi
    echo "[launch] bringing up $NS via $POLY_DIR/setup-polybot-ns.sh (needs sudo)"
    sudo "$POLY_DIR/setup-polybot-ns.sh"
}

verify_tunnel() {
    local ip
    ip=$(sudo ip netns exec "$NS" curl -s --max-time 10 https://ifconfig.me || echo FAILED)
    echo "[launch] tunnel external IP: $ip"
    if [[ "$ip" == "FAILED" ]]; then
        echo "[launch] tunnel not reachable — aborting"
        exit 3
    fi
    if [[ "$ip" =~ ^1\.|^27\.|^103\.|^118\.|^119\. ]]; then
        echo "[launch] WARNING: IP looks like APAC; verify this isn't a Singapore IP"
    fi
}

run_in_ns() {
    # Run a command as samsam inside the polybot netns with conda activated.
    sudo -E ip netns exec "$NS" sudo -E -u samsam \
        --preserve-env=HOME,PATH,CONDA_PREFIX,CONDA_DEFAULT_ENV,VIRTUAL_ENV,POLYMARKET_MODE,POLYMARKET_DRY_RUN \
        env HOME=/home/samsam \
        "$@"
}

# ── Preflight shortcut: runs onboard_check inside the tunnel and exits ───
if [[ "$MODE" == "preflight" ]]; then
    ensure_ns
    verify_tunnel
    echo "[launch] running preflight inside ns=$NS"
    run_in_ns "$(which python3)" "$PROJECT_DIR/scripts/onboard_check.py"
    exit $?
fi

# ── Dashboard picker: DASHBOARD=rust|python (rust default) ───────────────
# If DASHBOARD is unset we default to rust; if the polychart binary is
# missing we print a build hint and fall back to python so developers
# aren't locked out by a missing target/release/ artefact.
POLYCHART_BIN="$PROJECT_DIR/tui/target/release/polychart"
pick_dashboard_cmd() {
    local choice="${DASHBOARD:-rust}"
    case "$choice" in
        rust)
            if [[ -x "$POLYCHART_BIN" ]]; then
                echo "$POLYCHART_BIN"
            else
                echo "[launch] DASHBOARD=rust but $POLYCHART_BIN missing;" >&2
                echo "[launch]   build it with:  cd tui && cargo build --release" >&2
                echo "[launch]   falling back to python dashboard" >&2
                echo "python3 $PROJECT_DIR/tui/python/dashboard.py"
            fi
            ;;
        python)
            echo "python3 $PROJECT_DIR/tui/python/dashboard.py"
            ;;
        *)
            echo "[launch] DASHBOARD=$choice not recognised (use rust|python);" >&2
            echo "[launch]   falling back to python" >&2
            echo "python3 $PROJECT_DIR/tui/python/dashboard.py"
            ;;
    esac
}

# ── Status: attach dashboard to a daemon that's already running ──────────
if [[ "$MODE" == "status" ]]; then
    if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
        echo "[launch] attaching to daemon pid=$(cat "$PID_FILE")"
    else
        echo "[launch] WARNING: no running daemon (no PID file or stale). Dashboard will show last snapshot."
    fi
    # shellcheck disable=SC2046
    exec $(pick_dashboard_cmd)
fi

# ── Kill any stale daemon (PID-file + process scan) ──────────────────────
# pgrep returns 1 when no match — under `set -e` that would silently abort
# the script, so we always append `|| true` and check the result string.
list_daemons() {
    # pgrep -f matches its own argv, so use -a + awk to keep only cmdlines
    # that look like a real daemon (python ... daemon_base_v1.py, no `pgrep`
    # or `awk` substrings).
    pgrep -af "daemon_base_v1\.py" 2>/dev/null \
        | awk '/python/ && !/pgrep/ && !/awk/ && !/launch_daemon/ {print $1}' \
        || true
}

existing_pids=$(list_daemons | tr '\n' ' ')
if [[ -n "${existing_pids// /}" ]]; then
    echo "[launch] found existing daemon process(es): $existing_pids"
    for pid in $existing_pids; do
        if [[ "$pid" != "$$" ]]; then
            echo "[launch] stopping pid=$pid"
            kill "$pid" 2>/dev/null || echo "[launch]   (could not signal $pid; may need sudo)"
        fi
    done
    sleep 3
    still=$(list_daemons | tr '\n' ' ')
    if [[ -n "${still// /}" ]]; then
        echo "[launch] forcing kill on: $still"
        for pid in $still; do
            kill -9 "$pid" 2>/dev/null || true
        done
        sleep 1
    fi
fi
rm -f "$PID_FILE"

# Clear kill switch in case it was left on from a previous session
rm -f "$STATE_DIR/KILL"

# ── Launch ───────────────────────────────────────────────────────────────
export POLYMARKET_MODE="$MODE"
if [[ "$DRYRUN" == "dryrun" || "$DRYRUN" == "dry_run" ]]; then
    export POLYMARKET_DRY_RUN=1
    echo "[launch] DRY_RUN=1 — orders will be logged, NOT posted"
fi

echo "[launch] mode=$MODE dry_run=${POLYMARKET_DRY_RUN:-0}"

if [[ "$MODE" == "live" ]]; then
    ensure_ns
    verify_tunnel
    echo "[launch] starting daemon inside ns=$NS (sudo required)"
    run_in_ns "$(which python3)" "$PROJECT_DIR/daemon_base_v1.py" &
    DAEMON_PID=$!
else
    python3 "$PROJECT_DIR/daemon_base_v1.py" &
    DAEMON_PID=$!
fi

echo "[launch] daemon pid=$DAEMON_PID"

# ── Live web GUI (aiohttp + websocket, reads state files only) ──────────
GUI_PORT=3006
GUI_URL="http://localhost:${GUI_PORT}"

start_live_gui() {
    if [[ ! -f "$PROJECT_DIR/scripts/live_dashboard.py" ]]; then
        echo "[launch] scripts/live_dashboard.py missing; skipping web GUI"
        return
    fi
    local spid
    python3 "$PROJECT_DIR/scripts/live_dashboard.py" --port "$GUI_PORT" \
        > "$STATE_DIR/live_gui.log" 2>&1 &
    spid=$!
    echo "$spid" > "$STATE_DIR/live_gui.pid"
    echo "[launch] ============================================================"
    echo "[launch]   web GUI:  $GUI_URL   (pid=$spid)"
    echo "[launch]   log:      $STATE_DIR/live_gui.log"
    echo "[launch]   rich TUI: starts below, Ctrl-C stops everything"
    echo "[launch] ============================================================"
    if [[ -z "${NO_BROWSER:-}" ]] && command -v xdg-open >/dev/null 2>&1; then
        (sleep 2 && xdg-open "$GUI_URL" >/dev/null 2>&1 &) >/dev/null 2>&1 || true
    fi
}

stop_live_gui() {
    local spid=""
    if [[ -f "$STATE_DIR/live_gui.pid" ]]; then
        spid=$(cat "$STATE_DIR/live_gui.pid" 2>/dev/null || true)
    fi
    if [[ -n "$spid" ]] && kill -0 "$spid" 2>/dev/null; then
        echo "[launch] stopping live GUI pid=$spid"
        kill "$spid" 2>/dev/null || true
        for _ in 1 2 3 4 5 6; do
            if ! kill -0 "$spid" 2>/dev/null; then
                break
            fi
            sleep 1
        done
        if kill -0 "$spid" 2>/dev/null; then
            echo "[launch] live GUI did not exit in 6s; SIGKILL"
            kill -9 "$spid" 2>/dev/null || true
        fi
        wait "$spid" 2>/dev/null || true
    fi
    pkill -f "python.*scripts/live_dashboard\.py" 2>/dev/null || true
    rm -f "$STATE_DIR/live_gui.pid"
}

start_live_gui

stop_daemon() {
    # Bounded escalation: SIGTERM the known PID + cmdline match,
    # wait up to 6s, then SIGKILL anything still alive. The outer
    # sudo/ip-netns wrappers are root-owned and our TERM may not
    # propagate to the python leaf; pkill -f by cmdline reaches it
    # directly since the leaf runs as samsam.
    if kill -0 "$DAEMON_PID" 2>/dev/null; then
        echo "[launch] stopping daemon pid=$DAEMON_PID"
        kill "$DAEMON_PID" 2>/dev/null || true
        pkill -TERM -f "python.*daemon_base_v1\.py" 2>/dev/null || true

        for _ in 1 2 3 4 5 6; do
            if ! kill -0 "$DAEMON_PID" 2>/dev/null \
               && ! pgrep -f "python.*daemon_base_v1\.py" >/dev/null 2>&1; then
                break
            fi
            sleep 1
        done

        if kill -0 "$DAEMON_PID" 2>/dev/null \
           || pgrep -f "python.*daemon_base_v1\.py" >/dev/null 2>&1; then
            echo "[launch] daemon did not exit in 6s; SIGKILL"
            kill -9 "$DAEMON_PID" 2>/dev/null || true
            pkill -KILL -f "python.*daemon_base_v1\.py" 2>/dev/null || true
        fi

        wait "$DAEMON_PID" 2>/dev/null || true
    fi
}

trap 'stop_live_gui; stop_daemon; exit 0' INT TERM

# ── TUI dashboard in foreground (DASHBOARD=rust|python, rust default) ────
echo "[launch] starting TUI dashboard (Ctrl-C stops dashboard + daemon)"
sleep 2
# shellcheck disable=SC2046
$(pick_dashboard_cmd) || true

# Dashboard exited (Ctrl-C). Stop live GUI + daemon too.
stop_live_gui
stop_daemon
echo "[launch] daemon stopped"
