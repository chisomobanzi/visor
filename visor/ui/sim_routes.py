"""Dashboard backend for the virtual headset: launch, watch, drive, playtest.

Everything a person does in the SIMULATOR workspace maps onto the same
XrClient verbs an agent uses, so what you can do by hand an agent can do,
and the other way round.
"""

from __future__ import annotations

import asyncio
import io
import json
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel

router = APIRouter(prefix="/api/xr")

# Instances launched from the dashboard, and playtests running in the background.
_sims: dict = {}
_playtests: dict[str, dict] = {}
_lock = threading.Lock()


def _client(name: str):
    from visor.xr import SimError, connect

    try:
        return connect(name)
    except SimError as e:
        raise HTTPException(404, str(e)) from None


def _xr(fn, *args, **kwargs):
    from visor.xr import SimError, XrError

    try:
        return fn(*args, **kwargs)
    except (XrError, SimError) as e:
        raise HTTPException(409, str(e)) from None
    except ValueError as e:
        raise HTTPException(400, str(e)) from None


# --------------------------------------------------------------------------
# what's installed and running


@router.get("/status")
def status():
    from visor.xr import paths
    from visor.xr import setup as xr_setup

    return {"installed": xr_setup.installed(), "home": str(paths.xr_home())}


@router.get("/running")
def running():
    from visor.xr import running as running_apps

    return running_apps()


@router.get("/personas")
def personas():
    from visor.xr.playtest.personas import PERSONAS

    return [asdict(p) for p in PERSONAS.values()]


@router.get("/suggest")
def suggest():
    """Things worth launching: the demo, plus Unreal projects near home."""
    out = [{"label": "Demo (hello_xr)", "target": "demo", "kind": "demo"}]
    home = Path.home()
    seen = set()
    for pattern in ("Developer/*/*.uproject", "Developer/*/*/*.uproject", "Projects/*/*.uproject",
                    "Documents/Unreal Projects/*/*.uproject"):
        for p in sorted(home.glob(pattern))[:12]:
            if p.resolve() in seen or "worktrees" in p.parts:
                continue
            seen.add(p.resolve())
            out.append({"label": p.stem, "target": str(p), "kind": "unreal"})
    return out


# --------------------------------------------------------------------------
# lifecycle


class LaunchBody(BaseModel):
    target: str
    map: Optional[str] = None
    headset: str = "quest3"
    scale: float = 1.0
    paused: bool = False
    seated: bool = False
    mic: bool = False


@router.post("/launch")
def launch(body: LaunchBody):
    from visor.xr import AppSpec, SimConfig, SimInstance

    spec = _xr(AppSpec.resolve, body.target, map_name=body.map or None)
    cfg = SimConfig(headset=body.headset, render_scale=body.scale, paused=body.paused,
                    eye_height=1.2 if body.seated else 1.65, microphone=body.mic)
    sim = _xr(SimInstance, spec, cfg)
    _xr(sim.start)
    with _lock:
        _sims[sim.name] = sim
    return {"name": sim.name, "app": spec.name, "kind": spec.kind, "run_dir": str(sim.run_dir)}


@router.get("/launching/{name}")
def launching(name: str):
    """Progress of an app that's still booting."""
    sim = _sims.get(name)
    if sim is None:
        raise HTTPException(404, "not launched from here")
    alive = sim.alive()
    note = "starting"
    if sim.socket_path.exists():
        try:
            st = sim.client.state()
            note = f"session {st['session']} · frame {int(st['render_frame'])}"
            if st["session"] == "focused" and st["render_frame"] > 20:
                return {"ready": True, "note": note}
        except Exception:
            note = "loading"
    if not alive:
        return {"ready": False, "failed": True, "note": sim.crash_summary(default=sim.log_tail(12))}
    return {"ready": False, "note": note, "log": sim.log_tail(3)}


@router.post("/{name}/stop")
def stop(name: str):
    sim = _sims.pop(name, None)
    if sim is not None:
        return {"stopped": name, "exit_code": sim.stop()}
    from visor.xr.sim import stop_instance

    return {"stopped": name, "outcome": _xr(stop_instance, name)}


# --------------------------------------------------------------------------
# watching


@router.get("/{name}/info")
def info(name: str):
    return _xr(_client(name).info)


@router.get("/{name}/state")
def state(name: str):
    xr = _client(name)
    st = _xr(xr.state)
    st["stats"] = _xr(xr.stats, 144)
    return st


