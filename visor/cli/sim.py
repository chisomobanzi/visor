"""`visor sim` — the virtual headset from the terminal.

Run any OpenXR app headless inside a simulated Quest 3, then look around,
point, press, step frame by frame, record and replay — or hand it to an AI
playtester.
"""

from __future__ import annotations

import json
import os
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
    from visor.xr import voice as tts

    # Optional: without speech, apps still get a (silent) microphone and recordings still play.
    if tts.available():
        have = tts.installed()
        table.add_row("[green]✓[/green]", "speech", f"[dim]{', '.join(have) if have else 'voices download on first use'}[/dim]")
    else:
        table.add_row("[yellow]–[/yellow]", "speech", "optional: pip install 'visor-dev[voice]'")
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
    mic: bool = typer.Option(False, "--mic", help="Let the app hear this computer's real microphone "
                             "(otherwise it hears only what `visor sim say` plays it)"),
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
    if mic:
        console.print("  [dim]microphone:[/dim] this computer's")
    elif sim.mic_proc:
        console.print("  [dim]microphone:[/dim] Visor's — talk to it with [bold]visor sim say \"hello\"[/bold]")
    for note in sim.notes:
        console.print(f"  [yellow]{note}[/yellow]")
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
    """Play back a recorded input timeline (and anything said), frame for frame."""
    from visor.xr.mic import MicError

    data = json.loads(script.read_text())
    xr = _client(target)
    try:
        r = _xr(xr.replay, data, base_dir=script.resolve().parent)
    except MicError as e:
        _die(str(e))
    late = int(r.get("late_frames", 0))
    said = f", {r['voice']} spoken lines" if r.get("voice") else ""
    console.print(f"[dim]scheduled {int(r['scheduled'])} input changes{said} from frame {int(r['start_frame'])}"
                  f"{f' ({late} frames late: the app was already past the start)' if late else ''}[/dim]")


# --------------------------------------------------------------------------
# the voice


@sim_app.command()
def say(
    text: Optional[str] = typer.Argument(None, help="What to say (spoken with offline text-to-speech)"),
    target: Optional[str] = typer.Option(None, "--app", "-a"),
    file: Optional[Path] = typer.Option(None, "--file", "-f", exists=True, help="Play a recording instead"),
    language: Optional[str] = typer.Option(None, "--lang", "-l", help="Speak this language: es, fr, de, pt-BR…"),
    voice: Optional[str] = typer.Option(None, "--voice", help="A Piper voice name, e.g. en_GB-alba-medium"),
    volume: float = typer.Option(1.0, "--volume", help="0.3 is softly spoken, 2 is loud"),
    speed: float = typer.Option(1.0, "--speed", help="Speaking rate (0.5–2)"),
    wait: bool = typer.Option(False, "--wait", "-w", help="Return when it's been said"),
):
    """Speak into the app's microphone. It hears only what Visor plays it."""
    from visor.xr import voice as tts
    from visor.xr.mic import MicError

    if not text and not file:
        _die("say something, or pass --file")
    xr = _client(target)
    try:
        if file:
            r = xr.play_audio(file, volume=volume)
        else:
            with console.status("finding the words") as status:
                def progress(msg: str) -> None:
                    status.update(f"[bold]{msg}[/bold]")

                speech = tts.synthesize(text, voice=voice, language=language, speed=speed, log=progress)
                r = xr.mic().play(speech.path, volume=volume, label=speech.text, kind="speech")
                r.update(text=speech.text, voice=speech.voice)
    except (tts.VoiceError, MicError) as e:
        _die(str(e))
    what = f"“{r['text']}”" if r.get("text") else str(file)
    console.print(f"[bold cyan]🗣[/bold cyan] {what} [dim]({r['duration']:.1f}s{', ' + r['voice'] if r.get('voice') else ''})[/dim]")
    if r.get("world") == "paused":
        console.print("  [yellow]the world is paused[/yellow] — it'll be heard when it runs "
                      "([bold]visor sim resume[/bold] or [bold]visor sim step 72[/bold])")
    elif not r.get("listening"):
        console.print("  [dim]the app isn't listening right now (no microphone stream open), so it hears nothing[/dim]")
    if wait:
        time.sleep(max(0.0, float(r.get("starts_in", 0.0))) + float(r["duration"]))


@sim_app.command()
def mic(target: Optional[str] = typer.Argument(None, help="Instance name (default: the only one)")):
    """Is the app listening? What has it been told?"""
    from visor.xr.mic import MicError

    xr = _client(target)
    try:
        st = xr.mic().status()
    except MicError as e:
        _die(str(e))
    if st["listening"]:
        for s in st["streams"]:
            console.print(f"[green]●[/green] listening — {s['client'] or 'the app'} "
                          f"[dim]{s['rate']} Hz × {s['channels']}{(' · ' + s['name']) if s['name'] else ''}[/dim]")
    else:
        console.print("[dim]○ not listening (no microphone stream open)[/dim]")
    if st["speaking"]:
        console.print(f"[cyan]🗣 speaking[/cyan] [dim]({st['remaining_s']:.1f}s left)[/dim]")
    if st["world"] == "paused":
        console.print("[yellow]world paused[/yellow] [dim]— the microphone waits with it[/dim]")
    for h in st["history"][-8:]:
        console.print(f"  [dim]{time.strftime('%H:%M:%S', time.localtime(h['at']))}[/dim] {h['label'] or '(audio)'} "
                      f"[dim]{h['duration']:.1f}s[/dim]")


