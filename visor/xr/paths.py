"""Where the virtual headset's pieces live on disk.

Built artefacts (Monado runtime, OpenXR loader, the Visor layer) go under
``$XDG_DATA_HOME/visor/xr``; sources and build trees under
``$XDG_CACHE_HOME/visor/xr`` so they can be deleted without losing anything.
"""

from __future__ import annotations

import os
import shlex
import shutil
import sys
from pathlib import Path

from visor.utils.host import data_home

PACKAGE_DIR = Path(__file__).resolve().parent


def xr_home() -> Path:
    return data_home() / "visor" / "xr"


def cache_home() -> Path:
    xdg = os.environ.get("XDG_CACHE_HOME")
    return (Path(xdg) if xdg else Path.home() / ".cache") / "visor" / "xr"


def runs_dir() -> Path:
    return xr_home() / "runs"


def monado_manifest() -> Path:
    return xr_home() / "monado" / "share" / "openxr" / "1" / "openxr_monado.json"


def layer_dir() -> Path:
    return xr_home() / "layer"


def layer_manifest() -> Path:
    return layer_dir() / "XrApiLayer_visor_agent.json"


def openxr_lib_dir() -> Path:
    return xr_home() / "openxr" / "lib"


def hello_xr() -> Path:
    return xr_home() / "openxr" / "bin" / "hello_xr"


def socket_dir() -> Path:
    """Matches ControlServer::default_dir() in the layer."""
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return Path(xdg) / "visor-xr"
    return Path(f"/tmp/visor-xr-{os.getuid()}")


def native_dir() -> Path:
    """The layer sources and Monado patch: in the repo when developing, inside
    the package when installed from a wheel."""
    for candidate in (PACKAGE_DIR.parent.parent / "native", PACKAGE_DIR / "native"):
        if (candidate / "layer" / "CMakeLists.txt").exists():
            return candidate
    raise FileNotFoundError("Visor's native sources are missing from this install")


# The optional extras (pyproject.toml) and the packages each one brings.
EXTRAS = {"app": "pywebview", "agent": "anthropic", "voice": "piper-tts faster-whisper"}


def install_hint(extra: str) -> str:
    """How to add an optional extra to *this* Visor: pipx installs inject the
    packages (a pip install wouldn't reach pipx's private environment)."""
    if os.environ.get("APPIMAGE"):
        return "this AppImage was built without them; rebuild it with packaging/appimage/build-appimage.sh"
    if f"{os.sep}pipx{os.sep}venvs{os.sep}" in sys.prefix:
        return f"pipx inject visor-dev {EXTRAS[extra]}"
    return f"pip install 'visor-dev[{extra}]'"


def visor_command() -> str:
    """A shell command that runs this same Visor later (for generated scripts):
    the AppImage if we're inside one, else the venv's script, else PATH."""
    appimage = os.environ.get("APPIMAGE")
    if appimage:
        return shlex.quote(appimage)
    beside = Path(sys.executable).parent / "visor"
    if beside.exists():
        return shlex.quote(str(beside))
    return shutil.which("visor") or f"{shlex.quote(sys.executable)} -m visor"
