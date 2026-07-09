"""Parsers that turn raw ADB/dumpsys output into structured data.

All parsers are pure functions (text in, data out) so they can be tested
against captured device output without a headset attached.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from visor.utils.constants import (
    CELSIUS_TEMPERATURE_TYPES,
    QUEST_PRODUCTS,
    TEMPERATURE_TYPES,
    THERMAL_STATUS_LABELS,
)


@dataclass
class Device:
    serial: str
    state: str  # device | unauthorized | offline
    product: Optional[str] = None
    model: Optional[str] = None
    transport_id: Optional[str] = None
    is_wireless: bool = False
    is_quest: bool = False


@dataclass
class BatteryInfo:
    level: int
    charging: bool
    temperature_c: Optional[float] = None
    voltage_mv: Optional[int] = None


@dataclass
class StorageInfo:
    total: str
    used: str
    available: str
    used_percent: int


@dataclass
class LogEntry:
    timestamp: str
    pid: int
    tid: int
    level: str
    tag: str
    message: str

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "pid": self.pid,
            "tid": self.tid,
            "level": self.level,
            "tag": self.tag,
            "message": self.message,
        }


_DEVICES_L_RE = re.compile(r"^(\S+)\s+(device|unauthorized|offline|recovery|sideload|bootloader)\s*(.*)$")

_LOGCAT_THREADTIME_RE = re.compile(
    r"^(\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d{3})\s+(\d+)\s+(\d+)\s+([VDIWEF])\s+(.*?)\s*:\s(.*)$"
)

_TEMPERATURE_RE = re.compile(
    r"Temperature\{mValue=(-?[\d.]+),\s*mType=(\d+),\s*mName=([^,]+),\s*mStatus=(\d+)\}"
)


def parse_devices_l(output: str) -> list[Device]:
    """Parse `adb devices -l` output."""
    devices = []
    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith("List of devices"):
            continue
        m = _DEVICES_L_RE.match(line)
        if not m:
            continue
        serial, state, rest = m.groups()
        attrs = dict(
            part.split(":", 1) for part in rest.split() if ":" in part
        )
        product = attrs.get("product")
        model = attrs.get("model", "").replace("_", " ") or None
        devices.append(
            Device(
                serial=serial,
                state=state,
                product=product,
                model=model,
                transport_id=attrs.get("transport_id"),
                is_wireless=bool(re.match(r"^[\d.]+:\d+$", serial)) or serial.startswith("adb-"),
                is_quest=product in QUEST_PRODUCTS,
            )
        )
    return devices


def parse_dumpsys_battery(output: str) -> BatteryInfo:
    """Parse `dumpsys battery` output."""
    values: dict[str, str] = {}
    for line in output.splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            values[key.strip()] = value.strip()
    temp = values.get("temperature")
    return BatteryInfo(
        level=int(values.get("level", -1)),
        charging=(
            values.get("AC powered") == "true"
            or values.get("USB powered") == "true"
            or values.get("Wireless powered") == "true"
        ),
        temperature_c=int(temp) / 10.0 if temp and temp.lstrip("-").isdigit() else None,
        voltage_mv=int(values["voltage"]) if values.get("voltage", "").isdigit() else None,
    )


def parse_df_line(output: str) -> Optional[StorageInfo]:
    """Parse a single `df -h <path>` data line (no header)."""
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[4].endswith("%") and parts[4][:-1].isdigit():
            return StorageInfo(
                total=parts[1],
                used=parts[2],
                available=parts[3],
                used_percent=int(parts[4].rstrip("%")),
            )
    return None


def parse_ip_route(output: str) -> Optional[str]:
    """Extract the device's WLAN IP from `ip route` output."""
    m = re.search(r"\bsrc\s+([\d.]+)", output)
    return m.group(1) if m else None


def parse_logcat_line(line: str) -> Optional[LogEntry]:
    """Parse one logcat line in threadtime format. Returns None for
    non-log lines (buffer markers, blank lines)."""
    m = _LOGCAT_THREADTIME_RE.match(line)
    if not m:
        return None
    timestamp, pid, tid, level, tag, message = m.groups()
    return LogEntry(
        timestamp=timestamp,
        pid=int(pid),
        tid=int(tid),
        level=level,
        tag=tag.strip(),
        message=message,
    )


def parse_package_list(output: str) -> list[str]:
    """Parse `pm list packages` output into bare package names."""
    return [
        line.strip().removeprefix("package:")
        for line in output.splitlines()
        if line.strip().startswith("package:")
    ]


def parse_thermalservice(output: str) -> dict:
    """Parse `dumpsys thermalservice` into overall status + temperature sensors.

    On Quest OS v14+ /sys/class/thermal is not readable over ADB, so this is
    the primary thermal data source.
    """
    status = 0
    m = re.search(r"Thermal Status:\s*(\d+)", output)
    if m:
        status = int(m.group(1))

    sensors = []
    seen = set()
    for match in _TEMPERATURE_RE.finditer(output):
        value, mtype, name, mstatus = match.groups()
        mtype = int(mtype)
        if mtype not in CELSIUS_TEMPERATURE_TYPES:
            continue
        if name in seen:  # "Current temperatures" section repeats cached ones
            continue
        seen.add(name)
        sensors.append(
            {
                "name": name,
                "temperature_c": float(value),
                "type": TEMPERATURE_TYPES.get(mtype, f"UNKNOWN_{mtype}"),
                "throttle_status": int(mstatus),
            }
        )

    return {
        "status": status,
        "status_label": THERMAL_STATUS_LABELS.get(status, f"UNKNOWN_{status}"),
        "throttling": status >= 1,
        "sensors": sensors,
    }
