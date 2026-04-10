#!/usr/bin/env bash
# scrape_all.sh -- Orchestrator for multi-asset Polymarket 5-minute scraper.
#
# Coordinates 5 assets (btc, eth, sol, xrp, doge) running dataset scripts
# in phased parallel execution with colored output and progress tracking.
set -euo pipefail

# ---------------------------------------------------------------------------
# Colors
# ---------------------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
MAGENTA='\033[0;35m'
CYAN='\033[0;36m'
WHITE='\033[1;37m'
NC='\033[0m'
BOLD='\033[1m'
DIM='\033[2m'

# Per-asset color map
declare -A ASSET_COLORS=(
    [btc]="$YELLOW"
    [eth]="$BLUE"
    [sol]="$MAGENTA"
    [xrp]="$CYAN"
    [doge]="$GREEN"
)

ASSETS=(btc eth sol xrp doge)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_DIR="${SCRIPT_DIR}/data"
LOG_DIR="${DATA_DIR}/logs"
SCRAP_DIR="${SCRIPT_DIR}/scrap"

# Track the WebSocket background PID (may remain empty)
WS_PID=""

# Track start time
START_EPOCH=$(date +%s)

# ---------------------------------------------------------------------------
# Signal handling / cleanup
# ---------------------------------------------------------------------------
cleanup() {
    echo ""
    echo -e "${YELLOW}Shutting down...${NC}"
    # Kill WebSocket capture if running
    if [ -n "${WS_PID}" ] && kill -0 "$WS_PID" 2>/dev/null; then
        kill "$WS_PID" 2>/dev/null || true
        wait "$WS_PID" 2>/dev/null || true
        echo -e "  ${DIM}Stopped WebSocket capture (PID ${WS_PID})${NC}"
    fi
    # Kill any remaining child processes
    local children
    children=$(jobs -p 2>/dev/null) || true
    if [ -n "$children" ]; then
        echo "$children" | xargs -r kill 2>/dev/null || true
        wait 2>/dev/null || true
    fi
    print_summary
    echo -e "${GREEN}Cleanup complete.${NC}"
}
trap cleanup EXIT INT TERM

# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------
print_banner() {
    echo ""
    echo -e "${BOLD}${WHITE}  =====================================================${NC}"
    echo -e "${BOLD}${WHITE}  |${NC}  ${BOLD}${YELLOW}P O L Y M A R K E T${NC}   ${BOLD}${CYAN}H U S T L E${NC}              ${BOLD}${WHITE}|${NC}"
    echo -e "${BOLD}${WHITE}  |${NC}  ${DIM}Multi-Asset 5-Minute Market Scraper${NC}               ${BOLD}${WHITE}|${NC}"
    echo -e "${BOLD}${WHITE}  =====================================================${NC}"
    echo -e "  ${DIM}Started: $(date '+%Y-%m-%d %H:%M:%S %Z')${NC}"
    echo -e "  ${DIM}Assets:  BTC  ETH  SOL  XRP  DOGE${NC}"
    echo ""
}

