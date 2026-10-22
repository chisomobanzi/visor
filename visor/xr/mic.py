"""Visor's microphone and ears: what an app in the virtual headset hears, and
what it plays.

Every app the simulator launches gets its own audio — a small helper process
serving the PulseAudio protocol (visor.xr.pulse) on a socket only that app is
pointed at — so twenty copies can each be spoken to separately, none of them
can hear the room, and the room hears none of them.

The microphone plays clips (speech from visor.xr.voice, or any audio file) on
one timeline that every stream the app opens shares, so a sentence lands the
same however and whenever the app listens. Between clips it hears a quiet room
rather than digital silence: real microphones never read zero, and apps that
calibrate against a noise floor expect one.

Its clock follows the world. While an agent holds the app paused between
actions, the microphone is paused too: nothing piles up in the app's capture
buffers, and speech queued while frozen starts when the world runs again —
the same game time every replay.

The speakers are the other way round: they run in real time, as sound cards
do, and Visor listens to them. Everything the app plays is mixed to mono at
16 kHz (speech recognition's native rate) and remembered for three minutes,
to be read back by time.

Control is JSON lines on ``<name>.mic`` beside the app's own socket.
"""

from __future__ import annotations

import base64
import json
import math
import operator
import os
import random
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import wave
from array import array
from dataclasses import dataclass
from itertools import accumulate
from pathlib import Path
from typing import Callable, Optional

from visor.xr import paths
from visor.xr.client import XrClient, XrError
from visor.xr.pulse import PulseServer

ROOM_DB = -60.0  # a quiet room through a headset mic
SPEECH_RMS = 0.1  # -20 dBFS: a normal voice, close to the mic
LEAD = 0.05  # seconds between "play" and the first sample
GAP = 0.25  # between queued clips, like a breath
ROOM_RATE = 48000

EAR_RATE = 16000  # what Visor hears the app's speakers at
EAR_SECONDS = 180  # how much of it the helper remembers
EAR_BLOCK = 800  # 50 ms: the resolution of loudness readings
SOUND_DB = -60.0  # quieter than this is silence (an idle mixer writes zeros)
QUIET_GAP = 0.8  # this long without sound ends a stretch of it (a sentence, a jingle)


class MicError(RuntimeError):
    pass


def pulse_socket(name: str) -> Path:
    return paths.socket_dir() / f"{name}.pulse"


def control_socket(name: str) -> Path:
    return paths.socket_dir() / f"{name}.mic"


# --------------------------------------------------------------------------
# Audio files


