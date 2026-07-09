"""Tests for output parsers — fixtures taken from a real Quest 3 (OS v14)."""

from visor.utils.parser import (
    parse_devices_l,
    parse_df_line,
    parse_dumpsys_battery,
    parse_ip_route,
    parse_logcat_line,
    parse_package_list,
    parse_thermalservice,
)

THERMALSERVICE = """IsStatusOverride: false
ThermalEventListeners:
\tcallbacks: 3
\tkilled: false
\tbroadcasts count: -1
ThermalStatusListeners:
\tcallbacks: 3
\tkilled: false
\tbroadcasts count: -1
Thermal Status: 0
Cached temperatures:
\tTemperature{mValue=33.172, mType=9, mName=nspss-0, mStatus=0}
\tTemperature{mValue=31.706001, mType=3, mName=soc-usr, mStatus=0}
\tTemperature{mValue=24.919, mType=2, mName=batt-virt-usr, mStatus=0}
\tTemperature{mValue=1.0, mType=6, mName=pmxr2230-bcl-lvl0, mStatus=1}
\tTemperature{mValue=-937.0, mType=7, mName=pm8550b-ibat-lvl0, mStatus=0}
\tTemperature{mValue=28.169, mType=4, mName=usb-conn, mStatus=0}
"""


class TestThermalParser:
    def test_parses_status_and_temperatures(self):
        result = parse_thermalservice(THERMALSERVICE)
        assert result["status"] == 0
        assert result["status_label"] == "NONE"
        sensors = {s["name"]: s for s in result["sensors"]}
        assert sensors["soc-usr"]["temperature_c"] == 31.706001
        assert sensors["soc-usr"]["type"] == "SKIN"
        assert sensors["batt-virt-usr"]["type"] == "BATTERY"
        assert sensors["usb-conn"]["type"] == "USB_PORT"

    def test_filters_non_temperature_sensors(self):
        # BCL levels (type 6/7) are current/voltage trip points, not temps
        result = parse_thermalservice(THERMALSERVICE)
        names = [s["name"] for s in result["sensors"]]
        assert "pmxr2230-bcl-lvl0" not in names
        assert "pm8550b-ibat-lvl0" not in names

    def test_throttling_status_labels(self):
        elevated = THERMALSERVICE.replace("Thermal Status: 0", "Thermal Status: 3")
        result = parse_thermalservice(elevated)
        assert result["status_label"] == "SEVERE"
        assert result["throttling"] is True


class TestLogcatParser:
    def test_threadtime_line(self):
        line = "07-10 14:23:45.123  1234  5678 E Unity   : NullReferenceException: Object reference not set"
        entry = parse_logcat_line(line)
        assert entry.timestamp == "07-10 14:23:45.123"
        assert entry.pid == 1234
        assert entry.tid == 5678
        assert entry.level == "E"
        assert entry.tag == "Unity"
        assert entry.message.startswith("NullReferenceException")

    def test_tag_with_spaces_and_colon_in_message(self):
        line = "07-10 09:01:02.000   800   800 W VrApi   : FPS=72,Prd=32ms,Tear=0"
        entry = parse_logcat_line(line)
        assert entry.tag == "VrApi"
        assert entry.message == "FPS=72,Prd=32ms,Tear=0"

    def test_non_matching_line_returns_none(self):
        assert parse_logcat_line("--------- beginning of main") is None


class TestDevicesParser:
    def test_quest3_line(self):
        line = "2G0YC5ZG8309QL         device usb:1-2 product:eureka model:Quest_3 device:eureka transport_id:6"
        d = parse_devices_l(f"List of devices attached\n{line}\n")[0]
        assert d.serial == "2G0YC5ZG8309QL"
        assert d.model == "Quest 3"
        assert d.is_quest


class TestSmallParsers:
    def test_battery(self):
        out = "  AC powered: true\n  status: 2\n  level: 42\n  scale: 100\n  temperature: 360\n  voltage: 3994\n"
        b = parse_dumpsys_battery(out)
        assert b.level == 42
        assert b.charging is True
        assert b.temperature_c == 36.0

    def test_df(self):
        s = parse_df_line("/dev/block/dm-55 448G  34G  415G   8% /data\n")
        assert (s.total, s.used, s.available, s.used_percent) == ("448G", "34G", "415G", 8)

    def test_ip_route(self):
        ip = parse_ip_route("192.168.1.0/24 dev wlan0 proto kernel scope link src 192.168.1.31 \n")
        assert ip == "192.168.1.31"
        assert parse_ip_route("") is None

    def test_package_list(self):
        out = "package:com.beatgames.beatsaber\npackage:quest.eleven.forfunlabs\n"
        assert parse_package_list(out) == [
            "com.beatgames.beatsaber",
            "quest.eleven.forfunlabs",
        ]
