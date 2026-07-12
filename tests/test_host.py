"""Host-OS helpers: install hints and process-detach flags per platform."""

from visor.utils.host import detached_popen_kwargs, install_hint


class TestInstallHint:
    def test_linux_uses_apt(self):
        assert "apt install" in install_hint("adb", platform="linux")

    def test_macos_uses_brew(self):
        assert "brew" in install_hint("ffmpeg", platform="darwin")

    def test_windows_uses_winget(self):
        assert "winget" in install_hint("scrcpy", platform="win32")

    def test_windows_variants_all_map(self):
        # any platform string starting with "win" is treated as Windows
        assert "winget" in install_hint("adb", platform="windows")

    def test_names_the_tool_and_path(self):
        hint = install_hint("scrcpy", platform="linux")
        assert "scrcpy" in hint and "PATH" in hint

    def test_unknown_tool_falls_back(self):
        assert "PATH" in install_hint("mystery-tool", platform="linux")


class TestDetachedPopenKwargs:
    def test_posix_uses_new_session(self):
        assert detached_popen_kwargs(platform="linux") == {"start_new_session": True}
        assert detached_popen_kwargs(platform="darwin") == {"start_new_session": True}

    def test_windows_uses_process_group(self):
        kwargs = detached_popen_kwargs(platform="win32")
        assert "creationflags" in kwargs
        assert "start_new_session" not in kwargs
