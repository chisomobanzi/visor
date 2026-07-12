"""Visor MCP server — lets Claude Code interact with a connected Quest headset.

Run with `visor-mcp` (stdio transport). Register in Claude Code with:

    claude mcp add quest -- visor-mcp
"""

from __future__ import annotations

import io
import os
import tempfile
from dataclasses import asdict
from typing import Optional

from mcp.server.fastmcp import FastMCP, Image

from visor import adb
from visor.adb.wrapper import VisorError

mcp = FastMCP(
    "visor-quest",
    instructions=(
        "Tools for interacting with a Meta Quest VR headset connected over "
        "ADB (USB or wireless): screenshots, logs, crash reports, app "
        "management, performance and thermal monitoring. If device_id is "
        "omitted, the single connected Quest is used automatically."
    ),
)

# Screenshots wider than this are downscaled before returning to the model
_MAX_IMAGE_WIDTH = 1400


class QuestError(Exception):
    """Raised with user-actionable messages; FastMCP reports them as tool errors."""


def _resolve_device(device_id: Optional[str]) -> str:
    """Pick the device to talk to, with clear errors for 0/multiple devices."""
    try:
        devices = [d for d in adb.discover_devices() if d.state == "device"]
    except VisorError as e:
        raise QuestError(str(e)) from None
    if device_id:
        if not any(d.serial == device_id for d in devices):
            available = ", ".join(d.serial for d in devices) or "none"
            raise QuestError(
                f"Device {device_id!r} not connected (available: {available})."
            )
        return device_id
    quests = [d for d in devices if d.is_quest]
    pool = quests or devices
    if not pool:
        unauthorized = []
        try:
            unauthorized = [
                d.serial for d in adb.discover_devices() if d.state == "unauthorized"
            ]
        except VisorError:
            pass
        if unauthorized:
            raise QuestError(
                f"Device(s) {unauthorized} connected but unauthorized — put on "
                "the headset and accept the USB debugging prompt."
            )
        raise QuestError(
            "No Quest connected. Check USB cable / wireless ADB and that "
            "developer mode is enabled."
        )
    if len(pool) > 1:
        raise QuestError(
            "Multiple devices connected — pass device_id. Available: "
            + ", ".join(f"{d.serial} ({d.model or 'unknown'})" for d in pool)
        )
    return pool[0].serial


def _wrap(fn, *args, **kwargs):
    """Run a visor call, translating VisorError into a clean tool error."""
    try:
        return fn(*args, **kwargs)
    except VisorError as e:
        raise QuestError(str(e)) from None


@mcp.tool()
def quest_devices() -> list[dict]:
    """List connected Quest/Android devices with connection state.

    Returns serial, model, connection state (device/unauthorized/offline),
    and whether the connection is wireless. Use this first if unsure what
    is connected."""
    return [asdict(d) for d in _wrap(adb.discover_devices)]


@mcp.tool()
def quest_info(device_id: Optional[str] = None) -> dict:
    """Get detailed device info: model, OS/firmware version, battery level
    and temperature, free storage, WiFi IP address, thermal status."""
    serial = _resolve_device(device_id)
    return asdict(_wrap(adb.get_device_info, serial))


