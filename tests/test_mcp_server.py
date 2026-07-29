"""Tests for the MCP server tool layer (mocked visor.adb underneath)."""


import pytest

from visor import adb as adb_module
from visor.mcp import server
from visor.utils.parser import Device, LogEntry


QUEST = Device(
    serial="2G0YC5ZG8309QL",
    state="device",
    product="eureka",
    model="Quest 3",
    is_quest=True,
)
PHONE = Device(serial="PHONE1", state="device", product="pixel", is_quest=False)
UNAUTH = Device(serial="LOCKED1", state="unauthorized", is_quest=False)


@pytest.fixture
def patch_adb(monkeypatch):
    def _patch(name, value):
        monkeypatch.setattr(adb_module, name, value)

    return _patch


class TestResolveDevice:
    def test_single_quest_auto_selected(self, patch_adb):
        patch_adb("discover_devices", lambda: [QUEST])
        assert server._resolve_device(None) == QUEST.serial

    def test_quest_preferred_over_other_android(self, patch_adb):
        patch_adb("discover_devices", lambda: [PHONE, QUEST])
        assert server._resolve_device(None) == QUEST.serial

    def test_no_devices_gives_actionable_error(self, patch_adb):
        patch_adb("discover_devices", lambda: [])
        with pytest.raises(server.QuestError, match="[Nn]o Quest"):
            server._resolve_device(None)

    def test_unauthorized_device_mentioned(self, patch_adb):
        patch_adb("discover_devices", lambda: [UNAUTH])
        with pytest.raises(server.QuestError, match="unauthorized"):
            server._resolve_device(None)

    def test_multiple_quests_require_explicit_id(self, patch_adb):
        q2 = Device(serial="OTHER", state="device", product="hollywood",
                    model="Quest 2", is_quest=True)
        patch_adb("discover_devices", lambda: [QUEST, q2])
        with pytest.raises(server.QuestError, match="device_id"):
            server._resolve_device(None)

    def test_explicit_id_validated(self, patch_adb):
        patch_adb("discover_devices", lambda: [QUEST])
        assert server._resolve_device(QUEST.serial) == QUEST.serial
        with pytest.raises(server.QuestError, match="not connected"):
            server._resolve_device("GONE")


class TestToolResponses:
    def test_quest_devices_returns_dicts(self, patch_adb):
        patch_adb("discover_devices", lambda: [QUEST])
        result = server.quest_devices()
        assert result[0]["serial"] == QUEST.serial
        assert result[0]["model"] == "Quest 3"

    def test_quest_logs_structured(self, patch_adb):
        patch_adb("discover_devices", lambda: [QUEST])
        entries = [
            LogEntry("07-10 14:23:46.500", 1234, 5678, "E", "Unity", "boom")
        ]
        patch_adb("logcat_dump", lambda *a, **k: entries)
        result = server.quest_logs(level="E")
        assert result["count"] == 1
        assert result["entries"][0]["tag"] == "Unity"
        assert result["filters"]["level"] == "E"

    def test_visor_errors_become_quest_errors(self, patch_adb):
        from visor.adb.wrapper import ADBCommandError

        patch_adb("discover_devices", lambda: [QUEST])

        def explode(*a, **k):
            raise ADBCommandError("Install failed: INSTALL_FAILED_OLDER_SDK")

        patch_adb("install_apk", explode)
        with pytest.raises(server.QuestError, match="INSTALL_FAILED_OLDER_SDK"):
            server.quest_install("/tmp/x.apk")


class TestScreenshotContent:
    """The image return is the one tool result that isn't plain JSON, so it is
    also the one the SDK is most likely to break underneath us."""

    def test_returns_an_image_block_and_a_caption(self, patch_adb, tmp_path):
        import io

        from PIL import Image as PILImage

        from mcp.server.mcpserver import Image

        from visor.adb.capture import Screenshot

        stereo = tmp_path / "shot.png"
        buf = io.BytesIO()
        PILImage.new("RGB", (4128, 2208), (20, 60, 90)).save(buf, format="PNG")
        stereo.write_bytes(buf.getvalue())

        patch_adb("discover_devices", lambda: [QUEST])
        patch_adb("undistort_eye", lambda img: img)  # ffmpeg-free
        patch_adb(
            "screenshot",
            lambda *a, **k: Screenshot(str(stereo), 4128, 2208, stereo.stat().st_size),
        )

        image, caption = server.quest_screenshot()
        assert isinstance(image, Image)
        block = image.to_image_content()  # what the client actually receives
        assert block.mime_type == "image/jpeg" and block.data
        assert "4128x2208" in caption and "left eye" in caption


class TestPerfTools:
    def test_perf_session_returns_summary_diagnosis_and_path(self, patch_adb, tmp_path, monkeypatch):
        from visor.adb.perf_session import PerfSession

        patch_adb("discover_devices", lambda: [QUEST])
        fake = PerfSession(
            device=QUEST.serial, package="com.my.game",
            started_at="t", duration_s=5,
            vrapi=[{"timestamp": "t", "pid": 1, "fps": 72, "target_fps": 72,
                    "elapsed_s": 0.0, "stale_frames": 0}],
        )
        patch_adb("record_session", lambda *a, **k: fake)
        monkeypatch.setattr(server, "_sessions_dir", lambda: str(tmp_path))
        result = server.quest_perf_session(duration_seconds=5)
        assert result["summary"]["sample_count"] == 1
        assert result["diagnosis"]["verdict"] in ("healthy", "no_vr_data")
        assert result["session_path"].startswith(str(tmp_path))
        import os
        assert os.path.exists(result["session_path"])

    def test_perf_diff_bad_path_is_clean_error(self, patch_adb):
        with pytest.raises(server.QuestError, match="session file"):
            server.quest_perf_diff("/nope/a.json", "/nope/b.json")


class TestToolRegistration:
    def test_all_expected_tools_registered(self):
        import anyio

        tools = anyio.run(server.mcp.list_tools)
        names = {t.name for t in tools}
        expected = {
            "quest_devices", "quest_info", "quest_screenshot", "quest_install",
            "quest_uninstall", "quest_launch", "quest_stop", "quest_logs",
            "quest_logs_stream", "quest_crash_logs", "quest_performance",
            "quest_monitor", "quest_push", "quest_pull", "quest_files",
            "quest_screen_record", "quest_app_list", "quest_clear_data",
            "quest_thermal", "quest_frame_timing", "quest_connect_wireless",
            "quest_set_refresh_rate", "quest_tracking", "quest_app_info",
            "quest_perf_session", "quest_perf_diff", "quest_app_memory",
            "quest_launch_benchmark", "quest_trace",
        }
        missing = expected - names
        assert not missing, f"tools missing from server: {missing}"

    def test_every_tool_has_description(self):
        import anyio

        tools = anyio.run(server.mcp.list_tools)
        for tool in tools:
            assert tool.description and len(tool.description) > 20, (
                f"{tool.name} needs a useful description"
            )
