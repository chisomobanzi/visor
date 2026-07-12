"""App launch benchmarking and perfetto trace capture — the deep-dive
tier when session-level metrics say *what* is slow but not *where*."""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from typing import Optional

from visor.adb.wrapper import ADBCommandError, ADBWrapper, get_adb

# Perfetto categories useful for VR/game frame investigation without
# drowning the trace: scheduling, CPU frequency, graphics, binder
DEFAULT_TRACE_CATEGORIES = "sched freq idle gfx view binder_driver hal dalvik"

_TRACE_DEVICE_PATH = "/data/misc/perfetto-traces/visor.pftrace"


@dataclass
class TraceResult:
    path: str
    duration_s: int
    size_bytes: int
    message: str


def parse_am_start(output: str) -> dict:
    """Parse `am start -W` output into launch timing."""
    if m := re.search(r"^Error:\s*(.+)$", output, re.MULTILINE):
        return {"status": "error", "error": m.group(1).strip()}
    result: dict = {"status": "unknown"}
    if m := re.search(r"Status:\s*(\w+)", output):
        result["status"] = m.group(1)
    if m := re.search(r"LaunchState:\s*(\w+)", output):
        result["launch_state"] = m.group(1)
    if m := re.search(r"TotalTime:\s*(\d+)", output):
        result["total_time_ms"] = int(m.group(1))
    if m := re.search(r"WaitTime:\s*(\d+)", output):
        result["wait_time_ms"] = int(m.group(1))
    return result


def resolve_launch_component(
    package: str, device_id: Optional[str] = None,
    adb: Optional[ADBWrapper] = None,
) -> str:
    """Find the launcher activity component for a package."""
    adb = adb or get_adb()
    output = adb.shell(
        f"cmd package resolve-activity --brief {package}", device_id=device_id
    )
    lines = [line.strip() for line in output.splitlines() if "/" in line]
    if not lines:
        raise ADBCommandError(f"{package} has no launchable activity.")
    return lines[-1]


def launch_benchmark(
    device_id: Optional[str] = None,
    package: str = "",
    runs: int = 3,
    settle_s: float = 3.0,
    adb: Optional[ADBWrapper] = None,
) -> dict:
    """Measure cold-start time: force-stop + `am start -W`, N times.

    Wakes the display first (a sleeping headset never reports the launch
    as drawn). Returns per-run timings and min/median/max stats.
    """
    adb = adb or get_adb()
    component = resolve_launch_component(package, device_id, adb=adb)

    try:
        adb.shell(
            "am broadcast -a com.oculus.vrpowermanager.prox_close",
            device_id=device_id,
        )
    except ADBCommandError:
        pass

    results = []
    for _ in range(runs):
        output = adb.shell(
            f"am start -W -S {component}", device_id=device_id, timeout=90
        )
        results.append(parse_am_start(output))
        time.sleep(settle_s)

    # leave the device tidy
    try:
        adb.shell(f"am force-stop {package}", device_id=device_id)
        adb.shell(
            "am broadcast -a com.oculus.vrpowermanager.automation_disable",
            device_id=device_id,
        )
    except ADBCommandError:
        pass

    ok = [r for r in results if r.get("total_time_ms") is not None]
    times = sorted(r["total_time_ms"] for r in ok)

    def stats(values):
        if not values:
            return None
        return {
            "min": values[0],
            "median": values[len(values) // 2],
            "max": values[-1],
        }

    return {
        "package": package,
        "component": component,
        "runs_completed": len(ok),
        "runs_failed": len(results) - len(ok),
        "total_time_ms": stats(times),
        "launch_state": ok[0].get("launch_state") if ok else None,
        "runs": results,
    }


def capture_trace(
    device_id: Optional[str] = None,
    duration_s: int = 10,
    save_path: Optional[str] = None,
    categories: str = DEFAULT_TRACE_CATEGORIES,
    adb: Optional[ADBWrapper] = None,
) -> TraceResult:
    """Capture a system-wide perfetto trace and pull it locally.

    Open the result at https://ui.perfetto.dev (drag the file in) to see
    per-thread scheduling, CPU frequency, and graphics events — this is
    the tool for pinpointing *which code* blocked a frame.
    """
    adb = adb or get_adb()
    duration_s = max(1, min(duration_s, 60))

    adb.shell(
        f"perfetto -o {_TRACE_DEVICE_PATH} -t {duration_s}s {categories}",
        device_id=device_id,
        timeout=duration_s + 30,
    )

    save_path = save_path or os.path.join(
        os.getcwd(), f"quest-trace-{time.strftime('%Y%m%d-%H%M%S')}.pftrace"
    )
    parent = os.path.dirname(os.path.abspath(save_path))
    os.makedirs(parent, exist_ok=True)
    try:
        adb.run(["pull", _TRACE_DEVICE_PATH, save_path],
                device_id=device_id, timeout=120)
    finally:
        try:
            adb.shell(f"rm -f {_TRACE_DEVICE_PATH}", device_id=device_id)
        except ADBCommandError:
            pass

    size = os.path.getsize(save_path) if os.path.exists(save_path) else 0
    return TraceResult(
        path=save_path,
        duration_s=duration_s,
        size_bytes=size,
        message=(
            f"Trace saved to {save_path} ({size // 1024} KB). Open it at "
            "https://ui.perfetto.dev to inspect scheduling, clocks, and "
            "graphics events."
        ),
    )
