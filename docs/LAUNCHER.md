# LAUNCHER — `launch` reference

## Plain-language summary

`launch` is the new keyboard-driven entry point for the polyhustle
trading bot. It walks the operator through a short menu (TUI layout →
run mode → strategy → execution mode → optional knobs → save preset →
confirm) and writes a JSON config that the modular daemon
(`polyhustle.cli`) reads. Saved presets at
`~/.polymarket-hustle/presets/<name>.json` let the operator skip the
menu on subsequent runs via `launch --preset <name>`. The previous
shell script `./launch_daemon.sh` has been replaced with a deprecation
shim that forwards to `launch --preset _legacy_default` for backwards
compatibility with cron jobs. This document is the user-facing
reference; the contract between the menu and the daemon is the JSON
schema in `polyhustle/cli.py`'s `LaunchConfig`.

---

## 1. Prerequisites

`launch` is a bash script that uses an external TUI binary for the
menu. It auto-detects, in this order:

1. **whiptail** (default on Debian/Ubuntu/Mint/RHEL/Fedora/Arch via
   `libnewt`). Install with `sudo apt install whiptail` or the
   distro-equivalent.
2. **dialog** (older but functionally equivalent). Install with
   `sudo apt install dialog`.
3. If neither is present the launcher exits non-zero with an install
   hint. Both accept the same `--menu` / `--checklist` / `--inputbox`
   / `--yesno` / `--msgbox` flags.

The menu strings include Nerd Font glyphs. If your terminal has a
[Nerd Font](https://www.nerdfonts.com/) installed (FiraCode Nerd Font
is known to work) the icons render correctly; otherwise they render
as boxes — the menu still works.

---

## 2. The menu

Run `./launch` from the repo root. Each step is a separate prompt;
**Enter** confirms, **Esc** backs up one step, **Ctrl-C** cancels
without writing config.

### Step 1 — TUI layout

| Choice           | Meaning                                              |
|------------------|------------------------------------------------------|
| `textual`        | Python Textual dashboard (current default)           |
| `ratatui` [DEV]  | Rust ratatui — in development, falls back to textual |

The Rust binary isn't wired into `polyhustle.cli` yet; selecting it
emits a warning msgbox and falls back to Python Textual.

### Step 2 — Run mode

| Choice        | Meaning                                                                |
|---------------|------------------------------------------------------------------------|
| `main`        | One trader strategy + optional benchmark observers (shadow-trade)      |
| `comparison`  | 2-3 strategies all running as traders, each with its own paper wallet  |
| `replay`      | Replay a captured session through the strategies                       |

### Step 3 — Strategy selection

Branches by mode chosen in step 2.

- **3a (main):** single-select main strategy (the trader). Then
  multi-select benchmarks from the remaining strategies, with an
  explicit `(none)` option to deliberately run no shadows.
- **3b (comparison):** multi-select 2-3 strategies. The launcher
  re-prompts if the count is out of bounds.
- **3c (replay):** list of subdirs under `daemon_state/scrapes/`,
  newest first. Each entry shows mode, duration, and session_id.
  In-progress captures (no `stop_ts`) are skipped.

### Step 4 — Execution mode

Only shown for `main`. Comparison and replay force the execution
mode automatically.

| Choice        | Meaning                                                                |
|---------------|------------------------------------------------------------------------|
| `live_real`   | Posts orders to Polymarket CLOB; requires the polybot WireGuard netns  |
| `live_dryrun` | Live websockets + signs orders, no POST                                |
| `paper`       | Websockets + simulated fills (no signing)                              |

For comparison: forced to `live_dryrun` with separate paper wallets.
For replay: forced to `replay`.

### Step 5 — Optional params

A sequence of input prompts pre-filled with the daemon's defaults.
Hit Enter to keep the default; type a new value to override; Esc to
back up to the previous prompt within step 5 (Esc on the first
prompt returns to step 4).

