"""Build the virtual headset: OpenXR loader + demo app, patched Monado, Visor layer.

Everything is built from source into the user's data directory — no root, no
system packages beyond a compiler, CMake, git and Vulkan. Each step leaves a
stamp so re-running only redoes what changed.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from visor.xr import paths

OPENXR_TAG = "release-1.1.65"
MONADO_TAG = "v25.1.0"
EIGEN_URL = "https://gitlab.com/libeigen/eigen/-/archive/3.4.0/eigen-3.4.0.tar.gz"
GLSLANG_URL = ("https://github.com/KhronosGroup/glslang/releases/download/16.6.0/"
               "glslang-16.6.0-linux-x86_64-release.tar.gz")

Log = Callable[[str], None]


class SetupError(RuntimeError):
    pass


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    fix: str = ""


# --------------------------------------------------------------------------
# helpers


def _run(cmd: list[str], log_file: Path, cwd: Optional[Path] = None, env: Optional[dict] = None) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with open(log_file, "ab") as f:
        f.write(("\n$ " + " ".join(cmd) + "\n").encode())
        f.flush()
        proc = subprocess.run(cmd, cwd=cwd, env=env, stdout=f, stderr=subprocess.STDOUT)
    if proc.returncode != 0:
        tail = log_file.read_text(errors="replace").splitlines()[-25:]
        raise SetupError(f"`{' '.join(cmd[:3])} …` failed (log: {log_file})\n" + "\n".join(tail))


def _stamp_path(step: str) -> Path:
    return paths.cache_home() / "stamps" / f"{step}.json"


def _stamp_ok(step: str, key: str) -> bool:
    p = _stamp_path(step)
    try:
        return json.loads(p.read_text()).get("key") == key
    except (OSError, ValueError):
        return False


def _stamp(step: str, key: str) -> None:
    p = _stamp_path(step)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"key": key}))


def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=120) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)


def _extract(tar: tarfile.TarFile, dest: Path, members=None) -> None:
    try:
        tar.extractall(dest, members=members, filter="data")
    except TypeError:  # Python before the tarfile filter backport
        tar.extractall(dest, members=members)


def _clone(url: str, tag: str, dest: Path, log_file: Path) -> None:
    if (dest / ".git").exists():
        return
    _run(["git", "clone", "-q", "--depth", "1", "--branch", tag, url, str(dest)], log_file)


def _tree_hash(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file() and "build" not in p.relative_to(root).parts:
            h.update(str(p.relative_to(root)).encode())
            h.update(p.read_bytes())
    return h.hexdigest()[:16]


def _swap_in(staged: Path, dest: Path) -> None:
    """Move a staged install into place file by file with rename(2): apps
    already running keep the old files they have mapped; new ones get these."""
    for src in sorted(staged.rglob("*")):
        if src.is_dir():
            continue
        target = dest / src.relative_to(staged)
        target.parent.mkdir(parents=True, exist_ok=True)
        if src.is_symlink():
            tmp = target.with_name(target.name + ".visor-new")
            tmp.unlink(missing_ok=True)
            tmp.symlink_to(os.readlink(src))
            os.replace(tmp, target)
        else:
            os.replace(src, target)
    shutil.rmtree(staged, ignore_errors=True)


def _install(build: Path, dest: Path, logf: Path, env: Optional[dict] = None,
             components: tuple[str, ...] = ()) -> None:
    staged = dest.parent / (dest.name + ".staging")
    shutil.rmtree(staged, ignore_errors=True)
    if components:
        for component in components:
            _run(["cmake", "--install", str(build), "--prefix", str(staged), "--component", component], logf, env=env)
    else:
        _run(["cmake", "--install", str(build), "--prefix", str(staged)], logf, env=env)
    _swap_in(staged, dest)


def _jobs() -> str:
    return str(max(2, (os.cpu_count() or 4) - 2))


# --------------------------------------------------------------------------
# checks


def check_prerequisites() -> list[Check]:
    checks = []
    for tool in ("git", "cmake", "c++", "cc", "python3"):
        found = shutil.which(tool)
        checks.append(Check(tool, bool(found), found or "not found",
                            "sudo apt install build-essential cmake git" if not found else ""))
    vk_header = any(Path(p, "vulkan/vulkan.h").exists() for p in ("/usr/include", "/usr/local/include"))
    checks.append(Check("Vulkan headers", vk_header, "vulkan/vulkan.h" if vk_header else "missing",
                        "sudo apt install libvulkan-dev"))
    vk_lib = any(Path(p).exists() for p in ("/usr/lib/x86_64-linux-gnu/libvulkan.so.1", "/usr/lib/libvulkan.so.1",
                                             "/usr/lib64/libvulkan.so.1"))
    checks.append(Check("Vulkan loader", vk_lib, "libvulkan.so.1" if vk_lib else "missing",
                        "sudo apt install libvulkan1 mesa-vulkan-drivers"))
    return checks


def installed() -> dict[str, bool]:
    return {
        "openxr": (paths.openxr_lib_dir() / "libopenxr_loader.so").exists(),
        "demo": paths.hello_xr().exists(),
        "monado": paths.monado_manifest().exists(),
        "layer": paths.layer_manifest().exists(),
    }


# --------------------------------------------------------------------------
# steps


def _deps(log: Log) -> dict:
    """glslangValidator and Eigen, from the system or downloaded."""
    deps = paths.cache_home() / "deps"
    env = dict(os.environ)
    glslang = shutil.which("glslangValidator")
    if not glslang:
        bin_dir = deps / "glslang" / "bin"
        if not (bin_dir / "glslangValidator").exists():
            log("downloading glslang (shader compiler)")
            tgz = deps / "glslang.tar.gz"
            _download(GLSLANG_URL, tgz)
            (deps / "glslang").mkdir(parents=True, exist_ok=True)
            with tarfile.open(tgz) as t:
                _extract(t, deps / "glslang")
            tgz.unlink()
        env["PATH"] = f"{bin_dir}:{env['PATH']}"
    prefix = deps / "prefix"
    has_eigen = subprocess.run(["pkg-config", "--exists", "eigen3"], capture_output=True).returncode == 0 \
        if shutil.which("pkg-config") else False
    if not has_eigen and not (prefix / "share" / "eigen3").exists():
        log("downloading Eigen (maths headers)")
        tgz = deps / "eigen.tar.gz"
        _download(EIGEN_URL, tgz)
        src = deps / "eigen"
        src.mkdir(parents=True, exist_ok=True)
        with tarfile.open(tgz) as t:
            members = t.getmembers()
            for m in members:
                m.name = m.name.split("/", 1)[1] if "/" in m.name else m.name
            _extract(t, src, members=[m for m in members if m.name])
        tgz.unlink()
        logf = paths.cache_home() / "logs" / "eigen.log"
        _run(["cmake", "-S", str(src), "-B", str(deps / "eigen-build"), f"-DCMAKE_INSTALL_PREFIX={prefix}",
              "-DBUILD_TESTING=OFF", "-DEIGEN_BUILD_DOC=OFF"], logf)
        _run(["cmake", "--install", str(deps / "eigen-build")], logf)
    return {"env": env, "prefix": prefix}


def build_openxr(log: Log, force: bool = False, env: Optional[dict] = None) -> None:
    key = OPENXR_TAG
    if not force and _stamp_ok("openxr", key) and installed()["demo"]:
        log("OpenXR loader and demo: up to date")
        return
    cache = paths.cache_home()
    src, build = cache / "openxr-sdk-source", cache / "build-openxr"
    logf = cache / "logs" / "openxr.log"
    log(f"fetching OpenXR SDK {OPENXR_TAG}")
    _clone("https://github.com/KhronosGroup/OpenXR-SDK-Source.git", OPENXR_TAG, src, logf)
    log("building the OpenXR loader and the demo app (hello_xr)")
    xxf86vm = next((str(p) for p in (Path("/usr/lib/x86_64-linux-gnu/libXxf86vm.so.1"),
                                     Path("/usr/lib/libXxf86vm.so.1")) if p.exists()), None)
    cfg = ["cmake", "-S", str(src), "-B", str(build), "-DCMAKE_BUILD_TYPE=Release",
           f"-DCMAKE_INSTALL_PREFIX={paths.xr_home() / 'openxr'}", "-DBUILD_TESTS=ON", "-DBUILD_SDK_TESTS=ON",
           "-DBUILD_CONFORMANCE_TESTS=OFF", "-DBUILD_API_LAYERS=OFF", "-DDYNAMIC_LOADER=ON",
           # hello_xr only needs Vulkan; OpenGL/EGL pull in headers distros split out.
           "-DCMAKE_DISABLE_FIND_PACKAGE_OpenGL=TRUE", "-DCMAKE_DISABLE_FIND_PACKAGE_EGL=TRUE",
           "-DCMAKE_DISABLE_FIND_PACKAGE_OpenGLES=TRUE"]
    if xxf86vm:
        cfg.append(f"-DX11_Xxf86vm_LIB={xxf86vm}")
    _run(cfg, logf, env=env)
    _run(["cmake", "--build", str(build), "-j", _jobs(), "--target", "openxr_loader", "hello_xr"], logf, env=env)
    # Only what we built: the loader (for the demo) and the headers (for the layer).
    _install(build, paths.xr_home() / "openxr", logf, env=env, components=("Loader", "Headers"))
    hello = build / "src" / "tests" / "hello_xr" / "hello_xr"
    paths.hello_xr().parent.mkdir(parents=True, exist_ok=True)
    tmp = paths.hello_xr().with_name("hello_xr.visor-new")
    shutil.copy2(hello, tmp)
    os.replace(tmp, paths.hello_xr())
    _stamp("openxr", key)


def build_monado(log: Log, force: bool = False, env: Optional[dict] = None, prefix: Optional[Path] = None) -> None:
    patches = sorted((paths.native_dir() / "monado").glob("*.patch"))
    key = MONADO_TAG + ":" + hashlib.sha256(b"".join(p.read_bytes() for p in patches)).hexdigest()[:16]
    if not force and _stamp_ok("monado", key) and installed()["monado"]:
        log("Monado (virtual headset runtime): up to date")
        return
    cache = paths.cache_home()
    src, build = cache / "monado", cache / "build-monado"
    logf = cache / "logs" / "monado.log"
    log(f"fetching Monado {MONADO_TAG}")
    _clone("https://gitlab.freedesktop.org/monado/monado.git", MONADO_TAG, src, logf)
    # Re-apply Visor's patch onto a pristine tree (this clone is ours alone).
    _run(["git", "reset", "-q", "--hard"], logf, cwd=src)
    _run(["git", "clean", "-qfdx", "src"], logf, cwd=src)
    for patch in patches:
        log(f"applying {patch.name}")
        _run(["git", "apply", "--whitespace=nowarn", str(patch)], logf, cwd=src)
    log("building Monado (in-process, headless)")
    cfg = ["cmake", "-S", str(src), "-B", str(build), "-DCMAKE_BUILD_TYPE=RelWithDebInfo",
           f"-DCMAKE_INSTALL_PREFIX={paths.xr_home() / 'monado'}", "-DXRT_FEATURE_SERVICE=OFF",
           "-DBUILD_TESTING=OFF", "-DXRT_BUILD_DRIVER_SIMULATED=ON", "-DXRT_MODULE_COMPOSITOR_NULL=ON",
           "-DXRT_FEATURE_STEAMVR_PLUGIN=OFF", "-DXRT_HAVE_OPENCV=OFF"]
    if prefix:
        cfg.append(f"-DCMAKE_PREFIX_PATH={prefix}")
    _run(cfg, logf, env=env)
    _run(["cmake", "--build", str(build), "-j", _jobs()], logf, env=env)
    _install(build, paths.xr_home() / "monado", logf, env=env)
    _stamp("monado", key)


def build_layer(log: Log, force: bool = False) -> None:
    native = paths.native_dir() / "layer"
    key = _tree_hash(native)
    if not force and _stamp_ok("layer", key) and installed()["layer"]:
        log("Visor layer: up to date")
        return
    build = paths.cache_home() / "build-layer"
    logf = paths.cache_home() / "logs" / "layer.log"
    log("building the Visor OpenXR layer")
    _run(["cmake", "-S", str(native), "-B", str(build), "-DCMAKE_BUILD_TYPE=RelWithDebInfo",
          f"-DOPENXR_INCLUDE_DIR={paths.xr_home() / 'openxr' / 'include'}"], logf)
    _run(["cmake", "--build", str(build), "-j", _jobs()], logf)
    _install(build, paths.layer_dir(), logf)
    _stamp("layer", key)


def setup(log: Log = print, force: bool = False, only: Optional[str] = None) -> None:
    bad = [c for c in check_prerequisites() if not c.ok]
    if bad:
        raise SetupError("missing prerequisites:\n" + "\n".join(f"  {c.name}: {c.fix}" for c in bad))
    if only == "layer":
        build_layer(log, force)
        return
    deps = _deps(log)
    build_openxr(log, force, env=deps["env"])
    build_monado(log, force, env=deps["env"], prefix=deps["prefix"])
    build_layer(log, force)
    log(f"virtual headset ready in {paths.xr_home()}")
