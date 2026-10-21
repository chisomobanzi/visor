"""`visor sim` — the virtual headset from the terminal.

Run any OpenXR app headless inside a simulated Quest 3, then look around,
point, press, step frame by frame, record and replay.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table

sim_app = typer.Typer(
    name="sim",
    help="Virtual headset: run OpenXR apps headless and drive them (look, point, press, step, record).",
    no_args_is_help=True,
)
console = Console()
err = Console(stderr=True)


def _die(message: str) -> None:
    err.print(f"[bold red]error:[/bold red] {message}")
    raise typer.Exit(1)


def _client(target: Optional[str]):
    from visor.xr import SimError, connect

    try:
        return connect(target)
    except SimError as e:
        _die(str(e))


def _xr(fn, *args, **kwargs):
    from visor.xr import SimError, XrError

    try:
        return fn(*args, **kwargs)
    except (XrError, SimError, ValueError) as e:
        _die(str(e))


# --------------------------------------------------------------------------
# install


@sim_app.command()
def setup(
    force: bool = typer.Option(False, "--force", help="Rebuild everything, even if up to date"),
    layer: bool = typer.Option(False, "--layer", help="Only rebuild the Visor OpenXR layer"),
):
    """Build the virtual headset (Monado runtime, OpenXR loader, Visor layer). No root needed."""
    from visor.xr import setup as xr_setup

    with console.status("[bold]building the virtual headset[/bold]") as status:
        def log(msg: str) -> None:
            status.update(f"[bold]{msg}[/bold]")
            console.print(f"  [dim]·[/dim] {msg}")

        try:
            xr_setup.setup(log=log, force=force, only="layer" if layer else None)
        except xr_setup.SetupError as e:
            _die(str(e))
    console.print("[green]✓[/green] ready — try [bold]visor sim run demo[/bold]")


@sim_app.command()
def doctor():
    """Check that the virtual headset works on this machine (runs the demo for a few seconds)."""
    from visor.xr import AppSpec, SimConfig, SimError, SimInstance
    from visor.xr import setup as xr_setup

    table = Table(show_header=False, box=None, pad_edge=False)
    ok_all = True
    for c in xr_setup.check_prerequisites():
        ok_all &= c.ok
        table.add_row("[green]✓[/green]" if c.ok else "[red]✗[/red]", c.name, f"[dim]{c.detail}[/dim]" if c.ok else c.fix)
    for name, ok in xr_setup.installed().items():
        ok_all &= ok
        table.add_row("[green]✓[/green]" if ok else "[red]✗[/red]", f"installed: {name}",
                      "" if ok else "run [bold]visor sim setup[/bold]")
    console.print(Panel(table, title="virtual headset", expand=False))
    if not ok_all:
        raise typer.Exit(1)
    with console.status("starting the demo app in the virtual headset…"):
        sim = SimInstance(AppSpec.resolve("demo"), SimConfig()).start()
        try:
            xr = sim.wait_ready(timeout=60)
            time.sleep(2)
            stats = xr.stats()
            frame = xr.capture()
        except SimError as e:
            sim.stop()
            _die(str(e))
        finally:
            pass
    sim.stop()
    console.print(f"[green]✓[/green] demo ran at [bold]{stats['fps']:.0f} fps[/bold] "
                  f"(target {stats['refresh_hz']:.0f} Hz), {int(stats['missed_frames'])} missed; "
                  f"captured a {frame.width}×{frame.height} frame")


# --------------------------------------------------------------------------
# running apps


def _status_table(apps: list[dict]) -> Table:
    t = Table(box=None, header_style="bold dim")
    for col in ("name", "app", "session", "frame", "fps", "headset"):
        t.add_column(col)
    for a in apps:
        if "error" in a:
            t.add_row(a["name"], "[red]unreachable[/red]", a["error"], "", "", "")
            continue
        session = a["session"] + (" [yellow](paused)[/yellow]" if a.get("paused") else "")
        fps = f"{a['fps']:.0f}/{a['refresh_hz']}" if a.get("refresh_hz") else f"{a['fps']:.0f}"
        t.add_row(a["name"], f"{a['app']} [dim]{a.get('engine', '')}[/dim]", session, str(a["frame"]), fps,
                  a.get("headset") or "[dim]external[/dim]")
    return t


@sim_app.command()
def run(
    target: str = typer.Argument(..., help="A .uproject, a packaged build (.sh), an executable, or 'demo'"),
    map_name: Optional[str] = typer.Option(None, "--map", "-m", help="Unreal map to open, e.g. /Game/Maps/Start"),
    headset: str = typer.Option("quest3", "--headset", help="quest3 | quest3s | quest2 | questpro"),
    scale: float = typer.Option(1.0, "--scale", help="Eye-buffer scale (0.5 is a quarter of the pixels)"),
    name: Optional[str] = typer.Option(None, "--name", "-n", help="Instance name (default: app + time)"),
    paused: bool = typer.Option(False, "--paused", help="Start frozen; advance with `visor sim step`"),
    deterministic: bool = typer.Option(False, "--deterministic", help="Fixed timestep, for exact replays (Unreal)"),
    seated: bool = typer.Option(False, "--seated", help="Seated eye height (1.2 m) instead of standing"),
    mic: bool = typer.Option(False, "--mic", help="Let the app use this computer's microphone"),
    capture_fps: float = typer.Option(0.0, "--capture-fps", help="Publish frames continuously at this rate"),
    detach: bool = typer.Option(False, "--detach", "-d", help="Leave it running and return"),
    timeout: float = typer.Option(300.0, "--timeout", help="Seconds to wait for the VR session"),
    args: Optional[list[str]] = typer.Argument(None, help="Extra arguments for the app (after --)"),
):
    """Launch an app inside the virtual headset."""
    from visor.xr import AppSpec, SimConfig, SimError, SimInstance

    spec = _xr(AppSpec.resolve, target, map_name=map_name, extra_args=args or [], deterministic=deterministic)
    cfg = SimConfig(headset=headset, render_scale=scale, paused=paused, microphone=mic, capture_fps=capture_fps,
                    eye_height=1.2 if seated else 1.65)
    sim = _xr(SimInstance, spec, cfg, name=name)
    _xr(sim.start)
    console.print(f"[bold]{spec.name}[/bold] → virtual {sim.headset.name} "
                  f"[dim]({sim.headset.eye_width}×{sim.headset.eye_height}/eye, {sim.headset.refresh_rates[0]} Hz)[/dim]")
    console.print(f"  [dim]run dir[/dim] {sim.run_dir}")
    try:
        with console.status("waiting for the VR session") as status:
            xr = sim.wait_ready(timeout=timeout, on_progress=lambda note: status.update(f"[bold]{note}[/bold]"))
    except SimError as e:
        sim.stop()
        _die(str(e))
    except KeyboardInterrupt:
        sim.stop()
        raise typer.Exit(130)
    info = xr.info()
    session = info.get("session", {})
    console.print(f"[green]●[/green] running — {session.get('interaction_profile') or 'no controllers bound'}, "
                  f"{len(info.get('actions', []))} actions; socket [dim]{sim.socket_path}[/dim]")
    if detach:
        console.print(f"  drive it with [bold]visor sim shot {sim.name}[/bold], "
                      f"[bold]visor sim press right trigger[/bold], stop with [bold]visor sim stop {sim.name}[/bold]")
        # Hand the process over: keep it alive after we exit.
        sim.proc = None
        return
    console.print("  [dim]ctrl-c stops it[/dim]")
    try:
        with Live(console=console, refresh_per_second=2, transient=True) as live:
            while sim.alive():
                try:
                    st = xr.stats(window=144)
                    state = xr.state()
                    line = (f"[green]●[/green] frame {int(state['render_frame'])}  "
                            f"{st['fps']:.0f} fps  p99 {st['frame_ms_p99']:.1f} ms  "
                            f"missed {int(st['missed_frames'])}"
                            + ("  [yellow]paused[/yellow]" if state.get("paused") else ""))
                except Exception:
                    line = "[yellow]●[/yellow] waiting…"
                live.update(line)
                time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    crash = sim.crash() if not sim.alive() else None
    code = sim.stop()
    if crash:
        console.print(Panel(sim.crash_summary(), title="[red]crashed[/red]", border_style="red"))
        raise typer.Exit(1)
    console.print(f"[dim]stopped (exit {code})[/dim]")


@sim_app.command("ls")
def list_running():
    """List apps running in the virtual headset (and any app with the Visor layer)."""
    from visor.xr import running

    apps = running()
    if not apps:
        console.print("[dim]nothing running — start something with[/dim] visor sim run demo")
        return
    console.print(_status_table(apps))


@sim_app.command()
def stop(
    target: Optional[str] = typer.Argument(None, help="Instance name (default: the only one)"),
    all_: bool = typer.Option(False, "--all", help="Stop every managed instance"),
):
    """Stop an app started by `visor sim run`."""
    from visor.xr import running

    apps = [a for a in running() if a.get("managed")]
    if not all_:
        if target:
            apps = [a for a in apps if a["name"] == target or a["name"].startswith(target)]
        elif len(apps) > 1:
            _die("several are running — name one, or pass --all")
    if not apps:
        _die("nothing to stop")
    from visor.xr.sim import stop_instance

    for a in apps:
        outcome = _xr(stop_instance, a["name"])
        console.print(f"[dim]{outcome}[/dim] {a['name']}")


@sim_app.command()
def info(target: Optional[str] = typer.Argument(None, help="Instance name (default: the only one)")):
    """What the app asked for: interaction profile, actions and their bindings."""
    xr = _client(target)
    data = _xr(xr.info)
    app_info = data.get("app", {})
    session = data.get("session", {})
    console.print(f"[bold]{app_info.get('name')}[/bold] [dim]{app_info.get('engine')} · OpenXR "
                  f"{app_info.get('api_version')} · {data.get('runtime')}[/dim]")
    console.print(f"session [bold]{session.get('state')}[/bold] · {session.get('refresh_hz', 0):.0f} Hz · "
                  f"profile {session.get('interaction_profile') or '—'}")
    t = Table(box=None, header_style="bold dim")
    t.add_column("action")
    t.add_column("type")
    t.add_column("bound to")
    for a in data.get("actions", []):
        if not a["bindings"]:
            continue
        bound = ", ".join(f"{b['hand'][0].upper()} {b['input']}{'/' + b['component'] if b['component'] else ''}"
                          for b in a["bindings"])
        t.add_row(a["name"], a["type"], bound)
    console.print(t)


@sim_app.command()
def shot(
    target: Optional[str] = typer.Argument(None, help="Instance name (default: the only one)"),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="PNG path (default ./sim-<frame>.png)"),
    both: bool = typer.Option(False, "--both", help="Both eyes side by side"),
):
    """Screenshot what the virtual headset sees."""
    xr = _client(target)
    if both:
        _xr(xr.configure, eyes="both")
    frame = _xr(xr.capture)
    path = output or Path(f"sim-{frame.frame}.png")
    frame.image.save(path)
    console.print(f"{path} [dim]({frame.width}×{frame.height}, frame {frame.frame})[/dim]")


@sim_app.command()
def look(
    yaw: float = typer.Argument(..., help="Degrees left (+) / right (-)"),
    pitch: float = typer.Argument(0.0, help="Degrees up (+) / down (-)"),
    target: Optional[str] = typer.Option(None, "--app", "-a"),
    over: int = typer.Option(24, "--over", help="Frames to turn over (0 snaps)"),
):
    """Turn the head to an absolute yaw/pitch."""
    _xr(_client(target).look, yaw, pitch, over=over)


@sim_app.command()
def press(
    hand: str = typer.Argument(..., help="left | right"),
    button: str = typer.Argument("trigger", help="trigger, grip, a, b, x, y, menu, stick"),
    frames: int = typer.Option(6, "--frames", help="How long to hold it"),
    target: Optional[str] = typer.Option(None, "--app", "-a"),
):
    """Press and release a controller button."""
    _xr(_client(target).press, hand, button, frames=frames)


@sim_app.command()
def click(
    x: float = typer.Argument(..., help="Pixel x in the last screenshot (left eye)"),
    y: float = typer.Argument(..., help="Pixel y"),
    width: int = typer.Option(960, "--width", help="Screenshot width the pixel refers to"),
    height: Optional[int] = typer.Option(None, "--height", help="Screenshot height (default: from the app)"),
    hand: str = typer.Option("right", "--hand"),
    target: Optional[str] = typer.Option(None, "--app", "-a"),
):
    """Point a controller at a pixel and pull the trigger — tap what you see."""
    xr = _client(target)
    if height is None:
        frame = _xr(xr.capture, fresh=False)
        height = round(frame.height * width / frame.width) if frame.eyes != 3 else round(frame.height * width * 2 / frame.width)
    aim = _xr(xr.click_pixel, x, y, width, height, hand=hand)
    console.print(f"[dim]aimed {hand} at yaw {aim['ypr'][0]:.1f}°, pitch {aim['ypr'][1]:.1f}°[/dim]")


@sim_app.command()
def stick(
    hand: str = typer.Argument(..., help="left | right"),
    x: float = typer.Argument(...),
    y: float = typer.Argument(...),
    frames: int = typer.Option(36, "--frames"),
    target: Optional[str] = typer.Option(None, "--app", "-a"),
):
    """Push a thumbstick for a while (e.g. `stick left 0 1` walks forward)."""
    _xr(_client(target).stick, hand, x, y, frames=frames)


@sim_app.command("set")
def set_pose(
    patch: str = typer.Argument(..., help='JSON patch, e.g. \'{"right": {"pos": [0.2, 1.4, -0.4], "trigger": 1}}\''),
    target: Optional[str] = typer.Option(None, "--app", "-a"),
):
    """Set head/controller poses and inputs directly (the layer's raw patch)."""
    try:
        data = json.loads(patch)
    except ValueError as e:
        _die(f"not JSON: {e}")
    _xr(_client(target).set, **data)


@sim_app.command()
def pause(target: Optional[str] = typer.Argument(None)):
    """Freeze the app (it waits at the start of its next frame)."""
    _xr(_client(target).pause)


@sim_app.command()
def resume(target: Optional[str] = typer.Argument(None)):
    """Let a paused app run again."""
    _xr(_client(target).resume)


@sim_app.command()
def step(
    frames: int = typer.Argument(1, help="Frames to advance"),
    target: Optional[str] = typer.Option(None, "--app", "-a"),
):
    """Advance a paused app by exactly N frames."""
    r = _xr(_client(target).step, frames)
    console.print(f"[dim]now at frame {int(r['frame'])}[/dim]")


@sim_app.command()
def event(
    name: str = typer.Argument(..., help="focus_lost | focus_gained | recenter | exit"),
    target: Optional[str] = typer.Option(None, "--app", "-a"),
):
    """Simulate a system event: open the Quest menu (focus_lost), recenter, quit."""
    _xr(_client(target).session_event, name)


@sim_app.command()
def record(
    target: Optional[str] = typer.Argument(None),
    output: Path = typer.Option(Path("sim.mp4"), "--output", "-o", help="Video path"),
    seconds: float = typer.Option(10.0, "--seconds", "-t"),
    fps: int = typer.Option(24, "--fps"),
    script: Optional[Path] = typer.Option(None, "--script", help="Also save the input timeline as a replay script"),
):
    """Record a video of the virtual headset (and optionally the inputs, for replay)."""
    from visor.xr.video import Recorder

    xr = _client(target)
    if script:
        _xr(xr.record_start)
    rec = Recorder(xr, output, fps=fps)
    _xr(rec.start)
    with console.status(f"recording {seconds:.0f}s"):
        try:
            time.sleep(seconds)
        except KeyboardInterrupt:
            pass
    rec.stop()
    console.print(f"{output} [dim]({rec.frames_written} frames, {rec.unique_frames} unique)[/dim]")
    if script:
        data = _xr(xr.record_stop)
        script.write_text(json.dumps({"visor_replay": 1, **data}, indent=1))
        console.print(f"{script} [dim]({len(data['events'])} input changes over {data['frames']} frames)[/dim]")


@sim_app.command()
def replay(
    script: Path = typer.Argument(..., exists=True, help="A replay script from `visor sim record --script`"),
    target: Optional[str] = typer.Option(None, "--app", "-a"),
):
    """Play back a recorded input timeline, frame for frame."""
    data = json.loads(script.read_text())
    xr = _client(target)
    r = _xr(xr.replay, data)
    late = int(r.get("late_frames", 0))
    console.print(f"[dim]scheduled {int(r['scheduled'])} input changes from frame {int(r['start_frame'])}"
                  f"{f' ({late} frames late: the app was already past the start)' if late else ''}[/dim]")
