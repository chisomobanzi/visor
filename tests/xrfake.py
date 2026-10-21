"""Stand-ins for a running app: a fake layer socket and a fake frame buffer.

The real layer is C++ inside an OpenXR app; these speak the same protocol
and memory layout so the Python side can be tested without a GPU.
"""

from __future__ import annotations

import json
import os
import socket
import struct
import sys
import tempfile
import threading
from pathlib import Path

from PIL import Image


class FakeLayer:
    """A Unix-socket server answering the layer's JSON-lines protocol."""

    def __init__(self, handler=None):
        self.dir = tempfile.mkdtemp(prefix="vxr")
        self.path = os.path.join(self.dir, "app.sock")
        self.calls: list[tuple[str, dict]] = []
        self.handler = handler or (lambda cmd, args: {})
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv.bind(self.path)
        self._srv.listen(4)
        self._stop = False
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stop:
            try:
                conn, _ = self._srv.accept()
            except OSError:
                return
            threading.Thread(target=self._client, args=(conn,), daemon=True).start()

    def _client(self, conn):
        f = conn.makefile("rwb")
        for line in f:
            req = json.loads(line)
            cmd, args = req.get("cmd"), req.get("args", {})
            self.calls.append((cmd, args))
            try:
                result = self.handler(cmd, args)
                reply = {"id": req.get("id"), "ok": True, "result": result}
            except Exception as e:  # mirrors the layer's {"ok": false, "error": ...}
                reply = {"id": req.get("id"), "ok": False, "error": str(e)}
            f.write((json.dumps(reply) + "\n").encode())
            f.flush()
        conn.close()

    def close(self):
        self._stop = True
        self._srv.close()


def write_frame_buffer(name: str, image: Image.Image, seq: int = 1, frame: int = 42, eyes: int = 1,
                       slot: int = 0, display_time: int = 123, writer: int = 2) -> Path:
    """Write a Visor frame buffer to /dev/shm exactly as the runtime does."""
    w, h = image.size
    slots, slot_bytes = 3, w * h * 4
    path = Path("/dev/shm") / name.lstrip("/")
    buf = bytearray(4096 + slots * slot_bytes)
    struct.pack_into("<8sIIQQII", buf, 0, b"VISORXR1", 1, slots, slot_bytes, seq, slot, writer)
    struct.pack_into("<QIIIIqqqI", buf, 64 + 64 * slot, seq, w, h, w * 4, eyes, frame, display_time, 999, 0)
    rgba = image.convert("RGBA").tobytes()
    buf[4096 + slot * slot_bytes: 4096 + slot * slot_bytes + len(rgba)] = rgba
    path.write_bytes(bytes(buf))
    return path


needs_posix_shm = sys.platform != "linux"
