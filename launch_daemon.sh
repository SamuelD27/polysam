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
BOOK_FEED_DIR="$STATE_DIR/book_feed"
SCRAPES_ROOT="$STATE_DIR/scrapes"
# Minimum free MB in daemon_state partition before we allow a launch.
# 2 GB covers the spec's 50 MB/h/token estimate for a multi-hour capture.
MIN_FREE_MB=2048

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

# ── Dashboard picker: DASHBOARD=legacy|textual|rust (legacy default) ─────
# legacy  = pre-Textual rich.Layout dashboard at tui/python/dashboard_legacy.py
#           (stable, default, what you had before the Textual rewrite)
# textual = post-rewrite Textual dashboard at tui/python/dashboard.py
# rust    = ratatui polychart binary (IN DEVELOPMENT, opt-in only)
POLYCHART_BIN="$PROJECT_DIR/tui/target/release/polychart"
pick_dashboard_cmd() {
    local choice="${DASHBOARD:-legacy}"
    case "$choice" in
        rust)
            echo "[launch] DASHBOARD=rust is IN DEVELOPMENT -- not verified against live daemon. Use DASHBOARD=legacy to revert." >&2
            if [[ -x "$POLYCHART_BIN" ]]; then
                echo "[launch] -> starting RUST TUI ($POLYCHART_BIN)" >&2
                echo "$POLYCHART_BIN"
            else
                echo "[launch] DASHBOARD=rust but $POLYCHART_BIN missing;" >&2
                echo "[launch]   build it with:  cd tui && cargo build --release" >&2
                echo "[launch]   falling back to legacy dashboard" >&2
                echo "[launch] -> starting LEGACY PYTHON TUI (tui/python/dashboard_legacy.py)" >&2
                echo "python3 $PROJECT_DIR/tui/python/dashboard_legacy.py"
            fi
            ;;
        textual)
            echo "[launch] -> starting TEXTUAL PYTHON TUI (tui/python/dashboard.py)" >&2
            echo "python3 $PROJECT_DIR/tui/python/dashboard.py"
            ;;
        legacy)
            echo "[launch] -> starting LEGACY PYTHON TUI (tui/python/dashboard_legacy.py)" >&2
            echo "python3 $PROJECT_DIR/tui/python/dashboard_legacy.py"
            ;;
        python)
            # Back-compat alias: old DASHBOARD=python used to mean "the Python TUI"
            # which at the time meant the Textual one. Now that legacy is the
            # default, interpret bare `python` as legacy (the new Python default).
            echo "[launch] DASHBOARD=python is deprecated; use legacy or textual. Treating as legacy." >&2
            echo "[launch] -> starting LEGACY PYTHON TUI (tui/python/dashboard_legacy.py)" >&2
            echo "python3 $PROJECT_DIR/tui/python/dashboard_legacy.py"
            ;;
        *)
            echo "[launch] DASHBOARD=$choice not recognised (use legacy|textual|rust);" >&2
            echo "[launch]   falling back to legacy" >&2
            echo "[launch] -> starting LEGACY PYTHON TUI (tui/python/dashboard_legacy.py)" >&2
            echo "python3 $PROJECT_DIR/tui/python/dashboard_legacy.py"
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

# Scraper pre-flight gates — run BEFORE launching daemon so we abort cleanly
# on a half-started session (no orphan daemon if disk/deps/stale-pid fails).
# Function is defined later but bash hoists function-lookup to call time.
preflight_scraper_gates_now() {
    # Gate 1: no live scraper pid from a prior session.
    if [[ -d "$SCRAPES_ROOT" ]]; then
        local pf pid
        while IFS= read -r pf; do
            pid=$(cat "$pf" 2>/dev/null || true)
            if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
                echo "[launch] ABORT: existing scraper pid=$pid still running ($pf)" >&2
                echo "[launch]        kill it first, or remove the stale pidfile." >&2
                exit 4
            fi
        done < <(find "$SCRAPES_ROOT" -name 'scraper.pid' -print 2>/dev/null || true)
    fi
    # Gate 2: disk space (2 GB minimum; spec estimates 50 MB/h/token).
    local free_mb
    free_mb=$(df -BM --output=avail "$STATE_DIR" | tail -1 | tr -dc '0-9')
    if [[ -z "$free_mb" ]] || [[ "$free_mb" -lt "$MIN_FREE_MB" ]]; then
        echo "[launch] ABORT: only ${free_mb:-0} MB free in $STATE_DIR; need >= $MIN_FREE_MB MB" >&2
        exit 5
    fi
    echo "[launch] disk check: ${free_mb} MB free (>= ${MIN_FREE_MB} MB required)"
    # Gate 3: scraper deps importable in the same python that will run it.
    python3 -c "import websockets, requests, py_clob_client" 2>/dev/null \
        || { echo "[launch] ABORT: scraper deps missing (websockets/requests/py_clob_client)" >&2; exit 6; }
}
mkdir -p "$SCRAPES_ROOT"
preflight_scraper_gates_now

# Session bootstrap — generated here (daemon_base_v1 has no session_id concept).
SESSION_ID=$(date -u +"%Y-%m-%dT%H-%M-%SZ")
LAUNCH_TS_UTC=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
LAUNCH_TS_NS=$(date -u +"%s%N")
echo "[launch] session_id=$SESSION_ID"

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

