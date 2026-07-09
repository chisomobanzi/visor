"""visor — Quest developer CLI for Linux.

MQDH-like functionality from the terminal: device management, app
install/launch, capture, logs, performance.
"""

from __future__ import annotations

import sys
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from visor import adb
from visor.adb.wrapper import VisorError

app = typer.Typer(
    name="visor",
    help="Quest developer toolkit for Linux — MQDH without the bloat.",
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
):
    """Take a screenshot (auto-wakes a sleeping headset)."""
    serial = resolve_device()
    with console.status("Capturing..."):
        shot = run(adb.screenshot, serial, output)
    console.print(
        f"[green]ok[/green] {shot.path} ({shot.width}x{shot.height}, "
        f"{shot.size_bytes // 1024} KB)"
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


@app.command()
def perf(
    monitor: Optional[int] = typer.Option(
        None, "--monitor", "-m", help="Monitor for N seconds instead of one snapshot"
    ),
    interval: int = typer.Option(2, "--interval", "-i", help="Sample interval (s)"),
):
    """Performance snapshot (or continuous monitoring with --monitor)."""
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


if __name__ == "__main__":
    app()
