"""Many virtual headsets at once.

A fleet runs N isolated copies of an app — each its own runtime, socket,
frame buffer and run directory — and either soaks them (run for a while,
measure, screenshot) or playtests them (one persona/seed per copy). Issues
are merged across copies by signature, so "8 of 20 runs hit this" reads as
one finding with a count.
"""

from __future__ import annotations

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Callable, Optional

from visor.xr import paths
from visor.xr.sim import AppSpec, SimConfig, SimError, SimInstance


@dataclass
class FleetConfig:
    target: str
    count: int = 4
    mode: str = "soak"  # soak | playtest
    seconds: float = 30.0  # soak length
    concurrency: int = 0  # 0 = all at once
    scale: float = 0.5  # eye-buffer scale; fleets trade pixels for copies
    headset: str = "quest3"
    map: Optional[str] = None
    personas: list[str] = field(default_factory=lambda: ["first-timer"])
    brain: str = "monkey"
    steps: int = 30
    model: str = "claude-opus-5-5"
    name: Optional[str] = None


@dataclass
class RunResult:
    index: int
    name: str
    status: str = "queued"  # queued | starting | running | done | crashed | failed
    persona: str = ""
    seed: int = 0
    fps: float = 0.0
    refresh_hz: float = 0.0
    p99_ms: float = 0.0
    missed: int = 0
    frame: int = 0
    issues: int = 0
    rating: Optional[int] = None
    shot: Optional[str] = None
    report: Optional[str] = None
    error: Optional[str] = None
    seconds: float = 0.0


def _signature(title: str) -> str:
    return re.sub(r"\d+(\.\d+)?", "#", title.lower()).strip()[:120]


