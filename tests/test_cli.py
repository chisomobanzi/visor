"""Tests for the visor CLI (typer runner with mocked visor.adb)."""

from unittest import mock

import pytest
from typer.testing import CliRunner

from visor import adb as adb_module
from visor.cli.main import app
from visor.utils.parser import BatteryInfo, Device, LogEntry, StorageInfo

runner = CliRunner()

QUEST = Device(
    serial="2G0YC5ZG8309QL", state="device", product="eureka",
    model="Quest 3", is_quest=True,
)


@pytest.fixture
def patch_adb(monkeypatch):
    def _patch(name, value):
        monkeypatch.setattr(adb_module, name, value)
    return _patch


class TestDevices:
    def test_devices_table(self, patch_adb):
        patch_adb("discover_devices", lambda: [QUEST])
        result = runner.invoke(app, ["devices"])
        assert result.exit_code == 0
        assert "2G0YC5ZG8309QL" in result.output
        assert "Quest 3" in result.output

    def test_no_devices_message(self, patch_adb):
        patch_adb("discover_devices", lambda: [])
        result = runner.invoke(app, ["devices"])
        assert result.exit_code == 0
        assert "No devices" in result.output

    def test_unauthorized_flagged(self, patch_adb):
        locked = Device(serial="X1", state="unauthorized")
        patch_adb("discover_devices", lambda: [locked])
        result = runner.invoke(app, ["devices"])
        assert "unauthorized" in result.output


class TestLogs:
    def test_logs_rendered_with_levels(self, patch_adb):
        patch_adb("discover_devices", lambda: [QUEST])
        entries = [
            LogEntry("07-10 14:23:45.123", 1, 2, "E", "Unity", "NullReferenceException"),
            LogEntry("07-10 14:23:46.000", 1, 2, "W", "VrApi", "FPS=72"),
        ]
        patch_adb("logcat_dump", lambda *a, **k: entries)
        result = runner.invoke(app, ["logs"])
        assert result.exit_code == 0
        assert "NullReferenceException" in result.output
        assert "VrApi" in result.output

    def test_logs_passes_filters(self, patch_adb):
        patch_adb("discover_devices", lambda: [QUEST])
        captured = {}

        def fake_dump(serial, lines=500, tag=None, level="W", pattern=None):
            captured.update(tag=tag, level=level, pattern=pattern, lines=lines)
            return []

        patch_adb("logcat_dump", fake_dump)
        result = runner.invoke(
            app, ["logs", "--tag", "Unity", "--level", "D", "--grep", "NullRef"]
        )
        assert result.exit_code == 0
        assert captured["tag"] == "Unity"
        assert captured["level"] == "D"
        assert captured["pattern"] == "NullRef"


class TestErrors:
    def test_visor_error_is_clean_not_traceback(self, patch_adb):
        from visor.adb.wrapper import DeviceUnauthorizedError

        def explode():
            raise DeviceUnauthorizedError(
                "Device is unauthorized. Put on the headset and accept the prompt."
            )

        patch_adb("discover_devices", explode)
        result = runner.invoke(app, ["devices"])
        assert result.exit_code == 1
        assert "unauthorized" in result.output
        assert "Traceback" not in result.output

    def test_install_missing_file(self, patch_adb):
        patch_adb("discover_devices", lambda: [QUEST])
        result = runner.invoke(app, ["install", "/nonexistent.apk"])
        assert result.exit_code == 1
        assert "not found" in result.output.lower()


class TestApps:
    def test_apps_listing(self, patch_adb):
        patch_adb("discover_devices", lambda: [QUEST])
        patch_adb("list_apps", lambda *a, **k: ["com.my.game", "com.other.app"])
        result = runner.invoke(app, ["apps"])
        assert result.exit_code == 0
        assert "com.my.game" in result.output

    def test_launch(self, patch_adb):
        patch_adb("discover_devices", lambda: [QUEST])
        patch_adb("launch_app", lambda pkg, serial: f"Launched {pkg}")
        result = runner.invoke(app, ["launch", "com.my.game"])
        assert result.exit_code == 0
        assert "Launched" in result.output


class TestPerf:
    def test_perf_snapshot(self, patch_adb):
        patch_adb("discover_devices", lambda: [QUEST])
        patch_adb(
            "get_performance_snapshot",
            lambda serial: {
                "cpu": {"cores": 6, "usage_percent": 17.8},
                "memory": {"total": "7.9G", "used": "6.5G", "free": "1.3G"},
                "thermal": {"status_label": "NONE", "throttling": False,
                            "max_skin_temp_c": 32.0},
                "battery": {"level": 50, "charging": True, "current_ma": -500.0},
                "foreground_app": "com.my.game",
                "top_processes": [
                    {"pid": 1, "cpu_percent": 50.0, "mem_percent": 10.0,
                     "name": "com.my.game"},
                ],
            },
        )
        result = runner.invoke(app, ["perf"])
        assert result.exit_code == 0
        assert "com.my.game" in result.output
        assert "17.8" in result.output

    def test_thermal_warning_shown(self, patch_adb):
        patch_adb("discover_devices", lambda: [QUEST])
        patch_adb(
            "get_thermal_state",
            lambda serial: {
                "status": 3, "status_label": "SEVERE", "throttling": True,
                "max_skin_temp_c": 43.5,
                "warn_threshold_c": 40.0, "critical_threshold_c": 46.0,
                "sensors": [
                    {"name": "soc-usr", "temperature_c": 43.5, "type": "SKIN",
                     "throttle_status": 3, "warning": True, "critical": False},
                ],
            },
        )
        result = runner.invoke(app, ["thermal"])
        assert result.exit_code == 0
        assert "SEVERE" in result.output
        assert "43.5" in result.output


class TestDeviceOption:
    def test_explicit_device_passed_through(self, patch_adb):
        q2 = Device(serial="OTHER1", state="device", product="hollywood",
                    model="Quest 2", is_quest=True)
        patch_adb("discover_devices", lambda: [QUEST, q2])
        captured = {}

        def fake_dump(serial, **kwargs):
            captured["serial"] = serial
            return []

        patch_adb("logcat_dump", fake_dump)
        result = runner.invoke(app, ["--device", "OTHER1", "logs"])
        assert result.exit_code == 0
        assert captured["serial"] == "OTHER1"

    def test_multiple_devices_without_flag_errors(self, patch_adb):
        q2 = Device(serial="OTHER1", state="device", product="hollywood",
                    model="Quest 2", is_quest=True)
        patch_adb("discover_devices", lambda: [QUEST, q2])
        result = runner.invoke(app, ["logs"])
        assert result.exit_code == 1
        assert "--device" in result.output
