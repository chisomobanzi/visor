"""visor — Quest developer CLI.

MQDH-like functionality from the terminal: device management, app
install/launch, capture, logs, performance.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from visor import adb
from visor.adb.wrapper import VisorError
from visor.publish.creds import load as load_credentials
from visor.publish.ovrtool import fetch as ovrtool_fetch
from visor.publish.ovrtool import version as ovrtool_version
from visor.publish.upload import is_release_channel
from visor.publish.upload import upload as publish_upload

app = typer.Typer(
    name="visor",
    help="Quest developer toolkit — MQDH without the bloat.",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
)
console = Console()
err_console = Console(stderr=True)

_state: dict = {"device": None}

LEVEL_STYLES = {
    "V": "dim",
    "D": "cyan",
    "I": "green",
    "W": "yellow",
    "E": "bold red",
    "F": "bold white on red",
}


def die(message: str) -> None:
    err_console.print(f"[bold red]error:[/bold red] {message}")
    raise typer.Exit(1)


def resolve_device() -> str:
    """Pick the target device: --device flag, else the single connected Quest."""
    if _state["device"]:
        return _state["device"]
    try:
        devices = [d for d in adb.discover_devices() if d.state == "device"]
    except VisorError as e:
        die(str(e))
    quests = [d for d in devices if d.is_quest]
    pool = quests or devices
    if not pool:
        try:
            locked = [d for d in adb.discover_devices() if d.state == "unauthorized"]
        except VisorError:
            locked = []
        if locked:
            die(
                f"device {locked[0].serial} is unauthorized — put on the "
                "headset and accept the USB debugging prompt."
            )
        die("no Quest connected. Check USB/wireless and developer mode.")
    if len(pool) > 1:
        serials = ", ".join(f"{d.serial} ({d.model or '?'})" for d in pool)
        die(f"multiple devices connected — pass --device <serial>. Found: {serials}")
    return pool[0].serial


def run(fn, *args, **kwargs):
    """Call a visor library function, exiting cleanly on VisorError."""
    try:
        return fn(*args, **kwargs)
    except VisorError as e:
        die(str(e))
    except FileNotFoundError as e:
        die(str(e))
    except ValueError as e:
        die(str(e))


@app.callback()
def main(
    device: Optional[str] = typer.Option(
        None, "--device", "-d", help="Device serial (from `visor devices`)."
    ),
):
    _state["device"] = device


# ---------------------------------------------------------------- devices


@app.command()
def devices():
    """List connected devices."""
    found = run(adb.discover_devices)
    if not found:
        console.print("No devices connected.")
        console.print(
            "[dim]Connect the headset via USB and accept the debugging "
            "prompt, or use `visor pair` for wireless.[/dim]"
        )
        return
    table = Table(title="Connected devices")
    table.add_column("Serial", style="bold")
    table.add_column("Model")
    table.add_column("State")
    table.add_column("Connection")
    for d in found:
        state_style = {"device": "green", "unauthorized": "yellow"}.get(d.state, "red")
        table.add_row(
            d.serial,
            d.model or "?",
            f"[{state_style}]{d.state}[/{state_style}]",
            "wireless" if d.is_wireless else "USB",
        )
    console.print(table)
    if any(d.state == "unauthorized" for d in found):
        console.print(
            "[yellow]![/yellow] unauthorized device: put on the headset and "
            "accept the 'Allow USB debugging' prompt."
        )


@app.command()
def info():
    """Detailed device info: battery, storage, firmware, network."""
    serial = resolve_device()
    with console.status("Reading device info..."):
        i = run(adb.get_device_info, serial)
    table = Table(title=f"{i.model or 'Device'} ({i.serial})", show_header=False)
    table.add_column("Field", style="bold")
    table.add_column("Value")
    table.add_row("OS version", i.os_version or "?")
    table.add_row("Firmware", i.firmware or "?")
    if i.battery:
        charge = " (charging)" if i.battery.charging else ""
        table.add_row(
            "Battery",
            f"{i.battery.level}%{charge}, {i.battery.temperature_c}°C",
        )
    if i.storage:
        table.add_row(
            "Storage",
            f"{i.storage.available} free of {i.storage.total} "
            f"({i.storage.used_percent}% used)",
        )
    table.add_row("WiFi IP", i.ip_address or "not connected")
    table.add_row("Thermal", i.thermal_status or "?")
    console.print(table)


@app.command("connect-wireless")
def connect_wireless():
    """Switch from USB to wireless ADB (cable can be unplugged after)."""
    serial = resolve_device()
    with console.status("Switching to wireless ADB..."):
        result = run(adb.connect_wireless, serial)
    console.print(f"[green]ok[/green] {result.message}")


@app.command()
def pair(
    address: str = typer.Argument(..., help="ip:port shown in Wireless Debugging"),
    code: str = typer.Argument(..., help="6-digit pairing code"),
):
    """Pair with a headset over WiFi (Settings > Developer > Wireless Debugging)."""
    if ":" not in address:
        die("address must be ip:port, e.g. 192.168.1.31:37123")
    ip, port = address.rsplit(":", 1)
    run(adb.pair_device, ip, int(port), code)
    console.print(f"[green]ok[/green] paired with {address}")


# ------------------------------------------------------------------- apps


@app.command()
def install(
    apk: str = typer.Argument(..., help="Path to the APK"),
    grant: bool = typer.Option(
        False, "--grant", "-g", help="Auto-grant all runtime permissions"
    ),
):
    """Install (sideload) an APK."""
    serial = resolve_device()
    with console.status(f"Installing {apk} (large APKs take a while)..."):
        run(adb.install_apk, apk, serial, grant_permissions=grant)
    console.print(f"[green]ok[/green] installed {apk}")


@app.command()
def uninstall(package: str):
    """Uninstall an app by package name."""
    serial = resolve_device()
    run(adb.uninstall_app, package, serial)
    console.print(f"[green]ok[/green] uninstalled {package}")


@app.command()
def apps(
    filter: str = typer.Option(
        "sideloaded", "--filter", "-f", help="sideloaded | system | all"
    ),
):
    """List installed apps (default: sideloaded only)."""
    serial = resolve_device()
    packages = run(adb.list_apps, serial, filter=filter)
    if not packages:
        console.print(f"No {filter} apps found.")
        return
    table = Table(title=f"{filter.capitalize()} apps ({len(packages)})")
    table.add_column("Package")
    for p in packages:
        table.add_row(p)
    console.print(table)


@app.command()
def launch(package: str):
    """Launch an app by package name."""
    serial = resolve_device()
    message = run(adb.launch_app, package, serial)
    console.print(f"[green]ok[/green] {message}")


@app.command()
def stop(package: str):
    """Force-stop an app."""
    serial = resolve_device()
    run(adb.stop_app, package, serial)
    console.print(f"[green]ok[/green] stopped {package}")


@app.command("app-info")
def app_info(package: str):
    """Show app version, install dates, permissions, data size."""
    serial = resolve_device()
    i = run(adb.get_app_info, package, serial)
    table = Table(title=package, show_header=False)
    table.add_column("Field", style="bold")
    table.add_column("Value")
    table.add_row("Version", f"{i.version_name or '?'} (code {i.version_code or '?'})")
    table.add_row("Target SDK", str(i.target_sdk or "?"))
    table.add_row("Installed", i.first_install or "?")
    table.add_row("Updated", i.last_update or "?")
    table.add_row("Data size", i.data_size or "?")
    table.add_row("Permissions", "\n".join(i.permissions) or "none")
    console.print(table)


@app.command("clear-data")
def clear_data(package: str):
    """Clear an app's data (reset to freshly-installed state)."""
    serial = resolve_device()
    run(adb.clear_app_data, package, serial)
    console.print(f"[green]ok[/green] cleared data for {package}")


