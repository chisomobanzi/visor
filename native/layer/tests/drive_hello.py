"""Drive hello_xr through the Visor layer: pose a hand, squeeze, turn the head.

Usage: python3 drive_hello.py <socket> <outdir>
Writes before/after PNGs so a human (or a test) can see the injection worked.
"""
import json
import mmap
import os
import socket
import struct
import sys
import time

from PIL import Image

sock_path, outdir = sys.argv[1], sys.argv[2]
s = socket.socket(socket.AF_UNIX)
s.connect(sock_path)
f = s.makefile("rwb")


def call(cmd, **args):
    f.write((json.dumps({"id": 1, "cmd": cmd, "args": args}) + "\n").encode())
    f.flush()
    reply = json.loads(f.readline())
    if not reply.get("ok"):
        raise RuntimeError(f"{cmd}: {reply.get('error')}")
    return reply["result"]


def grab(name):
    c = call("capture", timeout_ms=3000)
    fd = os.open("/dev/shm" + c["shm"], os.O_RDONLY)
    m = mmap.mmap(fd, 0, prot=mmap.PROT_READ)
    _, _, _, slot_bytes, _, slot = struct.unpack_from("<8sIIQQI", m, 0)
    seq, w, h, stride, eyes, frame = struct.unpack_from("<QIIIIq", m, 64 + 64 * slot)
    off = 4096 + slot_bytes * slot
    Image.frombuffer("RGBA", (w, h), m[off:off + stride * h], "raw", "RGBA", stride, 1).convert("RGB").save(
        os.path.join(outdir, name))
    print(f"{name}: frame {frame} {w}x{h}")


def wait_frames(n):
    start = call("state")["frame"]
    while call("state")["frame"] < start + n:
        time.sleep(0.005)


# Bring both hands up in front of the face.
call("set", left={"pos": [-0.15, 1.55, -0.5], "ypr": [0, 0, 0]}, right={"pos": [0.15, 1.55, -0.5], "ypr": [0, 0, 0]})
wait_frames(3)
grab("hands_up.png")
# Squeeze the right hand: hello_xr shrinks the cube it draws there.
call("set", right={"squeeze": 1.0})
wait_frames(3)
acts = {a["name"]: a.get("state") for a in call("info")["actions"]}
print("grab_object state:", acts.get("grab_object"))
grab("right_squeezed.png")
# Turn the head 40 degrees to the left over half a second.
call("set", head={"ypr": [40, 0, 0]}, over=36)
wait_frames(40)
grab("head_turned.png")
haptics = [e for e in call("events")["events"] if e["type"] == "haptic"]
print("haptic events from the app:", len(haptics), haptics[-1] if haptics else None)
print("stats:", {k: round(v, 2) for k, v in call("stats").items() if isinstance(v, (int, float))})
