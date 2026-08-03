"""Host-OS helpers — install guidance, data paths and process-detach flags.

Visor talks to the headset entirely through adb, which runs on Linux,
macOS, and Windows alike, so the library itself is cross-platform. These
helpers keep the OS-specific bits (how to install a missing tool, where
user data lives, how to detach a child process) in one place.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def data_home() -> Path:
    """Base directory for Visor's own persistent files (XDG on all platforms).

    Used for the desktop launcher and the fetched ovr-platform-util; keeping
    one definition means they can never disagree about where things live.
    """
    xdg = os.environ.get("XDG_DATA_HOME")
    return Path(xdg) if xdg else Path.home() / ".local" / "share"

_INSTALL_HINTS = {
    "linux": {
        "adb": "sudo apt install android-sdk-platform-tools  (or: sudo pacman -S android-tools)",
        "ffmpeg": "sudo apt install ffmpeg",
        "scrcpy": "sudo apt install scrcpy",
    },
    "darwin": {
        "adb": "brew install android-platform-tools",
        "ffmpeg": "brew install ffmpeg",
        "scrcpy": "brew install scrcpy",
    },
    "win32": {
        "adb": "winget install --id Google.PlatformTools "
        "(or download platform-tools from https://developer.android.com/tools/releases/platform-tools)",
        "ffmpeg": "winget install --id Gyan.FFmpeg",
        "scrcpy": "winget install --id Genymobile.scrcpy",
    },
}


def _os_key(platform: str | None = None) -> str:
    platform = platform if platform is not None else sys.platform
    if platform == "darwin":
        return "darwin"
    if platform.startswith("win"):
        return "win32"
    return "linux"


def install_hint(tool: str, platform: str | None = None) -> str:
    """Return an OS-appropriate one-line install hint for an external tool.

    ``platform`` overrides ``sys.platform`` (for testing). Falls back to a
    generic message for unknown tools.
    """
    cmd = _INSTALL_HINTS.get(_os_key(platform), {}).get(tool)
    if cmd:
        return f"Install it with: {cmd}. Then make sure {tool} is on your PATH."
    return f"Install {tool} and make sure it is on your PATH."


def detached_popen_kwargs(platform: str | None = None) -> dict:
    """Popen kwargs that let a child outlive the calling process.

    POSIX uses ``start_new_session`` (setsid); Windows uses a new process
    group. Referencing the Windows-only constant is guarded so it is never
    evaluated on POSIX.
    """
    if _os_key(platform) == "win32":
        # CREATE_NEW_PROCESS_GROUP (0x200) exists only on Windows builds of
        # subprocess; fall back to the literal so this stays importable and
        # testable on POSIX, where these kwargs are never passed to Popen.
        flag = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        return {"creationflags": flag}
    return {"start_new_session": True}
