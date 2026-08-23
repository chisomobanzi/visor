"""Finding and describing the APKs on this machine.

The publish flow starts with a choice — which build? — and a project directory
holds a lot of them: Shipping next to Development, today's next to last week's,
plus whatever a packaging script archived. Typing a path from memory at that
moment is how the wrong build reaches the store, so this lists what is actually
on disk, newest first, each one labelled with the facts that tell them apart.

An APK that cannot be read is still listed. Hiding a file the user can plainly
see in the folder is worse than showing it with a note saying we could not read
it — they may well know something we do not.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from visor.publish import apk as apk_reader

#: Unreal writes partially-linked APKs here on its way to a real one.
SKIP_DIRECTORIES = {"Intermediate", "Saved", "DerivedDataCache", ".git", "obj"}

DEFAULT_LIMIT = 25


@dataclass(frozen=True)
class Build:
    path: Path
    size_bytes: int
    modified: float
    package: Optional[str] = None
    version_code: Optional[int] = None
    version_name: Optional[str] = None
    signed_by: Optional[str] = None
    debug_signed: bool = False
    error: Optional[str] = None

    @property
    def label(self) -> str:
        """What to put in a picker, when the folder name is the only clue."""
        if self.version_name:
            return f"{self.path.name} — {self.version_name} ({self.version_code})"
        return self.path.name

    def summary(self) -> dict:
        return {
            "path": str(self.path),
            "name": self.path.name,
            "parent": str(self.path.parent),
            "label": self.label,
            "size_bytes": self.size_bytes,
            "modified": self.modified,
            "package": self.package,
            "version_code": self.version_code,
            "version_name": self.version_name,
            "signed_by": self.signed_by,
            "debug_signed": self.debug_signed,
            "error": self.error,
        }


def describe(path: Path) -> Build:
    """Read one APK's identifying facts, tolerating one we cannot parse."""
    path = Path(path)
    stat = path.stat()
    try:
        info = apk_reader.inspect(path)
    except apk_reader.ApkError as error:
        return Build(
            path=path,
            size_bytes=stat.st_size,
            modified=stat.st_mtime,
            error=str(error),
        )
    return Build(
        path=path,
        size_bytes=info.size_bytes,
        modified=stat.st_mtime,
        package=info.package,
        version_code=info.version_code,
        version_name=info.version_name,
        signed_by=info.signature.common_name,
        debug_signed=info.signature.debug_signed,
    )


def discover(root, limit: int = DEFAULT_LIMIT) -> list[Build]:
    """Every APK at or below `root`, newest first."""
    root = Path(root)
    if root.is_file():
        return [describe(root)] if root.suffix.lower() == ".apk" else []
    if not root.is_dir():
        return []

    found: list[Path] = []
    for current, directories, files in os.walk(root):
        directories[:] = [d for d in directories if d not in SKIP_DIRECTORIES]
        found.extend(
            Path(current) / name for name in files if name.lower().endswith(".apk")
        )

    found.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return [describe(path) for path in found[:limit]]


def discover_all(roots, limit: int = DEFAULT_LIMIT) -> list[Build]:
    """Discover across several roots, deduplicated, still newest first."""
    seen: dict[Path, Build] = {}
    for root in roots:
        for build in discover(root, limit=limit):
            seen.setdefault(build.path.resolve(), build)
    ordered = sorted(seen.values(), key=lambda b: b.modified, reverse=True)
    return ordered[:limit]


def default_roots(start) -> list[Path]:
    """Where builds live for a given directory.

    An Unreal project puts them under `Binaries/Android`, which is worth
    pointing at directly — walking a whole project tree to find three files is
    slow enough to notice.
    """
    start = Path(start)
    if not start.is_dir():
        return []
    if any(start.glob("*.uproject")):
        candidates = [
            start / "Binaries" / "Android",
            start / "Build" / "Android",
        ]
        roots = [c for c in candidates if c.is_dir()]
        if roots:
            return roots
    return [start]
