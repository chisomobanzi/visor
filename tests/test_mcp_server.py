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