# ── L2 book scraper (auto-launched alongside daemon) ─────────────────────
# Rationale: every live/dryrun session needs full L2 capture for R2.2
# book-walked replay. See docs/backtest_implementation_progress.md §R2.2.
# scrape_book.py hard-codes its output path (FEED_DIR = daemon_state/book_feed),
# so the session dir gets a symlink pointing at the canonical location plus a
# session_window_ns field in the manifest for reconcile.py to filter by time.
SCRAPER_PID=""
SCRAPE_SESSION_DIR=""
DAEMON_EXIT_CODE=""
SCRAPER_EXIT_CODE=""

start_scraper() {
    SCRAPE_SESSION_DIR="$SCRAPES_ROOT/$SESSION_ID"
    mkdir -p "$SCRAPE_SESSION_DIR"
    # Relative symlink keeps the session dir portable across repo moves.
    if [[ ! -e "$SCRAPE_SESSION_DIR/book_feed" ]]; then
        ln -s "../../book_feed" "$SCRAPE_SESSION_DIR/book_feed"
    fi

    local scraper_log="$SCRAPE_SESSION_DIR/scraper.log"
    local scraper_pidfile="$SCRAPE_SESSION_DIR/scraper.pid"

    if [[ "$MODE" == "live" ]]; then
        # Same wrapper as the daemon. Public WS isn't auth-gated but we
        # keep egress consistent and the Gamma token-discovery call stays
        # inside the tunnel.
        run_in_ns "$(which python3)" "$PROJECT_DIR/scripts/scrape_book.py" \
            > "$scraper_log" 2>&1 &
    else
        python3 "$PROJECT_DIR/scripts/scrape_book.py" \
            > "$scraper_log" 2>&1 &
    fi
    SCRAPER_PID=$!
    echo "$SCRAPER_PID" > "$scraper_pidfile"
    echo "[launch] scraper pid=$SCRAPER_PID  log=$scraper_log"

    # Confirm scraper stays up briefly. Don't abort the daemon if it
    # dies — capture is degraded, trading is not. Operator decides.
    sleep 5
    if ! kill -0 "$SCRAPER_PID" 2>/dev/null \
       && ! pgrep -f "python.*scripts/scrape_book\.py" >/dev/null 2>&1; then
        echo "[launch] ERROR scraper failed to stay up for 5s; capture degraded" >&2
        echo "[launch]       check $scraper_log (daemon continues running)" >&2
        SCRAPER_PID=""
    fi
}

stop_scraper() {
    # Lifecycle-independent from daemon: if daemon crashed mid-session,
    # we still call this once to flush gzip tails.
    if [[ -z "$SCRAPER_PID" ]] && [[ -n "$SCRAPE_SESSION_DIR" ]] \
       && [[ -f "$SCRAPE_SESSION_DIR/scraper.pid" ]]; then
        SCRAPER_PID=$(cat "$SCRAPE_SESSION_DIR/scraper.pid" 2>/dev/null || true)
    fi
    if [[ -z "$SCRAPER_PID" ]]; then
        SCRAPER_EXIT_CODE="none"
        return 0
    fi
    if kill -0 "$SCRAPER_PID" 2>/dev/null; then
        echo "[launch] stopping scraper pid=$SCRAPER_PID"
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
            echo "[launch] scraper did not exit in 10s; SIGKILL (gzip tail may be short)"
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
    [[ -n "$SCRAPE_SESSION_DIR" ]] && rm -f "$SCRAPE_SESSION_DIR/scraper.pid"
}

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
    echo "[launch] manifest: $SCRAPE_SESSION_DIR/manifest.json"
}

update_manifest_stop() {
    [[ -z "$SCRAPE_SESSION_DIR" ]] && return 0
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

start_scraper
write_manifest_launch

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
            DAEMON_EXIT_CODE="sigkill"
        else
            DAEMON_EXIT_CODE="sigterm"
        fi

        wait "$DAEMON_PID" 2>/dev/null || true
    else
        DAEMON_EXIT_CODE="already_dead"
    fi
}

CLEANUP_DONE=0
cleanup_session() {
    # Idempotent — second call returns immediately so the manifest's
    # stop_ts and exit-code fields keep the values from the first call.
    # stop_daemon/stop_scraper would otherwise overwrite their
    # *_EXIT_CODE state variable to "already_dead" on a re-entry,
    # corrupting what update_manifest_stop later writes.
    if [[ "$CLEANUP_DONE" == "1" ]]; then
        return 0
    fi
    CLEANUP_DONE=1
    stop_live_gui
    stop_daemon
    stop_scraper
    update_manifest_stop
    echo "[launch] daemon stopped"
}

# EXIT covers normal script end and traps that fall through. HUP covers
# terminal close. INT/TERM cover Ctrl-C and external `kill` of the
# launcher pid. SIGKILL is untrappable; nothing can prevent that orphan.
trap 'cleanup_session' EXIT HUP INT TERM

# ── TUI dashboard in foreground (DASHBOARD=python|rust, python default) ──
echo "[launch] starting TUI dashboard (Ctrl-C stops dashboard + daemon)"
sleep 2
# shellcheck disable=SC2046
$(pick_dashboard_cmd) || true

# Dashboard exited; EXIT trap runs cleanup_session.
