"""One autonomous playtest, start to report.

1. Boot the app in the virtual headset with the persona's body.
2. Measure real-time performance for a few seconds (that's what players feel).
3. Pause the world, start recording inputs and a time-lapse, and hand control
   to the brain: screenshot -> decision -> action -> a few frames -> repeat,
   with the oracles watching every step.
4. Measure performance again, collect the crash log if there was a crash,
   and write the report: issues with screenshots, a replay script that
   reproduces the run, the video, the frame-timing trace.
"""

from __future__ import annotations

import json
import math
import re
import shlex
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from PIL import Image

from visor.xr import paths
from visor.xr import voice as tts
from visor.xr.client import XrClient, XrError, qrot
from visor.xr.frames import FrameError, FrameReader
from visor.xr.hearing import Ears, HearingError
from visor.xr.mic import MicError
from visor.xr.playtest.brains import (ACTION_TOOLS, Brain, BrainContext, Call, Observation, describe_controls,
                                      make_brain)
from visor.xr.playtest.oracles import Issue, Oracles
from visor.xr.playtest.personas import Persona, get_persona
from visor.xr.sim import AppSpec, SimConfig, SimError, SimInstance, detect_crash

FPS = 72
LISTEN_WAIT = 12.0  # seconds of world time to let the app finish talking before each look
LISTEN_EXPECT = 1.5  # after an action, how long a reply gets to begin
STALL_NOTE = 10.0  # seconds the app may take over an action's frames before the freeze is an issue
STALL_LIMIT = 20 * 60.0  # seconds without drawing anything before the app counts as hung
STALL_POLL = 5.0
DEFAULT_GOAL =("Start the experience as a new player would and get as far as you can through it, "
                "noting everything that gets in your way.")


