"""Headless tests for the `launch` bash menu.

The launcher's interactive flow uses whiptail (or dialog) for menus,
which require a TTY for selection — neither tool reads `--menu` picks
from stdin reliably across the whiptail-vs-dialog split. Driving them
through `expect` is fragile and adds a system dependency.

Instead, the launcher honours `POLYHUSTLE_LAUNCH_TEST_MODE=1` and
reads its selections from `LAUNCH_TEST_*` env vars, short-circuiting
every TUI call. `POLYHUSTLE_LAUNCH_DRY_RUN=1` further short-circuits
the `python -m polyhustle.cli` shell-out so the test rig only sees
the JSON config that would have been launched.

Each test sets the env-var "transcript" for one menu walk-through,
shells out to `bash launch`, captures the printed JSON, and asserts
on its content. Tests work whether the host has whiptail, dialog, or
neither because TUI detection is also bypassed in test mode.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LAUNCH_SCRIPT = REPO_ROOT / "launch"
LAUNCH_DAEMON_SCRIPT = REPO_ROOT / "launch_daemon.sh"
TEMP_RE = re.compile(r"/tmp/polyhustle-launch-\d+\.json")


# ───────────────────────────── helpers ──────────────────────────────
def _run_launch(env_overrides: dict[str, str], *, args: list[str] | None = None,
                timeout: float = 10.0) -> subprocess.CompletedProcess:
    """Run `bash launch` with the given env overrides and return the result.

    Always sets POLYHUSTLE_LAUNCH_TEST_MODE=1 + POLYHUSTLE_LAUNCH_DRY_RUN=1
    so the launcher prints its assembled config and exits cleanly without
    spawning a daemon.
    """
    env = os.environ.copy()
    env["POLYHUSTLE_LAUNCH_TEST_MODE"] = "1"
    env["POLYHUSTLE_LAUNCH_DRY_RUN"] = "1"
    env.update(env_overrides)
    cmd = ["bash", str(LAUNCH_SCRIPT)] + (args or [])
    return subprocess.run(
        cmd, env=env, capture_output=True, text=True, timeout=timeout,
        cwd=str(REPO_ROOT),
    )


def _extract_config_json(stdout: str) -> dict:
    """Pull the JSON block out of the dry-run launcher output.

    The launcher prints `[launch] config at <path>:` followed by the
    pretty-printed JSON. Find the first `{` after that marker and
    json.loads the rest.
    """
    marker = "[launch] config at "
    idx = stdout.find(marker)
    if idx < 0:
        raise AssertionError(f"no config marker in stdout:\n{stdout}")
    brace = stdout.find("{", idx)
    if brace < 0:
        raise AssertionError(f"no JSON brace after marker:\n{stdout}")
    # JSON is pretty-printed with sort_keys; consume from { to matching }.
    blob = stdout[brace:]
    return json.loads(blob)


def _temp_config_path(stdout: str) -> str | None:
    """Return the /tmp/polyhustle-launch-<pid>.json path the run reported."""
    m = TEMP_RE.search(stdout)
    return m.group(0) if m else None


# ───────────────────────────── fixtures ─────────────────────────────
@pytest.fixture
def isolated_presets(tmp_path, monkeypatch):
    """Redirect $HOME so the launcher writes presets into a temp dir.

    Saved presets land at $HOME/.polymarket-hustle/presets/<name>.json,
    so swapping $HOME is the cleanest isolation. monkeypatch only affects
    THIS process's env; the subprocess.run call inherits the override
    via env=os.environ.copy() inside _run_launch.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    return fake_home / ".polymarket-hustle" / "presets"


