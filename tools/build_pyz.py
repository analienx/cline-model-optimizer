#!/usr/bin/env python3
"""Build a deterministic ``cmo.pyz`` zipapp.

Every archive member is written with a fixed timestamp and sorted order so the
same source tree always produces the same sha256 digest. The digest is the
installed artifact identity reported by ``cmo doctor`` and the dashboard.
"""

from __future__ import annotations

import hashlib
import json
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "python"
DEFAULT_TARGET = REPO / "dist" / "cmo.pyz"
FIXED_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
MAIN = "from cmo.cli import main\n\nraise SystemExit(main())\n"


def collect() -> list[tuple[str, Path]]:
    members: list[tuple[str, Path]] = []
    for path in sorted(SRC.rglob("*")):
        if not path.is_file():
            continue
        if "__pycache__" in path.parts or path.suffix in (".pyc", ".pyo"):
            continue
        members.append((path.relative_to(SRC).as_posix(), path))
    return members


def build(target: Path) -> dict:
    target.parent.mkdir(parents=True, exist_ok=True)
    members = collect()
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        info = zipfile.ZipInfo("__main__.py", date_time=FIXED_TIMESTAMP)
        info.external_attr = 0o644 << 16
        info.compress_type = zipfile.ZIP_DEFLATED
        zf.writestr(info, MAIN)
        for name, path in members:
            info = zipfile.ZipInfo(name, date_time=FIXED_TIMESTAMP)
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, path.read_bytes())
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    manifest = {
        "schema": "cmo.artifact-manifest/v1",
        "artifact": target.name,
        "sha256": digest,
        "members": len(members) + 1,
        "main": MAIN.strip(),
        "source": "python/",
    }
    (target.parent / "cmo-manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def main(argv: list[str]) -> int:
    target = Path(argv[1]) if len(argv) > 1 else DEFAULT_TARGET
    manifest = build(target)
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
