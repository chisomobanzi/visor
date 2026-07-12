"""Performance session recording — the core primitive of visor's profiler.

A session captures, over a chosen window: per-second VrApi metrics for
the target app, periodic memory anatomy (dumpsys meminfo App Summary),
thermal snapshots, and derived events (level changes, thermal
transitions, stale-frame bursts, low-memory kills). Sessions serialize
to JSON so they can be replayed, summarized, and diffed.
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Callable, Optional

from visor.adb.performance import get_foreground_app
from visor.adb.vrapi import parse_vrapi_line
from visor.adb.wrapper import ADBCommandError, ADBWrapper, get_adb
from visor.utils.parser import parse_meminfo_summary, parse_thermalservice

SCHEMA = "visor-perf-session/1"

# stale frames in one second above which we flag a hitch
STALE_BURST_THRESHOLD = 5


@dataclass
class PerfSession:
    device: str
    package: Optional[str]
    started_at: str
    duration_s: float
    vrapi: list = field(default_factory=list)    # per-second samples (dicts + elapsed_s)
    memory: list = field(default_factory=list)   # meminfo summaries (+ elapsed_s)
    thermal: list = field(default_factory=list)  # {elapsed_s, status_label, max_skin_temp_c}
    events: list = field(default_factory=list)   # {elapsed_s, kind, detail}
    meta: dict = field(default_factory=dict)     # model, os_version, etc.

    def to_dict(self) -> dict:
        d = asdict(self)
        d["schema"] = SCHEMA
        return d


def save_session(session: PerfSession, path: str) -> None:
    with open(path, "w") as f:
        json.dump(session.to_dict(), f, indent=1)


def load_session(path: str) -> PerfSession:
    with open(path) as f:
        data = json.load(f)
    data.pop("schema", None)
    return PerfSession(**data)


# --------------------------------------------------------------- events


def derive_events(vrapi: list, thermal: list) -> list:
    """Turn raw sample streams into annotated moments worth looking at."""
    events = []

    prev = None
    for s in vrapi:
        if prev is not None:
            for key, kind in (("gpu_level", "gpu_level_change"),
                              ("cpu_level", "cpu_level_change")):
                if s.get(key) is not None and prev.get(key) is not None \
                        and s[key] != prev[key]:
                    events.append({
                        "elapsed_s": s["elapsed_s"],
                        "kind": kind,
                        "detail": f"{prev[key]} -> {s[key]}",
                    })
            if s.get("target_fps") and prev.get("target_fps") \
                    and s["target_fps"] != prev["target_fps"]:
                events.append({
                    "elapsed_s": s["elapsed_s"],
                    "kind": "refresh_rate_change",
                    "detail": f"{prev['target_fps']}Hz -> {s['target_fps']}Hz",
                })
        if (s.get("stale_frames") or 0) >= STALE_BURST_THRESHOLD:
            events.append({
                "elapsed_s": s["elapsed_s"],
                "kind": "stale_burst",
                "detail": f"{s['stale_frames']} stale frames in 1s "
                          f"(fps {s.get('fps')}/{s.get('target_fps')})",
            })
        prev = s

    prev_status = None
    for t in thermal:
        status = t.get("status_label")
        if prev_status is not None and status != prev_status:
            events.append({
                "elapsed_s": t["elapsed_s"],
                "kind": "thermal_status",
                "detail": f"{prev_status} -> {status} "
                          f"(skin {t.get('max_skin_temp_c')}°C)",
            })
        prev_status = status

    events.sort(key=lambda e: e["elapsed_s"])
    return events


# -------------------------------------------------------------- summary


def _pctl(values: list, fraction: float):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(fraction * len(ordered)))]


def summarize_session(session: PerfSession) -> dict:
    """Headline stats for a session — what you'd tell a teammate."""
    vrapi = session.vrapi
    summary: dict = {
        "package": session.package,
        "duration_s": session.duration_s,
        "sample_count": len(vrapi),
        "event_count": len(session.events),
    }

    if not vrapi:
        summary["note"] = (
            "No VrApi samples captured — the target app was not rendering "
            "in VR during the session (2D panels and background apps don't "
            "emit VR frame metrics)."
        )
    else:
        fps = [s["fps"] for s in vrapi if s.get("fps") is not None]
        targets = [s["target_fps"] for s in vrapi if s.get("target_fps")]
        target = max(set(targets), key=targets.count) if targets else None
        stale_total = sum(s.get("stale_frames") or 0 for s in vrapi)
        total_frames = sum(fps) if fps else 0
        gpu_utils = [s["gpu_util"] for s in vrapi if s.get("gpu_util") is not None]
        cpu_utils = [s["cpu_util"] for s in vrapi if s.get("cpu_util") is not None]
        worst_cores = [
            s["cpu_util_worst_core"] for s in vrapi
            if s.get("cpu_util_worst_core") is not None
        ]
        app_gpu = [s["app_gpu_ms"] for s in vrapi if s.get("app_gpu_ms") is not None]

        summary["fps"] = {
            "avg": round(sum(fps) / len(fps), 1) if fps else None,
            "min": min(fps) if fps else None,
            "p5": _pctl(fps, 0.05),
            "target": target,
        }
        summary["stale_frames"] = {
            "total": stale_total,
            "percent_of_frames": round(stale_total / total_frames * 100, 2)
            if total_frames else None,
            "worst_second": max((s.get("stale_frames") or 0) for s in vrapi),
        }
        summary["gpu"] = {
            "avg_util": round(sum(gpu_utils) / len(gpu_utils), 3) if gpu_utils else None,
            "p95_util": _pctl(gpu_utils, 0.95),
            "max_level": max((s.get("gpu_level") or 0) for s in vrapi),
            "avg_app_gpu_ms": round(sum(app_gpu) / len(app_gpu), 2) if app_gpu else None,
            "p95_app_gpu_ms": _pctl(app_gpu, 0.95),
        }
        summary["cpu"] = {
            "avg_util": round(sum(cpu_utils) / len(cpu_utils), 3) if cpu_utils else None,
            "p95_worst_core": _pctl(worst_cores, 0.95),
            "max_level": max((s.get("cpu_level") or 0) for s in vrapi),
        }
        free = [s["free_memory_mb"] for s in vrapi if s.get("free_memory_mb")]
        if free:
            summary["free_memory_mb"] = {"start": free[0], "min": min(free), "end": free[-1]}

    if session.memory:
        first, last = session.memory[0], session.memory[-1]
        summary["memory"] = {
            "graphics_kb": last.get("graphics_kb"),
            "graphics_growth_kb": (last.get("graphics_kb") or 0) - (first.get("graphics_kb") or 0),
            "total_pss_kb": last.get("total_pss_kb"),
            "total_pss_growth_kb": (last.get("total_pss_kb") or 0) - (first.get("total_pss_kb") or 0),
        }

    if session.thermal:
        temps = [t["max_skin_temp_c"] for t in session.thermal
                 if t.get("max_skin_temp_c") is not None]
        summary["thermal"] = {
            "start_status": session.thermal[0].get("status_label"),
            "end_status": session.thermal[-1].get("status_label"),
            "max_skin_temp_c": max(temps) if temps else None,
            "skin_temp_rise_c": round(temps[-1] - temps[0], 1) if len(temps) > 1 else None,
        }

    return summary


