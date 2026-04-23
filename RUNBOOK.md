# Polymarket Live-Trading Runbook

End-to-end command reference for the `daemon_base_v1` + `EnhancedStrategy`
stack on Polymarket BTC 5-minute binary markets.

- **Funder (proxy wallet):** `0xf5f6fbf6d64890b59a70a33e45fd705957d34049`
- **Conda env:** `polymarket-env`
- **Tunnel:** WireGuard → ProtonVPN (`polybot` netns), configured in `/home/samsam/polymarket/`
- **Strategy:** BTC only (ETH/SOL/XRP/DOGE scaffolded but paper-only)

Polymarket's authenticated CLOB endpoints are geo-blocked from Singapore, so
**live mode must run inside the `polybot` namespace**. Paper mode has no
geo restriction.

---

## 0. One-time setup

Already done on this machine. Kept here for rebuild reference.

```bash
# Conda env (already exists; packages pre-installed)
conda activate polymarket-env
cd /home/samsam/polymarket-hustle
uv pip install -r requirements.txt

# Secrets go in .env (chmod 600, gitignored) — already populated:
#   POLYMARKET_PRIVATE_KEY, POLYMARKET_FUNDER, (optional) API key/secret/passphrase
cat .env.example   # for reference
```

## 1. Activate the env

```bash
source /home/samsam/miniconda3/etc/profile.d/conda.sh
conda activate polymarket-env
cd /home/samsam/polymarket-hustle
```

## 2. Preflight (inside tunnel)

Verifies wallet, derives fresh L2 API creds, fetches balance, lists open
positions. Needs sudo (to bring up the WireGuard netns).

```bash
./launch_daemon.sh preflight
```

All-green output:
```
[OK ] POLYMARKET_PRIVATE_KEY set
[OK ] POLYMARKET_FUNDER set
[OK ] ClobClient init + api creds
[OK ] COLLATERAL balance -- $33.85 USDC
[OK ] open positions
[OK ] TokenResolver (current BTC slug)
[OK ] data-api /trades reachable
```

Any FAIL → stop and diagnose before running the daemon.

## 3. Paper mode (no tunnel needed)

Runs the strategy against live market data but only simulates fills in memory.
Safe to leave running 24/7 on the Singapore IP.

```bash
./launch_daemon.sh
# or explicitly:
./launch_daemon.sh paper
```

Ctrl-C to stop. State at `daemon_state/state.json`, logs at `daemon_state/daemon.log`.

## 4. Dry-run live (orders logged, NOT posted)

Same path as live but `create_market_order()` skips `post_order()`.
Use this to verify the full signed-payload flow before risking capital.

```bash
./launch_daemon.sh live dryrun
```

Look for `DRY_RUN market_order token=…` lines in the log — these are the
payloads that would have hit the wire.

## 5. Live (real orders)

First real run. Clamp size hard to $1 so at most $1 is risked per trade.

```bash
MAX_TRADE_SIZE_USDC=1 ./launch_daemon.sh live
```

Gradual ramp (watch paper PnL in parallel; divergence > 5¢/trade = investigate):

```bash
MAX_TRADE_SIZE_USDC=1   ./launch_daemon.sh live
MAX_TRADE_SIZE_USDC=10  ./launch_daemon.sh live
MAX_TRADE_SIZE_USDC=25  ./launch_daemon.sh live
./launch_daemon.sh live                        # full $100 default
```

## 6. Monitoring

The launcher opens a full-screen Textual TUI dashboard (`scripts/dashboard.py`).
Three-row grid: header (BTC/sigma/strike/slug/progress/connections/KILL) across
the top; middle row splits live curves (hero UP/DOWN prices, BTC line chart,
market/fair overlay) next to the refined-strategy panel (stats, persistent
per-strategy PnL mini-chart, open position, recent trades); bottom row splits
the YES-token orderbook (adaptive depth, top-N asks/spread/bids) next to the
BASE/ENHANCED paper-benchmark banner and a scrolling refined orders log.

The dashboard polls the Polymarket CLOB `/book` endpoint directly from inside
the process (2 s cadence, 10 s backoff after 3 consecutive failures). From SG
in paper mode it 403s/timeouts and shows `no book data (reason: ...)` --
that is expected. To see the book, run the dashboard inside the `polybot`
netns.

Refreshes at 2 Hz. Ctrl-C stops both the dashboard and the daemon.

To watch from a second terminal without launching a new daemon:

```bash
python3 scripts/dashboard.py
```

Structured event log (one JSON per line) is at `daemon_state/events.jsonl`.
Useful for offline analysis:

