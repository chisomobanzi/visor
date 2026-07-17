"""Screenshot, screen recording, and scrcpy casting."""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Optional

from visor.adb.wrapper import ADBCommandError, ADBWrapper, get_adb
from visor.utils.constants import MAX_SCREENRECORD_SECONDS
from visor.utils.host import detached_popen_kwargs, install_hint

# Quest shows a per-eye, barrel-distorted image warped for the physical lens, so
# a raw capture looks like a fisheye shot through the lens. These ffmpeg
# lenscorrection coefficients undo most of it; the crop trims residual black
# edges. Tuned against Quest 3 capture; roughly right for Quest 2/Pro too.
_LENS_K1 = -0.5
_LENS_K2 = 0.06
_LENS_CROP = 0.84


@dataclass
class Screenshot:
    path: str
    width: Optional[int]
    height: Optional[int]
    size_bytes: int


@dataclass
class Recording:
    path: str
    duration_seconds: int
    size_bytes: int


@dataclass
class CastSession:
    pid: int
    message: str


def _timestamped(directory: str, ext: str) -> str:
    os.makedirs(directory, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return os.path.join(directory, f"quest-{stamp}.{ext}")


def _screencap(adb: ADBWrapper, device_id: Optional[str]) -> bytes:
    return adb.run(
        ["exec-out", "screencap", "-p"],
        device_id=device_id,
        timeout=30,
        binary=True,
    )


def screenshot(
    device_id: Optional[str] = None,
    save_path: Optional[str] = None,
    wake: bool = True,
    adb: Optional[ADBWrapper] = None,
) -> Screenshot:
    """Capture the current screen and save it locally as PNG.

    Uses `exec-out screencap -p` (raw binary over the wire — no CRLF
    mangling, no temp file on device). When the headset isn't being worn
    the display is off and screencap returns nothing; with wake=True we
    simulate the proximity sensor (prox_close broadcast) to wake the
    display, capture, then restore normal sensor behavior.
    """
    adb = adb or get_adb()
    data = _screencap(adb, device_id)

    if (not data or not data.startswith(b"\x89PNG")) and wake:
        adb.shell(
            "am broadcast -a com.oculus.vrpowermanager.prox_close",
            device_id=device_id,
        )
        time.sleep(3)
        try:
            data = _screencap(adb, device_id)
        finally:
            # hand proximity control back to the sensor so the display
            # doesn't stay on draining battery
            try:
                adb.shell(
                    "am broadcast -a com.oculus.vrpowermanager.automation_disable",
                    device_id=device_id,
                )
            except ADBCommandError:
                pass

    if not data or not data.startswith(b"\x89PNG"):
        raise ADBCommandError(
            "Screenshot capture returned no image data even after waking "
            "the display. Put the headset on (or check the USB connection) "
            "and retry."
        )

    save_path = save_path or _timestamped("screenshots", "png")
    parent = os.path.dirname(os.path.abspath(save_path))
    os.makedirs(parent, exist_ok=True)
    with open(save_path, "wb") as f:
        f.write(data)

    width = height = None
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as img:
            width, height = img.size
    except Exception:
        pass

    return Screenshot(
        path=save_path, width=width, height=height, size_bytes=len(data)
    )


def screen_record(
    device_id: Optional[str] = None,
    duration_seconds: int = 30,
    save_path: Optional[str] = None,
    adb: Optional[ADBWrapper] = None,
) -> Recording:
    """Record the screen for N seconds and pull the video locally.

    Android caps screenrecord at 180 seconds.
    """
    if not 1 <= duration_seconds <= MAX_SCREENRECORD_SECONDS:
        raise ValueError(
            f"duration_seconds must be 1-{MAX_SCREENRECORD_SECONDS} "
            "(Android screenrecord limit is 180s)."
        )
    adb = adb or get_adb()
    save_path = save_path or _timestamped("recordings", "mp4")

    remote = f"/sdcard/visor-recording-{int(duration_seconds)}s.mp4"
    adb.shell(
        f"screenrecord --time-limit {duration_seconds} {remote}",
        device_id=device_id,
        timeout=duration_seconds + 30,
    )
    try:
        parent = os.path.dirname(os.path.abspath(save_path))
        os.makedirs(parent, exist_ok=True)
        adb.run(["pull", remote, save_path], device_id=device_id, timeout=300)
    finally:
        adb.shell(f"rm -f {remote}", device_id=device_id)

    size = os.path.getsize(save_path) if os.path.exists(save_path) else 0
    return Recording(
        path=save_path, duration_seconds=duration_seconds, size_bytes=size
    )


def start_screen_stream(
    device_id: Optional[str] = None,
    size: str = "1664x896",
    fps: int = 15,
    quality: int = 7,
    bit_rate: str = "8M",
    crop_left_eye: bool = True,
    undistort: bool = True,
    adb: Optional[ADBWrapper] = None,
) -> tuple[subprocess.Popen, subprocess.Popen]:
    """Start a live MJPEG stream of the headset screen.

    Pipes `adb exec-out screenrecord --output-format=h264 -` (continuous
    H.264, the same mechanism scrcpy uses) through ffmpeg into an MJPEG
    multipart stream (boundary "ffmpeg") readable straight from the
    returned ffmpeg process's stdout — browsers render it natively in an
    <img> tag.

    Returns (adb_process, ffmpeg_process). Caller owns cleanup: terminate
    both when done. Android caps each screenrecord run at 180s, so a
    long-lived consumer must respawn on EOF.
    """
    adb = adb or get_adb()
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise ADBCommandError(
            "ffmpeg is required for live streaming. " + install_hint("ffmpeg")
        )

    adb_cmd = [adb.adb_path]
    if device_id:
        adb_cmd += ["-s", device_id]
    adb_cmd += [
        "exec-out", "screenrecord", "--output-format=h264",
        "--size", size, "--bit-rate", bit_rate, "-",
    ]
    filters = []
    if crop_left_eye:
        filters.append("crop=iw/2:ih:0:0")
    if undistort:
        # Undo the Quest lens (barrel) distortion so the eye buffer reads as a
        # flat rectangular view instead of a fisheye shot through the lens,
        # then trim the residual black edges.
        filters.append(
            f"lenscorrection=cx=0.5:cy=0.5:k1={_LENS_K1}:k2={_LENS_K2}:i=bilinear"
        )
        filters.append(f"crop=iw*{_LENS_CROP}:ih*{_LENS_CROP}")
    filters.append(f"fps={fps}")
    vf = ",".join(filters)
    ffmpeg_cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error",
        "-f", "h264", "-i", "-",
        "-vf", vf, "-q:v", str(quality), "-f", "mpjpeg", "-",
    ]

    adb_proc = subprocess.Popen(
        adb_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    )
    ffmpeg_proc = subprocess.Popen(
        ffmpeg_cmd,
        stdin=adb_proc.stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    adb_proc.stdout.close()  # let ffmpeg own the pipe so adb gets SIGPIPE
    return adb_proc, ffmpeg_proc


def stop_stream_processes(*procs: subprocess.Popen) -> None:
    """Terminate stream processes, escalating to kill."""
    for proc in procs:
        if proc.poll() is None:
            proc.terminate()
    for proc in procs:
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()  # reap, or the killed process lingers as a zombie


def start_cast(
    device_id: Optional[str] = None,
    adb: Optional[ADBWrapper] = None,
    extra_args: Optional[list[str]] = None,
) -> CastSession:
    """Start scrcpy casting in a detached process. Requires scrcpy installed."""
    scrcpy = shutil.which("scrcpy")
    if not scrcpy:
        raise ADBCommandError(
            "scrcpy is not installed (required for casting). " + install_hint("scrcpy")
        )
    cmd = [scrcpy]
    if device_id:
        cmd += ["-s", device_id]
    # Quest renders both eyes; crop roughly to one eye for a watchable view
    cmd += extra_args or []

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **detached_popen_kwargs(),
    )
    time.sleep(0.5)
    if proc.poll() is not None:
        raise ADBCommandError(
            "scrcpy exited immediately — is the device connected and screen on?"
        )
    return CastSession(
        pid=proc.pid,
        message=f"scrcpy casting started (pid {proc.pid}). "
        "Close the scrcpy window to stop.",
    )
