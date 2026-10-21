"""Read frames the virtual headset publishes into POSIX shared memory.

The writer is either Visor's patched Monado compositor or the layer itself;
both use the layout documented in native/monado (null_visor_capture.c):
a 4 KiB header, then ``slot_count`` RGBA8 images. Each slot carries a
sequence number that is zero while it is being written, so a reader can tell
a torn copy from a good one.
"""

from __future__ import annotations

import mmap
import os
import struct
import time
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

_HEADER = struct.Struct("<8sIIQQII")  # magic, version, slots, slot_bytes, latest_seq, latest_slot, writer
_SLOT = struct.Struct("<QIIIIqqqI")  # seq, w, h, stride, eyes, frame, display_time, capture_ns, format
_DATA_OFFSET = 4096


class FrameError(RuntimeError):
    pass


@dataclass
class Frame:
    image: Image.Image
    seq: int
    frame: int  # the app's frame index when known, else -1
    eyes: int  # bit 0 left, bit 1 right (side by side when both)
    capture_ns: int
    display_time: int = 0  # when the app meant it to be seen (runtime clock); repeats while paused

    @property
    def width(self) -> int:
        return self.image.width

    @property
    def height(self) -> int:
        return self.image.height

    def eye(self, which: str = "left") -> Image.Image:
        """One eye from a side-by-side capture (or the image itself)."""
        if self.eyes != 3:
            return self.image
        half = self.image.width // 2
        box = (0, 0, half, self.image.height) if which == "left" else (half, 0, self.image.width, self.image.height)
        return self.image.crop(box)


class FrameReader:
    """Maps a frame buffer by name ("/visor-xr-1234") and reads the newest image."""

    def __init__(self, shm_name: str):
        self.name = shm_name
        self._mm: mmap.mmap | None = None
        self._size = 0

    def _map(self) -> mmap.mmap:
        path = Path("/dev/shm") / self.name.lstrip("/")
        size = path.stat().st_size if path.exists() else 0
        if self._mm is None or size != self._size:
            self.close()
            if not size:
                raise FrameError(f"no frame buffer at {path}")
            fd = os.open(path, os.O_RDONLY)
            try:
                self._mm = mmap.mmap(fd, size, prot=mmap.PROT_READ)
            finally:
                os.close(fd)
            self._size = size
            magic = self._mm[:8]
            if magic != b"VISORXR1":
                self.close()
                raise FrameError(f"{path} is not a Visor frame buffer")
        return self._mm

    def close(self) -> None:
        if self._mm is not None:
            self._mm.close()
            self._mm = None

    def latest_seq(self) -> int:
        mm = self._map()
        return _HEADER.unpack_from(mm, 0)[4]

    def read(self, retries: int = 5) -> Frame:
        """The newest complete frame. Retries if the writer laps us mid-copy."""
        for _ in range(retries):
            mm = self._map()
            _, _, slots, slot_bytes, latest, slot, _ = _HEADER.unpack_from(mm, 0)
            if latest == 0:
                raise FrameError("no frame published yet")
            hdr = 64 + 64 * slot
            seq, w, h, stride, eyes, frame, disp, cap_ns, _fmt = _SLOT.unpack_from(mm, hdr)
            if seq == 0 or w == 0 or h == 0:
                time.sleep(0.002)
                continue
            off = _DATA_OFFSET + slot_bytes * slot
            pixels = mm[off:off + stride * h]
            if _SLOT.unpack_from(mm, hdr)[0] != seq:
                continue  # overwritten while we copied
            image = Image.frombuffer("RGBA", (w, h), pixels, "raw", "RGBA", stride, 1).convert("RGB")
            return Frame(image=image, seq=seq, frame=frame, eyes=eyes, capture_ns=cap_ns, display_time=disp)
        raise FrameError("frame buffer kept changing under the reader")

    def __enter__(self) -> "FrameReader":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
