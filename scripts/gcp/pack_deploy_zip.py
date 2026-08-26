"""Pack project into a deploy zip (used by deploy.ps1)."""
from __future__ import annotations

import os
import sys
import zipfile
from pathlib import Path

EXCLUDE_DIRS = {".git", ".pytest_cache", "__pycache__", "terminals", "node_modules"}
EXCLUDE_PATH_PREFIXES = (("data", "raw"), ("data", "raw_tick"))
EXCLUDE_ROOT_FILES = {".env"}


def should_skip(rel: Path) -> bool:
    parts = rel.parts
    if any(p in EXCLUDE_DIRS for p in parts):
        return True
    for ep in EXCLUDE_PATH_PREFIXES:
        if len(parts) >= len(ep) and tuple(parts[: len(ep)]) == ep:
            return True
    if len(parts) == 1 and rel.name in EXCLUDE_ROOT_FILES:
        return True
    return False


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: pack_deploy_zip.py <project_root> <archive.zip>", file=sys.stderr)
        return 2
    root = Path(sys.argv[1]).resolve()
    archive = Path(sys.argv[2])
    if archive.exists():
        archive.unlink()

    count = 0
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(root)
            if should_skip(rel):
                continue
            zf.write(path, rel.as_posix())
            count += 1
    print(f"packed {count} files -> {archive}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
