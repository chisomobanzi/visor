"""Quest-specific features: guardian, tracking, refresh rate, sensors.

These poke at Meta's system services, which are undocumented and shift
between OS versions — everything here is best-effort and returns whatever
the device exposes rather than raising when a probe comes back empty.
"""

from __future__ import annotations

import re
from typing import Optional

from visor.adb.wrapper import ADBCommandError, ADBWrapper, get_adb
from visor.utils.constants import QUEST_PRODUCTS, REFRESH_RATES


def _try_shell(adb: ADBWrapper, cmd: str, device_id: Optional[str]) -> str:
    try:
        return adb.shell(cmd, device_id=device_id, timeout=15)
    except ADBCommandError:
        return ""


def get_guardian_status(
    device_id: Optional[str] = None, adb: Optional[ADBWrapper] = None
) -> dict:
    """Check guardian/boundary state (best-effort across OS versions)."""
    adb = adb or get_adb()
    result: dict = {"guardian_configured": None, "details": {}}

    out = _try_shell(
        adb,
        "dumpsys GuardianService 2>/dev/null | grep -iE 'GuardianState|boundary' | head -20",
        device_id,
    )
    if m := re.search(r"GuardianState:\s*(\d+)", out):
        state = int(m.group(1))
        result["guardian_configured"] = state >= 2
        result["details"]["guardian_state"] = state

    props = _try_shell(adb, "getprop | grep -i guardian", device_id)
    for line in props.splitlines():
        if m := re.match(r"\[([^\]]+)\]: \[([^\]]*)\]", line.strip()):
            result["details"][m.group(1)] = m.group(2)

    if result["guardian_configured"] is None and not result["details"]:
        result["note"] = (
            "Guardian state is not exposed over ADB on this OS version."
        )
    return result


def get_tracking_status(
    device_id: Optional[str] = None, adb: Optional[ADBWrapper] = None
) -> dict:
    """Head/hand/eye/body tracking status.

    Reports which oculus.internal.tracking.* services the OS has
    registered (the reliable signal available over ADB) plus any
    tracking-related system properties.
    """
    adb = adb or get_adb()
    services = _try_shell(
        adb, "dumpsys -l | grep -iE 'tracking' ", device_id
    )

    def service_state(needle: str) -> str:
        return (
            "service registered"
            if needle.lower() in services.lower()
            else "service not registered"
        )

    tracking_props = _try_shell(
        adb, "getprop | grep -iE 'handtrack|eyetrack|bodytrack|slam' | head -20",
        device_id,
    )
    details = {}
    for line in tracking_props.splitlines():
        if m := re.match(r"\[([^\]]+)\]: \[([^\]]*)\]", line.strip()):
            details[m.group(1)] = m.group(2)

    return {
        "head_tracking": "6DoF inside-out (always on for Quest)",
        "hand_tracking": service_state("IHandTrackingService"),
        "eye_tracking": service_state("IFaceEyeTrackingService"),
        "body_tracking": service_state("IInsideOutBodyTrackingService"),
        "controller_tracking": service_state("IControllerTrackingService"),
        "details": details,
    }


def set_refresh_rate(
    device_id: Optional[str] = None,
    rate: int = 72,
    adb: Optional[ADBWrapper] = None,
) -> dict:
    """Set display refresh rate via debug.oculus.refreshRate.

    Valid rates depend on the model (Quest 3: 72/80/90/120). Takes effect
    for apps that don't explicitly request a rate; a restart of the running
    app may be needed.
    """
    adb = adb or get_adb()
    product = adb.shell("getprop ro.product.device", device_id=device_id).strip()
    valid = REFRESH_RATES.get(product)
    if valid and rate not in valid:
        model = QUEST_PRODUCTS.get(product, product)
        raise ValueError(
            f"{rate}Hz is not supported on {model} — valid rates: "
            f"{'/'.join(str(r) for r in valid)}"
        )
    adb.shell(f"setprop debug.oculus.refreshRate {rate}", device_id=device_id)
    return {
        "rate": rate,
        "message": f"Refresh rate hint set to {rate}Hz "
        "(apps that request an explicit rate will override this).",
    }


def get_headset_sensors(
    device_id: Optional[str] = None, adb: Optional[ADBWrapper] = None
) -> list[dict]:
    """List hardware sensors the OS reports via sensorservice.

    Quest's sensorservice dump is a near-unstructured blob with embedded
    calibration JSON, so we extract sensor type identifiers by regex over
    the full text rather than line parsing.
    """
    adb = adb or get_adb()
    output = _try_shell(
        adb,
        "dumpsys sensorservice 2>/dev/null | grep -oE "
        "'(oculus|android)\\.sensor\\.[a-zA-Z0-9_.]+' | sort -u",
        device_id,
    )
    sensors = []
    for line in output.splitlines():
        sensor_type = line.strip().rstrip(".")
        if not sensor_type:
            continue
        sensors.append(
            {
                "type": sensor_type,
                "name": sensor_type.split(".sensor.", 1)[-1]
                .replace(".", " ")
                .replace("_", " "),
            }
        )
    return sensors
