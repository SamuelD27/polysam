"""Tests for VariantSpec loader."""
import json
from pathlib import Path

import pytest

from experiments._tools.variant_spec import VariantSpec


def _write(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "variant.json"
    p.write_text(json.dumps(data))
    return p


def test_load_env_only(tmp_path):
    p = _write(tmp_path, {
        "name": "v1",
        "hypothesis": "h",
        "tweak_type": "env_only",
        "env": {"X": "1"},
    })
    spec = VariantSpec.load(p)
    assert spec.name == "v1"
    assert spec.tweak_type == "env_only"
    assert spec.env == {"X": "1"}
    assert spec.patches == []
    assert spec.new_files == []


def test_load_code_patch(tmp_path):
    p = _write(tmp_path, {
        "name": "v2",
        "hypothesis": "h",
        "tweak_type": "code_patch",
        "patches": [{"file": "a.py", "find": "foo", "replace": "bar"}],
    })
    spec = VariantSpec.load(p)
    assert spec.tweak_type == "code_patch"
    assert spec.patches[0]["file"] == "a.py"


def test_load_new_module(tmp_path):
    p = _write(tmp_path, {
        "name": "v3",
        "hypothesis": "h",
        "tweak_type": "new_module",
        "new_files": [{"path": "x.py", "content": "pass\n"}],
        "patches": [{"file": "d.py", "find": "A", "replace": "B"}],
    })
    spec = VariantSpec.load(p)
    assert spec.new_files[0]["path"] == "x.py"


def test_invalid_tweak_type(tmp_path):
    p = _write(tmp_path, {"name": "v", "hypothesis": "h", "tweak_type": "nonsense"})
    with pytest.raises(ValueError, match="tweak_type"):
        VariantSpec.load(p)


def test_missing_required_field(tmp_path):
    p = _write(tmp_path, {"tweak_type": "env_only"})
    with pytest.raises((KeyError, TypeError, ValueError)):
        VariantSpec.load(p)