# ------------------------------------------------------------- recorder


_LMKD_RE = re.compile(r"(?:lowmemorykiller|lmkd).*[Kk]ill(?:ing)?\s+'?([\w.]+)'?", )


class LiveRecorder:
    """Records a performance session in the background.

    start() spawns the collectors; snapshot() returns the in-progress
    session (safe to call repeatedly — this is what the dashboard polls);
    stop() finalizes and returns the completed session.
    """

    def __init__(
        self,
        device_id: str,
        package: Optional[str] = None,
        poll_interval_s: float = 2.0,
        wake_display: bool = True,
        adb: Optional[ADBWrapper] = None,
    ):
        self.wake_display = wake_display
        self.adb = adb or get_adb()
        self.device_id = device_id
        self.poll_interval_s = poll_interval_s
        self.package = package
        self.pid: Optional[int] = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._logcat_proc: Optional[subprocess.Popen] = None
        self._start_monotonic = 0.0
        self.session: Optional[PerfSession] = None

    # -- lifecycle

    def start(self) -> PerfSession:
        if self.wake_display:
            # a sleeping display pauses VR rendering entirely — no
            # rendering, no VrApi metrics (same trick as screenshots)
            try:
                self.adb.shell(
                    "am broadcast -a com.oculus.vrpowermanager.prox_close",
                    device_id=self.device_id,
                )
            except ADBCommandError:
                pass
        if self.package is None:
            try:
                self.package = get_foreground_app(self.device_id, adb=self.adb)
            except ADBCommandError:
                self.package = None
        self._resolve_pid()

        meta = {}
        try:
            meta["model"] = self.adb.shell(
                "getprop ro.product.model", device_id=self.device_id
            ).strip()
        except ADBCommandError:
            pass

        self.session = PerfSession(
            device=self.device_id,
            package=self.package,
            started_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
            duration_s=0,
            meta=meta,
        )
        self._start_monotonic = time.monotonic()
        self._spawn_logcat_reader()
        poller = threading.Thread(target=self._poll_loop, daemon=True)
        poller.start()
        self._threads.append(poller)
        return self.session

    def stop(self) -> PerfSession:
        self._stop.set()
        if self.wake_display:
            # hand control back to the proximity sensor (no-op when worn)
            try:
                self.adb.shell(
                    "am broadcast -a com.oculus.vrpowermanager.automation_disable",
                    device_id=self.device_id,
                )
            except ADBCommandError:
                pass
        if self._logcat_proc and self._logcat_proc.poll() is None:
            self._logcat_proc.terminate()
            try:
                self._logcat_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._logcat_proc.kill()
                self._logcat_proc.wait()
        for t in self._threads:
            t.join(timeout=5)
        with self._lock:
            self.session.duration_s = round(self._elapsed(), 1)
            self._keep_target_pid_samples()
            self.session.events = derive_events(
                self.session.vrapi, self.session.thermal
            ) + [e for e in self.session.events if e["kind"] == "lmkd_kill"]
            self.session.events.sort(key=lambda e: e["elapsed_s"])
        return self.session

    def snapshot(self) -> dict:
        """In-progress view for live UIs: current session data + summary."""
        with self._lock:
            self.session.duration_s = round(self._elapsed(), 1)
            self._keep_target_pid_samples()
            self.session.events = derive_events(
                self.session.vrapi, self.session.thermal
            ) + [e for e in self.session.events if e["kind"] == "lmkd_kill"]
            data = self.session.to_dict()
            data["summary"] = summarize_session(self.session)
            data["recording"] = not self._stop.is_set()
            return data

    # -- internals

    def _elapsed(self) -> float:
        return time.monotonic() - self._start_monotonic

    def _resolve_pid(self):
        if not self.package:
            return
        try:
            out = self.adb.shell(
                f"pidof {self.package}", device_id=self.device_id
            ).strip()
            self.pid = int(out.split()[0]) if out else None
        except (ADBCommandError, ValueError):
            self.pid = None

    def _keep_target_pid_samples(self):
        """Multiple processes emit VrApi lines (the app AND the runtime
        shell); keep only the target app's. If the pid is unknown, keep
        the dominant emitter with the highest app GPU time (the shell
        idles near zero)."""
        samples = self.session.vrapi
        if not samples:
            return
        if self.pid is not None:
            self.session.vrapi = [s for s in samples if s["pid"] == self.pid]
            return
        by_pid: dict = {}
        for s in samples:
            by_pid.setdefault(s["pid"], []).append(s)
        def busyness(pid):
            gpu = [x.get("app_gpu_ms") or 0 for x in by_pid[pid]]
            return sum(gpu) / len(gpu)
        best = max(by_pid, key=busyness)
        self.session.vrapi = by_pid[best]

    def _spawn_logcat_reader(self):
        cmd = [self.adb.adb_path]
        if self.device_id:
            cmd += ["-s", self.device_id]
        # -T 1: only new lines; watch VrApi plus low-memory killer tags
        cmd += ["logcat", "-T", "1", "-v", "threadtime",
                "-s", "VrApi", "lmkd", "lowmemorykiller"]
        self._logcat_proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True
        )
        reader = threading.Thread(target=self._read_logcat, daemon=True)
        reader.start()
        self._threads.append(reader)

    def _read_logcat(self):
        assert self._logcat_proc and self._logcat_proc.stdout
        for line in self._logcat_proc.stdout:
            if self._stop.is_set():
                break
            sample = parse_vrapi_line(line)
            if sample:
                d = sample.to_dict()
                d["elapsed_s"] = round(self._elapsed(), 1)
                with self._lock:
                    self.session.vrapi.append(d)
                # late pid resolution: app may have been mid-launch
                if self.pid is None and self.package:
                    self._resolve_pid()
                continue
            if m := _LMKD_RE.search(line):
                with self._lock:
                    self.session.events.append({
                        "elapsed_s": round(self._elapsed(), 1),
                        "kind": "lmkd_kill",
                        "detail": f"low-memory killer killed {m.group(1)}",
                    })

    def _poll_loop(self):
        while not self._stop.is_set():
            elapsed = round(self._elapsed(), 1)
            try:
                thermal = parse_thermalservice(
                    self.adb.shell("dumpsys thermalservice",
                                   device_id=self.device_id, timeout=10)
                )
                skin = [s["temperature_c"] for s in thermal["sensors"]
                        if s["type"] == "SKIN"]
                with self._lock:
                    self.session.thermal.append({
                        "elapsed_s": elapsed,
                        "status_label": thermal["status_label"],
                        "max_skin_temp_c": max(skin) if skin else None,
                    })
            except ADBCommandError:
                pass

            if self.package:
                try:
                    mem = parse_meminfo_summary(
                        self.adb.shell(f"dumpsys meminfo {self.package}",
                                       device_id=self.device_id, timeout=10)
                    )
                    if mem:
                        mem["elapsed_s"] = elapsed
                        with self._lock:
                            self.session.memory.append(mem)
                except ADBCommandError:
                    pass

            self._stop.wait(self.poll_interval_s)


def record_session(
    device_id: str,
    duration_s: float = 30,
    package: Optional[str] = None,
    poll_interval_s: float = 2.0,
    adb: Optional[ADBWrapper] = None,
    on_progress: Optional[Callable[[float], None]] = None,
) -> PerfSession:
    """Blocking convenience wrapper: record for duration_s and return the
    finished session."""
    recorder = LiveRecorder(
        device_id, package=package, poll_interval_s=poll_interval_s, adb=adb
    )
    recorder.start()
    start = time.monotonic()
    try:
        while (elapsed := time.monotonic() - start) < duration_s:
            if on_progress:
                on_progress(elapsed)
            time.sleep(min(1.0, duration_s - elapsed))
    finally:
        session = recorder.stop()
    return session
