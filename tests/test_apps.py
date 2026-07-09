"""Tests for app management."""

from unittest import mock

import pytest

from visor.adb.apps import (
    clear_app_data,
    get_app_info,
    install_apk,
    launch_app,
    list_apps,
    stop_app,
    uninstall_app,
)
from visor.adb.wrapper import ADBCommandError

PACKAGES_3 = """package:com.beatgames.beatsaber
package:quest.eleven.forfunlabs
package:com.mycompany.mygame
"""

DUMPSYS_PACKAGE = """Packages:
  Package [com.mycompany.mygame] (a1b2c3):
    userId=10123
    pkg=Package{a1b2c3 com.mycompany.mygame}
    codePath=/data/app/~~xyz==/com.mycompany.mygame-abc==
    versionCode=42 minSdk=29 targetSdk=32
    versionName=1.3.7
    firstInstallTime=2026-07-01 10:00:00
    lastUpdateTime=2026-07-09 18:30:00
    requested permissions:
      android.permission.INTERNET
      android.permission.RECORD_AUDIO
    install permissions:
      android.permission.INTERNET: granted=true
"""


@pytest.fixture
def adb():
    return mock.Mock()


class TestInstall:
    def test_install_success(self, adb, tmp_path):
        apk = tmp_path / "game.apk"
        apk.write_bytes(b"PK")
        adb.run.return_value = "Performing Streamed Install\nSuccess\n"
        result = install_apk(str(apk), device_id="S", adb=adb)
        assert result.success is True
        args = adb.run.call_args.args[0]
        assert args[0] == "install"
        assert "-r" in args  # replace by default

    def test_missing_apk_raises_before_calling_adb(self, adb):
        with pytest.raises(FileNotFoundError):
            install_apk("/nonexistent/game.apk", device_id="S", adb=adb)
        adb.run.assert_not_called()

    def test_install_failure_surfaces_reason(self, adb, tmp_path):
        apk = tmp_path / "game.apk"
        apk.write_bytes(b"PK")
        adb.run.return_value = (
            "Performing Streamed Install\n"
            "adb: failed to install game.apk: Failure "
            "[INSTALL_FAILED_UPDATE_INCOMPATIBLE: signatures do not match]\n"
        )
        with pytest.raises(ADBCommandError, match="INSTALL_FAILED_UPDATE_INCOMPATIBLE"):
            install_apk(str(apk), device_id="S", adb=adb)


class TestListApps:
    def test_sideloaded_filter_uses_third_party_flag(self, adb):
        adb.shell.return_value = PACKAGES_3
        apps = list_apps("S", filter="sideloaded", adb=adb)
        assert "com.mycompany.mygame" in apps
        assert "pm list packages -3" in adb.shell.call_args.args[0]

    def test_all_filter(self, adb):
        adb.shell.return_value = PACKAGES_3
        list_apps("S", filter="all", adb=adb)
        cmd = adb.shell.call_args.args[0]
        assert "-3" not in cmd


class TestLaunchStop:
    def test_launch_uses_monkey_launcher_intent(self, adb):
        adb.shell.return_value = "Events injected: 1"
        launch_app("com.mycompany.mygame", device_id="S", adb=adb)
        cmd = adb.shell.call_args.args[0]
        assert "monkey" in cmd and "com.mycompany.mygame" in cmd

    def test_launch_unknown_package_raises(self, adb):
        adb.shell.return_value = (
            "** No activities found to run, monkey aborted.\nEvents injected: 0"
        )
        with pytest.raises(ADBCommandError, match="not installed|No launchable"):
            launch_app("com.not.installed", device_id="S", adb=adb)

    def test_stop(self, adb):
        stop_app("com.mycompany.mygame", device_id="S", adb=adb)
        assert "am force-stop com.mycompany.mygame" in adb.shell.call_args.args[0]


class TestAppInfo:
    def test_parses_dumpsys_package(self, adb):
        def fake_shell(cmd, device_id=None, timeout=None):
            if "dumpsys package" in cmd:
                return DUMPSYS_PACKAGE
            if "du -sh" in cmd:
                return "1.2G\t/sdcard/Android/data/com.mycompany.mygame\n"
            return ""

        adb.shell.side_effect = fake_shell
        info = get_app_info("com.mycompany.mygame", device_id="S", adb=adb)
        assert info.version_name == "1.3.7"
        assert info.version_code == 42
        assert info.first_install == "2026-07-01 10:00:00"
        assert info.last_update == "2026-07-09 18:30:00"
        assert "android.permission.RECORD_AUDIO" in info.permissions

    def test_unknown_package_raises(self, adb):
        adb.shell.return_value = "Unable to find package: com.nope\n"
        with pytest.raises(ADBCommandError, match="not installed"):
            get_app_info("com.nope", device_id="S", adb=adb)


class TestUninstallClear:
    def test_uninstall_success(self, adb):
        adb.run.return_value = "Success\n"
        assert uninstall_app("com.mycompany.mygame", device_id="S", adb=adb) is True

    def test_uninstall_failure(self, adb):
        adb.run.return_value = "Failure [DELETE_FAILED_INTERNAL_ERROR]\n"
        with pytest.raises(ADBCommandError, match="DELETE_FAILED"):
            uninstall_app("com.mycompany.mygame", device_id="S", adb=adb)

    def test_clear_data(self, adb):
        adb.shell.return_value = "Success\n"
        assert clear_app_data("com.mycompany.mygame", device_id="S", adb=adb) is True