# ─────────────────────────── tests ──────────────────────────────────
def test_main_refined_no_benchmarks_live_dryrun_defaults(isolated_presets):
    """Spec case 1: main → refined → none → live_dryrun → defaults.

    Asserts the resulting JSON has the right mode, strategy, empty
    benchmarks, live_dryrun execution mode, and no overridden params
    (because the operator hit Enter on every default).
    """
    result = _run_launch({
        "LAUNCH_TEST_TUI_LAYOUT": "textual",
        "LAUNCH_TEST_RUN_MODE": "main",
        "LAUNCH_TEST_MAIN_STRATEGY": "refined",
        "LAUNCH_TEST_BENCHMARKS": "none",
        "LAUNCH_TEST_EXECUTION_MODE": "live_dryrun",
    })
    assert result.returncode == 0, result.stderr
    cfg = _extract_config_json(result.stdout)
    assert cfg["mode"] == "main"
    assert cfg["main_strategy"] == "refined"
    assert cfg["benchmarks"] == []
    assert cfg["execution_mode"] == "live_dryrun"
    assert cfg["params"] == {}
    assert cfg["tui_layout"] == "textual"


def test_comparison_two_strategies_forces_live_dryrun(isolated_presets):
    """Spec case 2: comparison → refined + walked_vwap.

    Comparison mode forces execution_mode=live_dryrun (no operator
    choice). The "separate-wallet" semantic is implicit in the
    comparison mode itself — polyhustle.cli builds a fresh PaperTrader
    per assignment when mode == "comparison".
    """
    # Try to override execution_mode via env — should be ignored.
    result = _run_launch({
        "LAUNCH_TEST_TUI_LAYOUT": "textual",
        "LAUNCH_TEST_RUN_MODE": "comparison",
        "LAUNCH_TEST_COMPARISON_STRATEGIES": "refined,walked_vwap",
        "LAUNCH_TEST_EXECUTION_MODE": "live_real",  # should be overridden
    })
    assert result.returncode == 0, result.stderr
    cfg = _extract_config_json(result.stdout)
    assert cfg["mode"] == "comparison"
    assert cfg["execution_mode"] == "live_dryrun"
    assert cfg["comparison_strategies"] == ["refined", "walked_vwap"]
    # Comparison mode JSON should NOT carry main_strategy / benchmarks —
    # those are main-mode-only fields.
    assert "main_strategy" not in cfg
    assert "benchmarks" not in cfg


def test_replay_mode_picks_capture(isolated_presets):
    """Spec case 3: replay → pick a captured session.

    Need a real scrapes dir for the launcher to walk. The repo ships
    several under daemon_state/scrapes/. The test asserts the replay
    JSON carries the chosen session_id and forces execution_mode=replay.
    """
    scrapes = REPO_ROOT / "daemon_state" / "scrapes"
    if not scrapes.is_dir():
        pytest.skip("no daemon_state/scrapes/ on this checkout")
    sessions = sorted([p.name for p in scrapes.iterdir() if p.is_dir()])
    if not sessions:
        pytest.skip("no captures available")
    session = sessions[-1]
    result = _run_launch({
        "LAUNCH_TEST_TUI_LAYOUT": "textual",
        "LAUNCH_TEST_RUN_MODE": "replay",
        "LAUNCH_TEST_REPLAY_SESSION": session,
    })
    assert result.returncode == 0, result.stderr
    cfg = _extract_config_json(result.stdout)
    assert cfg["mode"] == "replay"
    assert cfg["execution_mode"] == "replay"
    assert cfg["replay_session"] == session


