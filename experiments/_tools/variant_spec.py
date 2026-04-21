"""Load + validate variant.json specs."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

VALID_TWEAK_TYPES = {"env_only", "code_patch", "new_module"}
ALLOWED_FIELDS = {"name", "hypothesis", "tweak_type", "env", "patches", "new_files"}


@dataclass
class VariantSpec:
    name: str
    hypothesis: str
    tweak_type: str
    env: dict[str, str] = field(default_factory=dict)
    patches: list[dict[str, Any]] = field(default_factory=list)
    new_files: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> "VariantSpec":
        data = json.loads(path.read_text())
        if "name" not in data or "hypothesis" not in data or "tweak_type" not in data:
            raise ValueError(
                f"variant.json missing required field (name/hypothesis/tweak_type): {path}"
            )
        if data["tweak_type"] not in VALID_TWEAK_TYPES:
            raise ValueError(
                f"invalid tweak_type {data['tweak_type']!r} in {path}; "
                f"allowed: {sorted(VALID_TWEAK_TYPES)}"
            )
        filtered = {k: v for k, v in data.items() if k in ALLOWED_FIELDS}
        return cls(**filtered)
