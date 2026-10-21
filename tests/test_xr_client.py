"""The Python side of the virtual headset: protocol, pose maths, frames."""

import math
import sys

import pytest
from PIL import Image

from tests.xrfake import FakeLayer, needs_posix_shm, write_frame_buffer

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Unix sockets / POSIX shm")


def _state(**over):
    st = {
        "frame": 10, "render_frame": 10, "session": "focused", "paused": False, "ipd": 0.064,
        "head": {"pos": [0.0, 1.65, 0.0], "rot": [0.0, 0.0, 0.0, 1.0], "ypr": [0.0, 0.0, 0.0]},
        "views": [{"fov": [-54.0, 45.0, 47.0, -51.0]}, {"fov": [-45.0, 54.0, 47.0, -51.0]}],
    }
    st.update(over)
    return st


@pytest.fixture
def layer():
    state = _state()

    def handler(cmd, args):
        if cmd == "state":
            return state
        if cmd == "boom":
            raise ValueError("the layer said no")
        return {"echo": cmd, "args": args}

    fake = FakeLayer(handler)
    yield fake
    fake.close()


class TestProtocol:
    def test_round_trip(self, layer):
        from visor.xr import XrClient

        with XrClient(layer.path) as xr:
            assert xr.call("ping", timeout=2) == {"echo": "ping", "args": {}}
            xr.set(right={"trigger": 1.0}, over=12)
        assert layer.calls[-1] == ("set", {"right": {"trigger": 1.0}, "over": 12})

    def test_layer_errors_become_xrerror(self, layer):
        from visor.xr import XrClient, XrError

        with XrClient(layer.path) as xr, pytest.raises(XrError, match="the layer said no"):
            xr.call("boom")

    def test_unreachable_app_is_a_clear_error(self, tmp_path):
        from visor.xr import XrClient, XrError

        with pytest.raises(XrError, match="can't reach the app"):
            XrClient(tmp_path / "nope.sock").call("ping")

    def test_press_schedules_down_then_up(self, layer):
        from visor.xr import XrClient

        with XrClient(layer.path) as xr:
            xr.press("left", "x", frames=9)
        cmd, args = layer.calls[-1]
        assert cmd == "schedule"
        assert args["events"] == [
            {"frame": 0, "set": {"left": {"x": True}}},
            {"frame": 9, "set": {"left": {"x": False}}},
        ]

    def test_analog_buttons_press_to_full_travel(self, layer):
        from visor.xr import XrClient

        with XrClient(layer.path) as xr:
            xr.press("right", "grip")
        events = layer.calls[-1][1]["events"]
        assert events[0]["set"] == {"right": {"squeeze": 1.0}}
        assert events[1]["set"] == {"right": {"squeeze": 0.0}}

    def test_unknown_button(self, layer):
        from visor.xr import XrClient, XrError

        with XrClient(layer.path) as xr, pytest.raises(XrError, match="unknown button"):
            xr.press("right", "turbo")


class TestPose:
    def test_look_ypr_straight_ahead(self):
        from visor.xr.client import look_ypr

        yaw, pitch, _ = look_ypr([0, 1.6, 0], [0, 1.6, -2])
        assert abs(yaw) < 1e-6 and abs(pitch) < 1e-6

    def test_look_ypr_left_and_down(self):
        from visor.xr.client import look_ypr

        yaw, pitch, _ = look_ypr([0, 1.6, 0], [-1, 0.6, -1])
        assert yaw == pytest.approx(45.0)
        assert pitch == pytest.approx(-35.26, abs=0.01)

    def test_qrot_yaw_turns_forward_to_left(self):
        from visor.xr.client import qrot

        q = [0.0, math.sin(math.radians(45)), 0.0, math.cos(math.radians(45))]  # 90 deg about +Y
        assert qrot(q, [0, 0, -1]) == pytest.approx([-1.0, 0.0, 0.0], abs=1e-6)

    def test_centre_pixel_ray_follows_fov_asymmetry(self, layer):
        from visor.xr import XrClient

        with XrClient(layer.path) as xr:
            origin, d = xr.pixel_ray(480, 503, 960, 1006)
        # Left eye: 54 deg out vs 45 in, 47 up vs 51 down, so the image centre
        # looks ~10.7 deg left and ~4.6 deg down of straight ahead.
        assert origin == pytest.approx([-0.032, 1.65, 0.0])
        yaw = math.degrees(math.atan2(-d[0], -d[2]))
        pitch = math.degrees(math.asin(d[1]))
        assert yaw == pytest.approx(10.66, abs=0.05)
        assert pitch == pytest.approx(-4.55, abs=0.05)

    def test_pixel_ray_hits_the_corner_angles(self, layer):
        from visor.xr import XrClient

        with XrClient(layer.path) as xr:
            _, d = xr.pixel_ray(0, 0, 960, 1006)
        assert math.degrees(math.atan2(-d[0], -d[2])) == pytest.approx(54.0, abs=0.01)


@pytest.mark.skipif(needs_posix_shm, reason="POSIX shared memory")
class TestFrames:
    def test_reads_the_latest_slot(self):
        from visor.xr.frames import FrameReader

        img = Image.new("RGB", (32, 20), (10, 200, 30))
        name = "/visor-test-frames-a"
        path = write_frame_buffer(name, img, seq=7, frame=99, slot=1)
        try:
            with FrameReader(name) as r:
                f = r.read()
            assert f.seq == 7 and f.frame == 99 and f.image.size == (32, 20)
            assert f.image.getpixel((5, 5)) == (10, 200, 30)
        finally:
            path.unlink()

    def test_side_by_side_eyes_split(self):
        from visor.xr.frames import FrameReader

        img = Image.new("RGB", (40, 10), (255, 0, 0))
        img.paste((0, 0, 255), (20, 0, 40, 10))
        name = "/visor-test-frames-b"
        path = write_frame_buffer(name, img, eyes=3)
        try:
            with FrameReader(name) as r:
                f = r.read()
            assert f.eye("left").getpixel((1, 1)) == (255, 0, 0)
            assert f.eye("right").getpixel((1, 1)) == (0, 0, 255)
        finally:
            path.unlink()

    def test_nothing_published_yet(self):
        from visor.xr.frames import FrameError, FrameReader

        name = "/visor-test-frames-c"
        path = write_frame_buffer(name, Image.new("RGB", (4, 4)), seq=0)
        try:
            with FrameReader(name) as r, pytest.raises(FrameError, match="no frame"):
                r.read()
        finally:
            path.unlink()

    def test_not_a_frame_buffer(self):
        from pathlib import Path

        from visor.xr.frames import FrameError, FrameReader

        path = Path("/dev/shm/visor-test-frames-d")
        path.write_bytes(b"x" * 8192)
        try:
            with pytest.raises(FrameError, match="not a Visor frame buffer"):
                FrameReader("/visor-test-frames-d").read()
        finally:
            path.unlink()
