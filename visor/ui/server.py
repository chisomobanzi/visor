"""Visor web dashboard — FastAPI backend reusing the visor.adb library.

Launch with `visor ui` (serves on localhost only; this exposes device
control, so it deliberately does not bind to 0.0.0.0).
"""

from __future__ import annotations

import io
import os
import tempfile
import time
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    Response,
    StreamingResponse,
)

from visor import adb
from visor.adb.wrapper import VisorError

app = FastAPI(title="visor", docs_url=None, redoc_url=None)

_STATIC = Path(__file__).parent / "static"


def _device(device_id: Optional[str] = None) -> str:
    try:
        devices = [d for d in adb.discover_devices() if d.state == "device"]
    except VisorError as e:
        raise HTTPException(status_code=503, detail=str(e)) from None
    if device_id:
        if not any(d.serial == device_id for d in devices):
            raise HTTPException(404, f"device {device_id} not connected")
        return device_id
    quests = [d for d in devices if d.is_quest] or devices
    if not quests:
        raise HTTPException(503, "No device connected")
    return quests[0].serial


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except VisorError as e:
        raise HTTPException(status_code=500, detail=str(e)) from None
    except (FileNotFoundError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e)) from None


@app.get("/", response_class=HTMLResponse)
def index():
    return (_STATIC / "index.html").read_text(encoding="utf-8")


@app.get("/api/devices")
def devices():
    try:
        return [asdict(d) for d in adb.discover_devices()]
    except VisorError as e:
        raise HTTPException(status_code=503, detail=str(e)) from None


@app.get("/api/info")
def info(device: Optional[str] = None):
    return asdict(_call(adb.get_device_info, _device(device)))


_VIEW_MAX_WIDTH = 1600


