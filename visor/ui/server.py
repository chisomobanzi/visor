"""Visor web dashboard — FastAPI backend reusing the visor.adb library.

Launch with `visor ui` (serves on localhost only; this exposes device
control, so it deliberately does not bind to 0.0.0.0).
"""

from __future__ import annotations

import io
import os
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response

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
    return (_STATIC / "index.html").read_text()


@app.get("/api/devices")
def devices():
    try:
        return [asdict(d) for d in adb.discover_devices()]
    except VisorError as e:
        raise HTTPException(status_code=503, detail=str(e)) from None


@app.get("/api/info")
def info(device: Optional[str] = None):
    return asdict(_call(adb.get_device_info, _device(device)))


@app.get("/api/screenshot")
def screenshot(device: Optional[str] = None, eye: str = "left"):
    serial = _device(device)
    path = os.path.join(tempfile.gettempdir(), f"visor-ui-{serial}.png")
    shot = _call(adb.screenshot, serial, path)

    from PIL import Image

    with Image.open(shot.path) as img:
        width, height = img.size
        if eye == "left" and width / height > 1.6:
            img = img.crop((0, 0, width // 2, height))
        if img.width > 1600:
            ratio = 1600 / img.width
            img = img.resize((1600, int(img.height * ratio)))
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=88)
    return Response(
        content=buf.getvalue(),
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store"},
    )


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
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
