"""Run OpenXR apps inside Visor's virtual headset.

The virtual headset is Monado (an open-source OpenXR runtime) with its null
compositor and simulated devices, patched to look like a Quest 3 and to hand
its frames to Visor; the Visor layer sits in front of it and holds the head,
the controllers and the pause button. Nothing about the app changes — it is
launched with a different runtime and one extra API layer.

Every instance is isolated: its own runtime (Monado runs in-process), socket,
frame buffer, microphone and run directory. So any number can run side by
side, limited only by GPU and memory.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from visor.xr import mic as xr_mic
from visor.xr import paths
from visor.xr.client import XrClient, XrError
from visor.xr.headsets import Headset, get_headset


class SimError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# What to run


@dataclass
class AppSpec:
    """How to start one app: the command, plus how it behaves."""

    name: str
    command: list[str]
    cwd: Optional[str] = None
    env: dict = field(default_factory=dict)
    kind: str = "binary"  # binary | hello_xr | unreal-project | unreal-build
    keep_stdin_open: bool = False
    project_dir: Optional[str] = None  # for Unreal: where Saved/ and logs live

    @classmethod
    def resolve(cls, target: str, map_name: Optional[str] = None, extra_args: Optional[list[str]] = None,
                deterministic: bool = False, refresh: int = 72) -> "AppSpec":
        extra = list(extra_args or [])
        if target in ("hello_xr", "demo", "hello"):
            exe = paths.hello_xr()
            if not exe.exists():
                raise SimError("the demo app isn't built yet — run `visor sim setup`")
            return cls(
                name="hello_xr",
                command=[str(exe), "-g", "Vulkan2", *extra],
                kind="hello_xr",
                keep_stdin_open=True,  # it quits on the first key press
                env={"LD_LIBRARY_PATH": str(paths.openxr_lib_dir())},
            )
        p = Path(target).expanduser()
        if p.is_dir():
            projects = sorted(p.glob("*.uproject"))
            if projects:
                p = projects[0]
            else:
                scripts = [s for s in sorted(p.glob("*.sh")) if (p / s.stem).is_dir()]
                if scripts:
                    p = scripts[0]
        if p.suffix == ".uproject" and p.exists():
            return cls._unreal_project(p.resolve(), map_name, extra, deterministic, refresh)
        if p.suffix == ".apk":
            raise SimError(
                "Android APKs need an Android OpenXR runtime; on desktop, run the same "
                "project built for Linux (or its .uproject). APK playtests are what "
                "Visor Cloud is for.")
        if p.suffix == ".sh" and p.exists():
            return cls(
                name=p.stem,
                command=["bash", str(p.resolve()), "-vr", "-RenderOffscreen", "-nosound", "-unattended", *extra,
                         *_unreal_fixed_step(deterministic, refresh)],
                kind="unreal-build",
                cwd=str(p.parent.resolve()),
            )
        exe = shutil.which(target) or (str(p.resolve()) if p.exists() else None)
        if exe:
            return cls(name=Path(exe).stem, command=[exe, *extra], kind="binary")
        raise SimError(f"don't know how to run '{target}' (try a .uproject, a packaged build, "
                       "an executable, or 'demo')")

    @classmethod
    def _unreal_project(cls, uproject: Path, map_name, extra, deterministic, refresh) -> "AppSpec":
        editor = find_unreal_editor(uproject)
        args = [str(editor), str(uproject)]
        if map_name:
            args.append(map_name)
        args += ["-game", "-vr", "-nosplash", "-nosound", "-RenderOffscreen", "-unattended", "-log"]
        if (uproject.parent / "Plugins" / "UnrealMCP").is_dir():
            args.append("-NoMCP")  # the project's own bridge must stay with the editor
        args += _unreal_fixed_step(deterministic, refresh)
        args += extra
        return cls(name=uproject.stem, command=args, kind="unreal-project", project_dir=str(uproject.parent))


def _unreal_fixed_step(deterministic: bool, refresh: int) -> list[str]:
    # A fixed timestep makes game time advance exactly one frame per frame, so
    # a recorded input timeline replays to the same result.
    return ["-UseFixedTimeStep", f"-FPS={refresh}", "-FixedSeed"] if deterministic else []


def find_unreal_editor(uproject: Path) -> Path:
    """Locate UnrealEditor for a project: $UE_ROOT, then the usual install spots."""
    assoc = ""
    try:
        assoc = json.loads(uproject.read_text()).get("EngineAssociation", "")
    except (OSError, ValueError):
        pass
    roots: list[Path] = []
    for var in ("UE_ROOT", "UNREAL_ENGINE_ROOT", "UE5_ROOT"):
        if os.environ.get(var):
            roots.append(Path(os.environ[var]))
    home = Path.home()
    for pattern in ("Unreal/*", "UnrealEngine*", "Epic/UE_*", "UE_*"):
        roots += sorted(home.glob(pattern), reverse=True)
    roots += sorted(Path("/opt").glob("UnrealEngine*"), reverse=True) + sorted(Path("/opt").glob("unreal*"), reverse=True)
    ranked = sorted(roots, key=lambda r: (assoc and assoc in r.name) is False)
    for root in ranked:
        for candidate in (root / "Engine/Binaries/Linux/UnrealEditor", root / "Engine/Binaries/Linux/UE4Editor"):
            if candidate.exists():
                return candidate
    raise SimError(f"can't find an Unreal Engine {assoc or ''} install — set UE_ROOT to its folder")


# --------------------------------------------------------------------------
# One running app


def _slug(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()[:40] or "app"


def _first_to_go(pid: int) -> None:
    """If memory runs out, the kernel should end a simulated app before
    anything the user is working in (an open editor, a browser). Raising a
    process's own OOM score needs no privileges; its children inherit it."""
    try:
        Path(f"/proc/{pid}/oom_score_adj").write_text("1000")
    except OSError:
        pass


