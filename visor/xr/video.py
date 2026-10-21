"""Record what the virtual headset sees to an MP4.

The runtime is asked to publish frames continuously; a thread copies the
newest one into ffmpeg at a steady rate, repeating a frame when the app is
slower than the video (so the video's clock stays honest about hitches).
"""

from __future__ import annotations

import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from visor.xr.client import XrClient
from visor.xr.frames import FrameError, FrameReader


class Recorder:
    def __init__(self, client: XrClient, out: str | Path, fps: int = 24, width: int = 960, eyes: str = "left"):
        if not shutil.which("ffmpeg"):
            raise RuntimeError("recording needs ffmpeg (sudo apt install ffmpeg)")
        self.client = client
        self.out = Path(out)
        self.fps = fps
        self.width = width
        self.eyes = eyes
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._proc: Optional[subprocess.Popen] = None
        self.frames_written = 0
        self.unique_frames = 0
        self.error: Optional[str] = None
        self._prev_cfg: dict = {}

    def start(self) -> "Recorder":
        cfg = self.client.configure()
        self._prev_cfg = {k: cfg.get(k) for k in ("capture_fps", "capture_width", "eyes")}
        self.client.configure(capture_width=self.width, eyes=self.eyes, capture_fps=self.fps)
        first = self.client.capture()
        self._size = first.image.size
        self.out.parent.mkdir(parents=True, exist_ok=True)
        self._proc = subprocess.Popen(
            ["ffmpeg", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
             "-s", f"{self._size[0]}x{self._size[1]}", "-r", str(self.fps), "-i", "-",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
             "-movflags", "+faststart", str(self.out)],
            stdin=subprocess.PIPE,
        )
        self._shm = self.client.call("capture", fresh=False)["shm"]
        self._thread = threading.Thread(target=self._run, args=(first.image,), daemon=True)
        self._thread.start()
        return self

    def _run(self, first_image) -> None:
        reader = FrameReader(self._shm)
        last_seq = -1
        current = first_image.tobytes()
        period = 1.0 / self.fps
        next_t = time.monotonic()
        try:
            while not self._stop.is_set():
                try:
                    seq = reader.latest_seq()
                    if seq != last_seq:
                        frame = reader.read()
                        if frame.image.size == self._size:
                            current = frame.image.tobytes()
                            self.unique_frames += 1
                        last_seq = seq
                except FrameError:
                    pass
                assert self._proc is not None and self._proc.stdin is not None
                self._proc.stdin.write(current)
                self.frames_written += 1
                next_t += period
                delay = next_t - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                else:
                    next_t = time.monotonic()
        except (BrokenPipeError, OSError) as e:
            self.error = str(e)
        finally:
            reader.close()

    def stop(self) -> Path:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        if self._proc and self._proc.stdin:
            try:
                self._proc.stdin.close()
            except OSError:
                pass
            self._proc.wait(timeout=60)
        try:
            restore = {k: v for k, v in self._prev_cfg.items() if v is not None}
            if restore:
                self.client.configure(**restore)
        except Exception:
            pass
        return self.out

    def __enter__(self) -> "Recorder":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()
