"""What has shipped from this machine, remembered.

Meta's dashboard knows which version codes a channel has served, but it only
says so interactively — the CLI cannot ask. So every upload Visor completes is
recorded here, per app and channel, and the runway reads it back: the history
table answers "did build 4 go up?", and the channel's version code prefills
itself instead of living in the developer's memory. One forgotten number cost
a real submission an extra build (two unuploadable APKs on disk, 2026-08-23);
this file is the reason that does not happen again.

The ledger is advisory, never authoritative: it knows what THIS machine sent,
not what the dashboard was told by other machines or humans. Every consumer
keeps the value editable.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Optional

#: A completed conversation with Meta about one APK.
@dataclass(frozen=True)
class Entry:
    package: Optional[str]
    version_code: Optional[int]
    version_name: Optional[str]
    channel: str
    apk_name: str
    apk_path: str
    size_bytes: Optional[int]
    status: str  # "uploaded" | "validated"
    build_url: Optional[str] = None
    notes: Optional[str] = None
    timestamp: float = field(default_factory=time.time)


_BUILD_URL = re.compile(
    r"https://developer\.oculus\.com/manage/applications/\d+/builds/\d+"
)


def parse_build_url(output: str) -> Optional[str]:
    """Meta's console names the uploaded build's dashboard URL; keep it.

    The utility wraps long lines at terminal width, so the URL can arrive
    split — join the text before searching rather than trusting any one line.
    """
    joined = re.sub(r"\s+", "", output)
    match = _BUILD_URL.search(joined)
    return match.group(0) if match else None


def load(path: Path) -> list[Entry]:
    """Every recorded entry, oldest first. A missing or corrupt ledger is
    empty, never an error — history must not block a new upload."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    entries = []
    for item in raw if isinstance(raw, list) else []:
        try:
            entries.append(Entry(**item))
        except TypeError:
            continue  # an older/newer schema's row is skipped, not fatal
    return entries


def record(path: Path, entry: Entry) -> list[Entry]:
    """Append one entry and persist. Returns the full ledger."""
    entries = load(path)
    entries.append(entry)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([asdict(e) for e in entries], indent=2), encoding="utf-8"
    )
    return entries


def latest_version_code(
    entries: Iterable[Entry], package: str, channel: str
) -> Optional[int]:
    """The highest version code this machine has UPLOADED for (package, channel).

    Validations are rehearsals — they consume nothing on Meta's side, so they
    do not count. `store` is compared case-insensitively because Meta spells
    the live channel in lowercase and the test channels in caps.
    """
    best: Optional[int] = None
    for e in entries:
        if e.status != "uploaded" or e.version_code is None:
            continue
        if e.package != package:
            continue
        if (e.channel or "").lower() != (channel or "").lower():
            continue
        if best is None or e.version_code > best:
            best = e.version_code
    return best