| Knob                              | Default | Notes                                                      |
|-----------------------------------|---------|------------------------------------------------------------|
| `max_trade_size_usdc`             | 5       | Per-trade ceiling                                          |
| `max_daily_loss_usdc`             | 50      | Daily loss circuit breaker                                 |
| `sigma_haircut`                   | 1.0     | Phase 5 F04 — multiplicative σ adjustment                  |
| `sl_absolute_against`             | unset   | Phase 4A — hard SL cap                                     |
| `sl_decay_enable`                 | 0       | Phase 4B — SL time-decay                                   |
| `position_size_edge_cap`          | unset   | Phase 5b — linear-interp input cap                         |
| `walked_vwap_partial_ok`          | 0       | Accept partial entry fills                                 |
| `min_top_of_book_shares_ratio`    | 1.0     | Top-size floor (sweep-design axis)                         |

When the operator's value matches the default, the key is **omitted**
from the JSON. Saved presets only carry knobs that were actually
changed, which makes diffs readable.

### Step 6 — Save as preset

Yes/No. On Yes, prompts for a name and copies the assembled config to
`~/.polymarket-hustle/presets/<name>.json`. Names must match
`[A-Za-z0-9_.-]+`. The temp config at `/tmp/polyhustle-launch-<pid>.json`
is always written either way.

### Step 7 — Confirmation

Shows the assembled config; pick `launch` / `edit` / `cancel`.
`edit` returns to step 6 (further Esc walks further back).
`cancel` exits cleanly with no daemon spawned.

### Step 8 — Launch

Invokes `python -m polyhustle.cli --config /tmp/polyhustle-launch-<pid>.json`.
For `live_real` the call is wrapped in `sudo ip netns exec polybot …`
(WireGuard tunnel). The temp config is removed when the launcher exits
or catches Ctrl-C / SIGTERM.

### Step 9 — Re-launch shortcut

```sh
launch --preset <name>          # bypass the menu, use saved preset
launch --list-presets           # print saved preset names, exit 0
launch --help                   # banner + usage, exit 0
```

---

## 3. JSON config format

The launcher writes (and `polyhustle.cli` reads) a JSON file matching
the `LaunchConfig` schema in `polyhustle/cli.py`. Example for `main`
mode:

```json
{
  "mode": "main",
  "tui_layout": "textual",
  "main_strategy": "walked_vwap",
  "benchmarks": ["refined", "enhanced", "base"],
  "execution_mode": "paper",
  "params": {}
}
```

Example for `comparison` mode (note: no `main_strategy` /
`benchmarks`; comparison strategies each get a separate paper wallet):

```json
{
  "mode": "comparison",
  "tui_layout": "textual",
  "comparison_strategies": ["refined", "walked_vwap"],
  "execution_mode": "live_dryrun",
  "params": {"max_trade_size_usdc": "10"}
}
```

Example for `replay`:

```json
{
  "mode": "replay",
  "tui_layout": "textual",
  "execution_mode": "replay",
  "replay_session": "2026-05-05T12-50-04Z",
  "params": {}
}
```

When the JSON omits `main_strategy` or `benchmarks` in replay mode,
`LaunchConfig` falls back to its built-in defaults
(`walked_vwap` / `[refined, enhanced, base]`).

---

## 4. Preset directory

```
~/.polymarket-hustle/presets/
├── _legacy_default.json     # auto-installed by launch_daemon.sh shim
├── _legacy_live.json        # ditto, for `./launch_daemon.sh live`
├── _legacy_dryrun.json      # ditto, for `./launch_daemon.sh live dry`
└── <your-presets>.json
```

The `_legacy_*` presets are managed by `launch_daemon.sh` and may be
overwritten on every legacy invocation. Don't edit them by hand —
write your own preset under a different name instead. The directory is
created lazily on first run.

Presets are plain JSON; you can version them under a separate dotfiles
repo if you want them tracked. The launcher doesn't care about file
content as long as it parses against `LaunchConfig.from_dict()`.

---

## 5. Migrating from launch_daemon.sh

| Old invocation                        | New invocation                                            |
|---------------------------------------|-----------------------------------------------------------|
| `./launch_daemon.sh`                  | `launch` (interactive) or `launch --preset _legacy_default` |
| `./launch_daemon.sh paper`            | `launch --preset _legacy_default`                         |
| `./launch_daemon.sh live`             | `launch --preset _legacy_live`                            |
| `./launch_daemon.sh live dry`         | `launch --preset _legacy_dryrun`                          |
| `./launch_daemon.sh status`           | (removed) read `daemon_state/scrapes/` manifests directly |
| `./launch_daemon.sh attach`           | (removed) run the dashboard binary directly               |
| `./launch_daemon.sh preflight`        | (removed) run `python scripts/onboard_check.py` directly  |
| `./launch_daemon.sh refresh_cache`    | (removed) run `python scripts/refresh_v2_balance_cache.py` directly |

