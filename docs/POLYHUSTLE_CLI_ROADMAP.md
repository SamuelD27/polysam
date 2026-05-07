# POLYHUSTLE_CLI_ROADMAP — what stands between us and one entry point

## Plain-language summary

The polyhustle.cli runtime is the modular, testable replacement for the
1958-line daemon_base_v1.py monolith. Today it powers the new keyboard-
driven `./launch` menu but is **not** yet a complete drop-in replacement
for the legacy script. Three features remain in launch_daemon.sh that
polyhustle.cli must absorb before the legacy path can be retired:
**L2 book scraper integration**, **session manifest emission**, and
**tunnel-verify pre-flight for live_real**. This document inventories
each gap with a concrete acceptance criterion. Until they land, the
operator runs capture-producing sessions through `./launch_daemon.sh`
and interactive non-capture sessions through `./launch`. When all three
gaps close, `launch_daemon.sh` becomes a thin shim that calls
`launch --preset _legacy_*` and the deprecation banner becomes real.

This doc is a tracking-issue file, not a design doc. Each section
links to the legacy code that defines today's behaviour and to the
polyhustle.cli touchpoint that needs the work.

---

## 1. L2 book scraper integration

**Today (legacy):**
`launch_daemon.sh:start_scraper()` spawns `scripts/scrape_book.py` as
a sibling subprocess of the daemon, writes the pid to
`daemon_state/scrapes/<session_id>/scraper.pid`, and gates on a 5-second
liveness check before declaring the session capture-ready. `stop_scraper()`
runs bounded SIGTERM-then-SIGKILL escalation in `cleanup_session()`.

**polyhustle.cli touchpoint:**
`polyhustle/cli.py:run_async` builds the Orchestrator and runs it. It
does not own a scraper subprocess. Adding scraper management means
deciding between two patterns:

- **Subprocess management.** The Orchestrator (or a sibling supervisor)
  spawns `scripts/scrape_book.py` and supervises its lifecycle. Lowest
  invasion; preserves the existing scraper code untouched.
- **Direct embedding.** The scraper's WS-subscription and gzipped-JSONL
  emission becomes a coroutine inside the polyhustle event loop. Cleaner
  long-term; bigger lift.

**Acceptance criterion:**
A `polyhustle.cli` paper-mode session, with no further wrapping,
produces `daemon_state/book_feed/<YYYY-MM-DD>/<slug>.jsonl.gz` files at
the same density (~675 events/sec on a representative slug) the legacy
script emits. Side-by-side replay of identical wallclock windows
through `experiments/backtest/harness.py` produces identical output.

**Risk if delayed:** every R2.2-style capture, every sweep input, and
every reconcile pass routes through `launch_daemon.sh`. Capacity to
run two simultaneous sessions (one per path) on the same host is
limited because the scraper is keyed to `daemon_state/book_feed/`
(no per-session output dir).

---

## 2. Session manifest emission

**Today (legacy):**
`launch_daemon.sh:write_manifest_launch()` writes
`daemon_state/scrapes/<session_id>/manifest.json` at launch with
session_id, launch_ts (UTC string + ns int), mode, daemon_pid,
scraper_pid, netns, scrape_output_dir, scrape_canonical_dir,
events_jsonl_path, daemon_log_path, git_sha, git_branch.
`update_manifest_stop()` patches in stop_ts and exit codes when
the trap fires.

**Consumer surface (why it matters):**
- `experiments/backtest/reconcile.py` joins on `session_window_ns`
  (launch_ts_ns to stop_ts_ns).
- `polyhustle/data/replay.py` (Session C stub) reads the manifest to
  know which book_feed window a replay session covers.
- The new `./launch` menu's step 3c (replay capture selection) reads
  manifest.json to populate the menu — sessions without a manifest are
  invisible to replay.

**polyhustle.cli touchpoint:**
`polyhustle/cli.py:run_async` does not write a manifest. It reuses
`daemon_base_v1.STATE_DIR` and `EventLogger(EVENTS_FILE)` so the
events.jsonl ends up in the same place the legacy script's events.jsonl
ends up — but the manifest itself is bash-emitted and lives outside
the python runtime.

**Acceptance criterion:**
A `polyhustle.cli` session, with no wrapping, writes a manifest file
schema-compatible with the existing
`daemon_state/scrapes/2026-05-05T12-50-04Z/manifest.json` such that
`./launch`'s step 3c menu lists it AND `experiments/backtest/reconcile.py`
parses session_window_ns correctly without modification.

**Implementation note:** the manifest fields `git_sha` /
`git_branch` are bash-friendly (`git -C "$PROJECT_DIR" rev-parse`).
A python equivalent should use `subprocess.run(["git", ...])` rather
than vendoring a python git library.

---

## 3. Tunnel-verify pre-flight for live_real

**Today (legacy):**
`launch_daemon.sh:verify_tunnel()` runs `curl -s --max-time 10
https://ifconfig.me` inside the polybot netns BEFORE launching the
daemon. Aborts the session if the request fails (FAILED) or warns
loudly if the visible IP looks APAC. Catches the silent-failure mode
where the namespace exists but the WireGuard tunnel inside it is dead
or routing to a Singapore exit — the daemon would launch cleanly and
every CLOB POST would 403.