def test_preset_save_and_reload(isolated_presets):
    """Spec case 4: --preset <name> bypasses the menu.

    Save a preset via LAUNCH_TEST_SAVE_PRESET, then re-run with
    --preset and confirm the loaded JSON matches.
    """
    # Step 1: save.
    saved = _run_launch({
        "LAUNCH_TEST_TUI_LAYOUT": "textual",
        "LAUNCH_TEST_RUN_MODE": "main",
        "LAUNCH_TEST_MAIN_STRATEGY": "walked_vwap",
        "LAUNCH_TEST_BENCHMARKS": "refined,enhanced",
        "LAUNCH_TEST_EXECUTION_MODE": "paper",
        "LAUNCH_TEST_PARAM_MAX_TRADE_SIZE_USDC": "7",
        "LAUNCH_TEST_PARAM_SIGMA_HAIRCUT": "0.85",
        "LAUNCH_TEST_SAVE_PRESET": "test_preset_x",
    })
    assert saved.returncode == 0, saved.stderr
    saved_cfg = _extract_config_json(saved.stdout)
    preset_path = isolated_presets / "test_preset_x.json"
    assert preset_path.exists(), f"preset not saved at {preset_path}"
    on_disk = json.loads(preset_path.read_text())
    assert on_disk == saved_cfg, "preset file diverges from launched config"

    # Step 2: --preset bypasses the menu. Note that --preset does NOT
    # need POLYHUSTLE_LAUNCH_TEST_MODE — it skips the menu by branch,
    # not by short-circuit. We still set DRY_RUN to avoid spawning the
    # daemon.
    reloaded = _run_launch({}, args=["--preset", "test_preset_x"])
    assert reloaded.returncode == 0, reloaded.stderr
    reloaded_cfg = _extract_config_json(reloaded.stdout)
    assert reloaded_cfg == saved_cfg


def test_invalid_preset_name_exits_nonzero(isolated_presets):
    """--preset on a missing name fails fast with a helpful message."""
    result = _run_launch({}, args=["--preset", "definitely_not_real"])
    assert result.returncode == 3
    assert "not found" in result.stderr


def test_list_presets_returns_clean_when_empty(isolated_presets):
    """--list-presets prints a message instead of erroring on empty dir."""
    result = _run_launch({}, args=["--list-presets"])
    assert result.returncode == 0
    assert "no presets" in result.stdout


def test_temp_config_lifecycle_clean_run(isolated_presets):
    """Temp file is written, then removed by the EXIT trap on clean exit."""
    result = _run_launch({
        "LAUNCH_TEST_TUI_LAYOUT": "textual",
        "LAUNCH_TEST_RUN_MODE": "main",
        "LAUNCH_TEST_MAIN_STRATEGY": "base",
        "LAUNCH_TEST_BENCHMARKS": "none",
        "LAUNCH_TEST_EXECUTION_MODE": "paper",
    })
    assert result.returncode == 0
    tmp = _temp_config_path(result.stdout)
    assert tmp is not None, "no temp path printed in output"
    # On clean exit the EXIT trap fires cleanup() and rm -f's the temp.
    assert not Path(tmp).exists(), f"temp file lingering at {tmp}"


