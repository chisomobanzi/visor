"""Logcat streaming, dumping, and crash log extraction."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from typing import Iterator, Optional

from visor.adb.wrapper import ADBWrapper, get_adb
from visor.utils.parser import LogEntry, parse_logcat_line


@dataclass
class CrashEntry:
    package: str
    crash_type: str  # data_app_crash | data_app_anr | data_app_native_crash
    timestamp: str
    stack_trace: str


def _logcat_filter_args(tag: Optional[str], level: str) -> list[str]:
    """Build logcat filterspec: tag-scoped or global level filter."""
    if tag:
        return [f"{tag}:{level}", "*:S"]
    return [f"*:{level}"]


def logcat_dump(
    device_id: Optional[str] = None,
    lines: int = 500,
    tag: Optional[str] = None,
    level: str = "W",
    pattern: Optional[str] = None,
    adb: Optional[ADBWrapper] = None,
) -> list[LogEntry]:
    """Dump recent logcat history as structured entries.

    pattern is applied client-side (case-insensitive regex over tag+message).
    """
    adb = adb or get_adb()
    args = ["logcat", "-d", "-v", "threadtime", "-t", str(lines)]
    args += _logcat_filter_args(tag, level)
    output = adb.run(args, device_id=device_id, timeout=30)

    entries = [e for e in (parse_logcat_line(l) for l in output.splitlines()) if e]
    if pattern:
        rx = re.compile(pattern, re.IGNORECASE)
        entries = [e for e in entries if rx.search(e.message) or rx.search(e.tag)]
    return entries


def logcat_clear(
    device_id: Optional[str] = None, adb: Optional[ADBWrapper] = None
) -> None:
    adb = adb or get_adb()
    adb.run(["logcat", "-c"], device_id=device_id, timeout=15)


def logcat_stream(
    device_id: Optional[str] = None,
    tag: Optional[str] = None,
    level: str = "W",
    pattern: Optional[str] = None,
    adb: Optional[ADBWrapper] = None,
) -> Iterator[LogEntry]:
    """Stream logcat in real time. Yields LogEntry objects until the
    consumer stops iterating (the adb process is then terminated)."""
    adb = adb or get_adb()
    cmd = [adb.adb_path]
    if device_id:
        cmd += ["-s", device_id]
    cmd += ["logcat", "-v", "threadtime"] + _logcat_filter_args(tag, level)

    rx = re.compile(pattern, re.IGNORECASE) if pattern else None
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True
    )
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            entry = parse_logcat_line(line.rstrip("\n"))
            if not entry:
                continue
            if rx and not (rx.search(entry.message) or rx.search(entry.tag)):
                continue
            yield entry
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()


_DROPBOX_HEADER_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+(\S+)\s+\((?:compressed )?text, \d+ bytes\)$"
)


def crash_log(
    device_id: Optional[str] = None,
    package_name: Optional[str] = None,
    adb: Optional[ADBWrapper] = None,
) -> list[CrashEntry]:
    """Extract crash/ANR reports from the system dropbox, optionally
    filtered to one package. Most recent first."""
    adb = adb or get_adb()
    crashes = []
    for kind in ("data_app_crash", "data_app_anr", "data_app_native_crash"):
        output = adb.shell(
            f"dumpsys dropbox --print {kind}", device_id=device_id, timeout=60
        )
        crashes.extend(_parse_dropbox(output, kind, package_name))
    crashes.sort(key=lambda c: c.timestamp, reverse=True)
    return crashes


def _parse_dropbox(
    output: str, kind: str, package_name: Optional[str]
) -> list[CrashEntry]:
    entries = []
    current_ts: Optional[str] = None
    current_lines: list[str] = []

    def flush():
        if current_ts is None:
            return
        body = "\n".join(current_lines).strip()
        pkg = ""
        if m := re.search(r"^(?:Process|Package):\s*(\S+?)(?:\s|$)", body, re.MULTILINE):
            pkg = m.group(1)
        if package_name and package_name not in pkg and package_name not in body[:500]:
            return
        entries.append(
            CrashEntry(
                package=pkg or (package_name or "unknown"),
                crash_type=kind,
                timestamp=current_ts,
                stack_trace=body,
            )
        )

    for line in output.splitlines():
        m = _DROPBOX_HEADER_RE.match(line.strip())
        if m:
            flush()
            current_ts = m.group(1)
            current_lines = []
        elif current_ts is not None:
            if set(line.strip()) == {"="}:
                continue
            current_lines.append(line)
    flush()
    return entries