@sim_app.command()
def voices(
    get: Optional[str] = typer.Option(None, "--get", help="Download a voice now: a language (es) or a Piper voice name"),
):
    """Voices for `visor sim say`: what's installed, and one for each language."""
    from visor.xr import voice as tts

    if not tts.available():
        console.print("[yellow]no text-to-speech engine[/yellow] — pip install 'visor-dev[voice]' "
                      "[dim](recordings still work: visor sim say --file hello.wav)[/dim]")
    if get:
        try:
            name = tts.resolve(voice=get if "-" in get else None, language=None if "-" in get else get)
            with console.status(f"downloading {name}"):
                tts.ensure(name)
        except tts.VoiceError as e:
            _die(str(e))
        console.print(f"[green]✓[/green] {name}")
    have = set(tts.installed())
    t = Table(box=None, header_style="bold dim")
    t.add_column("lang")
    t.add_column("voice")
    t.add_column("")
    for lang, name in sorted(tts.LANGUAGE_VOICES.items()):
        t.add_row(lang, name, "[green]installed[/green]" if name in have else "[dim]downloads on first use[/dim]")
    for name in sorted(have - set(tts.LANGUAGE_VOICES.values())):
        t.add_row("", name, "[green]installed[/green]")
    console.print(t)
    console.print("[dim]any voice from https://huggingface.co/rhasspy/piper-voices works: --voice <name>[/dim]")


@sim_app.command("mic-server", hidden=True)
def mic_server(name: str = typer.Argument(...)):
    """Serve one app's microphone (started by `visor sim run`)."""
    from visor.xr.mic import main as mic_main

    raise typer.Exit(mic_main([name]))


# --------------------------------------------------------------------------
# autonomous playtests


@sim_app.command()
def personas():
    """The playtester personas and what each is best at catching."""
    from visor.xr.playtest.personas import PERSONAS

    t = Table(box=None, header_style="bold dim")
    t.add_column("persona")
    t.add_column("who")
    t.add_column("catches")
    for p in PERSONAS.values():
        t.add_row(f"[bold]{p.key}[/bold]", p.tagline, ", ".join(p.watch_for[:3]))
    console.print(t)


@sim_app.command()
def playtest(
    target: str = typer.Argument(..., help="A .uproject, packaged build, OpenXR executable, or 'demo'"),
    persona: str = typer.Option("first-timer", "--persona", "-p", help="See `visor sim personas`"),
    goal: Optional[str] = typer.Option(None, "--goal", "-g", help="What the playtester should try to do"),
    brain: str = typer.Option("claude", "--brain", help="claude (an AI playtester) | monkey (seeded random input)"),
    model: str = typer.Option("claude-opus-5-5", "--model"),
    effort: str = typer.Option("medium", "--effort", help="low | medium | high | xhigh | max"),
    steps: int = typer.Option(40, "--steps", "-n", help="Maximum actions"),
    minutes: float = typer.Option(20.0, "--minutes", help="Wall-clock limit"),
    seed: int = typer.Option(0, "--seed", help="Seed for the monkey brain"),
    map_name: Optional[str] = typer.Option(None, "--map", "-m"),
    headset: str = typer.Option("quest3", "--headset"),
    no_video: bool = typer.Option(False, "--no-video"),
    open_report: bool = typer.Option(False, "--open", help="Open the HTML report when done"),
):
    """Let an autonomous playtester play the app and write a bug report.

    Each run produces a report with every issue (screenshot, repro steps), a
    replay script that reproduces the session, a video, a frame-timing trace
    and the crash log if it crashed."""
    from visor.xr.playtest.session import DEFAULT_GOAL, Playtest, PlaytestConfig

    if brain == "claude" and not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
                                  or Path.home().joinpath(".config", "anthropic").exists()):
        _die("the Claude playtester needs Anthropic credentials (export ANTHROPIC_API_KEY, or `ant auth login`). "
             "Try --brain monkey for a model-free run.")
    cfg = PlaytestConfig(target=target, persona=persona, goal=goal or DEFAULT_GOAL, brain=brain, model=model,
                         effort=effort, max_steps=steps, max_minutes=minutes, seed=seed, map=map_name,
                         headset=headset, video=not no_video)
    sev_style = {"blocker": "bold white on red", "major": "bold red", "minor": "yellow", "note": "dim"}
    status = console.status("starting")
    status.start()

    def on_event(kind: str, data: dict) -> None:
        if kind == "status":
            status.update(f"[bold]{data['message']}[/bold]")
        elif kind == "action":
            status.update(f"step {data['step']}/{steps}")
            args = ", ".join(f"{k}={v}" for k, v in data["args"].items())
            if data.get("thought"):
                console.print(f"  [dim italic]{data['thought'][:160]}[/dim italic]")
            console.print(f"  [bold cyan]{data['step']:>3}[/bold cyan] {data['action']}[dim]({args})[/dim]")
        elif kind == "issue":
            i = data["issue"]
            console.print(f"      [{sev_style.get(i['severity'], '')}] {i['severity'].upper()} [/] {i['title']}")
        elif kind == "error":
            console.print(f"  [red]{data['message']}[/red]")

    try:
        test = Playtest(cfg, on_event=on_event)
    except ValueError as e:
        status.stop()
        _die(str(e))
    console.print(f"[bold]{test.persona.name}[/bold] playtests [bold]{target}[/bold] "
                  f"[dim]({brain}{'' if brain != 'claude' else ' · ' + model}, up to {steps} actions)[/dim]")
    try:
        report = test.run()
    except KeyboardInterrupt:
        status.stop()
        console.print("[yellow]stopped early[/yellow]")
        raise typer.Exit(130)
    finally:
        status.stop()
    v = report.get("verdict") or {}
    issues = report.get("issues", [])
    counts = {s: sum(1 for i in issues if i["severity"] == s) for s in ("blocker", "major", "minor", "note")}
    summary = " · ".join(f"{n} {s}" for s, n in counts.items() if n) or "no issues"
    rating = v.get("rating")
    achieved = v.get("goal_achieved")
    outcome = ("goal reached" if achieved else "goal not reached") if achieved is not None else "scripted run"
    console.print(Panel(
        f"{v.get('summary', '')}\n\n[bold]{summary}[/bold]   "
        f"{f'rating {rating}/10   ' if rating is not None else ''}{outcome}",
        title=f"{report['persona']['name']} · {report['app']['name']}", expand=False))
    html = test.dir / "report.html"
    console.print(f"report  file://{html}")
    if open_report:
        import webbrowser

        webbrowser.open(f"file://{html}")