def _proc_children(pid: int) -> list[int]:
    out = []
    for stat in Path("/proc").glob("[0-9]*/stat"):
        try:
            fields = stat.read_text().rsplit(")", 1)[1].split()
            if int(fields[1]) == pid:
                out.append(int(stat.parent.name))
        except (OSError, IndexError, ValueError):
            continue
    return out


@dataclass
class SimConfig:
    headset: str = "quest3"
    render_scale: float = 1.0
    eye_height: float = 1.65
    capture_fps: float = 0.0  # continuous capture rate (0 = on request)
    capture_width: int = 960
    eyes: str = "left"
    paused: bool = False  # start paused, waiting for the first step
    microphone: bool = False  # let the app hear this computer's real mic
    virtual_mic: bool = True  # else Visor's own: it hears only what Visor plays it (visor.xr.mic)
    gpu: Optional[int] = None  # Vulkan device index for the compositor
    extra_env: dict = field(default_factory=dict)


class SimInstance:
    """An app running in the virtual headset."""

    def __init__(self, app: AppSpec, config: Optional[SimConfig] = None, name: Optional[str] = None,
                 run_dir: Optional[Path] = None):
        self.app = app
        self.config = config or SimConfig()
        self.headset: Headset = get_headset(self.config.headset).scaled(self.config.render_scale)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.name = name or f"{_slug(app.name)}-{stamp}-{os.getpid() % 1000:03d}"
        self.run_dir = Path(run_dir) if run_dir else paths.runs_dir() / self.name
        self.socket_path = paths.socket_dir() / f"{self.name}.sock"
        self.shm_name = f"/visor-{self.name}"
        self.proc: Optional[subprocess.Popen] = None
        self.mic_proc: Optional[subprocess.Popen] = None
        self.started_at: Optional[float] = None
        self.notes: list[str] = []  # things that didn't work out but didn't stop the launch
        self._client: Optional[XrClient] = None
        self._log = None

    # -- environment ------------------------------------------------------

    def environment(self) -> dict:
        h = self.headset
        env = dict(os.environ)
        env.update(self.app.env)
        if "LD_LIBRARY_PATH" in self.app.env and os.environ.get("LD_LIBRARY_PATH"):
            env["LD_LIBRARY_PATH"] = self.app.env["LD_LIBRARY_PATH"] + ":" + os.environ["LD_LIBRARY_PATH"]
        layer_path = str(paths.layer_dir())
        env["XR_API_LAYER_PATH"] = layer_path + (":" + os.environ["XR_API_LAYER_PATH"]
                                                 if os.environ.get("XR_API_LAYER_PATH") else "")
        enabled = [x for x in os.environ.get("XR_ENABLE_API_LAYERS", "").split(":") if x]
        env["XR_ENABLE_API_LAYERS"] = ":".join(["XR_APILAYER_VISOR_agent", *enabled])
        o, i, u, d = h.fov
        env.update({
            "XR_RUNTIME_JSON": str(paths.monado_manifest()),
            "XRT_COMPOSITOR_NULL": "1",
            "XRT_COMPOSITOR_NULL_VIEW_WIDTH": str(h.eye_width),
            "XRT_COMPOSITOR_NULL_VIEW_HEIGHT": str(h.eye_height),
            "XRT_COMPOSITOR_NULL_REFRESH_RATES": ",".join(str(r) for r in h.refresh_rates),
            "XRT_COMPOSITOR_LOG": "warn",
            "SIMULATED_ENABLE": "1",
            "VISOR_XR_VK_BARRIER_IN_WAIT": "1",
            "VISOR_XR_SOCKET": str(self.socket_path),
            "VISOR_XR_SHM": self.shm_name,
            "VISOR_XR_FOV": f"{o},{i},{u},{d}",
            "VISOR_XR_EYE_HEIGHT": str(self.config.eye_height),
            "VISOR_XR_CAPTURE_FPS": str(self.config.capture_fps),
            "VISOR_XR_CAPTURE_WIDTH": str(self.config.capture_width),
            "VISOR_XR_CAPTURE_EYES": self.config.eyes,
        })
        if self.config.paused:
            env["VISOR_XR_PAUSED"] = "1"
        if self.config.gpu is not None:
            env["XRT_COMPOSITOR_FORCE_GPU_INDEX"] = str(self.config.gpu)
        if not self.config.microphone:
            # The app's audio server is Visor's microphone (or nothing at all):
            # either way it can't hear, or stream, this room. PipeWire's own
            # socket is out of reach too, for apps (and ALSA) that skip PulseAudio.
            env["PULSE_SERVER"] = (f"unix:{xr_mic.pulse_socket(self.name)}" if self.config.virtual_mic
                                   else "unix:/nonexistent")
            env["PIPEWIRE_REMOTE"] = "/nonexistent"
        env.update({k: str(v) for k, v in self.config.extra_env.items()})
        return env

    def command(self) -> list[str]:
        cmd = list(self.app.command)
        if self.app.kind == "unreal-project":
            cmd.append(f"-userdir={self.run_dir / 'ue'}")
        return cmd

    # -- lifecycle --------------------------------------------------------

    def start(self) -> "SimInstance":
        missing = [p for p in (paths.monado_manifest(), paths.layer_manifest()) if not p.exists()]
        if missing:
            raise SimError("the virtual headset isn't installed — run `visor sim setup`")
        self.run_dir.mkdir(parents=True, exist_ok=True)
        paths.socket_dir().mkdir(parents=True, exist_ok=True, mode=0o700)
        self.socket_path.unlink(missing_ok=True)
        if self.config.virtual_mic and not self.config.microphone:
            # Up before the app: apps open their microphone once, early, and don't retry.
            (paths.socket_dir() / f"{self.name}.json").unlink(missing_ok=True)  # stale: would end the new mic
            try:
                self.mic_proc = xr_mic.start_helper(self.name, self.run_dir / "mic.log")
            except (xr_mic.MicError, OSError) as e:
                self.config.virtual_mic = False
                self.notes.append(f"no microphone this time: {e}")
        self._log = open(self.run_dir / "app.log", "ab")
        cmd = self.command()
        stdin = subprocess.DEVNULL
        if self.app.keep_stdin_open:
            # Apps that quit on a key press get a stdin that never ends: a FIFO
            # the child itself holds open for writing, so it outlives us.
            fifo = self.run_dir / "stdin.fifo"
            fifo.unlink(missing_ok=True)
            os.mkfifo(fifo, 0o600)
            stdin = os.open(fifo, os.O_RDWR)
        try:
            self.proc = subprocess.Popen(
                cmd,
                cwd=self.app.cwd,
                env=self.environment(),
                stdin=stdin,
                stdout=self._log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        finally:
            if isinstance(stdin, int) and stdin >= 0:
                os.close(stdin)
        _first_to_go(self.proc.pid)
        self.started_at = time.time()
        self._write_meta(cmd)
        return self

    def _write_meta(self, cmd: list[str]) -> None:
        meta = {
            "name": self.name,
            "app": asdict(self.app),
            "command": cmd,
            "pid": self.proc.pid if self.proc else None,
            "socket": str(self.socket_path),
            "shm": self.shm_name,
            "headset": asdict(self.headset),
            "config": asdict(self.config),
            "run_dir": str(self.run_dir),
            "started_at": self.started_at,
            "mic": ({"pid": self.mic_proc.pid, "control": str(xr_mic.control_socket(self.name))}
                    if self.mic_proc else None),
        }
        (self.run_dir / "instance.json").write_text(json.dumps(meta, indent=2))
        (paths.socket_dir() / f"{self.name}.json").write_text(json.dumps(meta))

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    @property
    def client(self) -> XrClient:
        if self._client is None:
            self._client = XrClient(self.socket_path)
            o, i, u, d = self.headset.fov
            self._client.fov_fallback = (o, i, u, d)
        return self._client

    def wait_ready(self, timeout: float = 300.0, min_frames: int = 30, on_progress=None) -> XrClient:
        """Block until the app is in a focused session and rendering."""
        deadline = time.time() + timeout
        last_note = ""
        while time.time() < deadline:
            if not self.alive():
                raise SimError(f"{self.app.name} exited before its VR session started "
                               f"(code {self.proc.returncode if self.proc else '?'}).\n"
                               + self.crash_summary(default=self.log_tail(25)))
            note = "starting"
            if self.socket_path.exists():
                try:
                    st = self.client.state()
                    note = f"session {st['session']}, frame {int(st['render_frame'])}"
                    if st["session"] == "focused" and st["render_frame"] >= min_frames:
                        return self.client
                    if self.config.paused and st["session"] in ("focused", "visible", "synchronized"):
                        return self.client
                except XrError:
                    note = "loading"
            if on_progress and note != last_note:
                on_progress(note)
                last_note = note
            time.sleep(0.5)
        raise SimError(f"{self.app.name} didn't reach a running VR session within {timeout:.0f}s ({last_note})")

    def stop(self, timeout: float = 20.0) -> Optional[int]:
        """Ask the app to exit, then insist. Never touches anything it didn't start."""
        code = None
        if self._client is not None:
            self._client.close()
            self._client = None
        if self.proc is not None:
            if self.proc.poll() is None:
                try:
                    self.proc.send_signal(signal.SIGTERM)
                    code = self.proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    code = self.proc.wait(timeout=10)
            else:
                code = self.proc.returncode
            # Unreal leaves shader compiler workers behind when killed.
            for child in _proc_children(self.proc.pid):
                try:
                    comm = Path(f"/proc/{child}/comm").read_text().strip()
                    if "ShaderCompile" in comm or "CrashReport" in comm:
                        os.kill(child, signal.SIGKILL)
                except OSError:
                    pass
        if self.mic_proc is not None:
            (paths.socket_dir() / f"{self.name}.json").unlink(missing_ok=True)  # its cue to go
            _end_helper(self.mic_proc.pid, lambda: self.mic_proc.poll() is not None)
            self.mic_proc.poll()
            self.mic_proc = None
        if self._log is not None:
            self._log.close()
            self._log = None
        self.cleanup()
        return code

    def cleanup(self) -> None:
        self.socket_path.unlink(missing_ok=True)
        (paths.socket_dir() / f"{self.name}.json").unlink(missing_ok=True)
        xr_mic.pulse_socket(self.name).unlink(missing_ok=True)
        xr_mic.control_socket(self.name).unlink(missing_ok=True)
        Path("/dev/shm" + self.shm_name).unlink(missing_ok=True)

    # -- diagnostics ------------------------------------------------------

    def log_tail(self, lines: int = 40) -> str:
        path = self.run_dir / "app.log"
        if not path.exists():
            return ""
        data = path.read_bytes()[-200_000:].decode("utf-8", "replace").splitlines()
        return "\n".join(data[-lines:])

    def crash(self) -> Optional[dict]:
        """What went wrong, if the app crashed: message and callstack."""
        return detect_crash(self.run_dir, self.app)

    def crash_summary(self, default: str = "") -> str:
        c = self.crash()
        if not c:
            return default
        stack = "\n".join("  " + line for line in c.get("callstack", [])[:12])
        return f"{c['message']}\n{stack}".rstrip()


_CRASH_PATTERNS = [
    re.compile(r"=== Critical error: ===\s*\n(.*)", re.M),
    re.compile(r"(Unhandled Exception: \S+.*)"),
    re.compile(r"(Assertion failed: .*)"),
    re.compile(r"(Fatal error: .*)"),
    re.compile(r"(VK_ERROR_DEVICE_LOST.*)"),
    re.compile(r"(Segmentation fault.*|free\(\): invalid pointer|double free or corruption.*)"),
]


def detect_crash(run_dir: Path, app: Optional[AppSpec] = None) -> Optional[dict]:
    """Find a crash in an app's logs (Unreal's crash folder first)."""
    crashes = sorted((run_dir / "ue" / "Saved" / "Crashes").glob("crashinfo-*"), key=lambda p: p.stat().st_mtime)
    if crashes:
        diag = crashes[-1] / "Diagnostics.txt"
        text = diag.read_text(errors="replace") if diag.exists() else ""
        msg = re.search(r'Exception was "(.*?)"', text, re.S)
        stack = re.search(r"<CALLSTACK START>\n(.*?)<CALLSTACK END>", text, re.S)
        return {
            "message": (msg.group(1).strip() if msg else "Unreal crash"),
            "callstack": [line.strip() for line in (stack.group(1).splitlines() if stack else []) if line.strip()],
            "report_dir": str(crashes[-1]),
        }
    log = run_dir / "app.log"
    if not log.exists():
        return None
    text = log.read_bytes()[-400_000:].decode("utf-8", "replace")
    for pattern in _CRASH_PATTERNS:
        m = pattern.search(text)
        if m:
            start = m.start()
            tail = text[start:start + 4000].splitlines()
            stack = [line.strip() for line in tail[1:40] if "[Callstack]" in line or line.strip().startswith("0x")]
            return {"message": m.group(1).strip()[:300], "callstack": stack}
    return None


# --------------------------------------------------------------------------
# Everything running right now


def running() -> list[dict]:
    """Apps with the Visor layer that answer on their socket right now."""
    from visor.xr.client import live_sockets

    out = []
    for sock in live_sockets():
        meta_path = sock.with_suffix(".json")
        meta = {}
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text())
            except ValueError:
                meta = {}
        entry = {"name": meta.get("name", sock.stem), "socket": str(sock), "managed": bool(meta),
                 "mic": sock.with_suffix(".mic").exists()}
        try:
            with XrClient(sock, timeout=3) as c:
                st = c.state()
                info = c.info()
                stats = c.stats(window=120)
            entry.update({
                "app": (info.get("app") or {}).get("name", "?"),
                "engine": (info.get("app") or {}).get("engine", ""),
                "pid": info.get("pid"),
                "session": st.get("session"),
                "frame": int(st.get("render_frame", 0)),
                "paused": st.get("paused", False),
                "fps": round(stats.get("fps", 0.0), 1),
                "refresh_hz": round(stats.get("refresh_hz", 0.0)),
                "runtime": info.get("runtime", ""),
                "run_dir": meta.get("run_dir"),
                "headset": (meta.get("headset") or {}).get("name"),
            })
        except XrError as e:
            entry["error"] = str(e)
        out.append(entry)
    return out


