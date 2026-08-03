"""Install a native desktop launcher (.desktop entry + icon) for `visor app`.

Freedesktop/Linux only. The functions write to ``$XDG_DATA_HOME`` (or
``~/.local/share``) so a plain user install needs no root. macOS/Windows don't
use .desktop files, so the CLI guards against them.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

# Aliased: the functions below take a `data_home` argument that would
# otherwise shadow the shared helper.
from visor.utils.host import data_home as _data_home

APP_ID = "visor"

_DESKTOP_ENTRY = """\
[Desktop Entry]
Type=Application
Name=Visor
GenericName=Quest Developer Toolkit
Comment=Meta Quest developer dashboard
Exec={exec_cmd}
Icon={icon}
Terminal=false
Categories=Development;
Keywords=quest;vr;adb;meta;headset;oculus;
StartupNotify=true
StartupWMClass=Visor
"""


def _visor_exec() -> str:
    """The command the launcher runs. Prefer the installed `visor` script;
    fall back to the current interpreter running the CLI module."""
    exe = shutil.which("visor")
    if exe:
        return f"{exe} app"
    return f"{sys.executable} -m visor.cli.main app"


def _icon_source() -> Path:
    import visor

    return Path(visor.__file__).parent / "ui" / "static" / "icon.svg"


def _refresh(apps_dir: Path, icon_theme_dir: Path) -> None:
    """Best-effort refresh of the desktop/icon caches; ignore if tools absent."""
    for cmd in (
        ["update-desktop-database", str(apps_dir)],
        ["gtk-update-icon-cache", "-f", "-t", str(icon_theme_dir)],
    ):
        exe = shutil.which(cmd[0])
        if not exe:
            continue
        try:
            subprocess.run([exe, *cmd[1:]], check=False, capture_output=True, timeout=15)
        except Exception:
            pass


def install_desktop_entry(data_home: Path | None = None) -> dict[str, Path]:
    """Write the .desktop entry and icon. Returns the paths written."""
    data = data_home or _data_home()
    apps = data / "applications"
    icons = data / "icons" / "hicolor" / "scalable" / "apps"
    apps.mkdir(parents=True, exist_ok=True)
    icons.mkdir(parents=True, exist_ok=True)

    icon_dst = icons / f"{APP_ID}.svg"
    icon_dst.write_bytes(_icon_source().read_bytes())

    desktop = apps / f"{APP_ID}.desktop"
    desktop.write_text(
        _DESKTOP_ENTRY.format(exec_cmd=_visor_exec(), icon=APP_ID),
        encoding="utf-8",
    )

    _refresh(apps, data / "icons" / "hicolor")
    return {"desktop": desktop, "icon": icon_dst}


def uninstall_desktop_entry(data_home: Path | None = None) -> list[Path]:
    """Remove the .desktop entry and icon. Returns the paths removed."""
    data = data_home or _data_home()
    removed: list[Path] = []
    for path in (
        data / "applications" / f"{APP_ID}.desktop",
        data / "icons" / "hicolor" / "scalable" / "apps" / f"{APP_ID}.svg",
    ):
        if path.exists():
            path.unlink()
            removed.append(path)
    _refresh(data / "applications", data / "icons" / "hicolor")
    return removed