def _frame_for_view(img, eye: str = "left", undistort: bool = True):
    """Crop a stereo device capture down to one eye, undo the lens barrel
    distortion, and cap the width — the shape you actually want to look at,
    paste into a chat, or attach to a bug report."""
    width, height = img.size
    if eye == "left" and width / height > 1.6:
        img = img.crop((0, 0, width // 2, height))
        if undistort:
            img = adb.undistort_eye(img)
    if img.width > _VIEW_MAX_WIDTH:
        ratio = _VIEW_MAX_WIDTH / img.width
        img = img.resize((_VIEW_MAX_WIDTH, int(img.height * ratio)))
    return img.convert("RGB")


@app.get("/api/screenshot")
def screenshot(device: Optional[str] = None, eye: str = "left", undistort: bool = True):
    serial = _device(device)
    path = os.path.join(tempfile.gettempdir(), f"visor-ui-{serial}.png")
    shot = _call(adb.screenshot, serial, path)

    from PIL import Image

    with Image.open(shot.path) as img:
        buf = io.BytesIO()
        _frame_for_view(img, eye, undistort).save(buf, format="JPEG", quality=88)
    return Response(
        content=buf.getvalue(),
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store"},
    )


# ------------------------------------------------------------ shot gallery
# Captures are kept as PNGs on disk so they survive a dashboard restart, and
# because PNG is the only image type browsers will put on the clipboard.


def _shots_dir() -> Path:
    path = Path.home() / ".visor" / "shots"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _shot_file(name: str) -> Path:
    """Resolve a gallery shot by basename only — no path traversal."""
    candidate = _shots_dir() / Path(name).name
    if candidate.suffix.lower() != ".png" or not candidate.is_file():
        raise HTTPException(404, f"no such shot: {Path(name).name}")
    return candidate


def _new_shot_file() -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = _shots_dir() / f"quest-{stamp}.png"
    n = 2
    while path.exists():
        path = _shots_dir() / f"quest-{stamp}-{n}.png"
        n += 1
    return path


def _shot_meta(path: Path) -> dict:
    from PIL import Image

    width = height = None
    try:
        with Image.open(path) as img:
            width, height = img.size
    except (OSError, ValueError):
        pass
    return {
        "name": path.name,
        "path": str(path),
        "captured_at": path.stat().st_mtime,
        "size_bytes": path.stat().st_size,
        "width": width,
        "height": height,
    }


@app.get("/api/shots")
def shots_list(limit: int = 120):
    files = sorted(
        _shots_dir().glob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    return {
        "dir": str(_shots_dir()),
        "total": len(files),
        "shots": [_shot_meta(p) for p in files[:limit]],
    }


@app.post("/api/shots")
def shot_capture(
    device: Optional[str] = None, eye: str = "left", undistort: bool = True
):
    """Grab the headset screen and file it in the gallery."""
    from PIL import Image

    serial = _device(device)
    tmp = os.path.join(tempfile.gettempdir(), f"visor-ui-{serial}.png")
    shot = _call(adb.screenshot, serial, tmp)
    dest = _new_shot_file()
    with Image.open(shot.path) as img:
        _frame_for_view(img, eye, undistort).save(dest, format="PNG")
    return _shot_meta(dest)


@app.post("/api/shots/upload")
async def shot_upload(file: UploadFile):
    """File a frame the browser grabbed off the live stream — capturing that
    way keeps the stream running instead of interrupting it with screencap."""
    from PIL import Image

    data = await file.read()
    try:
        with Image.open(io.BytesIO(data)) as img:
            frame = img.convert("RGB")
    except (OSError, ValueError):
        raise HTTPException(400, "not a readable image") from None
    dest = _new_shot_file()
    frame.save(dest, format="PNG")
    return _shot_meta(dest)


# (name, mtime) -> thumbnail JPEG bytes; shot files are never rewritten
_thumb_cache: dict = {}


@app.get("/api/shots/{name}")
def shot_get(name: str, thumb: bool = False):
    path = _shot_file(name)
    if not thumb:
        # served inline: the gallery renders it in an <img>, and the SAVE link
        # supplies its own filename via the download attribute
        return FileResponse(path, media_type="image/png")
    key = (path.name, path.stat().st_mtime)
    data = _thumb_cache.get(key)
    if data is None:
        from PIL import Image

        with Image.open(path) as img:
            img = img.convert("RGB")
            img.thumbnail((360, 360))
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=78)
        data = buf.getvalue()
        if len(_thumb_cache) > 200:
            _thumb_cache.clear()
        _thumb_cache[key] = data
    return Response(
        content=data,
        media_type="image/jpeg",
        headers={"Cache-Control": "max-age=31536000, immutable"},
    )


@app.delete("/api/shots/{name}")
def shot_delete(name: str):
    path = _shot_file(name)
    path.unlink()
    return {"deleted": path.name}


# serial -> (adb_proc, ffmpeg_proc) of the current live stream (last-wins)
_active_streams: dict = {}

# serial -> LiveRecorder for the in-progress perf session (last-wins)
_active_recorders: dict = {}


def _sessions_dir() -> Path:
    path = Path.home() / ".visor" / "sessions"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _session_file(name: str) -> Path:
    """Resolve a session by basename only — no path traversal."""
    candidate = _sessions_dir() / Path(name).name
    if not candidate.is_file():
        raise HTTPException(404, f"no such session: {Path(name).name}")
    return candidate


@app.post("/api/perf/session/start")
def perf_session_start(package: Optional[str] = None, device: Optional[str] = None):
    from visor.adb.perf_session import LiveRecorder

    serial = _device(device)
    old = _active_recorders.pop(serial, None)
    if old:
        old.stop()
    recorder = LiveRecorder(serial, package=package or None)
    _call(recorder.start)
    _active_recorders[serial] = recorder
    return {"package": recorder.package, "recording": True}


@app.get("/api/perf/session/live")
def perf_session_live(device: Optional[str] = None):
    serial = _device(device)
    recorder = _active_recorders.get(serial)
    if not recorder:
        raise HTTPException(404, "no session recording — start one first")
    return recorder.snapshot()


@app.post("/api/perf/session/stop")
def perf_session_stop(device: Optional[str] = None):
    import time as _time

    from visor.adb.diagnosis import diagnose_session
    from visor.adb.perf_session import save_session, summarize_session

    serial = _device(device)
    recorder = _active_recorders.pop(serial, None)
    if not recorder:
        raise HTTPException(404, "no session recording")
    session = recorder.stop()
    stamp = _time.strftime("%Y%m%d-%H%M%S")
    name = f"{(session.package or 'unknown').replace('.', '_')}-{stamp}.json"
    save_session(session, str(_sessions_dir() / name))
    return {
        "name": name,
        "summary": summarize_session(session),
        "diagnosis": diagnose_session(session),
        "session": session.to_dict(),
    }


@app.get("/api/perf/sessions")
def perf_sessions():
    import json as _json

    sessions = []
    for path in sorted(_sessions_dir().glob("*.json"), reverse=True):
        try:
            with open(path, encoding="utf-8") as f:
                data = _json.load(f)
            sessions.append({
                "name": path.name,
                "package": data.get("package"),
                "started_at": data.get("started_at"),
                "duration_s": data.get("duration_s"),
                "samples": len(data.get("vrapi", [])),
            })
        except (OSError, ValueError):
            continue
    return {"sessions": sessions}


@app.get("/api/perf/session")
def perf_session_get(name: str):
    from visor.adb.diagnosis import diagnose_session
    from visor.adb.perf_session import load_session, summarize_session

    session = load_session(str(_session_file(name)))
    return {
        "name": Path(name).name,
        "session": session.to_dict(),
        "summary": summarize_session(session),
        "diagnosis": diagnose_session(session),
    }


@app.get("/api/perf/diff")
def perf_diff(before: str, after: str):
    from visor.adb.diagnosis import diff_sessions
    from visor.adb.perf_session import load_session

    return diff_sessions(
        load_session(str(_session_file(before))),
        load_session(str(_session_file(after))),
    )


def _wake_display(serial: str, restore: bool = False):
    action = "automation_disable" if restore else "prox_close"
    try:
        adb.get_adb().shell(
            f"am broadcast -a com.oculus.vrpowermanager.{action}", device_id=serial
        )
    except VisorError:
        pass


@app.get("/api/stream")
async def stream(device: Optional[str] = None, fps: int = 15):
    """Live MJPEG stream of the headset screen (multipart/x-mixed-replace).

    Renders directly in an <img> tag. Restarts the underlying screenrecord
    transparently when Android's 180s cap is hit. Must be an async
    generator: Starlette only cancels those promptly on client disconnect,
    and cancellation is what tears down the adb/ffmpeg pipeline.
    """
    import os as _os

    import anyio

    serial = _device(device)
    _wake_display(serial)

    # last-wins: a new viewer replaces any existing stream for this device
    old = _active_streams.pop(serial, None)
    if old:
        adb.stop_stream_processes(*old)
    try:
        procs = adb.start_screen_stream(serial, fps=fps)
    except VisorError as e:
        raise HTTPException(503, str(e)) from None
    _active_streams[serial] = procs

    async def gen():
        current = procs
        try:
            while True:
                chunk = await anyio.to_thread.run_sync(
                    _os.read, current[1].stdout.fileno(), 65536,
                    abandon_on_cancel=True,
                )
                if chunk:
                    yield chunk
                    continue
                # EOF: 180s screenrecord cap (or device gone) — respawn if
                # we're still the active stream for this device
                if _active_streams.get(serial) is not current:
                    break
                adb.stop_stream_processes(*current)
                try:
                    current = adb.start_screen_stream(serial, fps=fps)
                except VisorError:
                    break
                _active_streams[serial] = current
        finally:
            adb.stop_stream_processes(*current)
            if _active_streams.get(serial) is current:
                _active_streams.pop(serial, None)
                _wake_display(serial, restore=True)

    return StreamingResponse(
        gen(), media_type="multipart/x-mixed-replace; boundary=ffmpeg"
    )


@app.post("/api/stream/stop")
def stream_stop(device: Optional[str] = None):
    """Kill the active live stream server-side. Belt-and-braces: browsers
    don't always close the multipart connection when the <img> goes away."""
    serial = _device(device)
    procs = _active_streams.pop(serial, None)
    if procs:
        adb.stop_stream_processes(*procs)
        _wake_display(serial, restore=True)
    return {"stopped": procs is not None}


@app.post("/api/record")
def record(seconds: int = 15, device: Optional[str] = None):
    serial = _device(device)
    rec = _call(adb.screen_record, serial, duration_seconds=seconds)
    return asdict(rec)


@app.get("/api/recording")
def recording(path: str):
    # Only serve files visor itself recorded (recordings/ or tmp)
    resolved = Path(path).resolve()
    allowed = [Path.cwd() / "recordings", Path(tempfile.gettempdir())]
    if not any(str(resolved).startswith(str(a.resolve())) for a in allowed):
        raise HTTPException(403, "path outside recording directories")
    if not resolved.is_file():
        raise HTTPException(404, "recording not found")
    return FileResponse(resolved, media_type="video/mp4")


@app.get("/api/logs")
def logs(
    device: Optional[str] = None,
    level: str = "W",
    tag: Optional[str] = None,
    grep: Optional[str] = None,
    lines: int = 300,
):
    entries = _call(
        adb.logcat_dump, _device(device),
        lines=lines, level=level, tag=tag or None, pattern=grep or None,
    )
    return {"count": len(entries), "entries": [e.to_dict() for e in entries]}


@app.get("/api/crashes")
def crashes(device: Optional[str] = None, package: Optional[str] = None):
    found = _call(adb.crash_log, _device(device), package or None)
    return {"count": len(found), "crashes": [asdict(c) for c in found[:10]]}


@app.get("/api/perf")
def perf(device: Optional[str] = None):
    return _call(adb.get_performance_snapshot, _device(device))


@app.get("/api/thermal")
def thermal(device: Optional[str] = None):
    return _call(adb.get_thermal_state, _device(device))


@app.get("/api/apps")
def apps(device: Optional[str] = None, filter: str = "sideloaded"):
    packages = _call(adb.list_apps, _device(device), filter=filter)
    return {"count": len(packages), "packages": packages}


@app.get("/api/apps/detail")
def app_detail(package: str, device: Optional[str] = None):
    return asdict(_call(adb.get_app_info, package, _device(device)))


@app.post("/api/apps/launch")
def app_launch(package: str, device: Optional[str] = None):
    return {"message": _call(adb.launch_app, package, _device(device))}


@app.post("/api/apps/stop")
def app_stop(package: str, device: Optional[str] = None):
    return {"message": _call(adb.stop_app, package, _device(device))}


@app.post("/api/apps/clear")
def app_clear(package: str, device: Optional[str] = None):
    _call(adb.clear_app_data, package, _device(device))
    return {"message": f"Cleared data for {package}"}


@app.post("/api/apps/uninstall")
def app_uninstall(package: str, device: Optional[str] = None):
    _call(adb.uninstall_app, package, _device(device))
    return {"message": f"Uninstalled {package}"}


@app.post("/api/apps/install")
async def app_install(file: UploadFile, device: Optional[str] = None):
    serial = _device(device)
    if not (file.filename or "").endswith(".apk"):
        raise HTTPException(400, "expected a .apk file")
    with tempfile.NamedTemporaryFile(suffix=".apk", delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name
    try:
        result = _call(adb.install_apk, tmp_path, serial)
        return {"message": f"Installed {file.filename}", "success": result.success}
    finally:
        os.unlink(tmp_path)


@app.get("/api/files")
def files(path: str = "/sdcard/", device: Optional[str] = None):
    entries = _call(adb.list_files, path, _device(device))
    return {
        "path": path,
        "entries": [asdict(e) for e in entries],
    }


@app.get("/api/files/download")
def file_download(path: str, device: Optional[str] = None):
    serial = _device(device)
    local = os.path.join(tempfile.gettempdir(), os.path.basename(path) or "file")
    _call(adb.pull_file, path, local, serial)
    return FileResponse(local, filename=os.path.basename(path))


@app.post("/api/files/upload")
async def file_upload(file: UploadFile, path: str = "/sdcard/", device: Optional[str] = None):
    serial = _device(device)
    local = os.path.join(tempfile.gettempdir(), file.filename or "upload")
    with open(local, "wb") as f:
        f.write(await file.read())
    try:
        dest = path.rstrip("/") + "/" + (file.filename or "upload")
        _call(adb.push_file, local, dest, serial)
        return {"message": f"Pushed {file.filename} to {dest}"}
    finally:
        os.unlink(local)


@app.post("/api/files/delete")
def file_delete(path: str, device: Optional[str] = None):
    _call(adb.delete_file, path, _device(device))
    return {"message": f"Deleted {path}"}


@app.post("/api/wireless")
def wireless(device: Optional[str] = None):
    return asdict(_call(adb.connect_wireless, _device(device)))


@app.post("/api/refresh-rate")
def refresh_rate(rate: int, device: Optional[str] = None):
    return _call(adb.set_refresh_rate, _device(device), rate)


@app.get("/api/tracking")
def tracking(device: Optional[str] = None):
    return _call(adb.get_tracking_status, _device(device))


def main(host: str = "127.0.0.1", port: int = 7700, open_browser: bool = True):
    import threading
    import webbrowser

    import uvicorn

    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(f"http://{host}:{port}")).start()
    # Visor serves plain HTTP (+ an MJPEG stream), no websockets. Pin uvicorn to
    # its bundled h11/no-ws handlers so a stray system `websockets`/`httptools`
    # (e.g. via a system-site-packages venv) can't break startup.
    uvicorn.run(app, host=host, port=port, log_level="warning", ws="none", http="h11")


def run_server_thread(host: str = "127.0.0.1", port: int = 7700):
    """Start the dashboard in a background daemon thread and return once it is
    serving. Used by the desktop app, which needs the main thread for its GUI
    loop. Signal handlers are disabled since we are not on the main thread.
    """
    import threading
    import time

    import uvicorn

    config = uvicorn.Config(
        app, host=host, port=port, log_level="warning", ws="none", http="h11"
    )
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(200):  # wait up to ~10s for startup
        if server.started:
            break
        time.sleep(0.05)
    return server


def launch_desktop_app(host: str = "127.0.0.1", port: int = 7700):
    """Open the dashboard in a native desktop window via pywebview."""
    try:
        import webview
    except ImportError as e:
        raise RuntimeError(
            "The desktop app needs pywebview, which isn't installed.\n"
            "  pipx:  pipx inject visor-dev pywebview\n"
            "  pip:   pip install 'visor-dev[app]'"
        ) from e

    run_server_thread(host, port)
    webview.create_window("Visor", f"http://{host}:{port}", width=1280, height=820)
    webview.start()  # blocks until the window closes; the server thread is a daemon


if __name__ == "__main__":
    main()
