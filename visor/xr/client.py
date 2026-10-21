"""Talk to a running OpenXR app through the Visor layer's control socket.

``XrClient`` is the whole remote control: the raw protocol (``call``) plus the
verbs an agent actually uses — look somewhere, point at what's on screen,
press a button, step the world forward — all in metres and degrees.

Coordinates are OpenXR stage space: +Y up, -Z forward, origin on the floor at
the play-area centre. Yaw turns left, pitch looks up.
"""

from __future__ import annotations

import json
import math
import socket
import threading
from pathlib import Path
from typing import Iterable, Optional, Sequence

from PIL import Image

from visor.xr import paths
from visor.xr.frames import Frame, FrameReader

Vec3 = Sequence[float]

# Agent-facing button names -> the rig's patch keys.
BUTTONS = {
    "trigger": "trigger",
    "squeeze": "squeeze",
    "grip": "squeeze",
    "a": "a",
    "b": "b",
    "x": "x",
    "y": "y",
    "primary": "a",
    "secondary": "b",
    "menu": "menu",
    "system": "menu",
    "stick": "stick_click",
    "thumbstick": "stick_click",
    "stick_click": "stick_click",
}
ANALOG = {"trigger", "squeeze"}


class XrError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Small pose maths (quaternions are [x, y, z, w])


def qrot(q: Sequence[float], v: Vec3) -> list[float]:
    x, y, z, w = q
    tx = 2 * (y * v[2] - z * v[1])
    ty = 2 * (z * v[0] - x * v[2])
    tz = 2 * (x * v[1] - y * v[0])
    return [
        v[0] + w * tx + (y * tz - z * ty),
        v[1] + w * ty + (z * tx - x * tz),
        v[2] + w * tz + (x * ty - y * tx),
    ]


def look_ypr(origin: Vec3, target: Vec3) -> list[float]:
    """Yaw/pitch (degrees) that point -Z from origin at target."""
    d = [t - o for t, o in zip(target, origin)]
    n = math.sqrt(sum(c * c for c in d)) or 1.0
    return [math.degrees(math.atan2(-d[0], -d[2])), math.degrees(math.asin(max(-1.0, min(1.0, d[1] / n)))), 0.0]


def _norm(v: Vec3) -> list[float]:
    n = math.sqrt(sum(c * c for c in v)) or 1.0
    return [c / n for c in v]