```bash
# all entries this session
jq 'select(.type=="entry_filled") | {ts, strategy, side: .position.side, px: .position.entry_price, size: .position.size_usdc}' daemon_state/events.jsonl

# realised pnl distribution
jq -r 'select(.type=="resolve" or .type=="exit_filled") | .trade.pnl' daemon_state/events.jsonl | awk '{s+=$1; n++} END {print "n="n" total="s" avg="s/n}'

# rejections (helpful when tuning risk / live fills)
jq 'select(.type=="entry_rejected" or .type=="exit_rejected")' daemon_state/events.jsonl
```

### Live web dashboard

Real-time browser dashboard on port 3006. Single-process aiohttp server that
tails daemon_state and streams deltas over a WebSocket to a TradingView
Lightweight Charts frontend. Charts append tick-by-tick (no page refresh,
no chart redraw). Auto-starts with `./launch_daemon.sh`.

```bash
# standalone (no daemon):
conda activate polymarket-env
python3 scripts/live_dashboard.py --port 3006
```

Open http://localhost:3006. History persists across reloads in
`daemon_state/dashboard_history.jsonl` (rolling 5000-row cap).

## 7. Kill switch

Stop new entries instantly without killing the process (open positions still
exit normally):

```bash
touch daemon_state/KILL
# to resume:
rm daemon_state/KILL
```

Hard stop:

```bash
kill "$(cat daemon_state/daemon.pid)"
```

## 8. Tunnel lifecycle

```bash
# bring up
sudo /home/samsam/polymarket/setup-polybot-ns.sh
# verify
sudo ip netns exec polybot curl -s https://ifconfig.me
# tear down
sudo /home/samsam/polymarket/teardown-polybot-ns.sh
```

The launcher brings the netns up automatically when needed.

## 9. Risk knobs (env overrides)

Set before running `./launch_daemon.sh live`:

```bash
export MAX_TRADE_SIZE_USDC=25      # clamp strategy sizing (default 100)
export MAX_DAILY_LOSS_USDC=100     # hit → new entries blocked until UTC rollover
export POLYMARKET_DRY_RUN=1        # skip post_order even in live mode
export KILL_SWITCH_FILE=/tmp/KILL  # custom kill-switch path
```

## 10. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `COLLATERAL balance FAIL 401` | L2 creds stale / geo-block | Clear `POLYMARKET_API_KEY/SECRET/PASSPHRASE` in `.env`; run preflight inside tunnel |
| `tunnel external IP: FAILED` | WireGuard peer unreachable | `sudo /home/samsam/polymarket/teardown-polybot-ns.sh` then re-run preflight |
| `TokenResolver FAIL` for current slug | Market not yet listed on Gamma | Wait 10–30s after a new 5-min cycle starts |
| `ENH ENTRY` logs but no fills in `data-api /trades` | Order rejected (thin book, FAK unfilled) | Check `daemon.log` for `entry unfilled (FAK)`; consider larger book or higher slippage tolerance |
| Balance low | wallet under $1 | Deposit USDC via polymarket.com (Singapore UI still works for deposits/withdrawals, only CLOB trading is geo-blocked) |

## 11. Key files

```
daemon_base_v1.py                        # main daemon (paper+live switchable)
active_bots/enhanced_strategy.py         # 3-pillar strategy (time-zone + TP/SL + squeeze)
active_bots/execution/executor.py        # Executor protocol + dataclasses
active_bots/execution/paper_executor.py  # legacy in-memory simulation
active_bots/execution/live_executor.py   # py-clob-client wrapper (FAK market orders)
active_bots/execution/token_resolver.py  # slug -> (yes_token_id, no_token_id) via Gamma
active_bots/execution/risk_manager.py    # daily loss / size / kill-switch
active_bots/execution/reconciler.py      # polls data-api /trades for fill audit
active_bots/execution/clob_client_factory.py  # ClobClient builder w/ proxy signing
active_bots/execution/event_logger.py    # structured JSONL event log
scripts/onboard_check.py                 # read-only preflight
scripts/dashboard.py                     # Textual TUI dashboard (2 Hz)
scripts/dashboard.tcss                   # Textual CSS grid for dashboard
scripts/live_dashboard.py                # live web dashboard (aiohttp + ws)
launch_daemon.sh                         # run + dashboard wrapper
.env                                     # secrets (chmod 600, gitignored)
daemon_state/state.json                  # live state snapshot (refreshed 5s)
daemon_state/daemon.log                  # human-readable rotating log
daemon_state/events.jsonl                # one JSON event per line (for analysis)
daemon_state/dashboard_history.jsonl     # live dashboard chart history (rolling 5000)
daemon_state/daemon.pid                  # PID lock
daemon_state/KILL                        # touch to block new entries
```

## 12. Current wallet snapshot

```
funder:   0xf5f6fbf6d64890b59a70a33e45fd705957d34049
balance:  $33.85 USDC
account:  50+ historical trades on record (legacy BTC 5m positions)
max size: $33 per trade feasible; default daemon cap is $100 (adjust MAX_TRADE_SIZE_USDC)
```

Deposit more USDC via polymarket.com before running the full $100 size.