**polyhustle.cli touchpoint:**
`polyhustle/cli.py:_build_trader("live_real")` calls into
`daemon_base_v1.build_executor()` to produce a Live executor. There
is no pre-flight. The launcher (`launch:step8_launch`) brings up the
namespace via `sudo "$POLY_DIR/setup-polybot-ns.sh"` but does NOT
verify the exit IP afterward.

**Acceptance criterion:**
For `execution_mode=live_real`, polyhustle.cli (or a thin live-mode
preflight wrapper inside it) runs the curl-through-netns check and
fails fast (non-zero exit + clear message) on tunnel-down or
APAC-IP states BEFORE the LiveExecutor begins signing/posting orders.

**Risk if delayed:** the new `./launch --preset _legacy_live` path
brings up the netns but skips this check. An operator who builds a
preset and re-runs it on a session where the tunnel has rotted will
launch a live trader that silently 403s on every order. The legacy
`launch_daemon.sh live` path retains the verify and is the safe path
for live_real until this lands.

---

## 4. Migration plan

Sequence (rough, not commitments):

1. **Ship the new launcher (this branch).** Done.
2. **Add scraper subprocess management to polyhustle.cli.** Lowest-risk
   first because the scraper code itself is untouched. Behaviour
   parity test: side-by-side capture comparison vs legacy script.
3. **Add manifest emission to polyhustle.cli.** Schema-compatible.
   Existing reconcile and replay-menu consumers should not need code
   changes — that's the schema-compat acceptance criterion.
4. **Add tunnel-verify pre-flight to polyhustle.cli's live_real path.**
   Smallest of the three; needs to run inside the netns before the
   first signed order.
5. **Flip the legacy script into a thin shim.** The body shrinks back
   to ~70 lines: deprecation banner + ensure_legacy_preset +
   exec `launch --preset _legacy_*`. The current C6 commit already
   expressed this shape; this step restores it once the underlying
   path supports the full feature set.
6. **Remove this roadmap doc** when all four items above are checked
   off.

Until step 5 lands, the bright-line rule is in CLAUDE.md §17 and
docs/LAUNCHER.md "When to use which" — capture-producing sessions go
through launch_daemon.sh, interactive non-capture sessions go through
launch.

---

## 5. Deferred — known issues with a trigger condition

These are real issues identified during diagnostic work but consciously
deferred. Each entry documents the trigger condition that should cause
us to un-defer it.

### 5.1 F1 — periodic snapshotter starves under sustained book traffic

**Where:** `scripts/scrape_book.py:_handle_book` sets
`st.last_snapshot_ts = time.time()` after applying every WS book event
(`scrape_book.py:603`). The periodic snapshotter at
`scrape_book.py:462` skips any token whose `now - last_snapshot_ts <
SNAPSHOT_CADENCE_S`. When book events arrive faster than every 5 s
(the active period of any liquid market — observed at 16/sec inside
the polybot netns during R3), the snapshotter NEVER writes a periodic
snapshot for that token.

**Why deferred:** the only consumer that needs reconstructable state
is `polyhustle/data/replay.py`, and replay reads `book` events and
`price_change` deltas directly (`replay.py:228-247`). Replay is
unaffected; ad-hoc analysis tools that read snapshots-only would
starve, but the only such tool was the (now retired) ad-hoc bash
spot-check predicate, replaced by `scripts/check_book_feed.py` —
which counts all event classes and doesn't depend on periodic
snapshots either.

**Trigger to un-defer:** any new tool needs snapshot density > 0.2 Hz
per token under sustained book traffic. (Equivalently: any operator
workflow that grep / replay-walks ONLY `type=="snapshot"` records
and expects a record every ~5 s during active windows.)

**Fix shape (when un-deferred):** either stop updating
`st.last_snapshot_ts` from `_handle_book`, or change the snapshotter
gate to `≥ N seconds since last *periodic* snapshot, regardless of
book events`. Track periodic-only timestamp in a separate field. Ship
default-off behind a feature flag per CLAUDE.md §7. Diagnostic
context: `reports/r3_failure_diagnostic.md` §6 / Finding F1.

---

## 6. Non-goals

These were on the legacy launch_daemon.sh path but are deliberately NOT
on this roadmap:

- **TUI dashboard auto-spawn.** The dashboard launching method is what
  the new `./launch` menu's step 1 consolidates. polyhustle.cli should
  not also spawn a dashboard subprocess; the operator either runs the
  TUI manually alongside, or uses the menu's step 1 (once polyhustle.cli
  consumes `tui_layout` from the JSON config).
- **Web GUI auto-spawn (`scripts/live_dashboard.py`).** Optional read-only
  state-file watcher; not pipeline-critical. Operators who want it run
  it manually.
- **`status` / `attach` / `preflight` / `refresh_cache` subcommands.**
  Grab-bag operator conveniences. Their replacements are documented in
  docs/LAUNCHER.md §"Migrating from launch_daemon.sh"; they're not on
  the path-consolidation roadmap.

These items can land in polyhustle.cli later if the operator decides
they should, but they don't block legacy-script retirement.
