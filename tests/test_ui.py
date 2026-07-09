"""Tests for the web dashboard backend (FastAPI TestClient, mocked adb)."""

import pytest
from fastapi.testclient import TestClient

from visor import adb as adb_module
from visor.ui.server import app
from visor.utils.parser import Device, LogEntry

client = TestClient(app, raise_server_exceptions=False)

QUEST = Device(
    serial="2G0YC5ZG8309QL", state="device", product="eureka",
    model="Quest 3", is_quest=True,
)


@pytest.fixture
def patch_adb(monkeypatch):
    def _patch(name, value):
        monkeypatch.setattr(adb_module, name, value)
    return _patch


class TestIndex:
    def test_serves_dashboard(self):
        r = client.get("/")
        assert r.status_code == 200
        assert "VISOR" in r.text
        # self-contained: no external resources
        assert "http://" not in r.text.replace("http://127.0.0.1", "")
        assert "cdn." not in r.text


class TestAPI:
    def test_devices(self, patch_adb):
        patch_adb("discover_devices", lambda: [QUEST])
        r = client.get("/api/devices")
        assert r.status_code == 200
        assert r.json()[0]["serial"] == QUEST.serial

    def test_no_device_gives_503(self, patch_adb):
        patch_adb("discover_devices", lambda: [])
        r = client.get("/api/perf")
        assert r.status_code == 503

    def test_logs(self, patch_adb):
        patch_adb("discover_devices", lambda: [QUEST])
        patch_adb("logcat_dump", lambda *a, **k: [
            LogEntry("07-10 14:23:45.123", 1, 2, "E", "Unity", "boom"),
        ])
        r = client.get("/api/logs?level=E")
        assert r.status_code == 200
        assert r.json()["entries"][0]["tag"] == "Unity"

    def test_visor_error_surfaces_as_500_with_detail(self, patch_adb):
        from visor.adb.wrapper import ADBCommandError

        patch_adb("discover_devices", lambda: [QUEST])

        def explode(*a, **k):
            raise ADBCommandError("device fell off the desk")

        patch_adb("get_performance_snapshot", explode)
        r = client.get("/api/perf")
        assert r.status_code == 500
        assert "fell off the desk" in r.json()["detail"]

    def test_recording_path_traversal_blocked(self):
        r = client.get("/api/recording?path=/etc/passwd")
        assert r.status_code == 403

    def test_apps_actions(self, patch_adb):
        patch_adb("discover_devices", lambda: [QUEST])
        patch_adb("launch_app", lambda pkg, serial: f"Launched {pkg}")
        r = client.post("/api/apps/launch?package=com.my.game")
        assert r.status_code == 200
        assert "Launched" in r.json()["message"]

    def test_install_rejects_non_apk(self, patch_adb):
        patch_adb("discover_devices", lambda: [QUEST])
        r = client.post(
            "/api/apps/install",
            files={"file": ("notanapk.txt", b"hello", "text/plain")},
        )
        assert r.status_code == 400
