"""MCP tools for the virtual headset: let an AI agent play VR apps.

The loop these tools are built for: look at the screen (xr_screenshot), act on
what's there (xr_click on a pixel, xr_press, xr_stick, xr_look), and see the
result in the same call. Pixel coordinates always refer to the last screenshot
returned for that app. While an app is paused (xr_pause) the world stands
still between calls — take as long as you like to think — and each action
steps it just far enough to show its effect.
"""

from __future__ import annotations

import io
import json
import time
from pathlib import Path
from typing import Optional

from mcp.server.mcpserver import Image
from PIL import Image as PILImage
from PIL import ImageDraw

from visor.mcp.server import QuestError, mcp
from visor.xr import AppSpec, SimConfig, SimError, SimInstance, XrClient, XrError, connect, running

_MAX_WIDTH = 1024
# Per app (socket path): size of the last image the model saw, and the sims we started.
_last_shot: dict[str, tuple[int, int]] = {}
_sims: dict[str, SimInstance] = {}


def _xr(app: Optional[str]) -> XrClient:
    try:
        return connect(app)
    except SimError as e:
        raise QuestError(str(e)) from None


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except (XrError, SimError, ValueError) as e:
        raise QuestError(str(e)) from None


def _grid(img: PILImage.Image) -> PILImage.Image:
    img = img.copy()
    d = ImageDraw.Draw(img)
    w, h = img.size
    for i in range(1, 10):
        x, y = w * i // 10, h * i // 10
        d.line([(x, 0), (x, h)], fill=(255, 255, 255), width=1)
        d.line([(0, y), (w, y)], fill=(255, 255, 255), width=1)
        d.text((x + 2, 2), str(x), fill=(255, 255, 0))
        d.text((2, y + 2), str(y), fill=(255, 255, 0))
    return img


