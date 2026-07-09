"""App management: install, uninstall, launch, stop, info."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Optional

from visor.adb.wrapper import ADBCommandError, ADBWrapper, get_adb
from visor.utils.constants import SYSTEM_PACKAGE_PREFIXES
from visor.utils.parser import parse_package_list


@dataclass
class InstallResult:
    success: bool
    package_path: str
    message: str


@dataclass
class AppInfo:
    package: str
    version_name: Optional[str] = None
    version_code: Optional[int] = None
    first_install: Optional[str] = None
    last_update: Optional[str] = None
    target_sdk: Optional[int] = None
    permissions: list[str] = field(default_factory=list)
    data_size: Optional[str] = None


def install_apk(
    path: str,
    device_id: Optional[str] = None,
    replace: bool = True,
    grant_permissions: bool = False,
    adb: Optional[ADBWrapper] = None,
) -> InstallResult:
    """Install/sideload an APK. `replace=True` (default) updates an
    existing install. Large APKs are common for Quest — 10 min timeout."""
    if not os.path.isfile(path):
        raise FileNotFoundError(f"APK not found: {path}")
    adb = adb or get_adb()

    args = ["install"]
    if replace:
        args.append("-r")
    if grant_permissions:
        args.append("-g")
    args.append(path)

    output = adb.run(args, device_id=device_id, timeout=600)
    if "Success" not in output:
        # adb install reports failures on stdout with returncode 0 sometimes
        reason = _extract_install_failure(output)
        raise ADBCommandError(f"Install failed: {reason}")
    return InstallResult(success=True, package_path=path, message="Installed successfully")


def _extract_install_failure(output: str) -> str:
    m = re.search(r"Failure\s*\[([^\]]+)\]", output)
    if m:
        return m.group(1)
    return output.strip() or "unknown error"


def uninstall_app(
    package_name: str,
    device_id: Optional[str] = None,
    adb: Optional[ADBWrapper] = None,
) -> bool:
    adb = adb or get_adb()
    output = adb.run(["uninstall", package_name], device_id=device_id, timeout=60)
    if "Success" not in output:
        raise ADBCommandError(
            f"Uninstall of {package_name} failed: {_extract_install_failure(output)}"
        )
    return True


def list_apps(
    device_id: Optional[str] = None,
    filter: str = "sideloaded",
    adb: Optional[ADBWrapper] = None,
) -> list[str]:
    """List installed packages.

    filter: 'sideloaded' (third-party, pm -3), 'system', or 'all'.
    """
    adb = adb or get_adb()
    flag = {"sideloaded": " -3", "system": " -s", "all": ""}.get(filter)
    if flag is None:
        raise ValueError(f"Unknown filter {filter!r}: use sideloaded|system|all")
    output = adb.shell(f"pm list packages{flag}", device_id=device_id)
    packages = parse_package_list(output)
    if filter == "sideloaded":
        # pm -3 already excludes system apps, but Quest preloads some
        # "third-party" Meta packages; filter the obvious OS ones out.
        packages = [
            p for p in packages
            if not p.startswith(SYSTEM_PACKAGE_PREFIXES)
        ]
    return sorted(packages)


def launch_app(
    package_name: str,
    device_id: Optional[str] = None,
    adb: Optional[ADBWrapper] = None,
) -> str:
    """Launch an app via its launcher intent (works for VR apps too —
    Quest launcher intents resolve through the same mechanism)."""
    adb = adb or get_adb()
    output = adb.shell(
        f"monkey -p {package_name} -c android.intent.category.LAUNCHER 1",
        device_id=device_id,
        timeout=20,
    )
    if "No activities found" in output or "monkey aborted" in output:
        raise ADBCommandError(
            f"{package_name} is not installed or has no launchable activity."
        )
    return f"Launched {package_name}"


def stop_app(
    package_name: str,
    device_id: Optional[str] = None,
    adb: Optional[ADBWrapper] = None,
) -> str:
    adb = adb or get_adb()
    adb.shell(f"am force-stop {package_name}", device_id=device_id)
    return f"Stopped {package_name}"


def clear_app_data(
    package_name: str,
    device_id: Optional[str] = None,
    adb: Optional[ADBWrapper] = None,
) -> bool:
    """Reset app to freshly-installed state (clears saves/settings)."""
    adb = adb or get_adb()
    output = adb.shell(f"pm clear {package_name}", device_id=device_id)
    if "Success" not in output:
        raise ADBCommandError(f"Failed to clear data for {package_name}: {output.strip()}")
    return True


def get_app_info(
    package_name: str,
    device_id: Optional[str] = None,
    adb: Optional[ADBWrapper] = None,
) -> AppInfo:
    adb = adb or get_adb()
    output = adb.shell(f"dumpsys package {package_name}", device_id=device_id)
    if "Unable to find package" in output or f"Package [{package_name}]" not in output:
        raise ADBCommandError(f"{package_name} is not installed.")

    info = AppInfo(package=package_name)

    if m := re.search(r"versionName=(\S+)", output):
        info.version_name = m.group(1)
    if m := re.search(r"versionCode=(\d+)", output):
        info.version_code = int(m.group(1))
    if m := re.search(r"targetSdk=(\d+)", output):
        info.target_sdk = int(m.group(1))
    if m := re.search(r"firstInstallTime=(.+)", output):
        info.first_install = m.group(1).strip()
    if m := re.search(r"lastUpdateTime=(.+)", output):
        info.last_update = m.group(1).strip()

    # Permissions: lines under "requested permissions:" until next section
    if m := re.search(r"requested permissions:\n((?:\s+\S+\n?)+)", output):
        info.permissions = [
            line.strip().split(":")[0]
            for line in m.group(1).splitlines()
            if line.strip().startswith(("android.", "com.", "oculus.", "horizonos."))
        ]

    try:
        du = adb.shell(
            f"du -sh /sdcard/Android/data/{package_name} 2>/dev/null",
            device_id=device_id,
        )
        if du.strip() and "denied" not in du.lower():
            info.data_size = du.split()[0]
    except ADBCommandError:
        pass

    return info
