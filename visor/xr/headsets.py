"""Headset profiles for the virtual headset.

A profile is everything an app can observe about the hardware it runs on:
eye-buffer size, refresh rates, per-eye field of view, IPD and where the
controller grip sits relative to its pointing ray. The defaults are a Quest 3.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace


@dataclass(frozen=True)
class Headset:
    key: str
    name: str
    eye_width: int  # recommended eye-buffer size at render scale 1.0
    eye_height: int
    refresh_rates: tuple[int, ...]  # first one is the boot rate
    # Left eye, degrees: outer (left), inner (right), up, down. The right eye
    # mirrors it.
    fov: tuple[float, float, float, float]
    ipd: float = 0.063
    # Controller geometry, see native/layer/src/rig.cpp: degrees the grip is
    # pitched up from the aim ray, and its offset in the aim frame (metres).
    grip_pitch: float = 90.0
    grip_pos: tuple[float, float, float] = (0.0, -0.03, 0.04)
    system_name: str = ""

    def scaled(self, render_scale: float) -> "Headset":
        """Same headset with a smaller (or larger) eye buffer, e.g. for fleets."""
        s = max(0.1, min(2.0, render_scale))
        return replace(
            self,
            eye_width=max(64, int(round(self.eye_width * s / 8)) * 8),
            eye_height=max(64, int(round(self.eye_height * s / 8)) * 8),
        )


HEADSETS: dict[str, Headset] = {
    "quest3": Headset(
        key="quest3",
        name="Meta Quest 3",
        eye_width=1680,
        eye_height=1760,
        refresh_rates=(72, 80, 90, 120),
        fov=(54.0, 45.0, 47.0, 51.0),
    ),
    "quest3s": Headset(
        key="quest3s",
        name="Meta Quest 3S",
        eye_width=1440,
        eye_height=1584,
        refresh_rates=(72, 80, 90, 120),
        fov=(52.0, 44.0, 47.0, 51.0),
    ),
    "quest2": Headset(
        key="quest2",
        name="Meta Quest 2",
        eye_width=1440,
        eye_height=1584,
        refresh_rates=(72, 80, 90, 120),
        fov=(52.0, 44.0, 45.0, 50.0),
    ),
    "questpro": Headset(
        key="questpro",
        name="Meta Quest Pro",
        eye_width=1600,
        eye_height=1696,
        refresh_rates=(72, 90),
        fov=(52.0, 43.0, 43.0, 50.0),
    ),
}

DEFAULT_HEADSET = "quest3"


def get_headset(key: str | None) -> Headset:
    key = (key or DEFAULT_HEADSET).lower().replace(" ", "").replace("-", "")
    if key not in HEADSETS:
        raise ValueError(f"unknown headset '{key}' (have: {', '.join(HEADSETS)})")
    return HEADSETS[key]


@dataclass
class Body:
    """Who is wearing the headset: the persona's physical setup."""

    eye_height: float = 1.65  # metres; ~1.20 seated
    handedness: str = "right"
    extras: dict = field(default_factory=dict)
