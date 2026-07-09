"""File push/pull/list/delete on the device."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Optional

from visor.adb.wrapper import ADBCommandError, ADBWrapper, get_adb

# Refuse to rm these outright — protects against catastrophic typos
_PROTECTED_PATHS = {"", "/", "/sdcard", "/data", "/system", "/storage"}


@dataclass
class TransferResult:
    success: bool
    message: str


@dataclass
class FileEntry:
    name: str
    is_dir: bool
    size: Optional[int]
    modified: Optional[str]
    permissions: Optional[str] = None


_LS_LONG_RE = re.compile(
    r"^([\-dlbcps][rwxsStT\-]{9})\s+\d+\s+\S+\s+\S+\s+(\d+)?\s*"
    r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})\s+(.+)$"
)


def push_file(
    local_path: str,
    device_path: str,
    device_id: Optional[str] = None,
    adb: Optional[ADBWrapper] = None,
) -> TransferResult:
    if not os.path.exists(local_path):
        raise FileNotFoundError(f"Local file not found: {local_path}")
    adb = adb or get_adb()
    output = adb.run(
        ["push", local_path, device_path], device_id=device_id, timeout=600
    )
    return TransferResult(success=True, message=output.strip().splitlines()[-1] if output.strip() else "Pushed")


def pull_file(
    device_path: str,
    local_path: str,
    device_id: Optional[str] = None,
    adb: Optional[ADBWrapper] = None,
) -> TransferResult:
    adb = adb or get_adb()
    parent = os.path.dirname(os.path.abspath(local_path))
    os.makedirs(parent, exist_ok=True)
    output = adb.run(
        ["pull", device_path, local_path], device_id=device_id, timeout=600
    )
    return TransferResult(success=True, message=output.strip().splitlines()[-1] if output.strip() else "Pulled")


def list_files(
    device_path: str,
    device_id: Optional[str] = None,
    adb: Optional[ADBWrapper] = None,
) -> list[FileEntry]:
    adb = adb or get_adb()
    output = adb.shell(f"ls -l '{device_path}'", device_id=device_id)
    if "No such file or directory" in output:
        raise ADBCommandError(f"No such file or directory on device: {device_path}")
    if "Permission denied" in output and not output.strip().startswith(("total", "-", "d", "l")):
        raise ADBCommandError(
            f"Permission denied reading {device_path} — many /data paths are "
            "not readable over ADB; app data is under /sdcard/Android/data/."
        )

    entries = []
    for line in output.splitlines():
        m = _LS_LONG_RE.match(line.strip())
        if not m:
            continue
        perms, size, modified, name = m.groups()
        entries.append(
            FileEntry(
                name=name.strip(),
                is_dir=perms.startswith("d"),
                size=int(size) if size else None,
                modified=modified,
                permissions=perms,
            )
        )
    return entries


def delete_file(
    device_path: str,
    device_id: Optional[str] = None,
    recursive: bool = False,
    adb: Optional[ADBWrapper] = None,
) -> bool:
    normalized = device_path.rstrip("/") if device_path != "/" else device_path
    if normalized in _PROTECTED_PATHS or device_path.rstrip("/") in _PROTECTED_PATHS:
        raise ValueError(f"Refusing to delete protected path: {device_path!r}")
    adb = adb or get_adb()
    flag = "-rf" if recursive else "-f"
    output = adb.shell(f"rm {flag} '{device_path}' && echo OK || echo RMFAIL", device_id=device_id)
    if "No such file or directory" in output:
        raise ADBCommandError(f"No such file or directory: {device_path}")
    if "RMFAIL" in output or "Permission denied" in output:
        raise ADBCommandError(f"Failed to delete {device_path}: {output.replace('RMFAIL', '').strip()}")
    return True
