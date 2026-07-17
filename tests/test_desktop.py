"""Desktop launcher install/uninstall."""

from visor.desktop import install_desktop_entry, uninstall_desktop_entry


class TestInstallDesktopEntry:
    def test_writes_desktop_and_icon(self, tmp_path):
        paths = install_desktop_entry(data_home=tmp_path)

        desktop = paths["desktop"]
        icon = paths["icon"]
        assert desktop.exists() and icon.exists()
        assert desktop == tmp_path / "applications" / "visor.desktop"
        assert icon == tmp_path / "icons" / "hicolor" / "scalable" / "apps" / "visor.svg"

    def test_desktop_entry_contents(self, tmp_path):
        desktop = install_desktop_entry(data_home=tmp_path)["desktop"]
        text = desktop.read_text(encoding="utf-8")
        assert "[Desktop Entry]" in text
        assert "Name=Visor" in text
        assert "Icon=visor" in text
        assert "Exec=" in text and " app" in text  # launches `visor app`

    def test_icon_is_real_svg(self, tmp_path):
        icon = install_desktop_entry(data_home=tmp_path)["icon"]
        assert icon.read_text(encoding="utf-8").lstrip().startswith("<svg")

    def test_uninstall_removes_both(self, tmp_path):
        install_desktop_entry(data_home=tmp_path)
        removed = uninstall_desktop_entry(data_home=tmp_path)

        assert len(removed) == 2
        assert not (tmp_path / "applications" / "visor.desktop").exists()

    def test_uninstall_when_absent_is_noop(self, tmp_path):
        assert uninstall_desktop_entry(data_home=tmp_path) == []