@mcp.tool()
def quest_screenshot(
    device_id: Optional[str] = None, save_path: Optional[str] = None
) -> list:
    """Take a screenshot of what's currently displayed in the headset and
    return the image so it can be viewed. Auto-wakes the display if the
    headset is asleep. Optionally saves to save_path (PNG)."""
    serial = _resolve_device(device_id)
    path = save_path or os.path.join(
        tempfile.gettempdir(), f"quest-screenshot-{serial}.png"
    )
    shot = _wrap(adb.screenshot, serial, path)

    # Quest captures are side-by-side stereo (~4128x2208); crop to the
    # left eye and downscale so the returned image is easy to view.
    from PIL import Image as PILImage

    with PILImage.open(shot.path) as img:
        width, height = img.size
        cropped_note = ""
        if width / height > 1.6:
            img = img.crop((0, 0, width // 2, height))
            width = width // 2
            cropped_note = ", cropped to left eye for viewing"
        if width > _MAX_IMAGE_WIDTH:
            ratio = _MAX_IMAGE_WIDTH / width
            img = img.resize((_MAX_IMAGE_WIDTH, int(height * ratio)))
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=85)

    meta = (
        f"Screenshot captured: {shot.width}x{shot.height}px, "
        f"{shot.size_bytes // 1024} KB, full stereo PNG saved to "
        f"{shot.path}{cropped_note}"
    )
    return [Image(data=buf.getvalue(), format="jpeg"), meta]


@mcp.tool()
def quest_install(
    apk_path: str,
    device_id: Optional[str] = None,
    grant_permissions: bool = False,
) -> dict:
    """Install (sideload) an APK onto the headset. Replaces an existing
    install of the same package (upgrade). Set grant_permissions=True to
    auto-grant all runtime permissions (useful for test builds)."""
    serial = _resolve_device(device_id)
    result = _wrap(
        adb.install_apk, apk_path, serial, grant_permissions=grant_permissions
    )
    return asdict(result)


@mcp.tool()
def quest_uninstall(package_name: str, device_id: Optional[str] = None) -> dict:
    """Uninstall an app from the headset by package name."""
    serial = _resolve_device(device_id)
    _wrap(adb.uninstall_app, package_name, serial)
    return {"success": True, "message": f"Uninstalled {package_name}"}


@mcp.tool()
def quest_launch(package_name: str, device_id: Optional[str] = None) -> dict:
    """Launch an app on the headset by package name (e.g. com.mycompany.mygame)."""
    serial = _resolve_device(device_id)
    return {"message": _wrap(adb.launch_app, package_name, serial)}


@mcp.tool()
def quest_stop(package_name: str, device_id: Optional[str] = None) -> dict:
    """Force-stop a running app on the headset."""
    serial = _resolve_device(device_id)
    return {"message": _wrap(adb.stop_app, package_name, serial)}


@mcp.tool()
def quest_app_list(
    device_id: Optional[str] = None, filter: str = "sideloaded"
) -> dict:
    """List installed apps. filter: 'sideloaded' (default — dev/third-party
    apps), 'system', or 'all'."""
    serial = _resolve_device(device_id)
    packages = _wrap(adb.list_apps, serial, filter=filter)
    return {"filter": filter, "count": len(packages), "packages": packages}


@mcp.tool()
def quest_app_info(package_name: str, device_id: Optional[str] = None) -> dict:
    """Get app details: version, install/update dates, target SDK,
    permissions, data size on disk."""
    serial = _resolve_device(device_id)
    return asdict(_wrap(adb.get_app_info, package_name, serial))


@mcp.tool()
def quest_clear_data(package_name: str, device_id: Optional[str] = None) -> dict:
    """Clear an app's data — resets it to freshly-installed state. Useful
    for wiping test state (saves, settings, caches)."""
    serial = _resolve_device(device_id)
    _wrap(adb.clear_app_data, package_name, serial)
    return {"success": True, "message": f"Cleared data for {package_name}"}


@mcp.tool()
def quest_logs(
    device_id: Optional[str] = None,
    lines: int = 200,
    level: str = "W",
    tag: Optional[str] = None,
    pattern: Optional[str] = None,
) -> dict:
    """Get recent logcat entries as structured logs (timestamp/level/tag/
    message). level: V/D/I/W/E/F (minimum severity, default W). tag scopes
    to one log tag (e.g. 'Unity' or 'VrApi'). pattern is a case-insensitive
    regex applied to tag+message."""
    serial = _resolve_device(device_id)
    entries = _wrap(
        adb.logcat_dump, serial, lines=lines, level=level, tag=tag, pattern=pattern
    )
    return {
        "count": len(entries),
        "filters": {"level": level, "tag": tag, "pattern": pattern},
        "entries": [e.to_dict() for e in entries],
    }


@mcp.tool()
def quest_logs_stream(
    duration_seconds: int = 10,
    device_id: Optional[str] = None,
    level: str = "W",
    tag: Optional[str] = None,
    pattern: Optional[str] = None,
    max_entries: int = 500,
) -> dict:
    """Watch logs in real time for duration_seconds (max 60) and return
    everything captured. Use while reproducing a bug: start this, interact
    with the app, and the tool returns what was logged in that window."""
    import threading

    duration_seconds = min(duration_seconds, 60)
    serial = _resolve_device(device_id)
    _wrap(adb.logcat_clear, serial)

    entries: list = []
    done = threading.Event()

    def collect():
        try:
            for entry in adb.logcat_stream(serial, tag=tag, level=level, pattern=pattern):
                entries.append(entry)
                if done.is_set() or len(entries) >= max_entries:
                    break
        except VisorError:
            pass

    t = threading.Thread(target=collect, daemon=True)
    t.start()
    t.join(timeout=duration_seconds)
    done.set()

    return {
        "duration_seconds": duration_seconds,
        "count": len(entries),
        "truncated": len(entries) >= max_entries,
        "entries": [e.to_dict() for e in entries],
    }


@mcp.tool()
def quest_crash_logs(
    package_name: Optional[str] = None, device_id: Optional[str] = None
) -> dict:
    """Get crash and ANR reports (Java crashes, native crashes, ANRs) from
    the system dropbox, most recent first. Filter by package_name to see
    crashes for one app. Each entry includes the full stack trace."""
    serial = _resolve_device(device_id)
    crashes = _wrap(adb.crash_log, serial, package_name)
    return {
        "count": len(crashes),
        "package_filter": package_name,
        "crashes": [asdict(c) for c in crashes[:10]],
    }


@mcp.tool()
def quest_performance(device_id: Optional[str] = None) -> dict:
    """Get a performance snapshot: CPU usage, memory, thermal status with
    throttling risk, battery level/drain (mA, negative = discharging),
    foreground app, and top processes by CPU."""
    serial = _resolve_device(device_id)
    return _wrap(adb.get_performance_snapshot, serial)


@mcp.tool()
def quest_monitor(
    duration_seconds: int = 30,
    interval_seconds: int = 2,
    device_id: Optional[str] = None,
) -> dict:
    """Monitor performance over time (max 120s) and return a timeseries of
    snapshots. Use while exercising an app to spot CPU spikes, memory
    growth, or thermal climb."""
    duration_seconds = min(duration_seconds, 120)
    serial = _resolve_device(device_id)
    samples = _wrap(
        adb.monitor_performance,
        serial,
        interval_seconds=interval_seconds,
        duration_seconds=duration_seconds,
    )
    return {"samples": samples, "count": len(samples)}


@mcp.tool()
def quest_thermal(device_id: Optional[str] = None) -> dict:
    """Check thermal state: overall throttle status, every temperature
    sensor with warning/critical flags, and max skin temperature. Quest
    throttles CPU/GPU as skin temps climb — watch for warnings during
    long play sessions."""
    serial = _resolve_device(device_id)
    return _wrap(adb.get_thermal_state, serial)


@mcp.tool()
def quest_frame_timing(
    package_name: Optional[str] = None, device_id: Optional[str] = None
) -> dict:
    """Get live VR frame timing for the running app: FPS vs target, stale
    frames, GPU/CPU utilization and dynamic levels, app GPU time vs frame
    budget. Captures ~6s of the VrApi metrics stream. Falls back to
    gfxinfo for 2D/panel apps that don't render in VR."""
    serial = _resolve_device(device_id)
    session = _wrap(
        adb.record_session, serial, duration_s=6, package=package_name,
        poll_interval_s=30,
    )
    if session.vrapi:
        summary = adb.summarize_session(session)
        return {
            "source": "vrapi",
            "package": session.package,
            "summary": summary,
            "latest_sample": session.vrapi[-1],
        }
    result = _wrap(adb.get_frame_timing, serial, package_name)
    result["note"] = (
        "No VrApi stream (app not rendering in VR) — gfxinfo data instead. "
        + result.get("note", "")
    )
    return result


def _sessions_dir() -> str:
    path = os.path.join(os.path.expanduser("~"), ".visor", "sessions")
    os.makedirs(path, exist_ok=True)
    return path


@mcp.tool()
def quest_perf_session(
    duration_seconds: int = 30,
    package_name: Optional[str] = None,
    device_id: Optional[str] = None,
) -> dict:
    """Record a full performance profiling session (max 300s) and return a
    summary, a bottleneck diagnosis (GPU-bound / CPU-bound / thermal
    throttling / memory pressure / hitching, with evidence and
    recommendations), and notable events. Use while the user exercises
    their app in the headset. The full session (per-second VrApi metrics,
    memory anatomy, thermal timeline) is saved to a JSON file whose path
    is returned — pass two such paths to quest_perf_diff to compare runs.
    Defaults to the current foreground app."""
    import time as _time

    duration_seconds = min(duration_seconds, 300)
    serial = _resolve_device(device_id)
    session = _wrap(
        adb.record_session, serial,
        duration_s=duration_seconds, package=package_name,
    )
    stamp = _time.strftime("%Y%m%d-%H%M%S")
    path = os.path.join(
        _sessions_dir(),
        f"{(session.package or 'unknown').replace('.', '_')}-{stamp}.json",
    )
    adb.save_session(session, path)
    return {
        "session_path": path,
        "summary": adb.summarize_session(session),
        "diagnosis": adb.diagnose_session(session),
        "events": session.events,
        "sample_count": len(session.vrapi),
    }


@mcp.tool()
def quest_perf_diff(
    before_session_path: str, after_session_path: str
) -> dict:
    """Compare two recorded perf sessions (JSON files from
    quest_perf_session), e.g. before/after an optimization. Returns metric
    deltas (fps, stale frames, GPU/CPU load, memory growth) and the
    verdict change."""
    try:
        before = adb.load_session(before_session_path)
        after = adb.load_session(after_session_path)
    except (OSError, ValueError, TypeError, KeyError) as e:
        raise QuestError(f"Could not load session file: {e}") from None
    return adb.diff_sessions(before, after)


@mcp.tool()
def quest_launch_benchmark(
    package_name: str, runs: int = 3, device_id: Optional[str] = None
) -> dict:
    """Benchmark an app's cold-start time: force-stop then timed launch,
    N times (max 5). Returns per-run timings and min/median/max — useful
    for measuring startup optimizations."""
    serial = _resolve_device(device_id)
    return _wrap(
        adb.launch_benchmark, serial, package_name, runs=min(runs, 5)
    )


@mcp.tool()
def quest_trace(
    duration_seconds: int = 10,
    save_path: Optional[str] = None,
    device_id: Optional[str] = None,
) -> dict:
    """Capture a perfetto system trace (max 60s): per-thread scheduling,
    CPU frequencies, graphics events. Use when session metrics show a
    problem (hitches, CPU-bound) and you need to see exactly what blocked.
    The .pftrace file can be opened at https://ui.perfetto.dev."""
    serial = _resolve_device(device_id)
    result = _wrap(
        adb.capture_trace, serial, duration_s=duration_seconds,
        save_path=save_path,
    )
    return asdict(result)


@mcp.tool()
def quest_app_memory(package_name: str, device_id: Optional[str] = None) -> dict:
    """Get an app's memory anatomy (PSS in KB): java heap, native heap,
    code, stack, and graphics — graphics allocations (textures, buffers,
    surfaces) are usually what exhausts memory in VR apps."""
    from visor.utils.parser import parse_meminfo_summary

    serial = _resolve_device(device_id)
    output = _wrap(
        adb.get_adb().shell,
        f"dumpsys meminfo {package_name}", device_id=serial, timeout=15,
    )
    summary = parse_meminfo_summary(output)
    if not summary:
        raise QuestError(
            f"{package_name} has no memory info — is it installed and running?"
        )
    return {"package": package_name, "pss_kb": summary}


@mcp.tool()
def quest_screen_record(
    duration_seconds: int = 15,
    save_path: Optional[str] = None,
    device_id: Optional[str] = None,
) -> dict:
    """Record the headset screen for N seconds (max 180) and save the MP4
    locally. Returns the file path — useful for capturing a bug repro."""
    serial = _resolve_device(device_id)
    rec = _wrap(
        adb.screen_record, serial, duration_seconds=duration_seconds,
        save_path=save_path,
    )
    return asdict(rec)


@mcp.tool()
def quest_push(
    local_path: str, device_path: str, device_id: Optional[str] = None
) -> dict:
    """Push a local file to the device (e.g. test assets to
    /sdcard/Android/data/<pkg>/files/)."""
    serial = _resolve_device(device_id)
    return asdict(_wrap(adb.push_file, local_path, device_path, serial))


@mcp.tool()
def quest_pull(
    device_path: str, local_path: str, device_id: Optional[str] = None
) -> dict:
    """Pull a file from the device to the local filesystem (e.g. save
    files, logs written by the app)."""
    serial = _resolve_device(device_id)
    return asdict(_wrap(adb.pull_file, device_path, local_path, serial))


@mcp.tool()
def quest_files(device_path: str, device_id: Optional[str] = None) -> dict:
    """List a directory on the device. App data lives under
    /sdcard/Android/data/<package>/files/."""
    serial = _resolve_device(device_id)
    entries = _wrap(adb.list_files, device_path, serial)
    return {
        "path": device_path,
        "count": len(entries),
        "entries": [asdict(e) for e in entries],
    }


@mcp.tool()
def quest_connect_wireless(device_id: Optional[str] = None) -> dict:
    """Switch the USB-connected headset to wireless ADB so the cable can
    be unplugged. The headset must be on WiFi."""
    serial = _resolve_device(device_id)
    return asdict(_wrap(adb.connect_wireless, serial))


@mcp.tool()
def quest_set_refresh_rate(rate: int, device_id: Optional[str] = None) -> dict:
    """Set the display refresh rate hint (Quest 3: 72/80/90/120 Hz).
    Apps that request an explicit rate override this."""
    serial = _resolve_device(device_id)
    try:
        return _wrap(adb.set_refresh_rate, serial, rate)
    except ValueError as e:
        raise QuestError(str(e)) from None


@mcp.tool()
def quest_tracking(device_id: Optional[str] = None) -> dict:
    """Check tracking subsystem status: head/hand/eye/body/controller
    tracking service availability, plus guardian boundary state if exposed."""
    serial = _resolve_device(device_id)
    tracking = _wrap(adb.get_tracking_status, serial)
    tracking["guardian"] = _wrap(adb.get_guardian_status, serial)
    return tracking


def main():
    mcp.run()


if __name__ == "__main__":
    main()