class XrClient:
    """A connection to one app. Thread-safe; reconnects if the socket drops."""

    def __init__(self, socket_path: str | Path, timeout: float = 15.0):
        self.socket_path = str(socket_path)
        self.timeout = timeout
        self._sock: Optional[socket.socket] = None
        self._file = None
        self._lock = threading.Lock()
        self._id = 0
        self._reader: Optional[FrameReader] = None
        self.fov_fallback = (54.0, 45.0, 47.0, 51.0)

    # -- plumbing ---------------------------------------------------------

    def _connect(self) -> None:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        try:
            s.connect(self.socket_path)
        except OSError as e:
            s.close()
            raise XrError(f"can't reach the app at {self.socket_path}: {e.strerror or e}") from None
        self._sock = s
        self._file = s.makefile("rwb")

    def close(self) -> None:
        with self._lock:
            if self._sock is not None:
                try:
                    self._sock.close()
                finally:
                    self._sock = None
                    self._file = None
            if self._reader is not None:
                self._reader.close()
                self._reader = None

    def __enter__(self) -> "XrClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def call(self, cmd: str, timeout: Optional[float] = None, **args):
        """Send one command; returns its result or raises XrError."""
        with self._lock:
            for attempt in (0, 1):
                if self._sock is None:
                    self._connect()
                assert self._sock is not None and self._file is not None
                self._sock.settimeout(timeout or self.timeout)
                self._id += 1
                line = json.dumps({"id": self._id, "cmd": cmd, "args": args}) + "\n"
                try:
                    self._file.write(line.encode())
                    self._file.flush()
                    raw = self._file.readline()
                except (OSError, socket.timeout) as e:
                    self._sock.close()
                    self._sock = None
                    if attempt == 0 and not isinstance(e, socket.timeout):
                        continue
                    raise XrError(f"{cmd}: {e}") from None
                if not raw:
                    self._sock.close()
                    self._sock = None
                    if attempt == 0:
                        continue
                    raise XrError(f"{cmd}: the app closed the connection (did it exit?)")
                reply = json.loads(raw)
                if not reply.get("ok"):
                    raise XrError(f"{cmd}: {reply.get('error', 'failed')}")
                return reply.get("result")
        raise XrError(f"{cmd}: no reply")

    # -- raw commands -----------------------------------------------------

    def ping(self) -> bool:
        try:
            return bool(self.call("ping", timeout=2).get("pong"))
        except XrError:
            return False

    def info(self) -> dict:
        return self.call("info")

    def state(self) -> dict:
        return self.call("state")

    def stats(self, window: int = 300, frames_since: Optional[int] = None) -> dict:
        args: dict = {"window": window}
        if frames_since is not None:
            args["frames_since"] = frames_since
        return self.call("stats", **args)

    def events(self, since: int = 0) -> dict:
        return self.call("events", since=since)

    def set(self, **patch) -> dict:
        """Queue a pose/input patch for the next frame, e.g.
        ``set(right={"trigger": 1.0}, head={"ypr": [30, 0, 0]}, over=36)``."""
        return self.call("set", **patch)

    def schedule(self, events: Iterable[dict], relative: bool = True) -> dict:
        return self.call("schedule", events=list(events), relative=relative)

    def replay(self, script: dict) -> dict:
        """Play back a recording. With its absolute start frame (and a fixed
        timestep) every input lands at the same game time as when recorded;
        older scripts without one start from the next frame."""
        start = script.get("start_frame")
        if start is None:
            return self.schedule(script["events"], relative=True)
        events = [{"frame": int(start) + int(e.get("frame", 0)), "set": e["set"]} for e in script["events"]]
        now = int(self.state().get("frame", 0))
        result = self.schedule(events, relative=False)
        result["start_frame"] = int(start)
        result["late_frames"] = max(0, now + 1 - int(start))
        return result

    def clear(self) -> None:
        self.call("clear")

    def reset(self, eye_height: Optional[float] = None) -> None:
        self.call("reset", **({"eye_height": eye_height} if eye_height else {}))

    def pause(self) -> None:
        self.call("pause")

    def resume(self) -> None:
        self.call("resume")

    def step(self, frames: int = 1, timeout: float = 30.0) -> dict:
        """Advance a paused app by exactly ``frames`` frames (pauses it first)."""
        return self.call("step", frames=frames, timeout_ms=int(timeout * 1000), timeout=timeout + 5)

    def record_start(self) -> None:
        self.call("record", action="start")

    def record_stop(self) -> dict:
        return self.call("record", action="stop")

    def configure(self, capture_width: Optional[int] = None, eyes: Optional[str] = None,
                  capture_fps: Optional[float] = None) -> dict:
        args = {k: v for k, v in (("capture_width", capture_width), ("eyes", eyes), ("capture_fps", capture_fps))
                if v is not None}
        return self.call("configure", **args)

    def session_event(self, event: str) -> None:
        """focus_lost | focus_gained | recenter | exit"""
        self.call("session", event=event)

    def geometry(self, grip_pitch: Optional[float] = None, grip_pos: Optional[Vec3] = None) -> None:
        geo: dict = {}
        if grip_pitch is not None:
            geo["grip_pitch"] = grip_pitch
        if grip_pos is not None:
            geo["grip_pos"] = list(grip_pos)
        self.set(geometry=geo)

    def drive(self, on: bool = True) -> None:
        """False hands poses and input back to the real runtime (watch only)."""
        self.call("drive", on=on)

    # -- frames -----------------------------------------------------------

    def capture(self, fresh: bool = True, timeout: float = 4.0) -> Frame:
        """The app's current view. ``fresh`` waits for a frame drawn after the call."""
        meta = self.call("capture", fresh=fresh, timeout_ms=int(timeout * 1000), timeout=timeout + 5)
        if self._reader is None or self._reader.name != meta["shm"]:
            if self._reader is not None:
                self._reader.close()
            self._reader = FrameReader(meta["shm"])
        frame = self._reader.read()
        if frame.frame < 0:
            frame.frame = int(meta.get("frame", -1))
        return frame

    def screenshot(self, path: Optional[str | Path] = None, eye: str = "left",
                   max_width: Optional[int] = None) -> Image.Image:
        img = self.capture().eye(eye)
        if max_width and img.width > max_width:
            img = img.resize((max_width, round(img.height * max_width / img.width)), Image.LANCZOS)
        if path:
            img.save(path)
        return img

    # -- the body ---------------------------------------------------------

    def look(self, yaw: float, pitch: float = 0.0, roll: float = 0.0, over: int = 0) -> None:
        """Turn the head to an absolute yaw/pitch (degrees), optionally over N frames."""
        self.set(head={"ypr": [yaw, pitch, roll]}, **({"over": over} if over else {}))

    def turn(self, dyaw: float = 0.0, dpitch: float = 0.0, over: int = 0) -> None:
        ypr = self.state()["head"]["ypr"]
        pitch = max(-89.0, min(89.0, ypr[1] + dpitch))
        self.look(ypr[0] + dyaw, pitch, ypr[2], over=over)

    def walk(self, forward: float = 0.0, right: float = 0.0, up: float = 0.0, over: int = 0) -> None:
        """Physically step (room-scale), relative to where the head faces."""
        st = self.state()["head"]
        yaw = math.radians(st["ypr"][0])
        fx, fz = -math.sin(yaw), -math.cos(yaw)
        rx, rz = math.cos(yaw), -math.sin(yaw)
        p = st["pos"]
        pos = [p[0] + fx * forward + rx * right, p[1] + up, p[2] + fz * forward + rz * right]
        self.set(head={"pos": pos}, **({"over": over} if over else {}))

    def hand_rest(self, hand: str) -> list[float]:
        """A natural spot for a hand: below the eyes, in front of the chest."""
        st = self.state()["head"]
        side = 0.10 if hand == "right" else -0.10
        return [a + b for a, b in zip(st["pos"], qrot(st["rot"], [side, -0.14, -0.24]))]

    def point(self, hand: str, target: Vec3, origin: Optional[Vec3] = None, over: int = 0) -> list[float]:
        """Aim a controller at a world point. Returns the aim yaw/pitch used."""
        origin = list(origin) if origin is not None else self.hand_rest(hand)
        ypr = look_ypr(origin, target)
        self.set(**{hand: {"pos": origin, "ypr": ypr}}, **({"over": over} if over else {}))
        return ypr

    def views(self) -> list[dict]:
        """Per-eye FOV (degrees) as the app last saw it."""
        st = self.state()
        views = st.get("views") or []
        if views:
            return views
        o, i, u, d = self.fov_fallback
        return [{"fov": [-o, i, u, -d]}, {"fov": [-i, o, u, -d]}]

    def pixel_ray(self, x: float, y: float, width: int, height: int, eye: str = "left",
                  state: Optional[dict] = None) -> tuple[list[float], list[float]]:
        """World-space ray through pixel (x, y) of an eye image of the given size."""
        st = state or self.state()
        views = st.get("views") or self.views()
        idx = 0 if eye == "left" else min(1, len(views) - 1)
        left, right, up, down = (math.tan(math.radians(a)) for a in views[idx]["fov"])
        tx = left + (x / width) * (right - left)
        ty = up - (y / height) * (up - down)
        head = st["head"]
        ipd = st.get("ipd", 0.063)
        eye_off = [-ipd / 2 if idx == 0 else ipd / 2, 0.0, 0.0]
        origin = [a + b for a, b in zip(head["pos"], qrot(head["rot"], eye_off))]
        direction = _norm(qrot(head["rot"], [tx, ty, -1.0]))
        return origin, direction

    def point_at_pixel(self, hand: str, x: float, y: float, width: int, height: int,
                       distance: float = 1.5, over: int = 0) -> dict:
        """Aim a controller so its ray crosses what's at pixel (x, y) of the
        last eye image. ``distance`` is a depth guess; near the eye line the
        guess barely matters."""
        st = self.state()
        origin, d = self.pixel_ray(x, y, width, height, state=st)
        target = [o + c * distance for o, c in zip(origin, d)]
        side = 0.08 if hand == "right" else -0.08
        head = st["head"]
        hand_pos = [a + b for a, b in zip(head["pos"], qrot(head["rot"], [side, -0.12, -0.22]))]
        ypr = self.point(hand, target, origin=hand_pos, over=over)
        return {"hand": hand, "pos": hand_pos, "ypr": ypr, "target": target}

    # -- input ------------------------------------------------------------

    def hold(self, hand: str, button: str, on: bool = True) -> None:
        key = BUTTONS.get(button.lower())
        if key is None:
            raise XrError(f"unknown button '{button}' (have: {', '.join(sorted(BUTTONS))})")
        value = (1.0 if on else 0.0) if button.lower() in ANALOG or key in ANALOG else on
        self.set(**{hand: {key: value}})

    def press(self, hand: str, button: str, frames: int = 6) -> dict:
        """Press and release a button, frame-accurately (works while paused:
        follow with ``step(frames + 2)``)."""
        key = BUTTONS.get(button.lower())
        if key is None:
            raise XrError(f"unknown button '{button}' (have: {', '.join(sorted(BUTTONS))})")
        down = 1.0 if key in ANALOG else True
        up = 0.0 if key in ANALOG else False
        return self.schedule([
            {"frame": 0, "set": {hand: {key: down}}},
            {"frame": max(1, frames), "set": {hand: {key: up}}},
        ])

    def stick(self, hand: str, x: float, y: float, frames: int = 36) -> dict:
        """Push the thumbstick for N frames, then let it spring back."""
        return self.schedule([
            {"frame": 0, "set": {hand: {"stick": [x, y]}}},
            {"frame": max(1, frames), "set": {hand: {"stick": [0.0, 0.0]}}},
        ])

    def click_pixel(self, x: float, y: float, width: int, height: int, hand: str = "right",
                    distance: float = 1.5, settle_frames: int = 4) -> dict:
        """Point at a pixel and pull the trigger — "tap what I see"."""
        aim = self.point_at_pixel(hand, x, y, width, height, distance=distance)
        self.schedule([
            {"frame": settle_frames, "set": {hand: {"trigger": 1.0}}},
            {"frame": settle_frames + 6, "set": {hand: {"trigger": 0.0}}},
        ])
        return aim

    def actions(self) -> list[dict]:
        return self.info().get("actions", [])

    def press_action(self, name: str, frames: int = 6, value: Optional[float] = None) -> dict:
        """Drive whatever input the app bound to its action ``name``."""
        name_l = name.lower()
        actions = self.actions()
        match = next((a for a in actions if a["name"].lower() == name_l), None) or next(
            (a for a in actions if name_l in a["name"].lower() and a["bindings"]), None)
        if match is None or not match["bindings"]:
            raise XrError(f"no bound action like '{name}'")
        b = match["bindings"][0]
        hand, inp = b["hand"], b["input"]
        if inp == "stick":
            comp = b.get("component")
            xy = [0.0, value if value is not None else 1.0]
            if comp == "x":
                xy = [value if value is not None else 1.0, 0.0]
            return self.stick(hand, xy[0], xy[1], frames=frames)
        key = {"trigger": "trigger", "squeeze": "squeeze", "primary": "a", "secondary": "b",
               "menu": "menu", "thumbrest": "thumbrest"}.get(inp)
        if key is None:
            raise XrError(f"action '{match['name']}' is bound to {inp}, which can't be pressed")
        return self.press(hand, key, frames=frames)


def live_sockets() -> list[Path]:
    """Sockets of apps currently running with the layer (stale ones skipped)."""
    out = []
    d = paths.socket_dir()
    if not d.is_dir():
        return out
    for sock in sorted(d.glob("*.sock")):
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(0.5)
        try:
            probe.connect(str(sock))
            out.append(sock)
        except ConnectionRefusedError:
            # Nobody listening: the app is gone. Tidy up after it.
            sock.unlink(missing_ok=True)
            sock.with_suffix(".json").unlink(missing_ok=True)
        except OSError:
            pass
        finally:
            probe.close()
    for meta in d.glob("*.json"):
        if not meta.with_suffix(".sock").exists():
            try:
                pid = json.loads(meta.read_text()).get("pid")
            except (OSError, ValueError):
                pid = None
            if not pid or not Path(f"/proc/{pid}").exists():
                meta.unlink(missing_ok=True)
    return out
