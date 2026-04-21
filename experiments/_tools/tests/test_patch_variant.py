"""Tests for patch applier."""
import json
from pathlib import Path

import pytest

from experiments._tools.patch_variant import apply_patches
from experiments._tools.variant_spec import VariantSpec


def _spec(tmp_path: Path, data: dict) -> VariantSpec:
    p = tmp_path / "variant.json"
    p.write_text(json.dumps(data))
    return VariantSpec.load(p)


def test_env_only_is_noop_on_files(tmp_path):
    (tmp_path / "daemon_base_v1.py").write_text("hello\n")
    spec = _spec(tmp_path, {"name": "v", "hypothesis": "h", "tweak_type": "env_only", "env": {"X": "1"}})
    apply_patches(tmp_path, spec)
    assert (tmp_path / "daemon_base_v1.py").read_text() == "hello\n"


def test_code_patch_replaces_find_block(tmp_path):
    target = tmp_path / "daemon_base_v1.py"
    target.write_text("line1\nFIND_ME\nline3\n")
    spec = _spec(tmp_path, {
        "name": "v", "hypothesis": "h", "tweak_type": "code_patch",
        "patches": [{"file": "daemon_base_v1.py", "find": "FIND_ME", "replace": "REPLACED"}],
    })
    apply_patches(tmp_path, spec)
    assert target.read_text() == "line1\nREPLACED\nline3\n"


def test_code_patch_missing_find_raises(tmp_path):
    (tmp_path / "a.py").write_text("nope\n")
    spec = _spec(tmp_path, {
        "name": "v", "hypothesis": "h", "tweak_type": "code_patch",
        "patches": [{"file": "a.py", "find": "MISSING", "replace": "X"}],
    })
    with pytest.raises(RuntimeError, match="not found"):
        apply_patches(tmp_path, spec)


def test_new_module_writes_files_and_applies_patches(tmp_path):
    (tmp_path / "daemon_base_v1.py").write_text("from active_bots.enhanced_strategy import EnhancedStrategy\n")
    spec = _spec(tmp_path, {
        "name": "v", "hypothesis": "h", "tweak_type": "new_module",
        "new_files": [{"path": "active_bots/pure_x.py", "content": "class PureX:\n    pass\n"}],
        "patches": [{
            "file": "daemon_base_v1.py",
            "find": "from active_bots.enhanced_strategy import EnhancedStrategy",
            "replace": "from active_bots.pure_x import PureX as EnhancedStrategy",
        }],
    })
    apply_patches(tmp_path, spec)
    assert (tmp_path / "active_bots/pure_x.py").read_text().startswith("class PureX:")
    assert "from active_bots.pure_x" in (tmp_path / "daemon_base_v1.py").read_text()


def test_patch_is_single_replacement(tmp_path):
    """Guard: if find-block appears twice, replace only the first (explicit count=1)."""
    target = tmp_path / "a.py"
    target.write_text("X\nX\n")
    spec = _spec(tmp_path, {
        "name": "v", "hypothesis": "h", "tweak_type": "code_patch",
        "patches": [{"file": "a.py", "find": "X", "replace": "Y"}],
    })
    apply_patches(tmp_path, spec)
    assert target.read_text() == "Y\nX\n"