def _duration(seconds: float) -> str:
    """16 min 34 s, 45 s, 1 h 2 min"""
    s = int(round(seconds))
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min" + (f" {s % 60} s" if s % 60 else "")
    return f"{s // 3600} h" + (f" {s % 3600 // 60} min" if s % 3600 // 60 else "")


@dataclass
class PlaytestConfig:
    target: str
    persona: str = "first-timer"
    goal: str = DEFAULT_GOAL
    brain: str = "claude"  # claude | monkey
    model: str = "claude-opus-5-5"
    effort: str = "medium"
    max_steps: int = 40
    max_minutes: float = 20.0
    seed: int = 0
    headset: str = "quest3"
    map: Optional[str] = None
    deterministic: bool = True  # fixed timestep so the replay reproduces the run
    warmup_seconds: float = 8.0
    view_width: int = 1024
    video: bool = True
    hearing: bool = True  # tell the agent what the app plays (speech transcribed, other sounds described)
    name: Optional[str] = None


@dataclass
class StepRecord:
    step: int
    frame: int
    action: str
    args: dict
    thought: str
    result: str
    screenshot: str
    t: float
    audio: str = ""  # what was said, for `say` steps (a WAV in the run dir)
    heard: str = ""  # what the app played afterwards, as the agent was told it
    heard_audio: str = ""  # and the sound itself (a WAV in the run dir)


class TimeLapse:
    """Writes every new app frame the runtime publishes into an MP4."""

    def __init__(self, xr: XrClient, out: Path, fps: int = 30):
        self.xr, self.out, self.fps = xr, out, fps
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._proc: Optional[subprocess.Popen] = None
        self.frames = 0

    def start(self) -> None:
        if not shutil.which("ffmpeg"):
            return
        self.xr.configure(capture_fps=30)
        meta = self.xr.call("capture", fresh=True, timeout_ms=4000, timeout=8)
        self._reader = FrameReader(meta["shm"])
        first = self._reader.read()
        self.size = first.image.size
        self._proc = subprocess.Popen(
            ["ffmpeg", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
             "-s", f"{self.size[0]}x{self.size[1]}", "-r", str(self.fps), "-i", "-",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "24", "-pix_fmt", "yuv420p",
             "-movflags", "+faststart", str(self.out)], stdin=subprocess.PIPE)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        last_seq = -1
        last_display = None
        while not self._stop.is_set():
            try:
                seq = self._reader.latest_seq()
                if seq != last_seq:
                    last_seq = seq
                    frame = self._reader.read()
                    # The compositor re-presents a paused app's last frame; keep only new ones.
                    if frame.image.size == self.size and frame.display_time != last_display:
                        last_display = frame.display_time
                        assert self._proc and self._proc.stdin
                        self._proc.stdin.write(frame.image.tobytes())
                        self.frames += 1
            except (FrameError, OSError, BrokenPipeError):
                pass
            time.sleep(0.01)

    def stop(self) -> Optional[Path]:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        if self._proc and self._proc.stdin:
            try:
                self._proc.stdin.close()
            except OSError:
                pass
            self._proc.wait(timeout=120)
        try:
            self.xr.configure(capture_fps=0)
        except XrError:
            pass
        return self.out if self.frames else None


class Playtest:
    def __init__(self, config: PlaytestConfig, on_event: Optional[Callable[[str, dict], None]] = None,
                 brain: Optional[Brain] = None):
        self.cfg = config
        self.persona: Persona = get_persona(config.persona)
        self.brain = brain or make_brain(config.brain, seed=config.seed, model=config.model, effort=config.effort)
        self.on_event = on_event or (lambda kind, data: None)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.name = config.name or f"playtest-{self.persona.key}-{stamp}"
        self.dir = paths.runs_dir() / self.name
        self.shots = self.dir / "shots"
        self.issues: list[Issue] = []
        self.steps: list[StepRecord] = []
        self.verdict: dict = {}
        self.perf: dict = {}
        self.crash: Optional[dict] = None
        self.stopped_for: Optional[str] = None  # ended early through no fault of the app
        self.sim: Optional[SimInstance] = None
        self.xr: Optional[XrClient] = None
        self.voice_events: list[dict] = []  # what was said, at which frame: replayed with the inputs
        self.heard: list[dict] = []  # what the app played, step by step
        self.ears: Optional[Ears] = None
        self._listening: Optional[bool] = None
        self._said = ""
        self._log_offset = 0
        self.started = time.time()

    # -- plumbing -----------------------------------------------------------

    def _emit(self, kind: str, **data) -> None:
        self.on_event(kind, data)

    def _new_log_lines(self) -> list[str]:
        log = self.sim.run_dir / "app.log" if self.sim else None
        if not log or not log.exists():
            return []
        with open(log, "rb") as f:
            f.seek(self._log_offset)
            data = f.read()
            self._log_offset = f.tell()
        return data.decode("utf-8", "replace").splitlines()

    def _shot(self, step: int) -> tuple[Image.Image, str, int]:
        assert self.xr
        frame = self.xr.capture(timeout=6)
        img = frame.eye("left")
        if img.width > self.cfg.view_width:
            img = img.resize((self.cfg.view_width, round(img.height * self.cfg.view_width / img.width)), Image.LANCZOS)
        rel = f"shots/{step:03d}.jpg"
        img.save(self.dir / rel, quality=85)
        return img, rel, frame.frame

    def _add_issue(self, issue: Issue) -> None:
        issue.id = f"V{len(self.issues) + 1:02d}"
        if not issue.steps_to_reproduce:
            issue.steps_to_reproduce = [f"{s.action} {json.dumps(s.args)}" for s in self.steps[-6:]]
        self.issues.append(issue)
        self._emit("issue", issue=issue.to_dict())

    # -- the body -----------------------------------------------------------

    def _head_point(self, right: float, up: float, forward: float) -> list[float]:
        st = self.xr.state()["head"]
        # Keep it level: reach relative to the head's yaw, not its pitch.
        yaw = math.radians(st["ypr"][0])
        q = [0.0, math.sin(yaw / 2), 0.0, math.cos(yaw / 2)]
        return [a + b for a, b in zip(st["pos"], qrot(q, [right, up, -forward]))]

    def _execute(self, call: Call, img_size: tuple[int, int]) -> tuple[str, int]:
        """Do one action; returns (what happened, frames to let it play out)."""
        xr = self.xr
        a = call.args
        p = self.persona
        hand = a.get("hand") or ("right" if "right" in p.hands else p.hands[0])
        if hand not in p.hands:
            return (f"Your {hand} hand isn't available (persona: {p.name}); use the {p.hands[0]} hand.", 0)
        w, h = img_size
        if call.name == "click":
            aim = xr.click_pixel(float(a["x"]), float(a["y"]), w, h, hand=hand)
            return (f"Clicked ({a['x']:.0f},{a['y']:.0f}) with the {hand} controller "
                    f"(aimed yaw {aim['ypr'][0]:.0f}°, pitch {aim['ypr'][1]:.0f}°).", 30)
        if call.name == "point":
            xr.point_at_pixel(hand, float(a["x"]), float(a["y"]), w, h)
            return (f"Pointing the {hand} controller at ({a['x']:.0f},{a['y']:.0f}).", 8)
        if call.name == "press":
            frames = max(2, round(float(a.get("hold_seconds", 0.1)) * FPS))
            xr.press(hand, a["button"], frames=frames)
            return (f"Pressed {hand} {a['button']} for {frames / FPS:.2f}s.", frames + 18)
        if call.name == "stick":
            frames = max(2, round(float(a.get("seconds", 0.5)) * FPS))
            xr.stick(hand, float(a["x"]), float(a["y"]), frames=frames)
            return (f"Pushed the {hand} stick ({float(a['x']):+.1f},{float(a['y']):+.1f}) for {frames / FPS:.1f}s.",
                    frames + 8)
        if call.name == "look":
            yaw = max(-p.max_turn_deg, min(p.max_turn_deg, float(a["yaw"])))
            pitch = max(-80.0, min(80.0, float(a["pitch"])))
            current = xr.state()["head"]["ypr"]
            angle = max(abs(yaw - current[0]), abs(pitch - current[1]))
            frames = max(12, round(angle / max(p.max_turn_speed, 1.0) * FPS))
            xr.look(yaw, pitch, over=frames)
            note = "" if yaw == float(a["yaw"]) else f" (you can't turn past {p.max_turn_deg:.0f}°)"
            return (f"Looking yaw {yaw:.0f}°, pitch {pitch:.0f}°{note}.", frames + 6)
        if call.name == "reach":
            target = self._head_point(float(a["right"]), float(a["up"]), float(a["forward"]))
            note = ""
            if self.persona.key == "seated":
                reach_y = max(0.5, min(1.6, target[1]))
                if reach_y != target[1]:
                    note = " You can't reach that far from your chair."
                    target[1] = reach_y
            ypr = xr.state()["head"]["ypr"]
            patch: dict = {"pos": target, "ypr": [ypr[0], -20.0, 0.0]}
            if "grab" in a:
                patch["squeeze"] = 1.0 if a["grab"] else 0.0
            xr.set(**{hand: patch}, over=18)
            return (f"Moved the {hand} hand to {[round(v, 2) for v in target]}"
                    f"{', gripping' if a.get('grab') else ''}.{note}", 24)
        if call.name == "walk":
            if self.persona.key == "seated":
                return ("You're seated and can't walk.", 0)
            xr.walk(forward=float(a.get("forward", 0.0)), right=float(a.get("right", 0.0)), over=48)
            return (f"Walked {float(a.get('forward', 0)):+.1f} m forward, {float(a.get('right', 0)):+.1f} m right.", 52)
        if call.name == "wait":
            secs = max(0.1, min(30.0, float(a.get("seconds", 1.0))))
            return (f"Waited {secs:.1f}s.", round(secs * FPS))
        if call.name == "say":
            return self._say(str(a.get("text", "")), a.get("language"))
        return (f"Unknown action {call.name}.", 0)

    def _say(self, text: str, language: Optional[str]) -> tuple[str, int]:
        """Speak into the app's microphone; the world runs for as long as it takes to say."""
        p = self.persona
        try:
            speech = tts.synthesize(text, language=language, speed=p.speech_speed,
                                    log=lambda m: self._emit("status", message=m))
            frame = int(self.xr.state().get("frame", 0))
            heard = self.xr.mic().play(speech.path, volume=p.voice_volume, label=speech.text, kind="speech")
        except (tts.VoiceError, MicError) as e:
            return (f"You can't speak here: {e}", 0)
        rel = f"voice/{len(self.voice_events) + 1:03d}.wav"
        (self.dir / "voice").mkdir(exist_ok=True)
        shutil.copyfile(speech.path, self.dir / rel)
        self.voice_events.append({"frame": frame, "audio": rel, "text": speech.text, "voice": speech.voice,
                                  "volume": p.voice_volume, "duration": round(speech.duration, 2)})
        self._said = rel
        note = "" if heard.get("listening") else " The app wasn't listening (its microphone was off), so nothing heard you."
        return (f"You said \"{speech.text}\" ({speech.duration:.1f}s).{note}", round((speech.duration + 0.8) * FPS))

    def _mic_note(self) -> str:
        """A sentence when the app turns its microphone on or off."""
        try:
            listening = bool(self.xr.mic().status()["listening"])
        except MicError:
            return ""
        before, self._listening = self._listening, listening
        if listening == before:
            return ""
        if listening:
            return " The app's microphone is on: it's listening."
        return "" if before is None else " The app turned its microphone off."

    def _listen(self, step: int) -> str:
        """What the app played since the last look, once it has finished saying
        it (the world runs on while it talks). Kept with the run; returns what
        to tell the agent."""
        if self.ears is None:
            return ""
        try:
            heard = self.ears.hear(wait=LISTEN_WAIT, expect=LISTEN_EXPECT if step else 0.0,
                                   step=lambda s: self.xr.step(max(1, round(s * FPS)), timeout=30))
        except (MicError, HearingError) as e:
            self._emit("status", message=f"can't hear the app: {e}")
            self.ears = None
            return ""
        if heard.silent:
            return ""
        rel = f"heard/{len(self.heard) + 1:03d}.wav"
        heard.save(self.dir / rel)
        frame = int(self.xr.state().get("render_frame", 0))
        self.heard.append({"step": step, "frame": frame, "audio": rel, **heard.to_json()})
        if self.steps and self.steps[-1].step == step:
            self.steps[-1].heard, self.steps[-1].heard_audio = heard.describe(), rel
        if heard.clipped > 0.001 and not any(i.category == "audio" for i in self.issues):
            self._add_issue(Issue(
                title="Audio clips at full volume", severity="minor", category="audio",
                description=f"{heard.clipped:.1%} of what the app played after step {step} was at full scale: "
                            "it's distorted on headphones, and the Quest's speakers will crackle.",
                step=step, frame=frame, source="oracle:audio"))
        return " " + heard.describe()

    def _advance(self, frames: int) -> tuple[float, float]:
        """Run the paused app ``frames`` frames, watching it from a second
        connection meanwhile. Returns (the longest it drew nothing, the whole
        time) in seconds: a level loading, shaders compiling, a hang. An app
        that draws nothing for STALL_LIMIT is closed as hung."""
        assert self.xr and self.sim
        start = time.monotonic()
        longest = [0.0]
        hung: list[float] = []
        done = threading.Event()
        sim = self.sim

        def watch() -> None:
            last, changed, told = None, start, start
            with XrClient(sim.socket_path, timeout=5.0) as probe:
                while not done.wait(STALL_POLL):
                    try:
                        drawn = probe.state()["render_frame"]
                    except (XrError, KeyError, TypeError):
                        if not sim.alive():
                            return
                        continue  # a wedged layer: the step's own timeout covers that
                    now = time.monotonic()
                    if drawn != last:
                        last, changed = drawn, now
                    longest[0] = max(longest[0], now - changed)
                    if now - changed >= STALL_LIMIT:
                        hung.append(now - changed)
                        sim.proc.send_signal(signal.SIGTERM)  # not sim.stop(): the step holds the client
                        try:
                            sim.proc.wait(timeout=15)
                        except subprocess.TimeoutExpired:
                            sim.proc.kill()
                        return
                    if now - changed >= 30 and now - told >= 60:
                        told = now
                        self._emit("status", message=f"the app hasn't drawn anything for {_duration(now - changed)}"
                                                     " (loading?); waiting without using up actions")

        watcher = threading.Thread(target=watch, name="stall watch", daemon=True)
        watcher.start()
        try:
            self.xr.step(frames, timeout=STALL_LIMIT + 60)
        except XrError:
            if not hung:
                raise
        finally:
            done.set()
            watcher.join(10)
        if hung:
            self.stopped_for = f"the app drew nothing for {_duration(hung[0])}, so Visor closed it as hung"
            last = self.steps[-1] if self.steps else None
            self._add_issue(Issue(
                title=f"The app stopped drawing for {_duration(hung[0])}",
                severity="blocker", category="functional", source="oracle:hang",
                description=(f"After {last.action if last else 'starting'} the app drew no new frame for "
                             f"{_duration(hung[0])}; Visor closed it as hung. The log tail is in app.log."),
                step=last.step if last else 0, frame=last.frame if last else 0,
                screenshot=last.screenshot if last else None))
            raise SimError(self.stopped_for)
        return longest[0], time.monotonic() - start

    def _stall_issue(self, step: int, frame: int, screenshot: Optional[str], frozen: float, total: float,
                     log: list[str]) -> tuple[Issue, str]:
        """An issue for a long freeze, and a line telling the agent about it.
        The log says what the app was busy with."""
        joined = "\n".join(log)
        loading = re.findall(r"LoadMap: (\S+)", joined)
        level = loading[-1].rsplit("/", 1)[-1] if loading else None
        shaders = "LogShaderCompilers" in joined or "ShaderCompileWorker" in joined
        last = self.steps[-1] if self.steps else None
        after = f"'{last.action}'" if last else "the last action"
        title = f"The view froze for {_duration(frozen)}" + (f" while {level} loaded" if level else f" after {after}")
        description = (f"After {after} the app drew no new frame for {_duration(frozen)} (the action took "
                       f"{_duration(total)} in all). In a headset the player is left looking at a frozen image "
                       "until it's back; long loads read better behind a loading screen or stereo layer.")
        if shaders:
            description += (" Unreal was compiling shaders meanwhile, which a packaged build doesn't do, so a Quest "
                            "build would freeze for less time than this.")
        issue = Issue(title=title, severity="major" if frozen >= 60 else "minor", category="performance",
                      description=description, step=step, frame=frame, source="oracle:stall", screenshot=screenshot,
                      extra={"frozen_s": round(frozen, 1), "total_s": round(total, 1), "level": level,
                             "compiling_shaders": shaders})
        note = (f" The app then drew nothing for {_duration(frozen)}"
                + (f" while it loaded {level}" if level else "") + "; it's drawing again now.")
        return issue, note

    def _observe(self, step: int, text: str) -> tuple[Observation, str, int]:
        text += self._listen(step)
        img, rel, frame = self._shot(step)
        left = self.cfg.max_steps - step
        events = self.xr.events(self._events_seen)
        self._events_seen = int(events.get("latest", self._events_seen))
        haptics = [e for e in events.get("events", []) if e.get("type") == "haptic"]
        extra = ""
        if haptics:
            hands = sorted({e.get("hand", "?") for e in haptics})
            extra = f" You felt {len(haptics)} haptic pulse(s) ({', '.join(hands)})."
        extra += self._mic_note()
        return Observation(image=img, text=f"{text}{extra} [{left} actions left]", step=step,
                           steps_left=left), rel, frame

    # -- the run ------------------------------------------------------------

    def run(self) -> dict:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.shots.mkdir(exist_ok=True)
        try:
            self._boot()
            self._play()
        except (SimError, XrError) as e:
            if self.sim and not self.sim.alive():
                self._record_crash()
            else:
                self.verdict = self.verdict or {"goal_achieved": False, "rating": 0, "summary": f"Stopped: {e}"}
                self._emit("error", message=str(e))
        except KeyboardInterrupt:  # Ctrl-C, or the CLI's SIGTERM/SIGHUP: still leave a report
            self.stopped_for = self.stopped_for or "the playtest was interrupted"
            self.verdict = self.verdict or {"goal_achieved": False, "summary": f"Stopped early: {self.stopped_for}."}
            raise
        finally:
            self._finish()
        return self.report

    def _boot(self) -> None:
        p = self.persona
        spec = AppSpec.resolve(self.cfg.target, map_name=self.cfg.map, deterministic=self.cfg.deterministic)
        self.app = spec
        self.sim = SimInstance(spec, SimConfig(headset=self.cfg.headset, eye_height=p.eye_height),
                               name=self.name, run_dir=self.dir)
        self._emit("status", message=f"starting {spec.name} as {p.name}")
        self.sim.start()
        for note in self.sim.notes:
            self._emit("status", message=note)
        self.xr = self.sim.wait_ready(timeout=600, on_progress=lambda note: self._emit("status", message=note))
        self._events_seen = 0
        if self.cfg.hearing and self.sim.mic_proc:
            # From launch (up to a minute back): the first look includes how the app greeted the player.
            self.ears = Ears(self.xr.mic(), since=max(self.sim.started_clock or 0.0, time.monotonic() - 60.0),
                             log=lambda m: self._emit("status", message=m))
            if not self.ears.transcribe:
                self._emit("status", message=f"hearing sounds but not words: {paths.install_hint('voice')}")
        if "right" not in p.hands:
            self.xr.set(right={"connected": False})
        if "left" not in p.hands:
            self.xr.set(left={"connected": False})
        self._emit("status", message=f"measuring real-time performance ({self.cfg.warmup_seconds:.0f}s)")
        time.sleep(self.cfg.warmup_seconds)
        self.perf["start"] = self.xr.stats(window=int(self.cfg.warmup_seconds * FPS))
        self.oracles = Oracles(refresh_hz=self.perf["start"].get("refresh_hz", 72.0),
                               freeze_steps=max(4, p.patience_steps - 1))
        for issue in self.oracles.performance(self.perf["start"], 0, 0, " while idle at start"):
            self._add_issue(issue)
        self._new_log_lines()  # startup noise isn't the playtest's fault
        info = self.xr.info()
        self.app_info = info
        self.xr.record_start()
        self.timelapse = TimeLapse(self.xr, self.dir / "video.mp4") if self.cfg.video else None
        if self.timelapse:
            self.timelapse.start()
        self.xr.pause()
        self.paused_from = int(self.xr.state()["render_frame"])

    def _play(self) -> None:
        assert self.xr
        info = self.app_info
        img, rel, frame = self._shot(0)
        ctx = BrainContext(persona=self.persona, goal=self.cfg.goal,
                           app_name=(info.get("app") or {}).get("name") or self.app.name,
                           controls=describe_controls(info.get("actions", [])),
                           image_size=img.size, max_steps=self.cfg.max_steps)
        self.brain.start(ctx)
        heard = self._listen(0)
        if heard:
            img, rel, frame = self._shot(0)  # the world may have moved on while the app talked
        obs = Observation(image=img, text="This is the first thing you see." + heard + self._mic_note(), step=0,
                          steps_left=self.cfg.max_steps)
        calls = self.brain.first(obs)
        deadline = self.started + self.cfg.max_minutes * 60
        step = 0
        while calls:
            results: list[tuple[Call, str]] = []
            frames = 0
            acted = False
            finished = False
            for call in calls:
                if call.name == "finish":
                    self.verdict = call.args
                    finished = True
                    results.append((call, "Playtest finished."))
                    continue
                if call.name == "report_issue":
                    a = call.args
                    self._add_issue(Issue(
                        title=a.get("title", "issue"), severity=a.get("severity", "minor"),
                        category=a.get("category", "functional"),
                        description=f"Expected: {a.get('expected', '')}\nActual: {a.get('actual', '')}",
                        step=step, frame=frame, source=f"agent:{self.persona.key}", screenshot=rel))
                    results.append((call, f"Recorded issue {self.issues[-1].id}."))
                    continue
                if call.name not in ACTION_TOOLS:
                    results.append((call, f"No such action '{call.name}'."))
                    continue
                if step >= self.cfg.max_steps or time.time() > deadline:
                    results.append((call, "Out of actions — call finish with your verdict."))
                    continue
                step += 1
                try:
                    text, f = self._execute(call, img.size)
                except XrError as e:
                    if not self.sim.alive():
                        raise
                    text, f = f"That didn't work: {e}", 0
                frames += f
                acted = acted or f > 0
                results.append((call, text))
                self.steps.append(StepRecord(step=step, frame=frame, action=call.name, args=call.args,
                                             thought=call.thought, result=text, screenshot="", t=time.time(),
                                             audio=self._said))
                self._said = ""
                self._emit("action", step=step, action=call.name, args=call.args, thought=call.thought, result=text)
            if finished:
                break
            frozen, took = self._advance(min(frames, 30 * FPS)) if frames else (0.0, 0.0)
            if not self.sim.alive():
                raise SimError("the app exited")
            log = self._new_log_lines()
            text = results[-1][1] if results else ""
            if frozen >= STALL_NOTE:
                deadline += took  # time the agent never got to use
                issue, note = self._stall_issue(step, frame, rel, frozen, took, log)
                self._add_issue(issue)
                text += note
            obs, rel, frame = self._observe(step, text)
            img = obs.image
            if self.steps:
                self.steps[-1].screenshot = rel
            for issue in self.oracles.after_step(step, frame, img, acted, rel):
                self._add_issue(issue)
            for issue in self.oracles.logs(step, frame, log + self._new_log_lines()):
                issue.screenshot = rel
                self._add_issue(issue)
            self._emit("view", step=step, frame=frame, screenshot=rel)
            if step >= self.cfg.max_steps and not any(c.name == "finish" for c in calls):
                obs.text += " You're out of actions: call finish now."
            calls = self.brain.next(results, obs)

    def _record_crash(self) -> None:
        reason = self.stopped_for or (self.sim.ended_by() if self.sim else None)
        if reason:  # not the app's doing: no crash to report
            self.stopped_for = reason
            self.verdict = self.verdict or {"goal_achieved": False, "summary": f"Stopped early: {reason}."}
            self._emit("error", message=f"stopped early: {reason}")
            return
        self.crash = detect_crash(self.dir, self.app) or {"message": "the app exited unexpectedly", "callstack": []}
        last = self.steps[-1] if self.steps else None
        self._add_issue(Issue(
            title=f"Crash: {self.crash['message'][:100]}",
            severity="blocker", category="crash", source="oracle:crash",
            description=self.crash["message"] + ("\n\n" + "\n".join(self.crash["callstack"][:20])
                                                 if self.crash.get("callstack") else ""),
            step=last.step if last else 0, frame=last.frame if last else 0,
            screenshot=last.screenshot if last else None))
        self.verdict = {"goal_achieved": False, "rating": 1, "summary": "The app crashed during the playtest."}

    def _finish(self) -> None:
        replay = None
        if self.xr and self.sim and self.sim.alive():
            try:
                replay = self.xr.record_stop()
                start = int(replay.get("start_frame") or 0)
                voice = [{**v, "frame": v["frame"] - start} for v in self.voice_events]
                (self.dir / "replay.json").write_text(json.dumps({"visor_replay": 1, **replay,
                                                                  **({"voice": voice} if voice else {})}, indent=1))
            except XrError:
                pass
            try:
                self.paused_to = int(self.xr.state()["render_frame"])
                self.xr.resume()
                time.sleep(5)
                self.perf["end"] = self.xr.stats(window=5 * FPS)
                for issue in self.oracles.performance(self.perf["end"], len(self.steps), 0, " after playing"):
                    self._add_issue(issue)
                trace = self.xr.stats(window=4096, frames_since=0)
                (self.dir / "frames.json").write_text(json.dumps(trace))
            except (XrError, AttributeError):
                pass
        video = self.timelapse.stop() if getattr(self, "timelapse", None) else None
        if self.sim:
            if self.sim.alive():
                self.sim.stop()
            elif not self.crash and not self.stopped_for:
                self._record_crash()
        self._write_repro(replay is not None)
        self.report = self._report(video)
        (self.dir / "report.json").write_text(json.dumps(self.report, indent=1, default=str))
        from visor.xr.playtest.report import write_html

        write_html(self.report, self.dir / "report.html")
        self._emit("done", report=str(self.dir / "report.html"))

    def _write_repro(self, have_replay: bool) -> None:
        target = shlex.quote(str(Path(self.cfg.target).expanduser().resolve()) if Path(self.cfg.target).expanduser().exists()
                             else self.cfg.target)
        name = f"repro-{self.name[-15:]}"
        opts = [f"--headset {self.cfg.headset}", "--paused"]
        if self.persona.eye_height < 1.5:
            opts.append("--seated")
        if self.cfg.map:
            opts.append(f"--map {shlex.quote(self.cfg.map)}")
        if self.cfg.deterministic:
            opts.append("--deterministic")
        visor = paths.visor_command()  # the Visor that wrote this, PATH or not
        lines = [
            "#!/usr/bin/env bash",
            f"# Reproduce playtest {self.name} ({self.persona.name}): launches the app in Visor's",
            "# virtual headset frozen at its first frame, then plays back every recorded input.",
            "set -euo pipefail",
            'cd "$(dirname "$0")"',
            f'VISOR="${{VISOR:-{visor}}}"',
            f"$VISOR sim run {target} --name {name} {' '.join(opts)} --detach",
        ]
        if have_replay:
            lines += [f"$VISOR sim replay replay.json --app {name}", f"$VISOR sim resume {name}",
                      f'echo "replaying — watch with: visor sim shot {name}; stop with: visor sim stop {name}"']
        path = self.dir / "repro.sh"
        path.write_text("\n".join(lines) + "\n")
        path.chmod(0o755)

    def _report(self, video: Optional[Path]) -> dict:
        info = getattr(self, "app_info", {}) or {}
        sev_rank = {s: i for i, s in enumerate(("blocker", "major", "minor", "note"))}
        issues = sorted((i.to_dict() for i in self.issues), key=lambda d: (sev_rank.get(d["severity"], 9), d["step"]))
        return {
            "visor_playtest": 1,
            "name": self.name,
            "created": datetime.now().isoformat(timespec="seconds"),
            "duration_s": round(time.time() - self.started, 1),
            "app": {"name": (info.get("app") or {}).get("name") or getattr(self, "app", None) and self.app.name,
                    "engine": (info.get("app") or {}).get("engine"), "target": self.cfg.target,
                    "runtime": info.get("runtime"),
                    "interaction_profile": (info.get("session") or {}).get("interaction_profile")},
            "persona": asdict(self.persona),
            "goal": self.cfg.goal,
            "brain": {"kind": self.brain.name, **self.brain.usage()},
            "config": asdict(self.cfg),
            "verdict": self.verdict,
            "issues": issues,
            "steps": [asdict(s) for s in self.steps],
            "heard": self.heard,
            "perf": self.perf,
            # Frames in between were single-stepped by the agent, not real time.
            "paused_frames": [getattr(self, "paused_from", None), getattr(self, "paused_to", None)],
            "crash": self.crash,
            "stopped_for": self.stopped_for,
            "artifacts": {
                "video": "video.mp4" if video else None,
                "replay": "replay.json" if (self.dir / "replay.json").exists() else None,
                "repro": "repro.sh",
                "frames": "frames.json" if (self.dir / "frames.json").exists() else None,
                "log": "app.log",
            },
        }
