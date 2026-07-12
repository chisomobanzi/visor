"""Tests for the dashboard perf-workspace endpoints."""

import json
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from visor import adb as adb_module
from visor.ui import server as ui_server
from visor.ui.server import app
from visor.utils.parser import Device

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


@pytest.fixture
def sessions_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(ui_server, "_sessions_dir", lambda: tmp_path)
    return tmp_path


def write_session(dirpath, name, fps=72, stale=0):
    data = {
        "schema": "visor-perf-session/1",
        "device": "S", "package": "com.my.game",
        "started_at": "2026-07-12T12:00:00", "duration_s": 10,
        "vrapi": [
            {"timestamp": "t", "pid": 1, "fps": fps, "target_fps": 72,
             "stale_frames": stale, "elapsed_s": float(i)}
            for i in range(10)
        ],
        "memory": [], "thermal": [], "events": [], "meta": {},
    }
    (dirpath / name).write_text(json.dumps(data))


class TestSessionEndpoints:
    def test_live_without_recording_is_404(self, patch_adb):
        patch_adb("discover_devices", lambda: [QUEST])
        r = client.get("/api/perf/session/live")
        assert r.status_code == 404

    def test_list_and_load_sessions(self, sessions_dir):
        write_session(sessions_dir, "com_my_game-1.json")
        r = client.get("/api/perf/sessions")
        assert r.status_code == 200
        assert r.json()["sessions"][0]["package"] == "com.my.game"

        r = client.get("/api/perf/session?name=com_my_game-1.json")
        assert r.status_code == 200
        body = r.json()
        assert body["summary"]["fps"]["avg"] == 72
        assert body["diagnosis"]["verdict"] == "healthy"

    def test_session_path_traversal_blocked(self, sessions_dir):
        r = client.get("/api/perf/session?name=../../etc/passwd")
        assert r.status_code == 404

    def test_diff_endpoint(self, sessions_dir):
        write_session(sessions_dir, "before.json", fps=61, stale=10)
        write_session(sessions_dir, "after.json", fps=72, stale=0)
        r = client.get("/api/perf/diff?before=before.json&after=after.json")
        assert r.status_code == 200
        assert r.json()["fps_avg"]["delta"] == 11

    def test_start_stop_roundtrip(self, patch_adb, sessions_dir, monkeypatch):
        from visor.adb.perf_session import PerfSession

        patch_adb("discover_devices", lambda: [QUEST])

        fake_session = PerfSession(
            device=QUEST.serial, package="com.my.game",
            started_at="t", duration_s=3,
            vrapi=[{"timestamp": "t", "pid": 1, "fps": 72, "target_fps": 72,
                    "stale_frames": 0, "elapsed_s": 0.0}],
        )

        class FakeRecorder:
            def __init__(self, serial, package=None):
                self.package = package or "com.my.game"
            def start(self):
                return fake_session
            def stop(self):
                return fake_session
            def snapshot(self):
                d = fake_session.to_dict()
                d["recording"] = True
                return d

        import visor.adb.perf_session as ps
        monkeypatch.setattr(ps, "LiveRecorder", FakeRecorder)

        r = client.post("/api/perf/session/start")
        assert r.status_code == 200
        assert r.json()["recording"] is True

        r = client.get("/api/perf/session/live")
        assert r.status_code == 200
        assert r.json()["recording"] is True

        r = client.post("/api/perf/session/stop")
        assert r.status_code == 200
        body = r.json()
        assert body["diagnosis"]["verdict"] == "healthy"
        assert (sessions_dir / body["name"]).exists()
