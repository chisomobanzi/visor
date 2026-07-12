"""Tests for the core ADB command wrapper.

TDD: these tests define the wrapper contract — ADB discovery, command
execution, and translation of raw ADB failures into clear Visor errors.
"""

import subprocess
from unittest import mock

import pytest

from visor.adb.wrapper import (
    ADBCommandError,
    ADBNotFoundError,
    ADBWrapper,
    DeviceNotFoundError,
    DeviceOfflineError,
    DeviceUnauthorizedError,
    MultipleDevicesError,
)


def make_completed(stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess(
        args=["adb"], returncode=returncode, stdout=stdout, stderr=stderr
    )


@pytest.fixture
def adb():
    with mock.patch("shutil.which", return_value="/usr/bin/adb"):
        yield ADBWrapper()


class TestADBDiscovery:
    def test_missing_adb_binary_raises_clear_error(self):
        with mock.patch("shutil.which", return_value=None):
            with pytest.raises(ADBNotFoundError, match="platform-tools"):
                ADBWrapper()

    def test_explicit_adb_path_is_used(self, tmp_path):
        fake_adb = tmp_path / "adb"
        fake_adb.write_text("#!/bin/sh\n")
        fake_adb.chmod(0o755)
        wrapper = ADBWrapper(adb_path=str(fake_adb))
        assert wrapper.adb_path == str(fake_adb)


class TestRun:
    def test_run_returns_stdout(self, adb):
        with mock.patch("subprocess.run", return_value=make_completed(stdout="hello\n")) as m:
            out = adb.run(["shell", "echo", "hello"])
        assert out == "hello\n"
        assert m.call_args.args[0][0] == "/usr/bin/adb"

    def test_device_id_inserted_before_subcommand(self, adb):
        with mock.patch("subprocess.run", return_value=make_completed()) as m:
            adb.run(["shell", "ls"], device_id="SERIAL123")
        cmd = m.call_args.args[0]
        assert cmd[:3] == ["/usr/bin/adb", "-s", "SERIAL123"]

    def test_unauthorized_device_raises_actionable_error(self, adb):
        err = "adb: device unauthorized.\nThis adb server's $ADB_VENDOR_KEYS is not set"
        with mock.patch(
            "subprocess.run", return_value=make_completed(stderr=err, returncode=1)
        ):
            with pytest.raises(DeviceUnauthorizedError, match="headset"):
                adb.run(["shell", "ls"])

    def test_device_not_found_raises(self, adb):
        err = "adb: device 'NOPE' not found"
        with mock.patch(
            "subprocess.run", return_value=make_completed(stderr=err, returncode=1)
        ):
            with pytest.raises(DeviceNotFoundError):
                adb.run(["shell", "ls"], device_id="NOPE")

    def test_no_devices_raises_device_not_found(self, adb):
        err = "adb: no devices/emulators found"
        with mock.patch(
            "subprocess.run", return_value=make_completed(stderr=err, returncode=1)
        ):
            with pytest.raises(DeviceNotFoundError, match="[Nn]o devices"):
                adb.run(["shell", "ls"])

    def test_offline_device_raises(self, adb):
        err = "adb: device offline"
        with mock.patch(
            "subprocess.run", return_value=make_completed(stderr=err, returncode=1)
        ):
            with pytest.raises(DeviceOfflineError):
                adb.run(["shell", "ls"])

    def test_multiple_devices_raises(self, adb):
        err = "adb: more than one device/emulator"
        with mock.patch(
            "subprocess.run", return_value=make_completed(stderr=err, returncode=1)
        ):
            with pytest.raises(MultipleDevicesError, match="device_id"):
                adb.run(["shell", "ls"])

    def test_generic_failure_raises_command_error_with_stderr(self, adb):
        with mock.patch(
            "subprocess.run",
            return_value=make_completed(stderr="something exploded", returncode=1),
        ):
            with pytest.raises(ADBCommandError, match="something exploded"):
                adb.run(["shell", "false"])

    def test_timeout_raises_command_error(self, adb):
        with mock.patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="adb", timeout=5),
        ):
            with pytest.raises(ADBCommandError, match="timed out"):
                adb.run(["shell", "sleep", "100"], timeout=5)


class TestShell:
    def test_shell_wraps_command(self, adb):
        with mock.patch("subprocess.run", return_value=make_completed(stdout="ok")) as m:
            adb.shell("getprop ro.product.model", device_id="S")
        cmd = m.call_args.args[0]
        assert cmd[-2:] == ["shell", "getprop ro.product.model"]

    def test_run_binary_returns_bytes(self, adb):
        raw = b"\x89PNG\r\n"
        completed = subprocess.CompletedProcess(
            args=["adb"], returncode=0, stdout=raw, stderr=b""
        )
        with mock.patch("subprocess.run", return_value=completed):
            out = adb.run(["exec-out", "screencap", "-p"], binary=True)
        assert out == raw
