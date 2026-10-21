"""Locating and fetching ovr-platform-util, Meta's build-upload CLI.

Visor can't redistribute the binary, so it either finds one the user already
has or fetches it on explicit request. The fetch must never be implicit: a
`visor upload` that silently downloads a Meta binary mid-command is a surprise
we don't want to hand anyone.
"""

import stat
import sys
from pathlib import Path

import pytest

from visor.publish import ovrtool


class TestDownloadUrl:
    """Per-OS download IDs, read from Meta's own download_app endpoint."""

    def test_linux_id(self):
        assert "5159709737372459" in ovrtool.download_url(platform="linux")

    def test_macos_id(self):
        assert "1462426033810370" in ovrtool.download_url(platform="darwin")

    def test_windows_id(self):
        assert "1076686279105243" in ovrtool.download_url(platform="win32")

    def test_all_point_at_oculus(self):
        for plat in ("linux", "darwin", "win32"):
            assert ovrtool.download_url(platform=plat).startswith(
                "https://www.oculus.com/download_app/"
            )

    def test_ids_are_distinct(self):
        urls = {ovrtool.download_url(platform=p) for p in ("linux", "darwin", "win32")}
        assert len(urls) == 3

    def test_unsupported_platform_raises(self):
        with pytest.raises(ovrtool.OvrToolError):
            ovrtool.download_url(platform="sunos")


class TestBinaryName:
    def test_posix_has_no_extension(self):
        assert ovrtool.binary_name(platform="linux") == "ovr-platform-util"
        assert ovrtool.binary_name(platform="darwin") == "ovr-platform-util"

    def test_windows_gets_exe(self):
        assert ovrtool.binary_name(platform="win32") == "ovr-platform-util.exe"


class TestCacheDir:
    def test_honours_xdg_data_home(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        assert ovrtool.cache_dir() == tmp_path / "visor" / "bin"

    def test_falls_back_to_local_share(self, monkeypatch):
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        assert ovrtool.cache_dir() == Path.home() / ".local" / "share" / "visor" / "bin"


class TestFind:
    """PATH wins over the Visor-managed cache — a user who installed the tool
    themselves should not be shadowed by whatever we fetched months ago."""

    def test_prefers_path(self, tmp_path, monkeypatch):
        on_path = tmp_path / "ovr-platform-util"
        on_path.write_text("#!/bin/sh\n")
        monkeypatch.setattr(ovrtool.shutil, "which", lambda _: str(on_path))
        assert ovrtool.find(platform="linux") == on_path

    def test_falls_back_to_cache(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ovrtool.shutil, "which", lambda _: None)
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        cached = tmp_path / "visor" / "bin" / "ovr-platform-util"
        cached.parent.mkdir(parents=True)
        cached.write_text("#!/bin/sh\n")
        assert ovrtool.find(platform="linux") == cached

    def test_returns_none_when_absent(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ovrtool.shutil, "which", lambda _: None)
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        assert ovrtool.find(platform="linux") is None


class TestRequire:
    """The no-surprise-downloads guarantee."""

    def test_missing_tool_raises_rather_than_downloading(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ovrtool.shutil, "which", lambda _: None)
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))

        def explode(*a, **k):  # pragma: no cover - must never run
            raise AssertionError("require() must not download implicitly")

        monkeypatch.setattr(ovrtool, "fetch", explode)
        with pytest.raises(ovrtool.OvrToolNotFoundError):
            ovrtool.require(platform="linux")

    def test_error_tells_you_how_to_get_it(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ovrtool.shutil, "which", lambda _: None)
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        with pytest.raises(ovrtool.OvrToolNotFoundError) as excinfo:
            ovrtool.require(platform="linux")
        message = str(excinfo.value)
        assert "visor fetch-tool" in message
        assert "developers.meta.com" in message

    def test_returns_the_binary_when_present(self, tmp_path, monkeypatch):
        found = tmp_path / "ovr-platform-util"
        found.write_text("#!/bin/sh\n")
        monkeypatch.setattr(ovrtool.shutil, "which", lambda _: str(found))
        assert ovrtool.require(platform="linux") == found


class TestFetch:
    def test_writes_binary_and_marks_executable(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        monkeypatch.setattr(
            ovrtool, "_download", lambda url, dest: dest.write_bytes(b"ELF-ish")
        )
        path = ovrtool.fetch(platform="linux")
        assert path.read_bytes() == b"ELF-ish"
        if sys.platform != "win32":  # a Windows host has no execute bit to set
            assert path.stat().st_mode & stat.S_IXUSR

    def test_downloads_from_the_platform_url(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        seen = {}

        def record(url, dest):
            seen["url"] = url
            dest.write_bytes(b"x")

        monkeypatch.setattr(ovrtool, "_download", record)
        ovrtool.fetch(platform="darwin")
        assert "1462426033810370" in seen["url"]

    def test_creates_the_cache_dir(self, tmp_path, monkeypatch):
        nested = tmp_path / "deep" / "nested"
        monkeypatch.setenv("XDG_DATA_HOME", str(nested))
        monkeypatch.setattr(ovrtool, "_download", lambda url, dest: dest.write_bytes(b"x"))
        path = ovrtool.fetch(platform="linux")
        assert path.exists()

    def test_partial_download_is_not_left_behind(self, tmp_path, monkeypatch):
        """A failed fetch must not leave a truncated binary that `find` would
        happily hand to the uploader on the next run."""
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))

        def die(url, dest):
            dest.write_bytes(b"half")
            raise OSError("connection reset")

        monkeypatch.setattr(ovrtool, "_download", die)
        with pytest.raises(ovrtool.OvrToolError):
            ovrtool.fetch(platform="linux")
        monkeypatch.setattr(ovrtool.shutil, "which", lambda _: None)
        assert ovrtool.find(platform="linux") is None


class TestVersion:
    def test_parses_version_output(self, monkeypatch, tmp_path):
        binary = tmp_path / "ovr-platform-util"
        binary.write_text("#!/bin/sh\n")

        class Result:
            returncode = 0
            stdout = "ovr-platform-util 1.15.0\n"
            stderr = ""

        monkeypatch.setattr(ovrtool.subprocess, "run", lambda *a, **k: Result())
        assert ovrtool.version(binary) == "1.15.0"

    def test_unparseable_output_is_reported_not_crashed(self, monkeypatch, tmp_path):
        binary = tmp_path / "ovr-platform-util"
        binary.write_text("#!/bin/sh\n")

        class Result:
            returncode = 0
            stdout = "\n"
            stderr = ""

        monkeypatch.setattr(ovrtool.subprocess, "run", lambda *a, **k: Result())
        assert ovrtool.version(binary) is None
