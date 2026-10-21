"""Bugs a playtest finds without anyone having to notice them.

The agent reports what it judges to be wrong; these oracles report what is
measurably wrong — the app died, froze, went black, stopped reacting to
input, dropped frames, or logged errors — so every run is worth something,
even a scripted one.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Optional

from PIL import Image, ImageChops, ImageStat

SEVERITIES = ("blocker", "major", "minor", "note")


@dataclass
class Issue:
    title: str
    severity: str
    category: str
    description: str
    step: int
    frame: int
    source: str = "agent"
    screenshot: Optional[str] = None
    steps_to_reproduce: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)
    id: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _thumb(img: Image.Image) -> Image.Image:
    return img.convert("L").resize((64, 64), Image.BILINEAR)


def image_change(a: Image.Image, b: Image.Image) -> float:
    """Mean absolute difference (0-255) between two frames, at thumbnail scale."""
    return ImageStat.Stat(ImageChops.difference(_thumb(a), _thumb(b))).mean[0]


def brightness(img: Image.Image) -> float:
    return ImageStat.Stat(_thumb(img)).mean[0]


# Unreal / generic log lines worth surfacing, and boilerplate that isn't.
_LOG_PREFIX = re.compile(r"^\[[^\]]*\]\[\s*\d+\]")
_INTERESTING = [
    (re.compile(r"Accessed None trying to read", re.I), "major", "functional", "Blueprint read a null reference"),
    (re.compile(r"Script Stack|Script call stack", re.I), "minor", "functional", "Blueprint runtime error"),
    (re.compile(r"Assertion failed", re.I), "blocker", "crash", "Assertion failed"),
    (re.compile(r"Ensure condition failed", re.I), "minor", "functional", "Ensure failed"),
    (re.compile(r"\bError:\s", re.I), "minor", "functional", "Error logged"),
    (re.compile(r"Unhandled exception|Exception thrown", re.I), "major", "crash", "Exception"),
]
_BENIGN = re.compile(
    r"LogClass: Error: .* is not initialized properly|Failed to read file .*SaveGames|"
    r"LogLinker: Warning|LogShaderCompilers|No local boot hotfix|LogDerivedDataCache|"
    r"Failed loading OVRPlugin|Failed to initialize Engine Telemetry|LogMRUK: Error: Failed to load MR Utility",
    re.I,
)


def scan_log(lines: list[str]) -> list[tuple[str, str, str, str]]:
    """Distinct interesting log messages: (message, severity, category, label)."""
    seen: dict[str, tuple[str, str, str, str]] = {}
    for raw in lines:
        if _BENIGN.search(raw):
            continue
        line = _LOG_PREFIX.sub("", raw).strip()
        for pattern, severity, category, label in _INTERESTING:
            if pattern.search(line):
                key = re.sub(r"\d+", "#", line)[:160]
                if key not in seen:
                    seen[key] = (line[:400], severity, category, label)
                break
    return list(seen.values())


class Oracles:
    """Watches a playtest step by step and raises issues on objective signals."""

    def __init__(self, refresh_hz: float = 72.0, freeze_steps: int = 4, black_steps: int = 3):
        self.refresh_hz = refresh_hz or 72.0
        self.freeze_steps = freeze_steps
        self.black_steps = black_steps
        self._prev: Optional[Image.Image] = None
        self._still = 0
        self._black = 0
        self._raised: set[str] = set()
        self._log_seen: set[str] = set()

    def _once(self, key: str) -> bool:
        if key in self._raised:
            return False
        self._raised.add(key)
        return True

    def after_step(self, step: int, frame: int, image: Image.Image, acted: bool,
                   screenshot: Optional[str]) -> list[Issue]:
        out: list[Issue] = []
        if self._prev is not None:
            change = image_change(self._prev, image)
            self._still = self._still + 1 if (acted and change < 0.6) else 0
            if self._still >= self.freeze_steps and self._once("frozen"):
                out.append(Issue(
                    title="View stopped responding to input",
                    severity="major", category="functional", source="oracle:freeze",
                    description=(f"The picture barely changed for {self._still} consecutive actions. Either the "
                                 "app ignored the input or it's soft-locked (frozen UI, stuck state)."),
                    step=step, frame=frame, screenshot=screenshot,
                    extra={"steps_unchanged": self._still},
                ))
        self._black = self._black + 1 if brightness(image) < 4.0 else 0
        if self._black >= self.black_steps and self._once("black"):
            out.append(Issue(
                title="Screen stayed black",
                severity="major", category="visual", source="oracle:black",
                description=f"The view was (nearly) all black for {self._black} steps in a row.",
                step=step, frame=frame, screenshot=screenshot,
            ))
        self._prev = image
        return out

    def logs(self, step: int, frame: int, new_lines: list[str]) -> list[Issue]:
        out = []
        for message, severity, category, label in scan_log(new_lines):
            key = re.sub(r"\d+", "#", message)[:160]
            if key in self._log_seen:
                continue
            self._log_seen.add(key)
            out.append(Issue(
                title=f"{label}: {message[:90]}",
                severity=severity, category=category, source="oracle:log",
                description=message, step=step, frame=frame,
            ))
        return out

    def performance(self, stats: dict, step: int, frame: int, label: str = "") -> list[Issue]:
        out = []
        fps = float(stats.get("fps") or 0.0)
        refresh = float(stats.get("refresh_hz") or self.refresh_hz)
        frames = int(stats.get("window_frames") or 0)
        if frames < 30:
            return out
        period_ms = 1000.0 / refresh
        if fps and fps < refresh * 0.9 and self._once("fps" + label):
            out.append(Issue(
                title=f"Frame rate below the display rate ({fps:.0f} of {refresh:.0f} fps){label}",
                severity="major" if fps < refresh * 0.75 else "minor",
                category="performance", source="oracle:perf",
                description=(f"Over {frames} frames the app averaged {fps:.1f} fps against a {refresh:.0f} Hz "
                             f"display; p95 frame time {stats.get('frame_ms_p95', 0):.1f} ms, p99 "
                             f"{stats.get('frame_ms_p99', 0):.1f} ms (budget {period_ms:.1f} ms). Meta's VRC "
                             "requires sustaining the display rate."),
                step=step, frame=frame, extra={"stats": stats},
            ))
        p99 = float(stats.get("frame_ms_p99") or 0.0)
        if p99 > 2.5 * period_ms and self._once("hitch" + label):
            out.append(Issue(
                title=f"Hitches: 1% of frames take {p99:.0f} ms{label}",
                severity="minor", category="performance", source="oracle:perf",
                description=(f"p99 frame time {p99:.1f} ms is over 2.5x the {period_ms:.1f} ms budget; "
                             "players feel these as stutters (and they're uncomfortable in VR)."),
                step=step, frame=frame, extra={"stats": stats},
            ))
        return out