def _shot(xr: XrClient, note: str = "", grid: bool = False, both: bool = False, save_path: Optional[str] = None):
    frame = _call(xr.capture)
    img = frame.image if both else frame.eye("left")
    if save_path:
        img.save(save_path)
    if img.width > _MAX_WIDTH:
        img = img.resize((_MAX_WIDTH, round(img.height * _MAX_WIDTH / img.width)), PILImage.LANCZOS)
    _last_shot[xr.socket_path] = (img.width if not both else img.width // 2, img.height)
    if grid:
        img = _grid(img)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    st = xr.state()
    head = st["head"]
    meta = (f"{note}{' — ' if note else ''}frame {frame.frame}, {img.width}x{img.height} "
            f"({'both eyes' if both else 'left eye'}); head yaw {head['ypr'][0]:.0f}° pitch {head['ypr'][1]:.0f}°"
            f"{', PAUSED' if st.get('paused') else ''}. Pixel coordinates for xr_click refer to this image.")
    return [Image(data=buf.getvalue(), format="jpeg"), meta]


def _settle(xr: XrClient, frames: int) -> None:
    """Let an action play out: step if paused, else wait the real time."""
    if xr.state().get("paused"):
        _call(xr.step, frames)
    else:
        time.sleep(frames / 72.0 + 0.1)


def _dims(xr: XrClient) -> tuple[int, int]:
    if xr.socket_path in _last_shot:
        return _last_shot[xr.socket_path]
    frame = _call(xr.capture, fresh=False)
    img = frame.eye("left")
    w = min(img.width, _MAX_WIDTH)
    return w, round(img.height * w / img.width)


# --------------------------------------------------------------------------
# lifecycle


@mcp.tool()
def xr_launch(
    target: str,
    map: Optional[str] = None,
    headset: str = "quest3",
    paused: bool = False,
    seated: bool = False,
    name: Optional[str] = None,
    wait_seconds: int = 300,
) -> dict:
    """Start a VR app inside Visor's virtual headset (a headless, simulated
    Quest 3 on this computer) so it can be played with the other xr_* tools.

    target: a .uproject (Unreal, runs in -game -vr mode), a packaged Linux build
    (.sh), any OpenXR executable, or "demo". `paused` starts it frozen so you
    can step frame by frame. Waits until the app's VR session is running
    (Unreal projects can take a few minutes the first time, compiling shaders)."""
    spec = _call(AppSpec.resolve, target, map_name=map)
    sim = _call(SimInstance, spec, SimConfig(headset=headset, paused=paused, eye_height=1.2 if seated else 1.65),
                name=name)
    _call(sim.start)
    try:
        xr = sim.wait_ready(timeout=wait_seconds)
    except SimError as e:
        sim.stop()
        raise QuestError(str(e)) from None
    _sims[sim.name] = sim
    info = xr.info()
    bound = [a for a in info.get("actions", []) if a["bindings"]]
    return {
        "name": sim.name,
        "app": spec.name,
        "headset": sim.headset.name,
        "interaction_profile": info.get("session", {}).get("interaction_profile"),
        "bound_actions": len(bound),
        "microphone": "visor (xr_say speaks into it)" if sim.mic_proc else "none",
        "notes": sim.notes,
        "run_dir": str(sim.run_dir),
        "next": "xr_screenshot to see it; xr_actions to learn its controls",
    }


@mcp.tool()
def xr_list() -> list[dict]:
    """Apps currently running in the virtual headset (or with the Visor layer)."""
    return running()


@mcp.tool()
def xr_stop(app: Optional[str] = None) -> dict:
    """Stop an app started with xr_launch, and report whether it had crashed."""
    xr = _xr(app)
    name = next((a["name"] for a in running() if a["socket"] == xr.socket_path), app)
    sim = _sims.pop(name or "", None)
    if sim is None:
        from visor.xr.sim import stop_instance

        return {"stopped": name, "outcome": _call(stop_instance, name)}
    crash = sim.crash()
    code = sim.stop()
    return {"stopped": name, "exit_code": code, "crash": crash}


# --------------------------------------------------------------------------
# seeing


@mcp.tool()
def xr_screenshot(app: Optional[str] = None, both_eyes: bool = False, grid: bool = False,
                  save_path: Optional[str] = None) -> list:
    """See what the headset sees right now (left eye by default). `grid`
    overlays a labelled 10% grid, handy for picking pixels to click."""
    xr = _xr(app)
    if both_eyes:
        _call(xr.configure, eyes="both")
    return _shot(xr, grid=grid, both=both_eyes, save_path=save_path)


@mcp.tool()
def xr_actions(app: Optional[str] = None, only_bound: bool = True) -> list[dict]:
    """The app's controls: every OpenXR action it defined, what controller
    input it is bound to (e.g. 'R trigger/value') and its current state.
    Use the names with xr_press_action."""
    xr = _xr(app)
    out = []
    for a in _call(xr.actions):
        if only_bound and not a["bindings"]:
            continue
        out.append({
            "name": a["name"],
            "type": a["type"],
            "bound_to": [f"{b['hand']} {b['input']}{'/' + b['component'] if b['component'] else ''}"
                         for b in a["bindings"]],
            "state": a.get("state"),
        })
    return out


# --------------------------------------------------------------------------
# acting


@mcp.tool()
def xr_click(x: float, y: float, hand: str = "right", app: Optional[str] = None, screenshot: bool = True) -> list:
    """Point a controller at pixel (x, y) of the last screenshot and pull the
    trigger — how you press buttons and UI in VR. Returns the view afterwards.
    If the app's laser lands off target, check the screenshot and adjust."""
    xr = _xr(app)
    w, h = _dims(xr)
    aim = _call(xr.click_pixel, x, y, w, h, hand=hand)
    _settle(xr, 24)
    note = f"clicked ({x:.0f},{y:.0f}) with {hand} (aim yaw {aim['ypr'][0]:.0f}°, pitch {aim['ypr'][1]:.0f}°)"
    return _shot(xr, note) if screenshot else [note]


@mcp.tool()
def xr_point(x: float, y: float, hand: str = "right", app: Optional[str] = None) -> list:
    """Aim a controller at pixel (x, y) without pressing — hover, check where
    the app's laser lands, then xr_press the trigger."""
    xr = _xr(app)
    w, h = _dims(xr)
    _call(xr.point_at_pixel, hand, x, y, w, h)
    _settle(xr, 6)
    return _shot(xr, f"{hand} controller aimed at ({x:.0f},{y:.0f})")


@mcp.tool()
def xr_press(button: str, hand: str = "right", frames: int = 6, app: Optional[str] = None,
             screenshot: bool = True) -> list:
    """Press and release a controller button: trigger, grip, a, b (right
    hand), x, y, menu (left hand), stick (thumbstick click). `frames` is how
    long it's held (72 frames = 1 second)."""
    xr = _xr(app)
    _call(xr.press, hand, button, frames=frames)
    _settle(xr, frames + 12)
    note = f"pressed {hand} {button} for {frames} frames"
    return _shot(xr, note) if screenshot else [note]


@mcp.tool()
def xr_hold(button: str, hand: str = "right", down: bool = True, app: Optional[str] = None) -> str:
    """Hold a button down (down=true) or let it go (down=false), e.g. to keep
    gripping an object while moving the hand."""
    _call(_xr(app).hold, hand, button, down)
    return f"{hand} {button} {'held' if down else 'released'}"


@mcp.tool()
def xr_stick(x: float, y: float, hand: str = "left", frames: int = 36, app: Optional[str] = None,
             screenshot: bool = True) -> list:
    """Push a thumbstick (x right, y forward, -1..1) for some frames. Left
    stick usually moves, right stick usually turns or teleports."""
    xr = _xr(app)
    _call(xr.stick, hand, x, y, frames=frames)
    _settle(xr, frames + 6)
    note = f"{hand} stick ({x:+.1f},{y:+.1f}) for {frames} frames"
    return _shot(xr, note) if screenshot else [note]


@mcp.tool()
def xr_look(yaw: float, pitch: float = 0.0, app: Optional[str] = None, over_frames: int = 24,
            screenshot: bool = True) -> list:
    """Turn the head to an absolute direction: yaw degrees left (+) or right
    (-) of where you started facing, pitch up (+) or down (-). Turns
    smoothly over `over_frames`."""
    xr = _xr(app)
    _call(xr.look, yaw, max(-89.0, min(89.0, pitch)), over=over_frames)
    _settle(xr, over_frames + 4)
    return _shot(xr, f"looking yaw {yaw:.0f}°, pitch {pitch:.0f}°") if screenshot else ["turned"]


@mcp.tool()
def xr_walk(forward: float = 0.0, right: float = 0.0, app: Optional[str] = None, over_frames: int = 48) -> list:
    """Physically step (room-scale) in metres, relative to where you face."""
    xr = _xr(app)
    _call(xr.walk, forward=forward, right=right, over=over_frames)
    _settle(xr, over_frames + 4)
    return _shot(xr, f"walked {forward:+.1f} m forward, {right:+.1f} m right")


@mcp.tool()
def xr_move_hand(hand: str, pos: list[float], yaw: float = 0.0, pitch: float = 0.0,
                 app: Optional[str] = None, over_frames: int = 12) -> list:
    """Place a controller at a stage-space position [x, y, z] in metres (y up,
    -z forward, floor at y=0) pointing yaw/pitch — for grabbing and reaching."""
    xr = _xr(app)
    _call(xr.set, **{hand: {"pos": pos, "ypr": [yaw, pitch, 0.0]}}, over=over_frames)
    _settle(xr, over_frames + 2)
    return _shot(xr, f"{hand} hand at {[round(v, 2) for v in pos]}")


@mcp.tool()
def xr_press_action(name: str, frames: int = 6, app: Optional[str] = None) -> list:
    """Trigger one of the app's own actions by name (see xr_actions), whatever
    button it is bound to."""
    xr = _xr(app)
    _call(xr.press_action, name, frames=frames)
    _settle(xr, frames + 12)
    return _shot(xr, f"pressed action {name}")


# --------------------------------------------------------------------------
# time


@mcp.tool()
def xr_pause(app: Optional[str] = None) -> str:
    """Freeze the app between frames. Nothing moves until xr_step / xr_resume,
    so you can think as long as you need. Actions then step the world just
    far enough to show their result."""
    _call(_xr(app).pause)
    return "paused"


@mcp.tool()
def xr_resume(app: Optional[str] = None) -> str:
    """Let a paused app run in real time again."""
    _call(_xr(app).resume)
    return "running"


@mcp.tool()
def xr_step(frames: int = 1, app: Optional[str] = None, screenshot: bool = True) -> list:
    """Advance a paused app by exactly `frames` frames (72 = one second)."""
    xr = _xr(app)
    r = _call(xr.step, frames)
    return _shot(xr, f"stepped to frame {int(r['frame'])}") if screenshot else [f"frame {int(r['frame'])}"]


# --------------------------------------------------------------------------
# watching


@mcp.tool()
def xr_stats(app: Optional[str] = None) -> dict:
    """Frame timing over the last few seconds: fps vs the refresh rate,
    p50/p95/p99 frame time, CPU time per frame, missed frames."""
    return _call(_xr(app).stats)


@mcp.tool()
def xr_events(since: int = 0, app: Optional[str] = None) -> dict:
    """What the app did that isn't on screen: haptic pulses (with action and
    hand), session state changes, interaction profile changes. Pass the last
    'latest' as `since` to get only new ones."""
    return _call(_xr(app).events, since)


@mcp.tool()
def xr_system_event(event: str, app: Optional[str] = None) -> list:
    """Simulate the Quest system: 'focus_lost' (user opened the Meta menu —
    apps must pause), 'focus_gained', 'recenter' (long-press of the Meta
    button), 'exit' (user quit from the menu — apps must exit cleanly)."""
    xr = _xr(app)
    _call(xr.session_event, event)
    if event == "exit":
        return ["asked the app to exit"]
    _settle(xr, 18)
    return _shot(xr, f"sent {event}")


@mcp.tool()
def xr_logs(app: Optional[str] = None, lines: int = 80, pattern: Optional[str] = None) -> dict:
    """The app's own log (stdout/stderr), and a crash summary if it crashed."""
    from visor.xr.sim import detect_crash

    xr = _xr(app)
    entry = next((a for a in running() if a["socket"] == xr.socket_path), None)
    run_dir = Path(entry["run_dir"]) if entry and entry.get("run_dir") else None
    if run_dir is None:
        raise QuestError("only apps started with xr_launch / visor sim run keep a log")
    text = (run_dir / "app.log").read_bytes()[-500_000:].decode("utf-8", "replace").splitlines()
    if pattern:
        text = [line for line in text if pattern.lower() in line.lower()]
    return {"log": "\n".join(text[-lines:]), "crash": detect_crash(run_dir)}


@mcp.tool()
def xr_record(seconds: float = 10.0, save_path: Optional[str] = None, app: Optional[str] = None,
              with_inputs: bool = True) -> dict:
    """Record a video of the next N seconds (plus a replay script of every
    input, so the same sequence can be played back exactly)."""
    from visor.xr.video import Recorder

    xr = _xr(app)
    out = Path(save_path or f"/tmp/visor-xr-{int(time.time())}.mp4")
    if with_inputs:
        _call(xr.record_start)
    rec = Recorder(xr, out)
    _call(rec.start)
    time.sleep(min(max(seconds, 1.0), 120.0))
    rec.stop()
    result = {"video": str(out), "frames": rec.frames_written}
    if with_inputs:
        data = _call(xr.record_stop)
        script = out.with_suffix(".replay.json")
        script.write_text(json.dumps({"visor_replay": 1, **data}, indent=1))
        result["replay_script"] = str(script)
    return result


@mcp.tool()
def xr_replay(script_path: str, app: Optional[str] = None) -> dict:
    """Play back a replay script (from xr_record or a playtest report) on a
    running app, frame for frame — including anything that was said."""
    from visor.xr.mic import MicError

    path = Path(script_path)
    data = json.loads(path.read_text())
    try:
        return _call(_xr(app).replay, data, base_dir=path.resolve().parent)
    except MicError as e:
        raise QuestError(str(e)) from None


# --------------------------------------------------------------------------
# the voice


@mcp.tool()
def xr_say(text: str, language: Optional[str] = None, voice: Optional[str] = None, volume: float = 1.0,
           app: Optional[str] = None, screenshot: bool = True) -> list:
    """Speak out loud into the headset's microphone: answer the app, pass a
    mic check, give a voice command. The app hears a natural voice (offline
    text-to-speech) and nothing else — never this computer's real microphone.
    `language` speaks another language (es, fr, de, pt, zh…); `volume` 0.3 is
    softly spoken, 2 is loud. If the app is paused, it runs while you speak."""
    from visor.xr import voice as tts
    from visor.xr.mic import MicError

    xr = _xr(app)
    try:
        r = xr.say(text, voice=voice, language=language, volume=volume)
    except (tts.VoiceError, MicError) as e:
        raise QuestError(str(e)) from None
    _settle(xr, round((float(r["duration"]) + 0.6) * 72))
    note = f"said “{r['text']}” ({r['duration']:.1f}s, {r['voice']})"
    if not r.get("listening"):
        note += " — the app had no microphone stream open, so it heard nothing"
    return _shot(xr, note) if screenshot else [note]


@mcp.tool()
def xr_mic(app: Optional[str] = None) -> dict:
    """Is the app listening (has it opened the microphone)? What has been
    said to it, and is anything still being spoken?"""
    from visor.xr.mic import MicError

    try:
        return _xr(app).mic().status()
    except MicError as e:
        raise QuestError(str(e)) from None
