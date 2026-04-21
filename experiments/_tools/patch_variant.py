"""Apply a VariantSpec's new_files + patches to a variant directory."""
from __future__ import annotations

import sys
from pathlib import Path

from experiments._tools.variant_spec import VariantSpec


def apply_patches(variant_dir: Path, spec: VariantSpec) -> None:
    """Write new_files and apply string-replace patches, in that order."""
    for nf in spec.new_files:
        target = variant_dir / nf["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(nf["content"])

    for p in spec.patches:
        target = variant_dir / p["file"]
        if not target.exists():
            raise RuntimeError(f"patch target not found: {target}")
        text = target.read_text()
        find = p["find"]
        if find not in text:
            raise RuntimeError(
                f"patch find-block not found in {target}: {find[:120]!r}"
            )
        new_text = text.replace(find, p["replace"], 1)
        target.write_text(new_text)


if __name__ == "__main__":
    variant_dir = Path(sys.argv[1])
    spec = VariantSpec.load(variant_dir / "variant.json")
    apply_patches(variant_dir, spec)
    print(f"applied {len(spec.patches)} patch(es) and {len(spec.new_files)} new file(s) to {variant_dir}")
