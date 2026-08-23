"""`quest_audit_build` as an assistant sees it.

The upload tool is the one that reaches Meta; this one only reads a file, so
it needs no credentials, no confirmation and no headset. That is the point of
having it separate: an assistant should be able to answer "will this build be
accepted?" without being anywhere near an irreversible action.
"""

import pytest

from tests.apkbuild import (
    DEBUG_CERT,
    V2_BLOCK_ID,
    make_apk,
    make_release_apk,
    signing_block,
    v2_value,
)
from visor.mcp import server


class TestAuditTool:
    def test_a_store_ready_build_comes_back_ready(self, tmp_path):
        apk = make_release_apk(tmp_path / "voyager.apk")
        result = server.quest_audit_build(str(apk))
        assert result["ready"] is True
        assert result["blockers"] == []

    def test_it_needs_no_credentials(self, tmp_path, monkeypatch):
        """Reading a file is not an account operation."""
        def explode(*a, **k):
            raise AssertionError("the audit tool must not touch credentials")

        monkeypatch.setattr(server, "load_credentials", explode)
        apk = make_release_apk(tmp_path / "voyager.apk")
        assert server.quest_audit_build(str(apk))["ready"] is True

    def test_a_debug_signed_build_names_the_blocker(self, tmp_path):
        apk = make_release_apk(
            tmp_path / "debug.apk",
            signing_block=signing_block({V2_BLOCK_ID: v2_value(DEBUG_CERT)}),
        )
        result = server.quest_audit_build(str(apk))
        assert result["ready"] is False
        assert any("debug" in b["detail"].lower() for b in result["blockers"])

    def test_every_check_is_returned_not_only_the_failures(self, tmp_path):
        apk = make_release_apk(tmp_path / "voyager.apk")
        ids = {c["id"] for c in server.quest_audit_build(str(apk))["checks"]}
        assert {"package", "size", "signed", "abi", "target-sdk"} <= ids

    def test_the_facts_the_file_states_are_returned_too(self, tmp_path):
        """So the assistant can answer 'which build is this?' in the same call."""
        apk = make_release_apk(tmp_path / "voyager.apk")
        build = server.quest_audit_build(str(apk))["build"]
        assert build["package"] == "com.speakeasy3d.voyager"
        assert build["version_code"] == 2
        assert build["debug_signed"] is False

    def test_a_missing_file_is_a_tool_error_not_a_crash(self, tmp_path):
        with pytest.raises(server.QuestError):
            server.quest_audit_build(str(tmp_path / "nope.apk"))

    def test_an_unreadable_file_is_a_tool_error(self, tmp_path):
        junk = tmp_path / "not-an.apk"
        junk.write_bytes(b"nope")
        with pytest.raises(server.QuestError):
            server.quest_audit_build(str(junk))

    def test_the_expected_package_can_be_asserted(self, tmp_path):
        apk = make_release_apk(tmp_path / "voyager.apk")
        result = server.quest_audit_build(str(apk), expect_package="com.example.other")
        assert result["ready"] is False

    def test_a_stale_version_code_is_a_blocker(self, tmp_path):
        apk = make_release_apk(tmp_path / "voyager.apk")
        result = server.quest_audit_build(str(apk), channel_version_code=99)
        assert result["ready"] is False
        assert any(b["id"] == "version-code" for b in result["blockers"])

    def test_it_says_what_to_do_next(self, tmp_path):
        ready = make_release_apk(tmp_path / "ok.apk")
        broken = make_apk(tmp_path / "unsigned.apk")
        assert "upload" in server.quest_audit_build(str(ready))["next_step"].lower()
        assert "cook" in server.quest_audit_build(str(broken))["next_step"].lower()


class TestListTool:
    def test_it_finds_builds_under_a_folder(self, tmp_path):
        make_release_apk(tmp_path / "voyager.apk")
        found = server.quest_list_builds(str(tmp_path))["builds"]
        assert found[0]["package"] == "com.speakeasy3d.voyager"

    def test_an_empty_folder_is_an_empty_list_not_an_error(self, tmp_path):
        assert server.quest_list_builds(str(tmp_path))["builds"] == []

    def test_a_missing_folder_is_a_tool_error(self, tmp_path):
        with pytest.raises(server.QuestError):
            server.quest_list_builds(str(tmp_path / "nowhere"))