The `_legacy_*` presets reproduce the JSON config that `polyhustle.cli`
needs for the matching execution mode. They do **not** reproduce the
full feature set the legacy script bundled (L2 book scraper, TUI
dashboard, session manifest, kill-stale-daemon sweep, netns
preflight). Those features are subsumed gradually as `polyhustle.cli`
grows; until then, operators who need them must continue invoking
the underlying scripts directly.

---

## 6. Headless tests

The launcher honours two env vars for testability:

- `POLYHUSTLE_LAUNCH_TEST_MODE=1` — short-circuit every `whiptail` /
  `dialog` call. Selections are read from `LAUNCH_TEST_*` env vars
  instead.
- `POLYHUSTLE_LAUNCH_DRY_RUN=1` — short-circuit the `python -m
  polyhustle.cli` shell-out. The launcher prints the assembled JSON
  config and exits 0 without spawning a daemon.

Mapping of menu steps to test-mode env vars:

| Menu step                   | Test-mode env var                                          |
|-----------------------------|------------------------------------------------------------|
| Step 1 (TUI layout)         | `LAUNCH_TEST_TUI_LAYOUT={textual,ratatui}`                 |
| Step 2 (run mode)           | `LAUNCH_TEST_RUN_MODE={main,comparison,replay}`            |
| Step 3a (main strategy)     | `LAUNCH_TEST_MAIN_STRATEGY={base,enhanced,refined,walked_vwap}` |
| Step 3a (benchmarks)        | `LAUNCH_TEST_BENCHMARKS=` comma-separated names, or `none` |
| Step 3b (comparison)        | `LAUNCH_TEST_COMPARISON_STRATEGIES=` comma-separated       |
| Step 3c (replay capture)    | `LAUNCH_TEST_REPLAY_SESSION=` session_id                   |
| Step 4 (execution mode)     | `LAUNCH_TEST_EXECUTION_MODE={live_real,live_dryrun,paper}` |
| Step 5 (each param)         | `LAUNCH_TEST_PARAM_<UPPERCASE_NAME>=` value (set even empty) |
| Step 6 (save preset)        | `LAUNCH_TEST_SAVE_PRESET=` name                            |
| Steps 7-8                   | (no input — autoadvance)                                   |

The test rig auto-detects whiptail / dialog and works on hosts that
have neither, because TUI detection is also bypassed when test mode
is on. Tests are at `tests/launcher/test_launch_menu.py`.

---

## 7. Trap behaviour and SIGINT cleanup

- **Clean exit** (menu cancelled, daemon completed normally): the EXIT
  trap removes `/tmp/polyhustle-launch-<pid>.json`.
- **SIGINT (Ctrl-C)** while the daemon is running: the trap sends
  SIGTERM to the daemon's pid, waits up to `SHUTDOWN_TIMEOUT_S=5`s,
  SIGKILLs if needed, then removes the temp config.
- **SIGTERM / SIGHUP**: same path as SIGINT.
- Saved preset files at `~/.polymarket-hustle/presets/` are **never**
  touched by cleanup. Only the temp config in `/tmp/` is reclaimed.

---

## 8. Where the contract lives

| Concern                               | Source of truth                                |
|---------------------------------------|------------------------------------------------|
| Menu flow + state machine             | `launch` (this script)                        |
| JSON config schema                    | `polyhustle/cli.py::LaunchConfig`             |
| Strategy registry (`main_strategy`, `benchmarks`, `comparison_strategies`) | `polyhustle/cli.py::STRATEGY_REGISTRY` |
| Preset directory                      | `$HOME/.polymarket-hustle/presets/`            |
| Replay capture inventory              | `daemon_state/scrapes/<session>/manifest.json` |

Adding a new strategy: see CLAUDE.md §15. Adding a new tunable knob to
step 5: edit `PARAM_NAMES` / `PARAM_PROMPTS` / `PARAM_DEFAULTS` (three
lock-stepped arrays in `launch`); the daemon picks up the env-var
form automatically because `polyhustle.cli._apply_params_to_env`
uppercases each key into `os.environ`.
