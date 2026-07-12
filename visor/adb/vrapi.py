"""Parser for the VrApi logcat line — Quest's built-in per-second VR
performance report.

While a VR app runs, the OpenXR runtime logs one `VrApi` line per second
per process containing frame timing, CPU/GPU levels and clocks,
utilization, and memory. Field sets vary between processes and OS builds,
so parsing is key-value tolerant: every field is optional except FPS.

Real v14 example (Voyager under load):
  FPS=72/72,Prd=36ms,Tear=0,Early=0,Stale=0,Stale2/5/10/max=0/0/0/0,
  VSnc=1,Lat=-4,Fov=0D,CPU4/GPU=2/5,1382/599MHz,OC=3F,...,Free=3247MB,
  Temp=32.0C/0.0C,TW=1.92ms,App=8.29ms,...,GPU%=0.81,CPU%=0.53(W0.55),...
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Optional

_LOGCAT_VRAPI_RE = re.compile(
    r"^(\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d{3})\s+(\d+)\s+\d+\s+[VDIWEF]\s+VrApi\s*:\s(FPS=.*)$"
)


@dataclass
class VrApiSample:
    """One second of VR runtime metrics for one process."""

    timestamp: str
    pid: int
    fps: Optional[int] = None
    target_fps: Optional[int] = None
    prediction_ms: Optional[float] = None
    tears: Optional[int] = None
    early_frames: Optional[int] = None
    stale_frames: Optional[int] = None
    stale_histogram: Optional[dict] = None  # consecutive-stale buckets 2/5/10/max
    cpu_level: Optional[int] = None
    gpu_level: Optional[int] = None
    cpu_mhz: Optional[int] = None
    gpu_mhz: Optional[int] = None
    memory_mhz: Optional[int] = None
    free_memory_mb: Optional[int] = None
    temperature_c: Optional[float] = None
    timewarp_gpu_ms: Optional[float] = None
    app_gpu_ms: Optional[float] = None
    guardian_gpu_ms: Optional[float] = None
    cpu_gpu_total_ms: Optional[float] = None
    gpu_util: Optional[float] = None  # 0..1
    cpu_util: Optional[float] = None  # 0..1 average across cores
    cpu_util_worst_core: Optional[float] = None  # 0..1
    layer_count: Optional[int] = None

    def to_dict(self) -> dict:
        return asdict(self)


def parse_vrapi_line(line: str) -> Optional[VrApiSample]:
    """Parse a full threadtime logcat line carrying a VrApi report.
    Returns None for anything else."""
    m = _LOGCAT_VRAPI_RE.match(line.strip())
    if not m:
        return None
    timestamp, pid, message = m.groups()
    return parse_vrapi_message(int(pid), timestamp, message)


def _num(value: str) -> Optional[float]:
    m = re.match(r"-?\d+(?:\.\d+)?", value)
    return float(m.group(0)) if m else None


def _int(value: Optional[str]) -> Optional[int]:
    n = _num(value) if value is not None else None
    return int(n) if n is not None else None


def parse_vrapi_message(pid: int, timestamp: str, message: str) -> VrApiSample:
    """Parse the comma-separated key=value payload of a VrApi report.

    Tolerant by design: unknown keys are ignored, known keys are optional.
    """
    fields: dict[str, str] = {}
    for part in message.split(","):
        if "=" in part:
            key, _, value = part.partition("=")
            fields[key.strip()] = value.strip()

    sample = VrApiSample(timestamp=timestamp, pid=pid)

    if fps := fields.get("FPS"):
        actual, _, target = fps.partition("/")
        sample.fps = _int(actual)
        sample.target_fps = _int(target) if target else None

    if prd := fields.get("Prd"):
        sample.prediction_ms = _num(prd)
    sample.tears = _int(fields.get("Tear")) if "Tear" in fields else None
    sample.early_frames = _int(fields.get("Early")) if "Early" in fields else None
    sample.stale_frames = _int(fields.get("Stale")) if "Stale" in fields else None

    if hist := fields.get("Stale2/5/10/max"):
        parts = hist.split("/")
        if len(parts) == 4:
            sample.stale_histogram = {
                "2": _int(parts[0]),
                "5": _int(parts[1]),
                "10": _int(parts[2]),
                "max": _int(parts[3]),
            }

    # "CPU4/GPU=2/5" -> CPU level 2, GPU level 5 (the "4" is core count)
    for key, value in fields.items():
        if key.startswith("CPU") and "/GPU" in key:
            levels = value.split("/")
            if len(levels) == 2:
                sample.cpu_level = _int(levels[0])
                sample.gpu_level = _int(levels[1])

    # clocks arrive as a bare "1382/599MHz" token (no key); find it
    if m := re.search(r"(?:^|,)(\d+)/(\d+)MHz(?:,|$)", message):
        sample.cpu_mhz = int(m.group(1))
        sample.gpu_mhz = int(m.group(2))

    if mem := fields.get("Mem"):
        sample.memory_mhz = _int(mem)
    if free := fields.get("Free"):
        sample.free_memory_mb = _int(free)
    if temp := fields.get("Temp"):
        sample.temperature_c = _num(temp)

    if tw := fields.get("TW"):
        sample.timewarp_gpu_ms = _num(tw)
    if app := fields.get("App"):
        sample.app_gpu_ms = _num(app)
    if gd := fields.get("GD"):
        sample.guardian_gpu_ms = _num(gd)
    if total := fields.get("CPU&GPU"):
        sample.cpu_gpu_total_ms = _num(total)

    if gpu_pct := fields.get("GPU%"):
        sample.gpu_util = _num(gpu_pct)
    if cpu_pct := fields.get("CPU%"):
        sample.cpu_util = _num(cpu_pct)
        if w := re.search(r"W([\d.]+)", cpu_pct):
            sample.cpu_util_worst_core = float(w.group(1))

    if lcnt := fields.get("LCnt"):
        sample.layer_count = _int(lcnt)

    return sample
