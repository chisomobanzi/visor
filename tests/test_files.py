"""Tests for file push/pull/list/delete."""

from unittest import mock

import pytest

from visor.adb.files import delete_file, list_files, pull_file, push_file
from visor.adb.wrapper import ADBCommandError

LS_LONG = """total 24
drwxrwx--x 2 u0_a123 media_rw 4096 2026-07-01 10:00 files
-rw-rw---- 1 u0_a123 media_rw 1048576 2026-07-09 18:30 save.dat
-rw-rw---- 1 u0_a123 media_rw     512 2026-07-10 09:15 config.json
"""


@pytest.fixture
def adb():
    return mock.Mock()


class TestPushPull:
    def test_push(self, adb, tmp_path):
        f = tmp_path / "file.txt"
        f.write_text("hi")
        adb.run.return_value = "file.txt: 1 file pushed, 0 skipped. 0.1 MB/s\n"
        result = push_file(str(f), "/sdcard/file.txt", device_id="S", adb=adb)
        assert result.success
        args = adb.run.call_args.args[0]
        assert args[0] == "push"

    def test_push_missing_local_raises(self, adb):
        with pytest.raises(FileNotFoundError):
            push_file("/nope.txt", "/sdcard/", device_id="S", adb=adb)
        adb.run.assert_not_called()

    def test_pull(self, adb, tmp_path):
        adb.run.return_value = "/sdcard/save.dat: 1 file pulled, 0 skipped.\n"
        result = pull_file("/sdcard/save.dat", str(tmp_path), device_id="S", adb=adb)
        assert result.success
        assert adb.run.call_args.args[0][0] == "pull"


class TestListFiles:
    def test_parses_ls_long(self, adb):
        adb.shell.return_value = LS_LONG
        entries = list_files("/sdcard/Android/data/com.my.app", device_id="S", adb=adb)
        by_name = {e.name: e for e in entries}
        assert by_name["files"].is_dir is True
        assert by_name["save.dat"].is_dir is False
        assert by_name["save.dat"].size == 1048576
        assert by_name["config.json"].modified == "2026-07-10 09:15"

    def test_no_such_directory_raises(self, adb):
        adb.shell.return_value = "ls: /sdcard/nope: No such file or directory\n"
        with pytest.raises(ADBCommandError, match="No such file"):
            list_files("/sdcard/nope", device_id="S", adb=adb)


class TestDelete:
    def test_delete_success(self, adb):
        adb.shell.return_value = ""
        assert delete_file("/sdcard/file.txt", device_id="S", adb=adb) is True
        assert "rm" in adb.shell.call_args.args[0]

    def test_delete_refuses_dangerous_paths(self, adb):
        for path in ["/", "/sdcard", "/sdcard/", "/data", ""]:
            with pytest.raises(ValueError):
                delete_file(path, device_id="S", adb=adb)
        adb.shell.assert_not_called()

    def test_delete_missing_raises(self, adb):
        adb.shell.return_value = "rm: /sdcard/nope.txt: No such file or directory\n"
        with pytest.raises(ADBCommandError, match="No such file"):
            delete_file("/sdcard/nope.txt", device_id="S", adb=adb)
