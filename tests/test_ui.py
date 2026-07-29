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


def _png(width, height, colour=(30, 90, 120)):
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (width, height), colour).save(buf, format="PNG")
    return buf.getvalue()


def _size(data):
    import io

    from PIL import Image

    with Image.open(io.BytesIO(data)) as img:
        return img.size


@pytest.fixture
def shots_dir(monkeypatch, tmp_path):
    """Point the gallery at a throwaway directory, not the real ~/.visor."""
    from visor.ui import server

    directory = tmp_path / "shots"
    directory.mkdir()
    monkeypatch.setattr(server, "_shots_dir", lambda: directory)
    return directory


class TestShotGallery:
    def test_upload_list_thumb_delete(self, shots_dir):
        assert client.get("/api/shots").json() == {
            "dir": str(shots_dir), "total": 0, "shots": []
        }

        r = client.post(
            "/api/shots/upload", files={"file": ("frame.png", _png(640, 480), "image/png")}
        )
        assert r.status_code == 200
        name = r.json()["name"]
        assert (r.json()["width"], r.json()["height"]) == (640, 480)

        listing = client.get("/api/shots").json()
        assert listing["total"] == 1 and listing["shots"][0]["name"] == name

        full = client.get(f"/api/shots/{name}")
        assert full.headers["content-type"] == "image/png"

        thumb = client.get(f"/api/shots/{name}?thumb=1")
        assert thumb.headers["content-type"] == "image/jpeg"
        assert max(_size(thumb.content)) <= 360 < max(_size(full.content))

        assert client.delete(f"/api/shots/{name}").status_code == 200
        assert client.get("/api/shots").json()["total"] == 0
        assert client.delete(f"/api/shots/{name}").status_code == 404

    def test_capture_crops_to_one_eye(self, patch_adb, shots_dir, tmp_path):
        from visor.adb.capture import Screenshot

        stereo = tmp_path / "stereo.png"
        stereo.write_bytes(_png(4128, 2208))

        patch_adb("discover_devices", lambda: [QUEST])
        patch_adb("undistort_eye", lambda img: img)  # ffmpeg-free
        patch_adb(
            "screenshot",
            lambda *a, **k: Screenshot(str(stereo), 4128, 2208, stereo.stat().st_size),
        )

        r = client.post("/api/shots")
        assert r.status_code == 200
        # 4128-wide stereo -> left eye (2064) -> capped at 1600
        assert r.json()["width"] == 1600
        assert (shots_dir / r.json()["name"]).is_file()

    def test_path_traversal_blocked(self, shots_dir):
        assert client.get("/api/shots/..%2F..%2Fetc%2Fpasswd").status_code == 404
        assert client.delete("/api/shots/..%2F..%2Fsecret.png").status_code == 404

    def test_upload_rejects_non_image(self, shots_dir):
        r = client.post(
            "/api/shots/upload", files={"file": ("x.png", b"not a png", "image/png")}
        )
        assert r.status_code == 400

