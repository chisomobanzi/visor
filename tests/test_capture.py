"""Tests for screenshot, screen recording, casting."""

from pathlib import Path
from unittest import mock

import pytest

from visor.adb.capture import screen_record, screenshot, start_cast

# Minimal valid 1x1 PNG
PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d4944415478da63fcff9fa11e0005fd02fe0a3b76360000000049454e44ae426082"
)


@pytest.fixture
def adb():
    return mock.Mock()


class TestScreenshot:
    def test_saves_png_and_returns_path(self, adb, tmp_path):
        adb.run.return_value = PNG_BYTES
        out = tmp_path / "shot.png"
        result = screenshot("S", str(out), adb=adb)
        assert result.path == str(out)
        assert out.read_bytes() == PNG_BYTES
        assert result.width == 1 and result.height == 1
        # exec-out avoids CRLF corruption of binary data
        args = adb.run.call_args.args[0]
        assert args[0] == "exec-out"
        assert adb.run.call_args.kwargs.get("binary") is True

    def test_empty_capture_raises(self, adb, tmp_path):
        adb.run.return_value = b""
        from visor.adb.wrapper import ADBCommandError

        with pytest.raises(ADBCommandError, match="[Ss]creenshot"):
            screenshot("S", str(tmp_path / "s.png"), wake=False, adb=adb)

    def test_sleeping_display_woken_via_prox_close(self, adb, tmp_path, monkeypatch):
        # First capture empty (display off), then wake broadcast, then PNG
        monkeypatch.setattr("time.sleep", lambda s: None)
        adb.run.side_effect = [b"", PNG_BYTES]
        adb.shell.return_value = "Broadcast completed: result=0"
        result = screenshot("S", str(tmp_path / "s.png"), adb=adb)
        assert result.size_bytes == len(PNG_BYTES)
        broadcasts = [c.args[0] for c in adb.shell.call_args_list]
        assert any("prox_close" in b for b in broadcasts)
        assert any("automation_disable" in b for b in broadcasts)

    def test_default_path_generated(self, adb, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        adb.run.return_value = PNG_BYTES
        result = screenshot("S", adb=adb)
        assert result.path.endswith(".png")
        assert Path(result.path).parent.name == "screenshots"


class TestScreenRecord:
    def test_duration_clamped_to_android_limit(self, adb, tmp_path):
        adb.shell.return_value = ""
        adb.run.return_value = "1 file pulled\n"
        out = tmp_path / "clip.mp4"
        with pytest.raises(ValueError, match="180"):
            screen_record("S", duration_seconds=300, save_path=str(out), adb=adb)

    def test_records_pulls_and_cleans_up(self, adb, tmp_path):
        adb.shell.return_value = ""
        adb.run.return_value = "1 file pulled\n"
        out = tmp_path / "clip.mp4"
        result = screen_record("S", duration_seconds=5, save_path=str(out), adb=adb)
        assert result.path == str(out)
        record_cmd = adb.shell.call_args_list[0].args[0]
        assert "screenrecord" in record_cmd and "--time-limit 5" in record_cmd
        cleanup_cmd = adb.shell.call_args_list[-1].args[0]
        assert "rm" in cleanup_cmd


class TestScreenStream:
    def test_missing_ffmpeg_raises_helpful_error(self, adb):
        from visor.adb.capture import start_screen_stream
        from visor.adb.wrapper import ADBCommandError

        with mock.patch("shutil.which", return_value=None):
            with pytest.raises(ADBCommandError, match="ffmpeg"):
                start_screen_stream("S", adb=adb)

    def test_pipeline_command_construction(self, adb):
        from visor.adb.capture import start_screen_stream

        adb.adb_path = "/usr/bin/adb"
        with mock.patch("shutil.which", return_value="/usr/bin/ffmpeg"):
            with mock.patch("subprocess.Popen") as popen:
                popen.return_value.stdout = mock.Mock()
                start_screen_stream("S", fps=20, adb=adb)
        adb_cmd = popen.call_args_list[0].args[0]
        ff_cmd = popen.call_args_list[1].args[0]
        assert adb_cmd[:3] == ["/usr/bin/adb", "-s", "S"]
        assert "screenrecord" in adb_cmd and "--output-format=h264" in adb_cmd
        assert "mpjpeg" in ff_cmd
        assert any("fps=20" in part for part in ff_cmd)
        # left-eye crop on by default (Quest renders side-by-side stereo)
        assert any("crop=iw/2" in part for part in ff_cmd)
        # ffmpeg reads from the adb process's stdout
        assert popen.call_args_list[1].kwargs["stdin"] is popen.return_value.stdout

    def test_undistort_applied_by_default(self, adb):
        from visor.adb.capture import start_screen_stream

        adb.adb_path = "/usr/bin/adb"
        with mock.patch("shutil.which", return_value="/usr/bin/ffmpeg"):
            with mock.patch("subprocess.Popen") as popen:
                popen.return_value.stdout = mock.Mock()
                start_screen_stream("S", adb=adb)
        ff_cmd = popen.call_args_list[1].args[0]
        assert any("lenscorrection" in part for part in ff_cmd)

    def test_undistort_can_be_disabled(self, adb):
        from visor.adb.capture import start_screen_stream

        adb.adb_path = "/usr/bin/adb"
        with mock.patch("shutil.which", return_value="/usr/bin/ffmpeg"):
            with mock.patch("subprocess.Popen") as popen:
                popen.return_value.stdout = mock.Mock()
                start_screen_stream("S", undistort=False, adb=adb)
        ff_cmd = popen.call_args_list[1].args[0]
        assert not any("lenscorrection" in part for part in ff_cmd)


class TestCast:
    def test_missing_scrcpy_raises_helpful_error(self, adb):
        from visor.adb.wrapper import ADBCommandError

        with mock.patch("shutil.which", return_value=None):
            with pytest.raises(ADBCommandError, match="scrcpy"):
                start_cast("S", adb=adb)

    def test_launches_scrcpy_detached(self, adb):
        with mock.patch("shutil.which", return_value="/usr/bin/scrcpy"):
            with mock.patch("subprocess.Popen") as popen:
                popen.return_value.poll.return_value = None
                popen.return_value.pid = 4242
                result = start_cast("S", adb=adb)
        assert result.pid == 4242
        cmd = popen.call_args.args[0]
        assert cmd[0] == "/usr/bin/scrcpy"
        assert "-s" in cmd and "S" in cmd


class TestUndistortEye:
    def test_returns_original_without_ffmpeg(self):
        from PIL import Image

        from visor.adb.capture import undistort_eye

        img = Image.new("RGB", (64, 64))
        with mock.patch("shutil.which", return_value=None):
            assert undistort_eye(img) is img

    def test_falls_back_when_ffmpeg_fails(self):
        from PIL import Image

        from visor.adb.capture import undistort_eye

        img = Image.new("RGB", (64, 64))
        with mock.patch("shutil.which", return_value="/usr/bin/ffmpeg"):
            with mock.patch("subprocess.run") as run:
                run.return_value = mock.Mock(returncode=1, stdout=b"")
                assert undistort_eye(img) is img
