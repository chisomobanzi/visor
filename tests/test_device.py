"""Tests for device discovery and info — fixtures are real Quest 3 output."""

from unittest import mock

import pytest

from visor.adb.device import (
    connect_wireless,
    discover_devices,
    get_device_info,
    pair_device,
)
from visor.adb.wrapper import ADBCommandError

# Real output from `adb devices -l` with a Quest 3 attached
DEVICES_L = """List of devices attached
2G0YC5ZG8309QL         device usb:1-2 product:eureka model:Quest_3 device:eureka transport_id:6
"""

DEVICES_L_MIXED = """List of devices attached
2G0YC5ZG8309QL         device usb:1-2 product:eureka model:Quest_3 device:eureka transport_id:6
1WMHH812345678         unauthorized usb:1-3 transport_id:7
192.168.1.31:5555      device product:eureka model:Quest_3 device:eureka transport_id:8
emulator-5554          device product:sdk_gphone64 model:sdk_gphone64_x86_64 device:generic transport_id:9
"""

# Real output from `dumpsys battery` on Quest 3
DUMPSYS_BATTERY = """Current Battery Service state:
  AC powered: true
  USB powered: false
  Wireless powered: false
  Dock powered: false
  Max charging current: 3000000
  Max charging voltage: 5000000
  Weak Charger: false
  Charge counter: 327563
  status: 2
  health: 2
  present: true
  level: 7
  scale: 100
  voltage: 3994
  temperature: 360
  technology: Li-ion
"""

DF_DATA = "/dev/block/dm-55 448G  34G  415G   8% /data\n"

IP_ROUTE = "192.168.1.0/24 dev wlan0 proto kernel scope link src 192.168.1.31 \n"


@pytest.fixture
def adb():
    return mock.Mock()


class TestDiscoverDevices:
    def test_single_quest_device(self, adb):
        adb.run.return_value = DEVICES_L
        devices = discover_devices(adb=adb)
        assert len(devices) == 1
        d = devices[0]
        assert d.serial == "2G0YC5ZG8309QL"
        assert d.state == "device"
        assert d.model == "Quest 3"
        assert d.product == "eureka"
        assert d.is_wireless is False

    def test_mixed_list_flags_unauthorized_and_wireless(self, adb):
        adb.run.return_value = DEVICES_L_MIXED
        devices = discover_devices(adb=adb)
        assert len(devices) == 4
        assert devices[1].state == "unauthorized"
        assert devices[2].is_wireless is True
        # Non-Quest devices are still listed but flagged
        assert devices[0].is_quest is True
        assert devices[3].is_quest is False

    def test_empty_list(self, adb):
        adb.run.return_value = "List of devices attached\n\n"
        assert discover_devices(adb=adb) == []


class TestGetDeviceInfo:
    def _wire_shell(self, adb, responses):
        """Map substring of shell command -> canned output."""

        def fake_shell(cmd, device_id=None, timeout=None):
            for key, value in responses.items():
                if key in cmd:
                    return value
            return ""

        adb.shell.side_effect = fake_shell

    def test_quest3_info(self, adb):
        self._wire_shell(
            adb,
            {
                "getprop ro.product.model": "Quest 3\n",
                "getprop ro.build.version.release": "14\n",
                "getprop ro.build.branch": "releases-oculus-14.0-v204\n",
                "getprop ro.product.device": "eureka\n",
                "dumpsys battery": DUMPSYS_BATTERY,
                "df ": DF_DATA,
                "ip route": IP_ROUTE,
            },
        )
        info = get_device_info("2G0YC5ZG8309QL", adb=adb)
        assert info.model == "Quest 3"
        assert info.os_version == "14"
        assert info.firmware == "releases-oculus-14.0-v204"
        assert info.battery.level == 7
        assert info.battery.charging is True
        assert info.battery.temperature_c == 36.0
        assert info.storage.total == "448G"
        assert info.storage.available == "415G"
        assert info.storage.used_percent == 8
        assert info.ip_address == "192.168.1.31"

    def test_no_wifi_means_no_ip(self, adb):
        self._wire_shell(adb, {"ip route": "", "dumpsys battery": DUMPSYS_BATTERY})
        info = get_device_info("X", adb=adb)
        assert info.ip_address is None


class TestConnectWireless:
    def test_happy_path(self, adb):
        adb.shell.return_value = IP_ROUTE
        adb.run.side_effect = [
            "",  # tcpip 5555
            "connected to 192.168.1.31:5555",  # connect
        ]
        result = connect_wireless("2G0YC5ZG8309QL", adb=adb)
        assert result.address == "192.168.1.31:5555"
        adb.run.assert_any_call(
            ["tcpip", "5555"], device_id="2G0YC5ZG8309QL", timeout=mock.ANY
        )

    def test_no_wifi_raises(self, adb):
        adb.shell.return_value = ""
        with pytest.raises(ADBCommandError, match="WiFi"):
            connect_wireless("2G0YC5ZG8309QL", adb=adb)

    def test_connect_refused_raises(self, adb):
        adb.shell.return_value = IP_ROUTE
        adb.run.side_effect = [
            "",
            "failed to connect to '192.168.1.31:5555': Connection refused",
        ]
        with pytest.raises(ADBCommandError, match="[Ff]ailed to connect"):
            connect_wireless("2G0YC5ZG8309QL", adb=adb)


class TestPairDevice:
    def test_pair_success(self, adb):
        adb.run.return_value = (
            "Successfully paired to 192.168.1.31:37123 [guid=adb-xxxx]"
        )
        result = pair_device("192.168.1.31", 37123, "123456", adb=adb)
        assert result is True

    def test_pair_failure_raises(self, adb):
        adb.run.return_value = "Failed: Wrong password or connection was dropped"
        with pytest.raises(ADBCommandError, match="[Pp]airing failed"):
            pair_device("192.168.1.31", 37123, "000000", adb=adb)
