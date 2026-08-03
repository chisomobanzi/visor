"""Locate, fetch and interrogate `ovr-platform-util`.

Meta ships the OVR Platform Utility as a standalone binary per OS, behind an
unversioned `download_app` redirect rather than a package manager. Its licence
doesn't let Visor redistribute it, so we do the next best thing: use one the
user already has, else fetch it into a Visor-managed cache on explicit request.

The fetch is never implicit. `require()` raises rather than reaching out to the
network mid-upload — a tool that quietly downloads a vendor binary because you
typed `visor upload` is a tool nobody can reason about.
"""

from __future__ import annotations

import re
import shutil
import stat
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Optional

from visor.adb.wrapper import VisorError
from visor.utils.host import data_home

# Meta's per-OS download IDs. Taken from the download_app endpoint MQDH and the
# published GitHub Actions both use; there is no versioned URL to pin to, and
# the binary self-updates in place once installed.
_DOWNLOAD_IDS = {
    "linux": "5159709737372459",
    "darwin": "1462426033810370",
    "win32": "1076686279105243",
}

_DOWNLOAD_ENDPOINT = "https://www.oculus.com/download_app/?id={id}"

_DOCS_URL = (
    "https://developers.meta.com/horizon/resources/"
    "publish-reference-platform-command-line-utility/"
)

_CHUNK = 1 << 20


class OvrToolError(VisorError):
    """The platform utility is present but something went wrong with it."""


class OvrToolNotFoundError(OvrToolError):
    """No ovr-platform-util on PATH or in the Visor cache."""


def _os_key(platform: Optional[str] = None) -> str:
    platform = platform if platform is not None else sys.platform
    if platform == "darwin":
        return "darwin"
    if platform.startswith("win"):
        return "win32"
    if platform.startswith("linux"):
        return "linux"
    return platform


def download_url(platform: Optional[str] = None) -> str:
    """Meta's download URL for this OS's build of the utility."""
    key = _os_key(platform)
    app_id = _DOWNLOAD_IDS.get(key)
    if not app_id:
        raise OvrToolError(
            f"Meta does not publish ovr-platform-util for {key!r}. "
            f"Supported: {', '.join(sorted(_DOWNLOAD_IDS))}."
        )
    return _DOWNLOAD_ENDPOINT.format(id=app_id)


def binary_name(platform: Optional[str] = None) -> str:
    return (
        "ovr-platform-util.exe"
        if _os_key(platform) == "win32"
        else "ovr-platform-util"
    )


def cache_dir() -> Path:
    """Where Visor keeps a fetched copy of the utility."""
    return data_home() / "visor" / "bin"


def find(platform: Optional[str] = None) -> Optional[Path]:
    """The utility to use, or None.

    PATH wins over the cache: someone who installed it themselves — or who
    keeps it pinned for CI — should not be shadowed by whatever Visor fetched
    months ago.
    """
    name = binary_name(platform)
    on_path = shutil.which(name)
    if on_path:
        return Path(on_path)
    cached = cache_dir() / name
    if cached.exists():
        return cached
    return None


def require(platform: Optional[str] = None) -> Path:
    """The utility, or an error explaining how to get it. Never downloads."""
    found = find(platform)
    if found:
        return found
    raise OvrToolNotFoundError(
        f"{binary_name(platform)} not found on PATH or in {cache_dir()}.\n"
        "Meta's licence doesn't let Visor bundle it, so fetch it explicitly:\n"
        "    visor fetch-tool\n"
        f"or download it yourself from {_DOCS_URL}"
    )


def _download(url: str, dest: Path) -> None:
    """Stream a URL to a path. Split out so tests can replace the network."""
    with urllib.request.urlopen(url) as response, open(dest, "wb") as handle:
        while True:
            chunk = response.read(_CHUNK)
            if not chunk:
                break
            handle.write(chunk)


def fetch(platform: Optional[str] = None) -> Path:
    """Download the utility into the Visor cache and return its path.

    Downloads to a sibling `.part` file and renames on success, so an
    interrupted fetch can never leave a truncated binary that `find()` would
    later hand to the uploader.
    """
    url = download_url(platform)
    target = cache_dir() / binary_name(platform)
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".part")

    try:
        _download(url, partial)
    except Exception as e:
        partial.unlink(missing_ok=True)
        raise OvrToolError(f"Could not download ovr-platform-util from {url}: {e}") from None

    partial.replace(target)
    if _os_key(platform) != "win32":
        target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
    return target


_VERSION_RE = re.compile(r"(\d+\.\d+(?:\.\d+)?)")


def version(binary: Optional[Path] = None) -> Optional[str]:
    """The utility's version string, or None if it didn't report one."""
    binary = binary or require()
    try:
        result = subprocess.run(
            [str(binary), "version"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise OvrToolError(f"Could not run {binary}: {e}") from None

    match = _VERSION_RE.search(f"{result.stdout}\n{result.stderr}")
    return match.group(1) if match else None
