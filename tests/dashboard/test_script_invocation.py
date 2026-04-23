"""Regression: 'python3 scripts/dashboard.py' must import cleanly.

When run as a script, sys.path[0] is scripts/, NOT the repo root, so the
`from active_bots.execution.token_resolver import TokenResolver` line
will ModuleNotFoundError unless dashboard.py adds the repo root to
sys.path itself. This test simulates the script-invocation environment.
"""
import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO / "scripts" / "dashboard.py"


def test_dashboard_imports_when_invoked_as_script(tmp_path):
    """Spawn a fresh interpreter with only scripts/ on sys.path[0] and
    confirm `import dashboard` succeeds (i.e. its own sys.path manipulation
    finds active_bots at the repo root)."""
    # -I = isolated mode: skips PYTHONPATH and user site-packages, mirroring
    # the launch_daemon.sh invocation where conda activates an env without
    # adding the repo root to PYTHONPATH.
    code = (
        "import sys; "
        f"sys.path.insert(0, {str(SCRIPT.parent)!r}); "
        "import dashboard; "
        "print('IMPORT_OK', dashboard.REPO)"
    )
    proc = subprocess.run(
        [sys.executable, "-I", "-c", code],
        capture_output=True, text=True, timeout=10,
        cwd=str(tmp_path),  # run from a tmp dir to prove cwd doesn't matter
    )
    assert proc.returncode == 0, (
        f"dashboard import failed:\nstdout={proc.stdout}\nstderr={proc.stderr}"
    )
    assert "IMPORT_OK" in proc.stdout
    assert str(REPO) in proc.stdout