class Fleet:
    def __init__(self, cfg: FleetConfig, on_update: Optional[Callable[[list[RunResult]], None]] = None):
        self.cfg = cfg
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.name = cfg.name or f"fleet-{stamp}"
        self.dir = paths.runs_dir() / self.name
        self.results = [RunResult(index=i, name=f"{self.name}-{i:02d}") for i in range(cfg.count)]
        self.on_update = on_update or (lambda r: None)
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def _update(self, r: RunResult, **fields) -> None:
        with self._lock:
            for k, v in fields.items():
                setattr(r, k, v)
        self.on_update(self.results)

    # -- one copy -----------------------------------------------------------

    def _soak(self, r: RunResult) -> None:
        cfg = self.cfg
        spec = AppSpec.resolve(cfg.target, map_name=cfg.map)
        sim = SimInstance(spec, SimConfig(headset=cfg.headset, render_scale=cfg.scale), name=r.name,
                          run_dir=self.dir / r.name)
        t0 = time.time()
        try:
            self._update(r, status="starting")
            sim.start()
            xr = sim.wait_ready(timeout=900)
            self._update(r, status="running")
            end = time.time() + cfg.seconds
            while time.time() < end and not self._stop.is_set():
                if not sim.alive():
                    raise SimError("exited")
                st = xr.stats(window=int(min(cfg.seconds, 10) * 72))
                self._update(r, fps=round(st["fps"], 1), refresh_hz=st["refresh_hz"], p99_ms=round(st["frame_ms_p99"], 1),
                             missed=int(st["missed_frames"]), frame=int(xr.state()["render_frame"]))
                time.sleep(1.0)
            st = xr.stats(window=int(cfg.seconds * 72))
            shot = self.dir / r.name / "last.jpg"
            xr.screenshot(shot, max_width=480)
            (self.dir / r.name / "stats.json").write_text(json.dumps(st))
            self._update(r, status="done", fps=round(st["fps"], 1), p99_ms=round(st["frame_ms_p99"], 1),
                         missed=int(st["missed_frames"]), shot=str(shot.relative_to(self.dir)))
        except Exception as e:  # one copy failing must not take the fleet down
            crash = sim.crash()
            self._update(r, status="crashed" if crash else "failed",
                         error=(crash or {}).get("message") or str(e)[:300])
        finally:
            sim.stop()
            self._update(r, seconds=round(time.time() - t0, 1))

    def _playtest(self, r: RunResult) -> None:
        from visor.xr.playtest.session import Playtest, PlaytestConfig

        cfg = self.cfg
        persona = cfg.personas[r.index % len(cfg.personas)]
        seed = r.index
        self._update(r, status="starting", persona=persona, seed=seed)
        t0 = time.time()
        pcfg = PlaytestConfig(target=cfg.target, persona=persona, brain=cfg.brain, model=cfg.model,
                              max_steps=cfg.steps, seed=seed, headset=cfg.headset, map=cfg.map, name=r.name,
                              video=True)

        def on_event(kind: str, data: dict) -> None:
            if kind == "action":
                self._update(r, status="running", frame=data.get("step", 0))
            elif kind == "issue":
                self._update(r, issues=r.issues + 1)

        try:
            test = Playtest(pcfg, on_event=on_event)
            test.dir = self.dir / r.name
            test.shots = test.dir / "shots"
            report = test.run()
            start = (report.get("perf") or {}).get("start") or {}
            last = next((s["screenshot"] for s in reversed(report.get("steps", [])) if s.get("screenshot")), None)
            self._update(r, status="crashed" if report.get("crash") else "done",
                         issues=len(report.get("issues", [])), rating=(report.get("verdict") or {}).get("rating"),
                         fps=round(start.get("fps", 0.0), 1), refresh_hz=start.get("refresh_hz", 0.0),
                         p99_ms=round(start.get("frame_ms_p99", 0.0), 1),
                         shot=f"{r.name}/{last}" if last else None, report=f"{r.name}/report.html")
        except Exception as e:
            self._update(r, status="failed", error=str(e)[:300])
        finally:
            self._update(r, seconds=round(time.time() - t0, 1))

    # -- all of them ----------------------------------------------------------

    def run(self) -> dict:
        self.dir.mkdir(parents=True, exist_ok=True)
        workers = self.cfg.concurrency or self.cfg.count
        job = self._soak if self.cfg.mode == "soak" else self._playtest
        started = time.time()
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(job, r) for r in self.results]
            for _ in as_completed(futures):
                pass
        summary = self.summary(time.time() - started)
        (self.dir / "fleet.json").write_text(json.dumps(summary, indent=1, default=str))
        from visor.xr.fleet_report import write_fleet_html

        write_fleet_html(summary, self.dir / "fleet.html")
        return summary

    def stop(self) -> None:
        self._stop.set()

    def summary(self, elapsed: float) -> dict:
        merged: dict[str, dict] = {}
        for r in self.results:
            if not r.report:
                continue
            try:
                rep = json.loads((self.dir / r.name / "report.json").read_text())
            except (OSError, ValueError):
                continue
            for issue in rep.get("issues", []):
                sig = _signature(issue["title"])
                m = merged.setdefault(sig, {"title": issue["title"], "severity": issue["severity"],
                                            "category": issue["category"], "runs": [], "example": None})
                if r.name not in m["runs"]:
                    m["runs"].append(r.name)
                if m["example"] is None and issue.get("screenshot"):
                    m["example"] = f"{r.name}/{issue['screenshot']}"
        sev = {"blocker": 0, "major": 1, "minor": 2, "note": 3}
        issues = sorted(merged.values(), key=lambda m: (sev.get(m["severity"], 9), -len(m["runs"])))
        done = [r for r in self.results if r.status == "done"]
        return {
            "visor_fleet": 1,
            "name": self.name,
            "config": asdict(self.cfg),
            "elapsed_s": round(elapsed, 1),
            "runs": [asdict(r) for r in self.results],
            "ok": len(done),
            "crashed": sum(1 for r in self.results if r.status == "crashed"),
            "failed": sum(1 for r in self.results if r.status == "failed"),
            "mean_fps": round(sum(r.fps for r in done) / len(done), 1) if done else 0.0,
            "issues": issues,
        }