@router.get("/{name}/events")
def events(name: str, since: int = 0):
    return _xr(_client(name).events, since)


@router.get("/{name}/shot")
def shot(name: str, eye: str = "left", width: int = 960):
    xr = _client(name)
    img = _xr(xr.screenshot, eye=eye, max_width=width)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88)
    return Response(buf.getvalue(), media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@router.get("/{name}/stream")
async def stream(name: str, fps: int = 20, width: int = 960):
    """MJPEG of the virtual headset's left eye."""
    from visor.xr.frames import FrameError, FrameReader

    xr = _client(name)
    fps = max(2, min(fps, 45))
    _xr(xr.configure, capture_fps=fps, capture_width=width)
    meta = _xr(xr.call, "capture", fresh=False, timeout_ms=4000)
    reader = FrameReader(meta["shm"])
    boundary = "visorframe"

    async def gen():
        last = -1
        idle = 0
        try:
            while True:
                try:
                    seq = reader.latest_seq()
                    if seq != last:
                        last = seq
                        frame = reader.read()
                        img = frame.eye("left")
                        buf = io.BytesIO()
                        img.save(buf, format="JPEG", quality=80)
                        data = buf.getvalue()
                        yield (f"--{boundary}\r\nContent-Type: image/jpeg\r\nContent-Length: {len(data)}\r\n\r\n"
                               ).encode() + data + b"\r\n"
                        idle = 0
                    else:
                        idle += 1
                except FrameError:
                    idle += 1
                if idle > fps * 30:
                    break  # nothing new for 30 s: the app is gone
                await asyncio.sleep(1.0 / fps)
        finally:
            reader.close()
            try:
                xr.configure(capture_fps=0)
            except Exception:
                pass
            xr.close()

    return StreamingResponse(gen(), media_type=f"multipart/x-mixed-replace; boundary={boundary}",
                             headers={"Cache-Control": "no-store"})


# --------------------------------------------------------------------------
# driving


class Patch(BaseModel):
    patch: dict


@router.post("/{name}/set")
def set_patch(name: str, body: Patch):
    return _xr(_client(name).set, **body.patch)


class Press(BaseModel):
    hand: str = "right"
    button: str = "trigger"
    frames: int = 6


@router.post("/{name}/press")
def press(name: str, body: Press):
    xr = _client(name)
    _xr(xr.press, body.hand, body.button, frames=body.frames)
    return _after(xr, body.frames + 10)


class Hold(BaseModel):
    hand: str = "right"
    button: str = "trigger"
    down: bool = True


@router.post("/{name}/hold")
def hold(name: str, body: Hold):
    _xr(_client(name).hold, body.hand, body.button, body.down)
    return {"ok": True}


class Click(BaseModel):
    x: float
    y: float
    width: int
    height: int
    hand: str = "right"
    press: bool = True


@router.post("/{name}/click")
def click(name: str, body: Click):
    xr = _client(name)
    if body.press:
        aim = _xr(xr.click_pixel, body.x, body.y, body.width, body.height, hand=body.hand)
        return {**_after(xr, 20), "aim": aim}
    aim = _xr(xr.point_at_pixel, body.hand, body.x, body.y, body.width, body.height)
    return {**_after(xr, 4), "aim": aim}


class Look(BaseModel):
    dyaw: float = 0.0
    dpitch: float = 0.0
    yaw: Optional[float] = None
    pitch: Optional[float] = None
    over: int = 0


@router.post("/{name}/look")
def look(name: str, body: Look):
    xr = _client(name)
    if body.yaw is not None:
        _xr(xr.look, body.yaw, body.pitch or 0.0, over=body.over)
    else:
        _xr(xr.turn, body.dyaw, body.dpitch, over=body.over)
    return {"ok": True}


class Walk(BaseModel):
    forward: float = 0.0
    right: float = 0.0
    over: int = 18


@router.post("/{name}/walk")
def walk(name: str, body: Walk):
    _xr(_client(name).walk, forward=body.forward, right=body.right, over=body.over)
    return {"ok": True}


class Stick(BaseModel):
    hand: str = "left"
    x: float = 0.0
    y: float = 0.0
    frames: int = 0  # 0 = hold until the next call


@router.post("/{name}/stick")
def stick(name: str, body: Stick):
    xr = _client(name)
    if body.frames:
        _xr(xr.stick, body.hand, body.x, body.y, frames=body.frames)
    else:
        _xr(xr.set, **{body.hand: {"stick": [body.x, body.y]}})
    return {"ok": True}


def _after(xr, frames: int) -> dict:
    """If paused, step just far enough for an action to show."""
    try:
        if xr.state().get("paused"):
            r = xr.step(frames)
            return {"stepped": int(r.get("frame", 0))}
    except Exception:
        pass
    return {}


@router.post("/{name}/pause")
def pause(name: str):
    _xr(_client(name).pause)
    return {"paused": True}


@router.post("/{name}/resume")
def resume(name: str):
    _xr(_client(name).resume)
    return {"paused": False}


@router.post("/{name}/step")
def step(name: str, frames: int = 1):
    return _xr(_client(name).step, max(1, min(frames, 72 * 30)))


@router.post("/{name}/event")
def system_event(name: str, event: str):
    if event not in ("focus_lost", "focus_gained", "recenter", "exit"):
        raise HTTPException(400, "event must be focus_lost, focus_gained, recenter or exit")
    _xr(_client(name).session_event, event)
    return {"ok": True}


_recordings: dict = {}


@router.post("/{name}/record/start")
def record_start(name: str):
    """Video of what the headset sees plus a replay script of every input."""
    from visor.xr import paths
    from visor.xr.video import Recorder

    if name in _recordings:
        raise HTTPException(409, "already recording")
    xr = _client(name)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = paths.runs_dir() / name / "recordings" / f"rec-{stamp}.mp4"
    _xr(xr.record_start)
    rec = Recorder(xr, out, fps=24)
    try:
        rec.start()
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from None
    _recordings[name] = {"recorder": rec, "client": xr, "started": time.time()}
    return {"recording": True, "path": str(out)}


@router.post("/{name}/record/stop")
def record_stop(name: str):
    entry = _recordings.pop(name, None)
    if entry is None:
        raise HTTPException(409, "not recording")
    rec = entry["recorder"]
    video = rec.stop()
    script = video.with_suffix(".replay.json")
    try:
        data = entry["client"].record_stop()
        script.write_text(json.dumps({"visor_replay": 1, **data}, indent=1))
    except Exception:
        script = None
    rel = f"/api/xr/runs/{name}/recordings/{video.name}"
    return {"video": str(video), "url": rel, "seconds": round(time.time() - entry["started"], 1),
            "frames": rec.frames_written, "replay": str(script) if script else None}


@router.post("/{name}/reset")
def reset(name: str):
    _xr(_client(name).reset)
    return {"ok": True}


# --------------------------------------------------------------------------
# playtests and fleets


class PlaytestBody(BaseModel):
    target: str
    persona: str = "first-timer"
    goal: Optional[str] = None
    brain: str = "claude"
    steps: int = 30
    model: str = "claude-opus-5-5"
    effort: str = "medium"
    map: Optional[str] = None


@router.post("/playtest")
def playtest_start(body: PlaytestBody):
    from visor.xr.playtest.session import DEFAULT_GOAL, Playtest, PlaytestConfig

    cfg = PlaytestConfig(target=body.target, persona=body.persona, goal=body.goal or DEFAULT_GOAL,
                         brain=body.brain, model=body.model, effort=body.effort, max_steps=body.steps,
                         map=body.map or None)
    entry: dict = {"status": "starting", "events": [], "issues": 0, "step": 0, "persona": body.persona,
                   "brain": body.brain, "target": body.target, "started": time.time()}

    def on_event(kind: str, data: dict) -> None:
        with _lock:
            if kind == "action":
                entry["step"] = data.get("step", 0)
                entry["status"] = "playing"
            elif kind == "issue":
                entry["issues"] += 1
            elif kind == "status":
                entry["note"] = data.get("message")
            entry["events"].append({"kind": kind, "t": time.time(), **{k: v for k, v in data.items()
                                                                       if k in ("message", "step", "action", "args",
                                                                                "thought", "result", "issue",
                                                                                "screenshot")}})
            entry["events"] = entry["events"][-200:]

    try:
        test = Playtest(cfg, on_event=on_event)
    except (ValueError, ImportError) as e:
        raise HTTPException(400, str(e)) from None
    entry["name"] = test.name
    with _lock:
        _playtests[test.name] = entry

    def run() -> None:
        try:
            report = test.run()
            entry["status"] = "crashed" if report.get("crash") else "done"
            entry["verdict"] = report.get("verdict")
        except Exception as e:
            entry["status"] = "failed"
            entry["note"] = str(e)

    threading.Thread(target=run, name=f"playtest-{test.name}", daemon=True).start()
    return {"name": test.name}


@router.get("/playtest/{name}")
def playtest_progress(name: str, since: float = 0.0):
    entry = _playtests.get(name)
    if entry is None:
        raise HTTPException(404, "no such playtest in this dashboard session")
    with _lock:
        out = {k: v for k, v in entry.items() if k != "events"}
        out["events"] = [e for e in entry["events"] if e["t"] > since]
    return out


_fleets: dict = {}


class FleetBody(BaseModel):
    target: str = "demo"
    count: int = 4
    mode: str = "soak"


@router.post("/fleet")
def fleet_start(body: FleetBody):
    from visor.xr.fleet import Fleet, FleetConfig

    if not 1 <= body.count <= 64:
        raise HTTPException(400, "count must be 1..64")
    f = Fleet(FleetConfig(target=body.target, count=body.count, mode=body.mode, seconds=30, steps=20,
                          personas=["first-timer", "speedrunner", "explorer"], brain="monkey"))
    _fleets[f.name] = {"fleet": f, "finished": False}

    def run() -> None:
        try:
            f.run()
        finally:
            _fleets[f.name]["finished"] = True

    threading.Thread(target=run, name=f"fleet-{f.name}", daemon=True).start()
    return {"name": f.name}


@router.get("/fleet/{name}")
def fleet_progress(name: str):
    entry = _fleets.get(name)
    if entry is None:
        raise HTTPException(404, "no such fleet in this dashboard session")
    res = entry["fleet"].results
    return {
        "count": len(res),
        "done": sum(1 for r in res if r.status in ("done", "crashed", "failed")),
        "running": sum(1 for r in res if r.status in ("starting", "running")),
        "failed": sum(1 for r in res if r.status in ("crashed", "failed")),
        "finished": entry["finished"],
    }


@router.get("/runs")
def runs(limit: int = 40):
    """Past playtests and fleets, newest first."""
    from visor.xr import paths

    root = paths.runs_dir()
    out = []
    if not root.exists():
        return out
    for d in sorted(root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        rep = d / "report.json"
        flt = d / "fleet.json"
        try:
            if rep.exists():
                r = json.loads(rep.read_text())
                issues = r.get("issues", [])
                thumb = next((s["screenshot"] for s in reversed(r.get("steps", [])) if s.get("screenshot")), None)
                out.append({
                    "kind": "playtest", "name": d.name, "app": (r.get("app") or {}).get("name"),
                    "persona": (r.get("persona") or {}).get("name"), "brain": (r.get("brain") or {}).get("kind"),
                    "created": r.get("created"), "verdict": r.get("verdict"), "crash": bool(r.get("crash")),
                    "issues": {s: sum(1 for i in issues if i["severity"] == s) for s in ("blocker", "major", "minor", "note")},
                    "steps": len(r.get("steps", [])), "report": f"/api/xr/runs/{d.name}/report.html",
                    "thumb": f"/api/xr/runs/{d.name}/{thumb}" if thumb else None,
                })
            elif flt.exists():
                f = json.loads(flt.read_text())
                out.append({
                    "kind": "fleet", "name": d.name, "app": (f.get("config") or {}).get("target"),
                    "count": len(f.get("runs", [])), "ok": f.get("ok"), "crashed": f.get("crashed"),
                    "mean_fps": f.get("mean_fps"), "issues": len(f.get("issues", [])),
                    "report": f"/api/xr/runs/{d.name}/fleet.html",
                    "created": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(d.stat().st_mtime)),
                })
        except (OSError, ValueError):
            continue
        if len(out) >= limit:
            break
    for name, entry in _playtests.items():
        if entry["status"] in ("starting", "playing") and not any(o["name"] == name for o in out):
            out.insert(0, {"kind": "playtest", "name": name, "app": entry["target"], "persona": entry["persona"],
                           "brain": entry["brain"], "live": True, "step": entry["step"], "issues_live": entry["issues"],
                           "status": entry["status"], "note": entry.get("note")})
    return out


@router.get("/runs/{name}/{path:path}")
def run_file(name: str, path: str):
    """Serve a run's report and artefacts (only from inside its run directory)."""
    from visor.xr import paths

    root = (paths.runs_dir() / name).resolve()
    target = (root / path).resolve()
    if paths.runs_dir().resolve() not in root.parents or root not in target.parents or not target.is_file():
        raise HTTPException(404, "not found")
    return FileResponse(target)