def test_temp_config_lifecycle_sigint(isolated_presets):
    """Temp file is removed when the launcher catches SIGINT mid-run.

    Spawn the launcher pointed at a sleep-hanging path (we use a
    LAUNCH_TEST flag that wedges it before the dry-run print), send
    SIGINT, and confirm the trap cleaned the temp.
    """
    # We don't have a built-in "wedge" flag; instead, disable DRY_RUN so
    # the script tries to invoke `python -m polyhustle.cli`. That call
    # will run for at least a beat (importing daemon, building the
    # orchestrator) before we kill it. The temp file is created BEFORE
    # the python invocation, so SIGINT-then-cleanup must remove it
    # regardless of how far the daemon got.
    env = os.environ.copy()
    env["POLYHUSTLE_LAUNCH_TEST_MODE"] = "1"
    # Note: NO POLYHUSTLE_LAUNCH_DRY_RUN; we want step8 to actually try
    # to invoke python so cleanup has work to do.
    env["LAUNCH_TEST_TUI_LAYOUT"] = "textual"
    env["LAUNCH_TEST_RUN_MODE"] = "main"
    env["LAUNCH_TEST_MAIN_STRATEGY"] = "base"
    env["LAUNCH_TEST_BENCHMARKS"] = "none"
    env["LAUNCH_TEST_EXECUTION_MODE"] = "paper"

    proc = subprocess.Popen(
        ["bash", str(LAUNCH_SCRIPT)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, cwd=str(REPO_ROOT),
        # New process group so SIGINT on us doesn't propagate to bash's
        # children; we want to test the trap's cleanup behaviour.
        start_new_session=True,
    )
    # Wait for the temp file to appear. The launcher prints its pid
    # marker only after creating the file.
    deadline = time.time() + 5.0
    pid = proc.pid
    expected_temp = f"/tmp/polyhustle-launch-{pid}.json"
    while time.time() < deadline and not Path(expected_temp).exists():
        time.sleep(0.05)
    if not Path(expected_temp).exists():
        # Daemon may have died early before writing the temp; try an
        # alternative: list /tmp/polyhustle-launch-*.json files newer
        # than the proc start.
        candidates = sorted(Path("/tmp").glob("polyhustle-launch-*.json"))
        if not candidates:
            proc.kill()
            proc.wait(timeout=5)
            pytest.skip("temp file never appeared — daemon may have failed early")
        expected_temp = str(candidates[-1])

    assert Path(expected_temp).exists(), f"temp file missing pre-SIGINT: {expected_temp}"

    # SIGINT to the process group so the trap fires.
    os.killpg(proc.pid, signal.SIGINT)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
        pytest.fail("launcher did not exit within 10s of SIGINT")

    assert not Path(expected_temp).exists(), \
        f"temp file lingering after SIGINT: {expected_temp}"


def test_optional_params_pass_through_to_json(isolated_presets):
    """LAUNCH_TEST_PARAM_<KEY> env vars land in the JSON params dict."""
    result = _run_launch({
        "LAUNCH_TEST_TUI_LAYOUT": "textual",
        "LAUNCH_TEST_RUN_MODE": "main",
        "LAUNCH_TEST_MAIN_STRATEGY": "walked_vwap",
        "LAUNCH_TEST_BENCHMARKS": "refined",
        "LAUNCH_TEST_EXECUTION_MODE": "paper",
        "LAUNCH_TEST_PARAM_MAX_TRADE_SIZE_USDC": "15",
        "LAUNCH_TEST_PARAM_SL_ABSOLUTE_AGAINST": "0.18",
        "LAUNCH_TEST_PARAM_POSITION_SIZE_EDGE_CAP": "0.30",
    })
    assert result.returncode == 0, result.stderr
    cfg = _extract_config_json(result.stdout)
    assert cfg["params"]["max_trade_size_usdc"] == "15"
    assert cfg["params"]["sl_absolute_against"] == "0.18"
    assert cfg["params"]["position_size_edge_cap"] == "0.30"


def test_help_exits_zero():
    """--help prints the banner and exits cleanly."""
    result = subprocess.run(
        ["bash", str(LAUNCH_SCRIPT), "--help"],
        capture_output=True, text=True, timeout=5,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0
    assert "launch — keyboard-driven launcher" in result.stdout
    assert "--preset" in result.stdout
    assert "Prerequisites" in result.stdout


# ─────────── launch_daemon.sh smoke tests ───────────────────────────
# These exercise the legacy capture-producing wrapper with stub daemon
# and stub scraper so the wrapper logic (preflight, manifest, traps)
# runs without spawning real trading code. The single test-mode
# divergence is LAUNCH_DAEMON_PROJECT_DIR_OVERRIDE — an env-var that
# points the script at a temp dir holding stub daemon_base_v1.py +
# scripts/scrape_book.py. Everything else runs the production code
# path.

def _stub_project_dir(tmp_path: Path) -> Path:
    """Build a fake $PROJECT_DIR with stub daemon + scraper scripts.

    The stubs sleep so they're alive long enough for the wrapper to
    pid-write the manifest. The wrapper doesn't care what the spawned
    python actually does — it records pid, waits, and traps cleanup.
    """
    proj = tmp_path / "project"
    proj.mkdir()
    (proj / "daemon_base_v1.py").write_text(
        "import time, sys\nsys.stderr.write('stub daemon up\\n')\ntime.sleep(60)\n"
    )
    scripts = proj / "scripts"
    scripts.mkdir()
    (scripts / "scrape_book.py").write_text(
        "import time, sys\nsys.stderr.write('stub scraper up\\n')\ntime.sleep(60)\n"
    )
    # The wrapper's preflight requires a daemon_state/ dir to gauge disk
    # space against. Make it.
    (proj / "daemon_state").mkdir()
    (proj / "daemon_state" / "scrapes").mkdir()
    (proj / "daemon_state" / "book_feed").mkdir()
    return proj


def _legacy_env(proj: Path) -> dict[str, str]:
    """Build the env for invoking launch_daemon.sh against a stub project."""
    env = os.environ.copy()
    env["LAUNCH_DAEMON_PROJECT_DIR_OVERRIDE"] = str(proj)
    # Skip conda activation — assume the test runner is already in
    # polymarket-env (otherwise the dependency-import check fails the
    # same way it would fail in production).
    env.setdefault("CONDA_DEFAULT_ENV", "polymarket-env")
    return env


def test_launch_daemon_paper_writes_manifest_skeleton(tmp_path):
    """launch_daemon.sh paper produces a manifest with the expected fields.

    Spawns the legacy wrapper against a stub project dir, waits up to
    10s for the manifest to appear, asserts shape, then SIGINTs and
    confirms cleanup runs (manifest gets stop_ts patched in).
    """
    proj = _stub_project_dir(tmp_path)
    proc = subprocess.Popen(
        ["bash", str(LAUNCH_DAEMON_SCRIPT), "paper"],
        env=_legacy_env(proj),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        cwd=str(proj),
        start_new_session=True,
    )
    scrapes = proj / "daemon_state" / "scrapes"
    deadline = time.time() + 15.0
    manifest = None
    while time.time() < deadline and manifest is None:
        for d in scrapes.iterdir() if scrapes.is_dir() else []:
            mf = d / "manifest.json"
            if mf.exists() and mf.stat().st_size > 0:
                manifest = mf
                break
        if manifest is None:
            time.sleep(0.1)
    if manifest is None:
        proc.kill()
        out, err = proc.communicate(timeout=5)
        pytest.fail(
            f"manifest never appeared. stdout={out!r}\nstderr={err!r}"
        )

    m = json.loads(manifest.read_text())
    # At launch the manifest carries open-ended state (stop_ts null,
    # exit codes null) and the live pid + session metadata.
    assert m["mode"] == "paper", m
    assert m["session_id"] == manifest.parent.name
    assert m["launch_ts_utc"]  # non-empty
    assert m["launch_ts_ns"] > 0
    assert m["stop_ts_utc"] is None
    assert m["stop_ts_ns"] is None
    assert m["daemon_exit_code"] is None
    assert m["scraper_exit_code"] is None
    assert m["daemon_pid"] is not None
    assert m["scraper_pid"] is not None
    assert m["netns"] is None  # paper mode
    assert m["events_jsonl_path"].endswith("events.jsonl")
    assert m["daemon_log_path"].endswith("daemon.log")
    assert "git_sha" in m
    assert "git_branch" in m

    # Cleanup: SIGINT the process group, expect the trap to update the
    # manifest with stop_ts and exit codes.
    os.killpg(proc.pid, signal.SIGINT)
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
        pytest.fail("launch_daemon.sh did not exit within 20s of SIGINT")

    m_final = json.loads(manifest.read_text())
    assert m_final["stop_ts_utc"] is not None, m_final
    assert m_final["stop_ts_ns"] is not None
    assert m_final["daemon_exit_code"] in {"sigterm", "sigkill", "already_dead"}
    assert m_final["scraper_exit_code"] in {"sigterm", "sigkill", "already_dead", "none"}


def test_launch_daemon_aborts_on_running_scraper_pid(tmp_path):
    """Preflight gate fires when an existing scraper pidfile points at a live pid.

    Spec says the preflight aborts with exit 4. Plant a scraper.pid
    file in a fake prior-session dir whose contents = the test runner's
    own pid (kill -0 self → success), then invoke the wrapper.
    """
    proj = _stub_project_dir(tmp_path)
    # Plant a fake prior-session dir with a live scraper.pid.
    prior = proj / "daemon_state" / "scrapes" / "1999-01-01T00-00-00Z"
    prior.mkdir()
    # Use the test runner's own pid — guaranteed alive (we're it).
    (prior / "scraper.pid").write_text(f"{os.getpid()}\n")

    result = subprocess.run(
        ["bash", str(LAUNCH_DAEMON_SCRIPT), "paper"],
        env=_legacy_env(proj),
        capture_output=True, text=True, timeout=15,
        cwd=str(proj),
    )
    assert result.returncode == 4, (
        f"expected exit 4 (scraper-pid abort), got {result.returncode}.\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "ABORT" in result.stderr
    assert "scraper" in result.stderr.lower()
    # The live pidfile must NOT have been removed (only stale ones are
    # rm'd by preflight). Confirm it's still there.
    assert (prior / "scraper.pid").exists()


def test_launch_daemon_removes_stale_scraper_pidfile(tmp_path):
    """Preflight removes a stale scraper.pid (dead pid) and proceeds.

    Plant a scraper.pid pointing at PID 1 minus a far-into-the-future
    offset so it's almost certainly not alive. The wrapper should
    silently rm it and continue to the manifest emit.
    """
    proj = _stub_project_dir(tmp_path)
    prior = proj / "daemon_state" / "scrapes" / "1999-01-01T00-00-00Z"
    prior.mkdir()
    # PIDs over 4_000_000 are out of range on Linux (default
    # kernel.pid_max=4_194_303); kill -0 on it returns ESRCH.
    stale_pidfile = prior / "scraper.pid"
    stale_pidfile.write_text("4000001\n")

    proc = subprocess.Popen(
        ["bash", str(LAUNCH_DAEMON_SCRIPT), "paper"],
        env=_legacy_env(proj),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        cwd=str(proj),
        start_new_session=True,
    )
    # Wait up to 10s for the manifest to appear (== preflight passed).
    scrapes = proj / "daemon_state" / "scrapes"
    deadline = time.time() + 10.0
    manifest = None
    while time.time() < deadline and manifest is None:
        for d in scrapes.iterdir():
            if d.name == prior.name:
                continue  # the planted dir
            mf = d / "manifest.json"
            if mf.exists() and mf.stat().st_size > 0:
                manifest = mf
                break
        if manifest is None:
            time.sleep(0.1)

    # Now SIGINT and confirm a clean exit. Whether or not we found a
    # manifest within 10s, the wrapper should exit cleanly.
    os.killpg(proc.pid, signal.SIGINT)
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
        pytest.fail("launch_daemon.sh did not exit within 20s of SIGINT")

    assert manifest is not None, (
        "preflight didn't proceed past the stale-pid check — "
        "either the rm failed or the daemon never spawned"
    )
    # Stale pidfile should be gone.
    assert not stale_pidfile.exists()


def test_launch_daemon_removed_subcommands_exit_nonzero(tmp_path):
    """status / attach / preflight / refresh_cache stubs exit 1 with hint."""
    proj = _stub_project_dir(tmp_path)
    for sub in ["status", "attach", "preflight", "refresh_cache"]:
        result = subprocess.run(
            ["bash", str(LAUNCH_DAEMON_SCRIPT), sub],
            env=_legacy_env(proj),
            capture_output=True, text=True, timeout=5,
            cwd=str(proj),
        )
        assert result.returncode == 1, f"{sub}: expected exit 1, got {result.returncode}"
        assert "no longer dispatched" in result.stderr, f"{sub}: missing hint"