def stop_instance(name: str, timeout: float = 20.0) -> str:
    """Stop an app by name: kill the process we recorded launching, or, for an
    app that merely has the layer, ask it to exit through OpenXR."""
    meta_path = paths.socket_dir() / f"{name}.json"
    try:
        meta = json.loads(meta_path.read_text())
    except (OSError, ValueError):
        meta = {}
    pid = meta.get("pid")
    sock = Path(meta.get("socket") or paths.socket_dir() / f"{name}.sock")
    if not pid:
        with XrClient(sock, timeout=5) as xr:
            xr.session_event("exit")
        return "asked the app to exit"
    pid = int(pid)
    try:
        os.killpg(pid, signal.SIGTERM)  # its own session: takes shader workers with it
    except ProcessLookupError:
        pass
    except PermissionError:
        os.kill(pid, signal.SIGTERM)
    deadline = time.time() + timeout
    while time.time() < deadline and Path(f"/proc/{pid}").exists():
        time.sleep(0.2)
    if Path(f"/proc/{pid}").exists():
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    _stop_mic(name, (meta.get("mic") or {}).get("pid"))
    sock.unlink(missing_ok=True)
    meta_path.unlink(missing_ok=True)
    Path("/dev/shm" + str(meta.get("shm") or f"/visor-{name}")).unlink(missing_ok=True)
    return "stopped"


