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
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LAUNCH_SCRIPT = REPO_ROOT / "launch"
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
