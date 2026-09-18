"""Deterministic .pyz builder: pure-Python zipapp from python/cmo.

Output digest is a function of source bytes only (fixed timestamps).
"""

from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

PKG = Path(__file__).resolve().parents[1] / "python" / "cmo"
OUT = Path(__file__).resolve().parents[1] / "dist" / "cmo.pyz"

HEADER = b"#!/usr/bin/env python3\n"


def build(out: Path = OUT) -> dict[str, object]:
    out.parent.mkdir(parents=True, exist_ok=True)
    entries: list[tuple[str, bytes, int]] = []
    for src in sorted(PKG.rglob("*.py")):
        arc = "cmo/" + str(src.relative_to(PKG)).replace("\\", "/")
        entries.append((arc, src.read_bytes(), 0o644))
    entries.sort()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for name, data, mode in entries:
            info = zipfile.ZipInfo(name, date_time=(2026, 9, 18, 0, 0, 0))
            info.external_attr = mode << 16
            zf.writestr(info, data)
        main = zipfile.ZipInfo("__main__.py", date_time=(2026, 9, 18, 0, 0, 0))
        main.external_attr = 0o644 << 16
        zf.writestr(main, "from cmo.cli import main\nraise SystemExit(main())\n")
    payload = HEADER + out.read_bytes()
    out.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    return {"artifact": str(out), "sha256": digest, "modules": len(entries) + 1,
            "schema": "cmo.artifact/v2"}


if __name__ == "__main__":
    import json
    print(json.dumps(build(), indent=2))