@sim_app.command()
def fleet(
    target: str = typer.Argument(..., help="What to run in every copy (.uproject, build, executable, 'demo')"),
    count: int = typer.Option(4, "--count", "-n", help="How many copies"),
    playtest: bool = typer.Option(False, "--playtest", help="Playtest each copy instead of just soaking it"),
    personas: str = typer.Option("first-timer", "--personas", help="Comma-separated, assigned round-robin"),
    brain: str = typer.Option("monkey", "--brain", help="monkey (free, seeded) | claude"),
    steps: int = typer.Option(30, "--steps", help="Actions per playtest"),
    seconds: float = typer.Option(30.0, "--seconds", help="Soak length per copy"),
    scale: float = typer.Option(0.5, "--scale", help="Eye-buffer scale per copy"),
    concurrency: int = typer.Option(0, "--concurrency", "-j", help="Copies at once (default: all)"),
    map_name: Optional[str] = typer.Option(None, "--map", "-m"),
    open_report: bool = typer.Option(False, "--open"),
):
    """Run many isolated copies at once — soak them, or playtest each with its own persona and seed."""
    from visor.xr.fleet import Fleet, FleetConfig

    cfg = FleetConfig(target=target, count=count, mode="playtest" if playtest else "soak", seconds=seconds,
                      concurrency=concurrency, scale=scale, map=map_name,
                      personas=[p.strip() for p in personas.split(",") if p.strip()], brain=brain, steps=steps)
    status_style = {"queued": "dim", "starting": "yellow", "running": "cyan", "done": "green",
                    "crashed": "bold red", "failed": "red"}

    def table(results) -> Table:
        t = Table(box=None, header_style="bold dim")
        for col in ("#", "status", "persona", "fps", "p99", "missed", "progress", "issues", "time"):
            t.add_column(col)
        for r in results:
            prog = (f"step {r.frame}" if playtest else f"frame {r.frame}") if r.frame else ""
            t.add_row(f"{r.index:02d}", f"[{status_style.get(r.status, '')}]{r.status}[/]", r.persona or "",
                      f"{r.fps:.0f}" if r.fps else "", f"{r.p99_ms:.0f} ms" if r.p99_ms else "",
                      str(r.missed) if r.fps else "", prog, str(r.issues or ""), f"{r.seconds:.0f}s" if r.seconds else "")
        return t

    f = Fleet(cfg)
    with Live(table(f.results), console=console, refresh_per_second=2) as live:
        f.on_update = lambda results: live.update(table(results))
        try:
            summary = f.run()
        except KeyboardInterrupt:
            f.stop()
            raise typer.Exit(130)
    console.print(f"[bold]{summary['ok']}/{count}[/bold] completed · {summary['crashed']} crashed · "
                  f"mean {summary['mean_fps']} fps · {len(summary['issues'])} distinct issues · "
                  f"{summary['elapsed_s'] / 60:.1f} min")
    page = f.dir / "fleet.html"
    console.print(f"report  file://{page}")
    if open_report:
        import webbrowser

        webbrowser.open(f"file://{page}")