# ---------------------------------------------------------------------------
# Dependency check
# ---------------------------------------------------------------------------
check_dependencies() {
    echo -e "${BOLD}${WHITE}[1/6] Checking dependencies...${NC}"

    # python3
    if ! command -v python3 &>/dev/null; then
        echo -e "  ${RED}ERROR: python3 not found${NC}"
        exit 1
    fi
    local pyver
    pyver=$(python3 --version 2>&1)
    echo -e "  ${GREEN}OK${NC} ${pyver}"

    # Required Python packages
    local missing=()
    for pkg in requests websockets; do
        if ! python3 -c "import ${pkg}" 2>/dev/null; then
            missing+=("$pkg")
        fi
    done
    if [ ${#missing[@]} -gt 0 ]; then
        echo -e "  ${RED}ERROR: Missing Python packages: ${missing[*]}${NC}"
        echo -e "  ${DIM}Install with: uv pip install ${missing[*]}${NC}"
        exit 1
    fi
    echo -e "  ${GREEN}OK${NC} Required packages available (requests, websockets)"

    # sqlite3 CLI (for summary)
    if ! command -v sqlite3 &>/dev/null; then
        echo -e "  ${YELLOW}WARN${NC} sqlite3 CLI not found -- summary will be limited"
    fi

    # Check scraper scripts exist
    local scripts=(01_markets.py 02_spot_prices.py 03_trades.py 04_price_histories.py 05_orderbooks.py 06_resolutions.py)
    local script_missing=0
    for s in "${scripts[@]}"; do
        if [ ! -f "${SCRAP_DIR}/${s}" ]; then
            echo -e "  ${RED}ERROR: ${SCRAP_DIR}/${s} not found${NC}"
            script_missing=1
        fi
    done
    if [ "$script_missing" -eq 1 ]; then
        exit 1
    fi
    echo -e "  ${GREEN}OK${NC} All scraper scripts present"
    echo ""
}

# ---------------------------------------------------------------------------
# Directory setup
# ---------------------------------------------------------------------------
setup_dirs() {
    echo -e "${BOLD}${WHITE}[2/6] Setting up directories...${NC}"
    mkdir -p "$DATA_DIR"
    mkdir -p "$LOG_DIR"
    echo -e "  ${GREEN}OK${NC} ${DATA_DIR}"
    echo -e "  ${GREEN}OK${NC} ${LOG_DIR}"
    echo ""
}

# ---------------------------------------------------------------------------
# Phase runner -- runs scripts for all assets in parallel, waits, reports
# ---------------------------------------------------------------------------
run_phase() {
    local phase_label="$1"
    local phase_num="$2"
    shift 2
    local scripts=("$@")

    echo -e "${BOLD}${WHITE}[${phase_num}] ${phase_label}${NC}"

    local pids=()
    local pid_labels=()

    for asset in "${ASSETS[@]}"; do
        local color="${ASSET_COLORS[$asset]}"
        for script in "${scripts[@]}"; do
            local logfile="${LOG_DIR}/${asset}_${script%.py}.log"
            echo -e "  ${color}[${asset^^}]${NC} Starting ${script}..."
            python3 "${SCRAP_DIR}/${script}" --asset "$asset" \
                > "$logfile" 2>&1 &
            pids+=($!)
            pid_labels+=("${asset}:${script}")
        done
    done

    # Wait with spinner
    local spin='|/-\'
    local i=0
    while true; do
        local running=0
        for pid in "${pids[@]}"; do
            if kill -0 "$pid" 2>/dev/null; then
                ((running++))
            fi
        done
        if [ "$running" -eq 0 ]; then
            break
        fi
        local c="${spin:i%${#spin}:1}"
        printf "\r  ${DIM}%s %d process(es) running...${NC}  " "$c" "$running"
        ((i++)) || true
        sleep 0.5
    done
    printf "\r                                                  \r"

    # Collect exit codes
    local failed=0
    local fail_details=()
    for idx in "${!pids[@]}"; do
        if ! wait "${pids[$idx]}"; then
            ((failed++))
            fail_details+=("${pid_labels[$idx]}")
        fi
    done

    if [ "$failed" -eq 0 ]; then
        echo -e "  ${GREEN}OK ${phase_label} complete${NC} (${#pids[@]} processes)"
    else
        echo -e "  ${RED}WARNING: ${failed}/${#pids[@]} process(es) failed -- check logs:${NC}"
        for detail in "${fail_details[@]}"; do
            local a="${detail%%:*}"
            local s="${detail##*:}"
            local color="${ASSET_COLORS[$a]}"
            echo -e "    ${color}[${a^^}]${NC} ${s}  ->  ${LOG_DIR}/${a}_${s%.py}.log"
        done
    fi
    echo ""
}

# ---------------------------------------------------------------------------
# WebSocket capture (background)
# ---------------------------------------------------------------------------
start_ws_capture() {
    echo -e "${BOLD}${WHITE}[3/6] WebSocket live capture...${NC}"
    local ws_script="${SCRAP_DIR}/07_ws_live.py"
    if [ ! -f "$ws_script" ]; then
        echo -e "  ${YELLOW}SKIP${NC} ${ws_script} not found -- skipping WS capture"
        echo ""
        return
    fi
    local ws_log="${LOG_DIR}/ws_live.log"
    python3 "$ws_script" > "$ws_log" 2>&1 &
    WS_PID=$!
    echo -e "  ${GREEN}OK${NC} WebSocket capture running (PID ${WS_PID})"
    echo ""
}

# ---------------------------------------------------------------------------
# Stop WebSocket capture
# ---------------------------------------------------------------------------
stop_ws_capture() {
    if [ -z "$WS_PID" ]; then
        return
    fi
    if kill -0 "$WS_PID" 2>/dev/null; then
        kill "$WS_PID" 2>/dev/null || true
        wait "$WS_PID" 2>/dev/null || true
        echo -e "  ${GREEN}OK${NC} WebSocket capture stopped (PID ${WS_PID})"
    else
        echo -e "  ${DIM}WebSocket process already exited${NC}"
    fi
    WS_PID=""
}

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print_summary() {
    local end_epoch
    end_epoch=$(date +%s)
    local elapsed=$((end_epoch - START_EPOCH))
    local mins=$((elapsed / 60))
    local secs=$((elapsed % 60))

    echo ""
    echo -e "${BOLD}${WHITE}  ===================== SUMMARY ======================${NC}"
    echo -e "  ${DIM}Elapsed: ${mins}m ${secs}s${NC}"
    echo ""

    local has_sqlite3=0
    if command -v sqlite3 &>/dev/null; then
        has_sqlite3=1
    fi

    for asset in "${ASSETS[@]}"; do
        local color="${ASSET_COLORS[$asset]}"
        local db="${DATA_DIR}/${asset}5m.db"
        if [ -f "$db" ]; then
            local size
            size=$(du -h "$db" | cut -f1)
            if [ "$has_sqlite3" -eq 1 ]; then
                local markets trades price_histories orderbooks spot resolutions
                markets=$(sqlite3 "$db" "SELECT COUNT(*) FROM markets" 2>/dev/null || echo "0")
                trades=$(sqlite3 "$db" "SELECT COUNT(*) FROM trades" 2>/dev/null || echo "0")
                price_histories=$(sqlite3 "$db" "SELECT COUNT(*) FROM price_histories" 2>/dev/null || echo "0")
                orderbooks=$(sqlite3 "$db" "SELECT COUNT(*) FROM orderbooks" 2>/dev/null || echo "0")
                spot=$(sqlite3 "$db" "SELECT COUNT(*) FROM spot" 2>/dev/null || echo "0")
                resolutions=$(sqlite3 "$db" "SELECT COUNT(*) FROM resolutions" 2>/dev/null || echo "0")
                echo -e "  ${color}[${asset^^}]${NC} ${size}  |  ${markets} markets  ${trades} trades  ${price_histories} prices  ${orderbooks} books  ${spot} candles  ${resolutions} resolutions"
            else
                echo -e "  ${color}[${asset^^}]${NC} ${size}"
            fi
        else
            echo -e "  ${color}[${asset^^}]${NC} ${DIM}no database${NC}"
        fi
    done

    echo ""
    echo -e "${BOLD}${WHITE}  =====================================================${NC}"
    echo ""
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
main() {
    print_banner
    check_dependencies
    setup_dirs
    start_ws_capture

    # Phase 1: Markets + Spot prices (10 processes)
    run_phase "Market catalogs & spot prices" "4/6" \
        01_markets.py 02_spot_prices.py

    # Phase 2: Trades + Price histories + Orderbooks (15 processes)
    run_phase "Trades, price histories & orderbooks" "5/6" \
        03_trades.py 04_price_histories.py 05_orderbooks.py

    # Phase 3: Resolutions (5 processes)
    run_phase "Resolutions" "6/6" \
        06_resolutions.py

    # Stop WebSocket capture
    echo -e "${BOLD}${WHITE}Stopping WebSocket capture...${NC}"
    stop_ws_capture
    echo ""
}

main