# ---------------------------------------------------------------- capture


@app.command()
def screenshot(
    output: Optional[str] = typer.Option(
        None, "--output", "-o", help="Save path (default ./screenshots/)"
    ),
    clean: bool = typer.Option(
        False,
        "--clean",
        "-c",
        help="Save the de-fisheyed left eye instead of the raw stereo PNG (needs ffmpeg).",
    ),
):
    """Take a screenshot (auto-wakes a sleeping headset)."""
    serial = resolve_device()
    with console.status("Capturing..."):
        shot = run(adb.screenshot, serial, output)
        width, height, size = shot.width, shot.height, shot.size_bytes
        if clean:
            from PIL import Image

            with Image.open(shot.path) as raw:
                w, h = raw.size
                eye = raw.crop((0, 0, w // 2, h)) if w / h > 1.6 else raw
                img = eye.copy()  # detach from the file before it closes
            img = adb.undistort_eye(img).convert("RGB")
            img.save(shot.path)
            width, height, size = img.width, img.height, os.path.getsize(shot.path)
    console.print(
        f"[green]ok[/green] {shot.path} ({width}x{height}, {size // 1024} KB)"
    )


@app.command()
def record(
    seconds: int = typer.Argument(30, help="Duration (max 180)"),
    output: Optional[str] = typer.Option(
        None, "--output", "-o", help="Save path (default ./recordings/)"
    ),
):
    """Record the screen for N seconds."""
    serial = resolve_device()
    with console.status(f"Recording {seconds}s (put the headset on!)..."):
        rec = run(adb.screen_record, serial, seconds, output)
    console.print(
        f"[green]ok[/green] {rec.path} ({rec.size_bytes // 1024} KB)"
    )


@app.command()
def cast():
    """Start scrcpy casting (requires scrcpy installed)."""
    serial = resolve_device()
    session = run(adb.start_cast, serial)
    console.print(f"[green]ok[/green] {session.message}")


# ------------------------------------------------------------------- logs


def _print_entries(entries):
    for e in entries:
        style = LEVEL_STYLES.get(e.level, "")
        console.print(
            f"[dim]{e.timestamp}[/dim] [{style}]{e.level}[/{style}] "
            f"[bold]{e.tag}[/bold]: {e.message}",
            highlight=False,
        )


@app.command()
def logs(
    tag: Optional[str] = typer.Option(None, "--tag", "-t", help="Filter by log tag"),
    level: str = typer.Option("W", "--level", "-l", help="Min level: V/D/I/W/E/F"),
    grep: Optional[str] = typer.Option(None, "--grep", "-g", help="Regex filter"),
    lines: int = typer.Option(500, "--lines", "-n", help="History to fetch"),
    follow: bool = typer.Option(False, "--follow", "-f", help="Stream live"),
):
    """Show recent logs (default: warnings and above)."""
    serial = resolve_device()
    if follow:
        console.print("[dim]streaming logs — ctrl-c to stop[/dim]")
        try:
            for entry in adb.logcat_stream(serial, tag=tag, level=level, pattern=grep):
                _print_entries([entry])
        except KeyboardInterrupt:
            return
        except VisorError as e:
            die(str(e))
        return
    entries = run(adb.logcat_dump, serial, lines=lines, tag=tag, level=level, pattern=grep)
    if not entries:
        console.print("[dim]no matching log entries[/dim]")
        return
    _print_entries(entries)


@app.command("logs-clear")
def logs_clear():
    """Clear the logcat buffer."""
    serial = resolve_device()
    run(adb.logcat_clear, serial)
    console.print("[green]ok[/green] logcat buffer cleared")


@app.command("crash-logs")
def crash_logs(
    package: Optional[str] = typer.Argument(None, help="Filter to one package"),
):
    """Show crash/ANR reports (most recent first)."""
    serial = resolve_device()
    with console.status("Reading crash reports..."):
        crashes = run(adb.crash_log, serial, package)
    if not crashes:
        console.print("[green]no crashes found[/green]" +
                      (f" for {package}" if package else ""))
        return
    for c in crashes[:5]:
        console.rule(f"[red]{c.crash_type}[/red] {c.package} at {c.timestamp}")
        console.print(c.stack_trace, highlight=False)
    if len(crashes) > 5:
        console.print(f"[dim]...and {len(crashes) - 5} older reports[/dim]")


# ------------------------------------------------------------------- perf


def _print_snapshot(snap: dict):
    cpu = snap.get("cpu", {})
    memory = snap.get("memory", {})
    thermal = snap.get("thermal", {})
    battery = snap.get("battery", {})

    table = Table(title="Performance snapshot", show_header=False)
    table.add_column("Metric", style="bold")
    table.add_column("Value")
    if cpu:
        table.add_row("CPU", f"{cpu.get('usage_percent', '?')}% across {cpu.get('cores', '?')} cores")
    if memory:
        table.add_row("Memory", f"{memory.get('used', '?')} used / {memory.get('total', '?')}")
    if thermal:
        throttle = "[red]THROTTLING[/red]" if thermal.get("throttling") else "ok"
        table.add_row(
            "Thermal",
            f"{thermal.get('status_label', '?')} ({throttle}), "
            f"max skin {thermal.get('max_skin_temp_c', '?')}°C",
        )
    if battery:
        drain = battery.get("current_ma")
        drain_str = f", {drain:+.0f} mA" if drain is not None else ""
        table.add_row("Battery", f"{battery.get('level', '?')}%{drain_str}")
    table.add_row("Foreground", snap.get("foreground_app") or "none")
    console.print(table)

    procs = snap.get("top_processes") or []
    if procs:
        ptable = Table(title="Top processes")
        ptable.add_column("PID", justify="right")
        ptable.add_column("CPU%", justify="right")
        ptable.add_column("MEM%", justify="right")
        ptable.add_column("Name")
        for p in procs:
            ptable.add_row(
                str(p["pid"]), f"{p['cpu_percent']:.1f}", f"{p['mem_percent']:.1f}",
                p["name"],
            )
        console.print(ptable)


perf_app = typer.Typer(
    help="Performance: snapshot, session profiling, reports, diffs.",
    invoke_without_command=True,
)
app.add_typer(perf_app, name="perf")


def _print_diagnosis(diag: dict):
    style = {"healthy": "green"}.get(diag["verdict"], "yellow")
    if any(f["severity"] == "critical" for f in diag["findings"]):
        style = "bold red"
    console.print(
        f"\n[{style}]● {diag['verdict'].replace('_', ' ').upper()}[/{style}] "
        f"{diag['headline']}"
    )
    if note := diag.get("note"):
        console.print(f"[dim]{note}[/dim]")
    for f in diag["findings"]:
        console.print(f"\n[bold]{f['kind'].replace('_', ' ')}[/bold] "
                      f"[dim]({f['severity']})[/dim]")
        for e in f["evidence"]:
            console.print(f"  [dim]evidence:[/dim] {e}")
        for r in f["recommendations"]:
            console.print(f"  [cyan]→[/cyan] {r}")


def _print_session_summary(summary: dict):
    table = Table(title="Session summary", show_header=False)
    table.add_column("Metric", style="bold")
    table.add_column("Value")
    if fps := summary.get("fps"):
        table.add_row(
            "FPS", f"{fps['avg']} avg / {fps['p5']} p5 "
                   f"(target {fps.get('target') or '?'}Hz)"
        )
    if stale := summary.get("stale_frames"):
        table.add_row(
            "Stale frames",
            f"{stale['total']} ({stale['percent_of_frames']}% of frames, "
            f"worst second {stale['worst_second']})",
        )
    if gpu := summary.get("gpu"):
        table.add_row(
            "GPU", f"util {gpu['avg_util']} avg / {gpu['p95_util']} p95, "
                   f"level ≤{gpu['max_level']}, app {gpu['avg_app_gpu_ms']}ms avg",
        )
    if cpu := summary.get("cpu"):
        table.add_row(
            "CPU", f"util {cpu['avg_util']} avg, worst core "
                   f"{cpu['p95_worst_core']} p95, level ≤{cpu['max_level']}",
        )
    if mem := summary.get("memory"):
        table.add_row(
            "Memory",
            f"graphics {(mem['graphics_kb'] or 0) // 1024}MB "
            f"({mem['graphics_growth_kb'] or 0:+} KB over session)",
        )
    if thermal := summary.get("thermal"):
        table.add_row(
            "Thermal",
            f"{thermal['start_status']} → {thermal['end_status']}, "
            f"max skin {thermal['max_skin_temp_c']}°C",
        )
    if note := summary.get("note"):
        table.add_row("Note", note)
    console.print(table)


@perf_app.command("record")
def perf_record(
    duration: int = typer.Option(30, "--duration", "-t", help="Seconds to record"),
    package: Optional[str] = typer.Option(
        None, "--app", "-a", help="Package to profile (default: foreground app)"
    ),
    output: Optional[str] = typer.Option(
        None, "--out", "-o", help="Session JSON path (default ./sessions/)"
    ),
):
    """Record a profiling session while you exercise the app, then print
    the summary and bottleneck diagnosis."""
    import time as _time

    serial = resolve_device()
    with console.status(f"Profiling for {duration}s — play the app now..."):
        session = run(adb.record_session, serial, duration_s=duration,
                      package=package)
    output = output or f"sessions/{(session.package or 'unknown').replace('.', '_')}-{_time.strftime('%Y%m%d-%H%M%S')}.json"
    import os as _os
    _os.makedirs(_os.path.dirname(_os.path.abspath(output)), exist_ok=True)
    run(adb.save_session, session, output)
    _print_session_summary(run(adb.summarize_session, session))
    _print_diagnosis(run(adb.diagnose_session, session))
    console.print(f"\n[green]ok[/green] session saved to {output}")


@perf_app.command("report")
def perf_report(session_file: str):
    """Summarize and diagnose a saved session JSON."""
    session = run(adb.load_session, session_file)
    console.print(f"[dim]{session.package} · {session.started_at} · "
                  f"{session.duration_s}s · {len(session.vrapi)} samples[/dim]")
    _print_session_summary(run(adb.summarize_session, session))
    _print_diagnosis(run(adb.diagnose_session, session))


@perf_app.command("diff")
def perf_diff(before: str, after: str):
    """Compare two saved sessions (before/after an optimization)."""
    d = run(
        adb.diff_sessions, run(adb.load_session, before), run(adb.load_session, after)
    )
    console.print(d["headline"])
    table = Table()
    table.add_column("Metric")
    table.add_column("Before", justify="right")
    table.add_column("After", justify="right")
    table.add_column("Δ", justify="right")
    for key in ("fps_avg", "fps_p5", "stale_percent", "gpu_util_avg",
                "app_gpu_p95_ms", "cpu_worst_core_p95", "graphics_growth_kb"):
        m = d.get(key)
        if not m or m.get("delta") is None:
            continue
        delta = m["delta"]
        lower_better = key not in ("fps_avg", "fps_p5")
        good = delta < 0 if lower_better else delta > 0
        style = "green" if good else "red" if delta else ""
        table.add_row(key, str(m["before"]), str(m["after"]),
                      f"[{style}]{delta:+}[/{style}]" if style else f"{delta:+}")
    console.print(table)
    console.print(f"verdict: {d['verdict']['before']} → {d['verdict']['after']}")


@perf_app.callback()
def perf(
    ctx: typer.Context,
    monitor: Optional[int] = typer.Option(
        None, "--monitor", "-m", help="Monitor for N seconds instead of one snapshot"
    ),
    interval: int = typer.Option(2, "--interval", "-i", help="Sample interval (s)"),
):
    """Performance snapshot (or continuous monitoring with --monitor)."""
    if ctx.invoked_subcommand is not None:
        return
    serial = resolve_device()
    if monitor:
        console.print(f"[dim]sampling every {interval}s for {monitor}s...[/dim]")
        samples = run(
            adb.monitor_performance, serial,
            interval_seconds=interval, duration_seconds=monitor,
        )
        table = Table(title=f"Performance over {monitor}s")
        table.add_column("t (s)", justify="right")
        table.add_column("CPU %", justify="right")
        table.add_column("Mem used", justify="right")
        table.add_column("Skin °C", justify="right")
        table.add_column("Batt %", justify="right")
        table.add_column("Foreground")
        for s in samples:
            table.add_row(
                f"{s.get('elapsed_s', 0):.0f}",
                str(s.get("cpu", {}).get("usage_percent", "?")),
                s.get("memory", {}).get("used", "?"),
                str(s.get("thermal", {}).get("max_skin_temp_c", "?")),
                str(s.get("battery", {}).get("level", "?")),
                s.get("foreground_app") or "-",
            )
        console.print(table)
        return
    with console.status("Sampling..."):
        snap = run(adb.get_performance_snapshot, serial)
    _print_snapshot(snap)


@app.command()
def bench(
    package: str = typer.Argument(..., help="Package to benchmark"),
    runs: int = typer.Option(3, "--runs", "-n", help="Number of cold starts"),
):
    """Benchmark app cold-start time (force-stop + timed launch, N runs)."""
    serial = resolve_device()
    with console.status(f"Benchmarking {package} cold start x{runs}..."):
        result = run(adb.launch_benchmark, serial, package, runs=runs)
    if not result["runs_completed"]:
        die(f"all {runs} launches failed: "
            f"{result['runs'][0].get('error', 'unknown error')}")
    stats = result["total_time_ms"]
    table = Table(title=f"Cold start · {package}", show_header=False)
    table.add_column("Stat", style="bold")
    table.add_column("Value")
    table.add_row("Runs", f"{result['runs_completed']} ok, {result['runs_failed']} failed")
    table.add_row("Median", f"{stats['median']} ms")
    table.add_row("Min / Max", f"{stats['min']} / {stats['max']} ms")
    table.add_row("Launch state", result["launch_state"] or "?")
    console.print(table)


@app.command()
def trace(
    duration: int = typer.Option(10, "--duration", "-t", help="Seconds (max 60)"),
    output: Optional[str] = typer.Option(None, "--out", "-o", help="Output .pftrace"),
):
    """Capture a perfetto system trace (scheduling, clocks, graphics) —
    open the file at ui.perfetto.dev."""
    serial = resolve_device()
    with console.status(f"Tracing for {duration}s — reproduce the problem now..."):
        result = run(adb.capture_trace, serial, duration_s=duration,
                     save_path=output)
    console.print(f"[green]ok[/green] {result.message}")


@app.command()
def thermal():
    """Thermal state and throttling risk."""
    serial = resolve_device()
    with console.status("Reading thermal sensors..."):
        state = run(adb.get_thermal_state, serial)
    label = state["status_label"]
    style = "green" if not state["throttling"] else "bold red"
    console.print(f"Thermal status: [{style}]{label}[/{style}]"
                  + (" — device is throttling" if state["throttling"] else ""))
    if state.get("max_skin_temp_c") is not None:
        console.print(
            f"Max skin temperature: {state['max_skin_temp_c']}°C "
            f"[dim](warn ≥{state['warn_threshold_c']}, "
            f"critical ≥{state['critical_threshold_c']})[/dim]"
        )
    table = Table(title="Sensors")
    table.add_column("Sensor")
    table.add_column("Type")
    table.add_column("°C", justify="right")
    table.add_column("")
    interesting = sorted(
        state["sensors"], key=lambda s: s["temperature_c"], reverse=True
    )
    for s in interesting[:15]:
        flag = ""
        if s.get("critical"):
            flag = "[bold red]CRITICAL[/bold red]"
        elif s.get("warning"):
            flag = "[yellow]warm[/yellow]"
        table.add_row(s["name"], s["type"], f"{s['temperature_c']:.1f}", flag)
    console.print(table)


# ------------------------------------------------------------------ files


@app.command()
def push(local: str, remote: str):
    """Push a local file to the device."""
    serial = resolve_device()
    with console.status(f"Pushing {local}..."):
        result = run(adb.push_file, local, remote, serial)
    console.print(f"[green]ok[/green] {result.message}")


@app.command()
def pull(remote: str, local: str = typer.Argument(".")):
    """Pull a file from the device."""
    serial = resolve_device()
    with console.status(f"Pulling {remote}..."):
        result = run(adb.pull_file, remote, local, serial)
    console.print(f"[green]ok[/green] {result.message}")


@app.command()
def ls(path: str = typer.Argument("/sdcard/")):
    """List a directory on the device."""
    serial = resolve_device()
    entries = run(adb.list_files, path, serial)
    table = Table(title=path)
    table.add_column("Name")
    table.add_column("Size", justify="right")
    table.add_column("Modified")
    for e in sorted(entries, key=lambda x: (not x.is_dir, x.name.lower())):
        name = f"[bold blue]{e.name}/[/bold blue]" if e.is_dir else e.name
        size = "" if e.is_dir or e.size is None else _human_size(e.size)
        table.add_row(name, size, e.modified or "")
    console.print(table)


def _human_size(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.0f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


@app.command()
def rm(
    path: str,
    recursive: bool = typer.Option(False, "--recursive", "-r"),
):
    """Delete a file on the device."""
    serial = resolve_device()
    run(adb.delete_file, path, serial, recursive=recursive)
    console.print(f"[green]ok[/green] deleted {path}")


# ---------------------------------------------------------- quest extras


@app.command("refresh-rate")
def refresh_rate(rate: int = typer.Argument(..., help="72/80/90/120 (model-dependent)")):
    """Set the display refresh rate hint."""
    serial = resolve_device()
    result = run(adb.set_refresh_rate, serial, rate)
    console.print(f"[green]ok[/green] {result['message']}")


@app.command()
def tracking():
    """Tracking subsystem status (head/hand/eye/body/controllers)."""
    serial = resolve_device()
    status = run(adb.get_tracking_status, serial)
    table = Table(title="Tracking", show_header=False)
    table.add_column("Subsystem", style="bold")
    table.add_column("Status")
    for key in ("head_tracking", "hand_tracking", "eye_tracking",
                "body_tracking", "controller_tracking"):
        table.add_row(key.replace("_", " "), str(status.get(key, "?")))
    console.print(table)


@app.command()
def sensors():
    """List hardware sensors."""
    serial = resolve_device()
    found = run(adb.get_headset_sensors, serial)
    if not found:
        console.print("no sensors reported")
        return
    table = Table(title="Sensors")
    table.add_column("Type")
    for s in found:
        table.add_row(s["type"])
    console.print(table)


@app.command()
def ui(
    port: int = typer.Option(7700, "--port", "-p"),
    no_browser: bool = typer.Option(False, "--no-browser", help="Don't open a browser"),
):
    """Launch the visor web dashboard (localhost only)."""
    from visor.ui.server import main as ui_main

    console.print(f"[green]visor dashboard[/green] → http://127.0.0.1:{port}  [dim](ctrl-c to stop)[/dim]")
    ui_main(port=port, open_browser=not no_browser)


@app.command("app")
def app_window(
    port: int = typer.Option(7700, "--port", "-p"),
):
    """Open the visor dashboard as a native desktop window."""
    from visor.ui.server import launch_desktop_app, wait_forever

    console.print(
        "[green]visor[/green] → opening desktop app …  [dim](close the window to stop)[/dim]"
    )
    try:
        reason = launch_desktop_app(port=port)
    except RuntimeError as e:  # nothing serving — a window would be blank
        die(str(e))
    if reason:
        # No window toolkit and no Chromium-family browser to borrow one
        # from — the dashboard is a web app, so a tab serves it just as well.
        console.print(f"[yellow]Opened in your browser instead:[/yellow] [dim]{reason}[/dim]")
        console.print(
            f"[green]visor dashboard[/green] → http://127.0.0.1:{port} [dim](ctrl-c to stop)[/dim]"
        )
        wait_forever()


@app.command("install-desktop")
def install_desktop(
    uninstall: bool = typer.Option(
        False, "--uninstall", help="Remove the launcher and icon instead."
    ),
):
    """Install an app-menu launcher + icon for `visor app` (Linux)."""
    import sys

    from visor.desktop import install_desktop_entry, uninstall_desktop_entry

    if sys.platform != "linux":
        console.print(
            "[yellow]install-desktop is Linux-only (freedesktop .desktop entries). "
            "On macOS/Windows, run `visor app` directly.[/yellow]"
        )
        raise typer.Exit(1)

    if uninstall:
        removed = uninstall_desktop_entry()
        if removed:
            console.print("[green]Visor launcher removed.[/green]")
        else:
            console.print("[yellow]No Visor launcher was installed.[/yellow]")
        return

    paths = install_desktop_entry()
    console.print("[green]Visor launcher installed.[/green]")
    console.print(f"  desktop entry: [dim]{paths['desktop']}[/dim]")
    console.print(f"  icon:          [dim]{paths['icon']}[/dim]")
    console.print("Find [bold]Visor[/bold] in your app menu, or run [bold]visor app[/bold].")


@app.command("fetch-tool")
def fetch_tool():
    """Download Meta's ovr-platform-util, needed to upload builds."""
    console.print(
        "Fetching [bold]ovr-platform-util[/bold] from Meta. It is Meta's tool, "
        "not part of Visor — Visor can't redistribute it, so it is downloaded "
        "on request and kept alongside your other user data."
    )
    try:
        path = ovrtool_fetch()
    except VisorError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)

    found_version = ovrtool_version(path)
    suffix = f" (version {found_version})" if found_version else ""
    console.print(f"[green]Ready{suffix}.[/green]")
    # soft_wrap: a path broken across lines is a path you can't copy.
    console.print(f"  [dim]{path}[/dim]", soft_wrap=True)


@app.command()
def upload(
    apk: Path = typer.Argument(
        ...,
        exists=True,
        dir_okay=False,
        help="Path to the .apk to upload",
    ),
    channel: str = typer.Option(
        "ALPHA", "--channel", "-c", help="Release channel (ALPHA, BETA, RC, store)"
    ),
    age_group: Optional[str] = typer.Option(
        None,
        "--age-group",
        help="TEENS_AND_ADULTS, MIXED_AGES or CHILDREN. "
        "Without one, Meta holds the build as a draft.",
    ),
    notes: Optional[str] = typer.Option(
        None, "--notes", "-n", help="Release notes shown to channel testers"
    ),
    yes: bool = typer.Option(
        False, "--yes", help="Required to publish to the live store channel"
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Let Meta validate the build, then stop without uploading it",
    ),
):
    """Upload a build to a Meta Horizon release channel."""
    if is_release_channel(channel) and not yes and not dry_run:
        console.print(
            f"[red]Channel {channel!r} is the live Meta Horizon store.[/red]\n"
            "Everyone who owns the app gets this build. "
            "Re-run with [bold]--yes[/bold] if that is what you meant."
        )
        raise typer.Exit(1)

    try:
        credentials = load_credentials()
    except VisorError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)

    verb = "Validating" if dry_run else "Uploading"
    console.print(f"{verb} [bold]{Path(apk).name}[/bold] to [bold]{channel}[/bold]…")
    try:
        result = publish_upload(
            apk,
            channel=channel,
            credentials=credentials,
            age_group=age_group,
            notes=notes,
            confirm_release=yes,
            validate_only=dry_run,
            on_line=lambda line: console.print(f"  [dim]{line}[/dim]"),
        )
    except VisorError as e:
        # The message is already redacted by the upload layer.
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)

    for note in result.preflight_notes:
        console.print(f"[yellow]{note}[/yellow]")

    if result.validated_only:
        console.print(f"[green]Build validated for {channel}.[/green]")
        console.print(
            "[dim]It was not uploaded — drop --dry-run to publish it.[/dim]"
        )
        return

    console.print(f"[green]Uploaded to {channel}.[/green]")
    console.print(
        "[dim]Channels start empty — invite testers in the Meta dashboard "
        "before anyone can install it.[/dim]"
    )


if __name__ == "__main__":
    app()