def _end_helper(pid: int, gone, wait: float = 3.0) -> None:
    """A microphone helper ends itself once its app is gone; this waits for
    that, and only then insists. (Signalling first is worse than slow: inside
    an AppImage the signal reaches the runtime, which unmounts the helper's
    files while it's still shutting down.)"""
    deadline = time.time() + wait
    while time.time() < deadline:
        if gone():
            return
        time.sleep(0.05)
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pid, sig)  # its own session: the AppImage runtime and wrappers too
        except (ProcessLookupError, PermissionError):
            return
        deadline = time.time() + 3.0
        while time.time() < deadline:
            if gone():
                return
            time.sleep(0.05)


def _stop_mic(name: str, pid: Optional[int]) -> None:
    """End an app's microphone helper — only if that pid really is it."""
    if pid:
        try:
            cmdline = Path(f"/proc/{int(pid)}/cmdline").read_bytes().split(b"\0")
            if name.encode() in cmdline and any(b"mic" in part for part in cmdline):
                _end_helper(int(pid), lambda: not Path(f"/proc/{int(pid)}").exists()
                            or Path(f"/proc/{int(pid)}/stat").read_text().split(") ")[-1].startswith("Z"))
        except (OSError, ValueError):
            pass
    xr_mic.pulse_socket(name).unlink(missing_ok=True)
    xr_mic.control_socket(name).unlink(missing_ok=True)


def connect(target: Optional[str] = None) -> XrClient:
    """A client for a running app: by name, socket path or pid; or the only one."""
    apps = running()
    if target:
        if Path(target).exists() and target.endswith(".sock"):
            return XrClient(target)
        for a in apps:
            if target in (a["name"], str(a.get("pid")), a["socket"]) or a["name"].startswith(target):
                return XrClient(a["socket"])
        raise SimError(f"no running app matches '{target}'")
    if not apps:
        raise SimError("nothing is running in the virtual headset (start one with `visor sim run`)")
    if len(apps) > 1:
        names = ", ".join(a["name"] for a in apps)
        raise SimError(f"several apps are running — pick one: {names}")
    return XrClient(apps[0]["socket"])


def shell_command(spec: AppSpec, instance: SimInstance) -> str:
    """The equivalent shell line, for repro scripts."""
    env = instance.environment()
    keys = [k for k in env if k.startswith(("XR_", "XRT_", "VISOR_XR_", "SIMULATED_", "PULSE_"))]
    exports = " ".join(f"{k}={shlex.quote(env[k])}" for k in sorted(keys))
    return f"{exports} {shlex.join(instance.command())}"
