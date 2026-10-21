"""Visor's virtual headset: run any OpenXR app headless, drive it, watch it.

    from visor.xr import AppSpec, SimInstance

    sim = SimInstance(AppSpec.resolve("demo")).start()
    xr = sim.wait_ready()
    xr.look(30, -10, over=36)
    xr.press("right", "trigger")
    xr.screenshot("view.png")
    sim.stop()
"""

from visor.xr.client import XrClient, XrError
from visor.xr.frames import Frame, FrameReader
from visor.xr.headsets import HEADSETS, Headset, get_headset
from visor.xr.sim import AppSpec, SimConfig, SimError, SimInstance, connect, running

__all__ = [
    "AppSpec",
    "Frame",
    "FrameReader",
    "HEADSETS",
    "Headset",
    "SimConfig",
    "SimError",
    "SimInstance",
    "XrClient",
    "XrError",
    "connect",
    "get_headset",
    "running",
]
