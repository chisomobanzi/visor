"""Performance monitoring: CPU, memory, thermal, battery, frame timing."""

from __future__ import annotations

import re
import time
from typing import Optional

from visor.adb.wrapper import ADBCommandError, ADBWrapper, get_adb
from visor.utils.constants import (
    _DEFAULT_THRESHOLDS,
    THERMAL_CRITICAL_C,
    THERMAL_THRESHOLDS_BY_TYPE,
    THERMAL_WARN_C,
)
from visor.utils.parser import parse_dumpsys_battery, parse_thermalservice

_CPU_LINE_RE = re.compile(r"(\d+)%cpu\b.*?(\d+(?:\.\d+)?)%idle")
_MEM_LINE_RE = re.compile(
    r"Mem:\s+(\S+)\s+total,\s+(\S+)\s+used,\s+(\S+)\s+free"
)
_RESUMED_RE = re.compile(r"ResumedActivity: ActivityRecord\{\S+ u\d+ (\S+)/")


def get_thermal_state(
    device_id: Optional[str] = None, adb: Optional[ADBWrapper] = None
) -> dict:
    """Thermal status with per-sensor throttle warnings.

    Quest throttles based on skin/SoC temps; sensors above THERMAL_WARN_C
    are flagged so you can back off before the OS clamps CPU/GPU levels.
    """
    adb = adb or get_adb()
    state = parse_thermalservice(
        adb.shell("dumpsys thermalservice", device_id=device_id)
    )
    skin_temps = []
    for sensor in state["sensors"]:
        temp = sensor["temperature_c"]
        warn, critical = THERMAL_THRESHOLDS_BY_TYPE.get(
            sensor["type"], _DEFAULT_THRESHOLDS
        )
        sensor["warning"] = temp >= warn
        sensor["critical"] = temp >= critical
        if sensor["type"] == "SKIN":
            skin_temps.append(temp)
    state["max_skin_temp_c"] = max(skin_temps) if skin_temps else None
    state["warn_threshold_c"] = THERMAL_WARN_C
    state["critical_threshold_c"] = THERMAL_CRITICAL_C
    return state


def _parse_top(output: str) -> tuple[dict, dict, list[dict]]:
    """Parse `top -b -n 1` into (cpu, memory, top_processes)."""
    cpu: dict = {}
    memory: dict = {}
    processes: list[dict] = []

    for line in output.splitlines():
        if m := _CPU_LINE_RE.search(line):
            total_pct, idle_pct = float(m.group(1)), float(m.group(2))
            if total_pct > 0:
                cpu = {
                    "cores": int(total_pct / 100),
                    "usage_percent": round((total_pct - idle_pct) / total_pct * 100, 2),
                }
            continue
        if m := _MEM_LINE_RE.search(line):
            memory = {"total": m.group(1), "used": m.group(2), "free": m.group(3)}
            continue
        parts = line.split()
        if len(parts) >= 12 and parts[0].isdigit():
            try:
                processes.append(
                    {
                        "pid": int(parts[0]),
                        "cpu_percent": float(parts[8]),
                        "mem_percent": float(parts[9]),
                        "name": parts[-1],
                    }
                )
            except ValueError:
                continue

    processes.sort(key=lambda p: p["cpu_percent"], reverse=True)
    return cpu, memory, processes


def get_foreground_app(
    device_id: Optional[str] = None, adb: Optional[ADBWrapper] = None
) -> Optional[str]:
    adb = adb or get_adb()
    output = adb.shell(
        "dumpsys activity activities | grep ResumedActivity", device_id=device_id
    )
    m = _RESUMED_RE.search(output)
    return m.group(1) if m else None


def get_performance_snapshot(
    device_id: Optional[str] = None, adb: Optional[ADBWrapper] = None
) -> dict:
    """One-shot performance snapshot: CPU, memory, thermal, battery,
    foreground app, top processes."""
    adb = adb or get_adb()

    cpu, memory, processes = _parse_top(
        adb.shell("top -b -n 1 -m 10", device_id=device_id, timeout=20)
    )

    thermal = get_thermal_state(device_id, adb=adb)

    battery: dict = {}
    try:
        b = parse_dumpsys_battery(adb.shell("dumpsys battery", device_id=device_id))
        battery = {
            "level": b.level,
            "charging": b.charging,
            "temperature_c": b.temperature_c,
        }
    except ADBCommandError:
        pass
    try:
        current = adb.shell(
            "cat /sys/class/power_supply/battery/current_now", device_id=device_id
        ).strip()
        if re.fullmatch(r"-?\d+", current):
            # microamps; negative = discharging
            battery["current_ma"] = int(current) / 1000.0
    except ADBCommandError:
        pass

    foreground = None
    try:
        foreground = get_foreground_app(device_id, adb=adb)
    except ADBCommandError:
        pass

    return {
        "cpu": cpu,
        "memory": memory,
        "thermal": {
            "status_label": thermal["status_label"],
            "throttling": thermal["throttling"],
            "max_skin_temp_c": thermal["max_skin_temp_c"],
        },
        "battery": battery,
        "foreground_app": foreground,
        "top_processes": processes[:5],
    }


def monitor_performance(
    device_id: Optional[str] = None,
    interval_seconds: float = 1.0,
    duration_seconds: float = 30.0,
    adb: Optional[ADBWrapper] = None,
) -> list[dict]:
    """Sample performance repeatedly; returns a timeseries of snapshots
    (each stamped with elapsed seconds)."""
    adb = adb or get_adb()
    samples = []
    start = time.monotonic()
    while (elapsed := time.monotonic() - start) < duration_seconds:
        snap = get_performance_snapshot(device_id, adb=adb)
        snap["elapsed_s"] = round(elapsed, 2)
        samples.append(snap)
        remaining = interval_seconds - ((time.monotonic() - start) % interval_seconds)
        time.sleep(min(remaining, max(0.0, duration_seconds - elapsed)))
    return samples


def get_frame_timing(
    device_id: Optional[str] = None,
    package_name: Optional[str] = None,
    adb: Optional[ADBWrapper] = None,
) -> dict:
    """Frame timing via `dumpsys gfxinfo <package>`.

    Note: OpenXR/Unity VR apps render outside the Android UI toolkit, so
    gfxinfo may report few frames for them; treat this as best-effort and
    prefer engine-side tooling (OVR Metrics) for precise VR frame data.
    """
    adb = adb or get_adb()
    output = adb.shell(
        f"dumpsys gfxinfo {package_name or ''}", device_id=device_id, timeout=20
    )
    result: dict = {"source": "gfxinfo", "package": package_name}

    if m := re.search(r"Total frames rendered:\s*(\d+)", output):
        result["total_frames"] = int(m.group(1))
    if m := re.search(r"Janky frames:\s*(\d+)\s*\(([\d.]+)%\)", output):
        result["janky_frames"] = int(m.group(1))
        result["janky_percent"] = float(m.group(2))

    percentiles = {}
    for m in re.finditer(r"(\d+)th percentile:\s*(\d+)ms", output):
        percentiles[f"p{m.group(1)}"] = int(m.group(2))
    if percentiles:
        result["percentiles_ms"] = percentiles

    if "total_frames" not in result:
        result["note"] = (
            f"No gfxinfo frame data for {package_name!r} — VR apps rendering "
            "via OpenXR bypass the Android UI pipeline."
        )
    return result