def load_audio(path: str | Path) -> tuple[array, int]:
    """Any audio file as mono float samples and their rate (WAV natively,
    anything else through ffmpeg)."""
    path = Path(path)
    if not path.is_file():
        raise MicError(f"no audio file at {path}")
    try:
        with wave.open(str(path), "rb") as w:
            channels, width, rate, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
            raw = w.readframes(n)
        return _pcm_to_mono(raw, width, channels), rate
    except (wave.Error, EOFError):
        pass
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise MicError(f"{path.name} isn't a PCM WAV, and converting it needs ffmpeg")
    proc = subprocess.run([ffmpeg, "-v", "error", "-i", str(path), "-f", "f32le", "-ac", "1", "-ar", "48000", "-"],
                          capture_output=True)
    if proc.returncode != 0 or not proc.stdout:
        raise MicError(f"couldn't read {path.name}: {proc.stderr.decode(errors='replace').strip()[:200]}")
    samples = array("f")
    samples.frombytes(proc.stdout[: len(proc.stdout) // 4 * 4])
    if sys.byteorder != "little":
        samples.byteswap()
    return samples, 48000


def _pcm_to_mono(raw: bytes, width: int, channels: int) -> array:
    if width == 1:
        ints, scale = [b - 128 for b in raw], 128.0
    elif width == 2:
        a = array("h")
        a.frombytes(raw[: len(raw) // 2 * 2])
        ints, scale = a, 32768.0
    elif width == 3:
        ints = [int.from_bytes(raw[i:i + 3], "little", signed=True) for i in range(0, len(raw) - 2, 3)]
        scale = 8388608.0
    elif width == 4:
        a = array("i")
        a.frombytes(raw[: len(raw) // 4 * 4])
        ints, scale = a, 2147483648.0
    else:
        raise MicError(f"unsupported WAV sample width {width}")
    if width in (2, 4) and sys.byteorder != "little":
        ints.byteswap()
    if channels == 1:
        return array("f", (v / scale for v in ints))
    return array("f", (sum(ints[i:i + channels]) / (scale * channels) for i in range(0, len(ints), channels)))


def normalize(samples: array, rms: float) -> array:
    """Scale to a speaking level (RMS over the voiced part), with peaks kept
    under full scale."""
    if not samples:
        return array("f")
    peak = max(abs(v) for v in samples) or 1.0
    voiced = [v for v in samples if abs(v) > 0.05 * peak] or list(samples)
    level = math.sqrt(sum(v * v for v in voiced) / len(voiced)) or 1.0
    gain = min(rms / level, 0.98 / peak)
    return array("f", (v * gain for v in samples))


# --------------------------------------------------------------------------
# The microphone


@dataclass
class Clip:
    start: float  # on the microphone's clock
    samples: array
    rate: int
    label: str = ""

    @property
    def duration(self) -> float:
        return len(self.samples) / self.rate

    @property
    def end(self) -> float:
        return self.start + self.duration


class Mic:
    """The sound at the microphone: room tone plus whatever clips are playing.

    ``now()`` is the microphone's clock. It runs in real time unless
    ``world_running`` says the app is frozen, so sound and game time agree.
    """

    def __init__(self, room_db: float = ROOM_DB, seed: int = 1):
        self._lock = threading.Lock()
        self.clips: list[Clip] = []
        self.pending: list[tuple[int, dict]] = []  # (game frame, play() kwargs): replays
        self.history: list[dict] = []
        self.world_running: Callable[[], bool] = lambda: True
        self.world_frame: Callable[[], int] = lambda: -1
        self.log: Callable[[str], None] = lambda m: None
        self._t = 0.0
        self._last_real: Optional[float] = None
        rng = random.Random(seed)
        level = 10 ** (room_db / 20)
        noise, y = [], 0.0
        for _ in range(ROOM_RATE):
            y = 0.85 * y + 0.15 * rng.gauss(0.0, 1.0)  # a soft low-pass: room tone, not hiss
            noise.append(y)
        rms = math.sqrt(sum(v * v for v in noise) / len(noise)) or 1.0
        self.room = array("f", (v * level / rms for v in noise))

    def now(self) -> float:
        with self._lock:
            return self._t

    def tick(self, real_now: float) -> None:
        running = self.world_running()
        with self._lock:
            if self._last_real is not None and running:
                self._t += min(real_now - self._last_real, 0.1)
            self._last_real = real_now
            self.clips = [c for c in self.clips if c.end > self._t - 5.0]
            due = []
            if self.pending:
                frame = self.world_frame()
                due = [p for p in self.pending if frame >= p[0]]
                self.pending = [p for p in self.pending if frame < p[0]]
        for at, kwargs in due:
            clip = self.play(**kwargs)
            self.log(f"frame {at}: playing {clip.duration:.1f}s{(' — ' + clip.label) if clip.label else ''}")

    def play_at(self, frame: int, samples: array, rate: int, volume: float = 1.0, label: str = "",
                kind: str = "audio") -> None:
        """Start a clip when the app reaches a game frame (replays)."""
        with self._lock:
            self.pending.append((frame, {"samples": samples, "rate": rate, "volume": volume, "label": label,
                                         "queue": False, "kind": kind}))

    def busy(self) -> bool:
        with self._lock:
            return bool(self.pending) or any(c.end > self._t for c in self.clips)

    def render(self, t0: float, rate: int, n: int) -> list[float]:
        room = self.room
        size = len(room)
        i = int(t0 * rate) % size
        out: list[float] = []
        while len(out) < n:
            take = min(n - len(out), size - i)
            out.extend(room[i:i + take])
            i = 0
        with self._lock:
            clips = [c for c in self.clips if c.end > t0 and c.start < t0 + n / rate]
        for c in clips:
            s, cr, last = c.samples, c.rate, len(c.samples) - 1
            first = max(0, math.ceil((c.start - t0) * rate))
            stop = min(n, math.ceil((c.end - t0) * rate))
            step = cr / rate
            pos = (t0 + first / rate - c.start) * cr
            for k in range(first, stop):
                j = int(pos)
                if j < last:
                    a = s[j]
                    out[k] += a + (s[j + 1] - a) * (pos - j)
                elif j == last:
                    out[k] += s[j]
                pos += step
        return out

    def play(self, samples: array, rate: int, volume: float = 1.0, label: str = "", queue: bool = True,
             kind: str = "audio") -> Clip:
        clip_samples = normalize(samples, SPEECH_RMS * max(0.0, volume))
        with self._lock:
            start = self._t + LEAD
            if queue and self.clips:
                start = max(start, max(c.end for c in self.clips) + GAP)
            clip = Clip(start, clip_samples, rate, label)
            self.clips.append(clip)
            self.history.append({"label": label, "kind": kind, "duration": round(clip.duration, 3), "at": time.time(),
                                 "mic_time": round(start, 3), "volume": volume})
        return clip

    def stop(self) -> None:
        with self._lock:
            self.clips = []
            self.pending = []

    def remaining(self) -> float:
        with self._lock:
            return max([c.end - self._t for c in self.clips] + [0.0])


class _Downsampler:
    """One stream's audio, brought down to EAR_RATE: each output sample is the
    mean of the input that falls in its span (a box filter — cheap, and enough
    anti-aliasing for speech). Carries its state across a stream's chunks."""

    def __init__(self, rate: int):
        self.step = rate / EAR_RATE
        self.edge = self.step  # where the output sample being gathered ends (input samples from chunk start)
        self.carry_sum = 0.0
        self.carry_n = 0

    def feed(self, x: array) -> array:
        n = len(x)
        cs = list(accumulate(x, initial=0.0))
        out = array("f")
        edge, step = self.edge, self.step
        prev, acc_sum, acc_n = 0, self.carry_sum, self.carry_n
        while edge <= n:
            i = int(edge)
            c = acc_n + i - prev
            out.append((acc_sum + cs[i] - cs[prev]) / c if c else (out[-1] if out else 0.0))
            prev, acc_sum, acc_n = i, 0.0, 0
            edge += step
        self.carry_sum = acc_sum + cs[n] - cs[prev]
        self.carry_n = acc_n + n - prev
        self.edge = edge - n
        return out


class Speakers:
    """What the app plays, as Visor hears it: every playback stream mixed to
    mono at EAR_RATE in a ring of the last EAR_SECONDS, on the monotonic clock
    (which every process on the machine shares, so callers can ask by time)."""

    def __init__(self, seconds: int = EAR_SECONDS, origin: Optional[float] = None):
        self.size = seconds * EAR_RATE
        self.ring = array("f", bytes(4 * self.size))
        self.origin = time.monotonic() if origin is None else origin  # ring index 0
        self.zeroed_to = 0  # absolute index from which nothing has been written (silence)
        self.last_sound: Optional[float] = None  # when the last audible block ended
        self.sound_since: Optional[float] = None  # when the current stretch of sound began
        self._streams: dict[int, tuple[_Downsampler, int, float]] = {}  # stream -> (resampler, next index, next t)
        self._lock = threading.Lock()

    def index(self, t: float) -> int:
        return round((t - self.origin) * EAR_RATE)

    def time(self, index: int) -> float:
        return self.origin + index / EAR_RATE

    def played(self, stream: int, t0: float, rate: int, samples: array) -> None:
        with self._lock:
            state = self._streams.get(stream)
            if state is None or abs(t0 - state[2]) > 0.002:  # new, or picking up after an underrun
                state = (_Downsampler(rate), self.index(t0), t0)
            down, at, _ = state
            out = down.feed(samples)
            self._streams[stream] = (down, at + len(out), t0 + len(samples) / rate)
            if out:
                self._mix(at, out)

    def forget(self, stream: int) -> None:
        with self._lock:
            self._streams.pop(stream, None)

    def _mix(self, at: int, out: array) -> None:
        end = at + len(out)
        if end > self.zeroed_to:  # clear last lap's audio (and any silent gap) before writing over it
            start = max(self.zeroed_to, end - self.size)
            self._fill(start, end, None)
            self.zeroed_to = end
        oldest = self.zeroed_to - self.size
        if at < oldest:  # too old to remember
            out = out[oldest - at:]
            at = oldest
        self._fill(at, at + len(out), out)
        # Loudness, block by block, so a stretch of sound begins and ends where it does.
        for i in range(0, len(out), EAR_BLOCK):
            block = out[i:i + EAR_BLOCK]
            if _db(block) > SOUND_DB:
                t_start, t_end = self.time(at + i), self.time(at + i + len(block))
                if self.last_sound is None or t_start - self.last_sound > QUIET_GAP:
                    self.sound_since = t_start
                self.last_sound = max(self.last_sound or t_end, t_end)

    def _fill(self, a: int, b: int, values: Optional[array]) -> None:
        """Zero (values None) or add ``values`` into absolute indices [a, b)."""
        while a < b:
            i = a % self.size
            n = min(b - a, self.size - i)
            if values is None:
                self.ring[i:i + n] = array("f", bytes(4 * n))
            else:
                self.ring[i:i + n] = array("f", map(operator.add, self.ring[i:i + n], values[:n]))
                values = values[n:]
            a += n

    def read(self, t_from: float, t_to: float) -> tuple[float, array]:
        """The mix between two times (silence where nothing played), clamped to
        what's remembered."""
        with self._lock:
            a = max(self.index(t_from), self.zeroed_to - self.size)
            b = max(a, self.index(t_to))
            out = array("f")
            j, written = a, min(b, self.zeroed_to)
            while j < written:
                i = j % self.size
                n = min(written - j, self.size - i)
                out.extend(self.ring[i:i + n])
                j += n
            if b > j:  # nothing played there (yet): silence
                out.extend(array("f", bytes(4 * (b - j))))
            return self.time(a), out

    def status(self, now: Optional[float] = None) -> dict:
        now = time.monotonic() if now is None else now
        with self._lock:
            last, since = self.last_sound, self.sound_since
        _, recent = self.read(now - 0.5, now)
        playing = last is not None and now - last < QUIET_GAP / 2
        return {
            "now": now,
            "playing": playing,
            "sound_since": since if playing else None,
            "quiet_for": None if last is None else round(max(0.0, now - last), 3),
            "level_db": round(_db(recent), 1),
            "memory_s": self.size // EAR_RATE,
        }


def _db(samples) -> float:
    if not samples:
        return -120.0
    level = math.sqrt(sum(v * v for v in samples) / len(samples))
    return 20 * math.log10(level) if level > 1e-6 else -120.0


def pcm16(samples: array) -> bytes:
    """Mono floats as 16-bit little-endian PCM."""
    a = array("h", (32767 if v >= 1.0 else -32767 if v <= -1.0 else int(v * 32767) for v in samples))
    if sys.byteorder != "little":
        a.byteswap()
    return a.tobytes()


class WorldWatch:
    """Is the app's world running? Asks the layer, ~25 times a second.

    Running unless the layer says it's paused *and* frames have stopped (a
    step runs frames while paused). If the layer can't be reached — still
    starting, or exiting — the world is taken to be running, as a real
    microphone would be.
    """

    def __init__(self, layer_socket: Path, active: Callable[[], bool] = lambda: True):
        self.layer_socket = layer_socket
        self.active = active  # only watch closely while someone listens or something's queued
        self.running = True
        self.paused = False
        self.frame = -1  # the rig's frame: what replays are scheduled against
        self._render_frame = -1
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self) -> "WorldWatch":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        client: Optional[XrClient] = None
        while not self._stop.wait(0.04 if self.active() else 0.25):
            if not self.layer_socket.exists():
                self.running, self.paused = True, False
                continue
            try:
                client = client or XrClient(self.layer_socket, timeout=1.0)
                st = client.state()
                render_frame = int(st.get("render_frame", 0))
                self.frame = int(st.get("frame", -1))
                self.paused = bool(st.get("paused"))
                self.running = not self.paused or render_frame != self._render_frame
                self._render_frame = render_frame
            except (XrError, OSError, ValueError):
                if client is not None:
                    client.close()
                client = None
                self.running, self.paused = True, False


# --------------------------------------------------------------------------
# The helper process: one per app


class _Control:
    """JSON lines on a Unix socket, the same shape as the layer's protocol."""

    def __init__(self, path: Path, handler: Callable[[str, dict], dict]):
        self.path = path
        self.handler = handler
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        path.unlink(missing_ok=True)
        self._sock.bind(str(path))
        os.chmod(path, 0o600)
        self._sock.listen(8)
        self._sock.settimeout(0.5)
        self._stop = threading.Event()
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self) -> None:
        self._stop.set()
        self._sock.close()
        self.path.unlink(missing_ok=True)

    def _accept(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            conn.settimeout(None)
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        with conn, conn.makefile("rwb") as f:
            for line in f:
                try:
                    req = json.loads(line)
                    result = self.handler(req.get("cmd", ""), req.get("args") or {})
                    reply = {"id": req.get("id"), "ok": True, "result": result}
                except (MicError, KeyError, ValueError, TypeError) as e:
                    reply = {"id": None, "ok": False, "error": str(e)}
                f.write((json.dumps(reply) + "\n").encode())
                f.flush()


def serve(name: str, log: Callable[[str], None] = print) -> int:
    """Run one app's microphone until the app is gone."""
    d = paths.socket_dir()
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    meta = d / f"{name}.json"
    mic = Mic()
    mic.log = log
    pulse: Optional[PulseServer] = None
    watch = WorldWatch(d / f"{name}.sock", active=lambda: mic.busy() or bool(pulse and pulse.streams)).start()
    mic.world_running = lambda: watch.running
    mic.world_frame = lambda: watch.frame
    speakers = Speakers()
    pulse = PulseServer(pulse_socket(name), mic, log=log, clock=mic.now, sink=speakers).start()

    watched: dict = {"pid": None}  # the app this microphone belongs to, once its record appears

    def speakers_status() -> dict:
        outputs = [{"client": s.client.name, "rate": s.rate, "channels": s.channels, "name": s.name,
                    "corked": s.corked} for s in list(pulse.playback.values())]
        return {"open": bool(outputs), "streams": outputs, "rate": EAR_RATE, **speakers.status()}

    def handle(cmd: str, args: dict) -> dict:
        if cmd == "ping":
            return {"pong": True}
        if cmd == "status":
            streams = pulse.listening()
            return {
                "app_pid": watched["pid"],
                "listening": bool(streams),
                "streams": [{"client": s.client.name, "rate": s.rate, "channels": s.channels, "name": s.name}
                            for s in streams],
                "clients": [c.name for c in list(pulse.clients)],
                "speaking": mic.remaining() > 0,
                "remaining_s": round(mic.remaining(), 3),
                "world": "paused" if not watch.running else "running",
                "history": mic.history[-20:],
                "speakers": speakers_status(),
            }
        if cmd == "speakers":
            return speakers_status()
        if cmd == "heard":
            # What the speakers played between two monotonic times, as 16-bit PCM.
            now = time.monotonic()
            t_to = min(float(args.get("to", now)), now)
            t_from = max(float(args.get("from", t_to - 10.0)), t_to - EAR_SECONDS)
            start, samples = speakers.read(t_from, t_to)
            levels = [round(_db(samples[i:i + EAR_BLOCK]), 1) for i in range(0, len(samples), EAR_BLOCK)]
            return {"from": start, "to": start + len(samples) / EAR_RATE, "rate": EAR_RATE, "levels": levels,
                    "pcm": base64.b64encode(pcm16(samples)).decode() if args.get("pcm", True) else None}
        if cmd == "play":
            samples, rate = load_audio(args["path"])
            volume, label = float(args.get("volume", 1.0)), str(args.get("label", ""))
            kind = "speech" if args.get("kind") == "speech" else "audio"
            if args.get("at_frame") is not None:
                mic.play_at(int(args["at_frame"]), samples, rate, volume=volume, label=label, kind=kind)
                log(f"queued for frame {int(args['at_frame'])}: {len(samples) / rate:.1f}s"
                    f"{(' — ' + label) if label else ''}")
                return {"duration": round(len(samples) / rate, 3), "at_frame": int(args["at_frame"])}
            clip = mic.play(samples, rate, volume=volume, label=label, queue=bool(args.get("queue", True)), kind=kind)
            listening = bool(pulse.listening())
            log(f"playing {clip.duration:.1f}s{(' — ' + clip.label) if clip.label else ''}"
                f"{'' if listening else ' (the app has no stream open)'}")
            return {"duration": round(clip.duration, 3), "starts_in": round(clip.start - mic.now(), 3),
                    "listening": listening, "world": "running" if watch.running else "paused"}
        if cmd == "stop":
            mic.stop()
            return {"stopped": True}
        raise MicError(f"unknown command '{cmd}'")

    control = _Control(control_socket(name), handle)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    log(f"microphone for {name} at {pulse_socket(name)}")
    started = time.time()
    try:
        # The app's record (written just after it launches) says whose microphone this is.
        while not stop.wait(0.25):
            try:
                pid = json.loads(meta.read_text()).get("pid")
            except (OSError, ValueError):
                if watched["pid"] or time.time() - started > 15:
                    break  # the instance was stopped (or never launched)
                continue
            watched["pid"] = pid
            if pid and not _alive(int(pid)):
                break
    finally:
        control.close()
        pulse.stop()
        watch.stop()
        log("microphone off")
    return 0


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def helper_command(name: str) -> tuple[list[str], dict]:
    """How to start a microphone helper that outlives this process, and its
    environment."""
    env = dict(os.environ)
    appimage = os.environ.get("APPIMAGE")
    if appimage:
        # Through the AppImage, which mounts its own copy: ours is unmounted
        # when this process exits, and nothing of the helper may live in it.
        return [appimage, "sim", "mic-server", name], env
    # The same Visor as ours, wherever it was imported from (a checkout, a venv).
    root = str(Path(__file__).resolve().parents[2])
    env["PYTHONPATH"] = root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    # Not `-m visor.xr.mic`: the package imports this module first, so runpy would run a second copy.
    return [sys.executable, "-c", "import sys; from visor.xr.mic import main; sys.exit(main())", name], env


def start_helper(name: str, log_path: Path, timeout: float = 10.0) -> subprocess.Popen:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    pulse_socket(name).unlink(missing_ok=True)
    cmd, env = helper_command(name)
    with open(log_path, "ab") as log:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                cwd=str(log_path.parent), env=env, start_new_session=True)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pulse_socket(name).exists() and control_socket(name).exists():
            return proc
        if proc.poll() is not None:
            break
        time.sleep(0.05)
    proc.kill()
    raise MicError(f"the microphone didn't start (see {log_path})")


# --------------------------------------------------------------------------
# Talking to a running microphone


class MicClient:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._rpc = XrClient(self.path, timeout=10.0)

    @classmethod
    def for_app(cls, layer_socket: str | Path) -> "MicClient":
        path = Path(layer_socket).with_suffix(".mic")
        if not path.exists():
            raise MicError("this app has no Visor microphone (it was started outside `visor sim run`, or with --mic, "
                           "so it hears this computer's real one)")
        return cls(path)

    def close(self) -> None:
        self._rpc.close()

    def status(self) -> dict:
        return self._call("status")

    def play(self, path: str | Path, volume: float = 1.0, label: str = "", queue: bool = True,
             at_frame: Optional[int] = None, kind: str = "audio") -> dict:
        """Play an audio file into the app's microphone: now (after anything
        already queued), or when the app reaches game frame ``at_frame``.
        ``kind`` "speech" marks a spoken line whose label is its text."""
        args: dict = {"path": str(Path(path).resolve()), "volume": volume, "label": label, "queue": queue,
                      "kind": kind}
        if at_frame is not None:
            args["at_frame"] = int(at_frame)
        return self._call("play", **args)

    def stop(self) -> None:
        self._call("stop")

    def speakers(self) -> dict:
        """What the app's speakers are doing: open streams, whether it's
        playing sound now, how loud, and how long it has been quiet."""
        return self._call("speakers")

    def heard(self, t_from: float, t_to: Optional[float] = None, pcm: bool = True) -> dict:
        """What the speakers played between two times on the monotonic clock
        (time.monotonic(), shared by every process on the machine): 16 kHz
        mono samples, and the loudness of each 50 ms."""
        args: dict = {"from": t_from, "pcm": pcm}
        if t_to is not None:
            args["to"] = t_to
        r = self._call("heard", **args)
        if pcm:
            raw = array("h")
            raw.frombytes(base64.b64decode(r["pcm"]))
            if sys.byteorder != "little":
                raw.byteswap()
            r["samples"] = array("f", (v / 32768.0 for v in raw))
        return r

    def _call(self, cmd: str, **args) -> dict:
        try:
            return self._rpc.call(cmd, **args)
        except XrError as e:
            raise MicError(str(e).split(": ", 1)[-1]) from None


def main(argv: Optional[list[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: visor sim mic-server <instance-name>", file=sys.stderr)
        return 2

    # The log is a file; what's said can be any language, whatever the locale.
    out = open(sys.stdout.fileno(), "w", encoding="utf-8", errors="replace", closefd=False, buffering=1)

    def log(msg: str) -> None:
        out.write(f"{time.strftime('%H:%M:%S')} {msg}\n")

    return serve(argv[0], log=log)
