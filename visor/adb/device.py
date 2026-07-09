"""Device discovery, info, and wireless connection."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from visor.adb.wrapper import ADBCommandError, ADBWrapper, get_adb
from visor.utils.constants import DEFAULT_WIRELESS_PORT, QUEST_PRODUCTS
from visor.utils.parser import (
    BatteryInfo,
    Device,
    StorageInfo,
    parse_devices_l,
    parse_df_line,
    parse_dumpsys_battery,
    parse_ip_route,
    parse_thermalservice,
)


@dataclass
class DeviceInfo:
    serial: str
    model: Optional[str]
    product: Optional[str]
    os_version: Optional[str]
    firmware: Optional[str]
    battery: Optional[BatteryInfo]
    storage: Optional[StorageInfo]
    ip_address: Optional[str]
    thermal_status: Optional[str] = None


@dataclass
class WirelessConnection:
    address: str
    message: str


def discover_devices(adb: Optional[ADBWrapper] = None) -> list[Device]:
    """List connected devices. Quest devices have is_quest=True."""
    adb = adb or get_adb()
    output = adb.run(["devices", "-l"])
    return parse_devices_l(output)


def get_device_info(device_id: str, adb: Optional[ADBWrapper] = None) -> DeviceInfo:
    """Detailed device info: model, firmware, battery, storage, network."""
    adb = adb or get_adb()

    def prop(name: str) -> Optional[str]:
        try:
            value = adb.shell(f"getprop {name}", device_id=device_id).strip()
            return value or None
        except ADBCommandError:
            return None

    model = prop("ro.product.model")
    product = prop("ro.product.device")

    battery = None
    try:
        battery = parse_dumpsys_battery(adb.shell("dumpsys battery", device_id=device_id))
    except ADBCommandError:
        pass

    storage = None
    try:
        storage = parse_df_line(adb.shell("df -h /data", device_id=device_id))
    except ADBCommandError:
        pass

    ip_address = None
    try:
        ip_address = parse_ip_route(adb.shell("ip route", device_id=device_id))
    except ADBCommandError:
        pass

    thermal_status = None
    try:
        thermal = parse_thermalservice(
            adb.shell("dumpsys thermalservice", device_id=device_id)
        )
        thermal_status = thermal["status_label"]
    except ADBCommandError:
        pass

    return DeviceInfo(
        serial=device_id,
        model=model,
        product=product,
        os_version=prop("ro.build.version.release"),
        firmware=prop("ro.build.branch"),
        battery=battery,
        storage=storage,
        ip_address=ip_address,
        thermal_status=thermal_status,
    )


def connect_wireless(
    device_id: str,
    port: int = DEFAULT_WIRELESS_PORT,
    adb: Optional[ADBWrapper] = None,
) -> WirelessConnection:
    """Switch a USB-connected device to wireless ADB.

    Requires the device to be on WiFi. After this succeeds the USB cable
    can be unplugged; the device stays reachable at <ip>:<port>.
    """
    adb = adb or get_adb()

    ip = parse_ip_route(adb.shell("ip route", device_id=device_id))
    if not ip:
        raise ADBCommandError(
            "Device has no WiFi connection — connect the headset to WiFi first."
        )

    adb.run(["tcpip", str(port)], device_id=device_id, timeout=15)

    address = f"{ip}:{port}"
    output = adb.run(["connect", address], timeout=15)
    if "connected" not in output.lower() or "failed" in output.lower():
        raise ADBCommandError(
            f"Failed to connect wirelessly to {address}: {output.strip()}"
        )
    return WirelessConnection(
        address=address,
        message=f"Wireless ADB active at {address}. USB cable can be unplugged.",
    )


def pair_device(
    ip: str, port: int, code: str, adb: Optional[ADBWrapper] = None
) -> bool:
    """Pair with a device using wireless debugging pairing code
    (Settings > Developer > Wireless Debugging on the headset)."""
    adb = adb or get_adb()
    output = adb.run(["pair", f"{ip}:{port}", code], timeout=30)
    if "successfully paired" not in output.lower():
        raise ADBCommandError(f"Pairing failed: {output.strip()}")
    return True
