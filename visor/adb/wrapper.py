"""Core ADB command wrapper.

Runs the `adb` CLI via subprocess (most reliable interface — no native
library dependencies) and translates raw ADB failures into clear,
actionable Visor errors.
"""

from __future__ import annotations

import shutil
import subprocess
from typing import Optional, Union


class VisorError(Exception):
    """Base class for all visor errors."""


class ADBNotFoundError(VisorError):
    pass


class ADBCommandError(VisorError):
    pass


class DeviceNotFoundError(VisorError):
    pass


class DeviceUnauthorizedError(VisorError):
    pass


class DeviceOfflineError(VisorError):
    pass


class MultipleDevicesError(VisorError):
    pass


_STDERR_ERROR_MAP = [
    # (substring in stderr, exception class, friendly message)
    (
        "unauthorized",
        DeviceUnauthorizedError,
        "Device is unauthorized. Put on the headset and accept the "
        "'Allow USB debugging' prompt (check 'Always allow' to skip this next time).",
    ),
    (
        "no devices/emulators found",
        DeviceNotFoundError,
        "No devices connected. Check the USB cable or wireless connection, "
        "and that developer mode is enabled on the headset.",
    ),
    (
        "not found",
        DeviceNotFoundError,
        "Device not found. It may have disconnected — check the connection "
        "with `adb devices`.",
    ),
    (
        "device offline",
        DeviceOfflineError,
        "Device is offline. Unplug/replug the USB cable, or if wireless, "
        "re-run connect (the headset may have slept or changed networks).",
    ),
    (
        "more than one device",
        MultipleDevicesError,
        "Multiple devices connected — specify which one with device_id "
        "(serial from `visor devices`).",
    ),
]


class ADBWrapper:
    """Thin, reliable wrapper around the adb binary."""

    def __init__(self, adb_path: Optional[str] = None):
        self.adb_path = adb_path or shutil.which("adb")
        if not self.adb_path:
            raise ADBNotFoundError(
                "adb binary not found. Install it with your package manager, "
                "e.g. `sudo apt install android-platform-tools` or "
                "`sudo pacman -S android-tools`."
            )

    def run(
        self,
        args: list[str],
        device_id: Optional[str] = None,
        timeout: float = 30,
        binary: bool = False,
        check: bool = True,
    ) -> Union[str, bytes]:
        """Run an adb command and return stdout.

        Raises a specific VisorError subclass on known failure modes,
        ADBCommandError otherwise.
        """
        cmd = [self.adb_path]
        if device_id:
            cmd += ["-s", device_id]
        cmd += args

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=not binary,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            raise ADBCommandError(
                f"adb command timed out after {timeout}s: {' '.join(args)}"
            ) from None
        except OSError as e:
            raise ADBCommandError(f"Failed to execute adb: {e}") from None

        if check and result.returncode != 0:
            stderr = result.stderr
            if isinstance(stderr, bytes):
                stderr = stderr.decode(errors="replace")
            self._raise_for_stderr(stderr, args)

        return result.stdout

    def shell(
        self,
        command: str,
        device_id: Optional[str] = None,
        timeout: float = 30,
    ) -> str:
        """Run a shell command on the device and return its output."""
        return self.run(["shell", command], device_id=device_id, timeout=timeout)

    def _raise_for_stderr(self, stderr: str, args: list[str]):
        lowered = (stderr or "").lower()
        for needle, exc_class, message in _STDERR_ERROR_MAP:
            if needle in lowered:
                raise exc_class(message)
        raise ADBCommandError(
            f"adb {' '.join(args)} failed: {stderr.strip() or 'unknown error'}"
        )


_default_wrapper: Optional[ADBWrapper] = None


def get_adb() -> ADBWrapper:
    """Shared ADBWrapper instance (created lazily so importing visor
    doesn't fail on machines without adb)."""
    global _default_wrapper
    if _default_wrapper is None:
        _default_wrapper = ADBWrapper()
    return _default_wrapper
