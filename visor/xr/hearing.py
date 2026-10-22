"""Hearing for the virtual headset: what an app plays, as words and sounds.

The app's audio helper (visor.xr.mic) remembers the last three minutes of what
its speakers played, mixed to mono at 16 kHz. Ears turn a stretch of that into
something an agent can read: the speech in it, transcribed offline by Whisper
(faster-whisper, part of ``pip install 'visor-dev[voice]'``), and the other
sounds, described by when, how long and how loud.

Agents hear in whole sentences. Ears.hear(wait=...) first lets the app finish
what it's saying — stepping a paused world forward if need be, since an app
streaming its speech only plays more as it runs — so a reply is never cut in
half between two observations.
"""

from __future__ import annotations

import io
import math
import os
import threading
import time
import wave
from array import array
from dataclasses import dataclass, field
from importlib.util import find_spec
from pathlib import Path
from typing import Callable, Optional

from visor.xr import paths
from visor.xr.mic import EAR_BLOCK, EAR_RATE, SOUND_DB, MicClient, pcm16

DEFAULT_MODEL = "base"  # ~145 MB, multilingual; "small" is better and slower, "tiny" faster and worse
LANGUAGES = {
    "en": "English", "es": "Spanish", "fr": "French", "de": "German", "it": "Italian", "pt": "Portuguese",
    "nl": "Dutch", "pl": "Polish", "ru": "Russian", "uk": "Ukrainian", "tr": "Turkish", "sv": "Swedish",
    "ar": "Arabic", "hi": "Hindi", "sw": "Swahili", "vi": "Vietnamese", "zh": "Chinese", "ja": "Japanese",
    "ko": "Korean",
}
LONG_SOUND = 6.0  # seconds of sound without speech: music or ambience, not an event
HOLD = 15.0  # a sentence still being said is kept back (for the next listen) unless it's older than this
SENTENCE_PAUSE = 1.0  # quiet this long after speech means it has finished talking (not just drawn breath)
EVENT_DB = 10.0  # how far above the background a sound must rise to count as an event
BACKGROUND_MIN = 2.5  # seconds a sound must keep going (and not be speech) to count as background
CLIPPED = 0.99  # a sample this close to full scale has been clipped


class HearingError(RuntimeError):
    pass


def available() -> bool:
    """Is speech recognition installed?"""
    return find_spec("faster_whisper") is not None


def model_name() -> str:
    return os.environ.get("VISOR_XR_HEARING_MODEL", DEFAULT_MODEL)


def models_dir() -> Path:
    return paths.xr_home() / "hearing"


def installed(name: Optional[str] = None) -> bool:
    return (models_dir() / f"models--Systran--faster-whisper-{name or model_name()}").is_dir()


_models: dict = {}
_models_lock = threading.Lock()


def _model(name: str, log: Callable[[str], None]):
    with _models_lock:
        if name not in _models:
            os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
            os.environ.setdefault("HF_HUB_VERBOSITY", "error")
            from faster_whisper import WhisperModel

            if not installed(name):
                log(f"downloading the {name} speech-recognition model (once)")
            threads = max(2, min(8, (os.cpu_count() or 4) // 4))
            try:
                model = WhisperModel(name, device="cpu", compute_type="int8", cpu_threads=threads,
                                     download_root=str(models_dir()))
            except Exception as e:  # a bad name, no network for the first download
                raise HearingError(f"couldn't load the {name} speech-recognition model: {e}") from None
            _models[name] = (model, threading.Lock())
        return _models[name]


_vad = None


def speech_probability(samples) -> list[float]:
    """Silero VAD (bundled with faster-whisper): how likely each 32 ms of
    16 kHz audio is speech. Empty if speech recognition isn't installed."""
    global _vad
    if not available() or not len(samples):
        return []
    import numpy as np
    from faster_whisper.vad import get_vad_model

    with _models_lock:
        if _vad is None:
            _vad = get_vad_model()
    audio = np.asarray(samples, dtype=np.float32)
    audio = np.pad(audio, (0, (-len(audio)) % 512))
    return [float(p) for p in np.asarray(_vad(audio)).reshape(-1)]


def speech_regions(samples, t0: float, threshold: float = 0.5, min_gap: float = 0.5) -> list[tuple[float, float]]:
    """Where speech is in 16 kHz audio starting at t0, by voice activity
    (not loudness, so music and ambience don't count). Empty without
    speech recognition installed."""
    probs = speech_probability(samples)
    chunk = 512 / EAR_RATE
    out: list[tuple[float, float]] = []
    for i, p in enumerate(probs):
        if p <= threshold:
            continue
        a, b = t0 + i * chunk, t0 + (i + 1) * chunk
        if out and a - out[-1][1] < min_gap:
            out[-1] = (out[-1][0], b)
        else:
            out.append((a, b))
    return out


@dataclass
class Line:
    """A sentence (or so) of speech, in monotonic time."""
    start: float
    end: float
    text: str
    language: Optional[str] = None


@dataclass
class Sound:
    """A stretch of sound, in monotonic time."""
    start: float
    end: float
    level_db: float  # its loudest 50 ms
    speech: bool = False

    @property
    def duration(self) -> float:
        return self.end - self.start


def transcribe(samples, language: Optional[str] = None, model: Optional[str] = None,
               log: Callable[[str], None] = lambda m: None) -> tuple[list[tuple[float, float, str]], Optional[str]]:
    """Speech in 16 kHz mono audio: [(start s, end s, text)] and the language
    it's in (None if unsure)."""
    if not available():
        raise HearingError("hearing words needs speech recognition: pip install 'visor-dev[voice]'")
    import numpy as np

    audio = np.asarray(samples, dtype=np.float32)
    if not len(audio):
        return [], None
    m, lock = _model(model or model_name(), log)
    with lock:
        segments, info = m.transcribe(audio, language=language, beam_size=1, vad_filter=True,
                                      vad_parameters={"min_silence_duration_ms": 500, "speech_pad_ms": 200},
                                      condition_on_previous_text=False)
        lines = [(s.start, s.end, s.text.strip()) for s in segments
                 if s.text.strip() and not (s.no_speech_prob > 0.6 and s.avg_logprob < -1.0)]
    lang = language or (info.language if lines and info.language_probability > 0.7 else None)
    return lines, lang


def _percentile(values: list[float], q: float) -> float:
    v = sorted(values)
    return v[min(len(v) - 1, int(q * len(v)))] if v else -120.0


class Scene:
    """Follows one app's sound over time, 50 ms at a time, telling events (a
    voice, a chime, an effect) from the background (music, ambience).

    A stretch of sound that keeps going for BACKGROUND_MIN seconds without
    being mostly speech becomes the background, and its beginning isn't an
    event. While there's a background, an event is sound rising EVENT_DB
    above it, and a second of silence ends it. Events closer than 0.4 s
    merge; blips under 100 ms don't count."""

    def __init__(self):
        self.bg: Optional[float] = None  # the background's level (dB), or None for silence

    def feed(self, levels: list[float], t0: float, speech: list[tuple[float, float]] = (),
             ongoing_since: Optional[float] = None) -> tuple[list[Sound], Optional[str]]:
        """Events in a run of loudness readings starting at t0, and how the
        background changed ("started" | "stopped" | None). ``ongoing_since``
        is when a sound still playing at the end began, if known."""
        block = EAR_BLOCK / EAR_RATE
        was = self.bg is not None
        events: list[Sound] = []
        run: list[float] = []  # a stretch of sound while there's no background
        run_start = run_last = None
        ev_start = ev_end = None  # an event over the background
        ev_peak = -120.0
        quiet = 0

        def in_speech(t: float) -> bool:
            return any(a - 0.2 <= t <= b + 0.2 for a, b in speech)

        def close_event():
            nonlocal ev_start
            if ev_start is not None and ev_end - ev_start >= 0.1:
                events.append(Sound(ev_start, ev_end, ev_peak))
            ev_start = None

        def close_run(true_start: Optional[float] = None):
            nonlocal run, run_start
            if not run:
                return
            begun = min(run_start, true_start) if true_start is not None else run_start
            if run_last + block - begun >= BACKGROUND_MIN and not self._speechy(run_start, len(run), in_speech):
                self.bg = _percentile(run, 0.3)
            elif run_last + block - run_start >= 0.1:
                events.append(Sound(run_start, run_last + block, max(run)))
            run, run_start = [], None

        for i, db in enumerate(levels):
            t = t0 + i * block
            if self.bg is None:
                if db > SOUND_DB:
                    if run and t - (run_last + block) > 0.4:
                        close_run()
                    if not run:
                        run_start = t
                    run.append(db)
                    run_last = t
                    if (t + block - run_start >= BACKGROUND_MIN and len(run) * block >= 0.8 * BACKGROUND_MIN
                            and not self._speechy(run_start, len(run), in_speech)):
                        self.bg = _percentile(run, 0.3)  # it has gone on long enough: it's the background
                        run, run_start = [], None
                elif run and t - (run_last + block) > 0.4:
                    close_run()
            else:
                if db <= SOUND_DB:
                    quiet += 1
                    if quiet * block >= 1.0:
                        close_event()
                        self.bg = None
                    continue
                quiet = 0
                if db >= self.bg + EVENT_DB:
                    if ev_start is not None and t - ev_end > 0.4:
                        close_event()
                    if ev_start is None:
                        ev_start, ev_peak = t, -120.0
                    ev_end, ev_peak = t + block, max(ev_peak, db)
                else:
                    self.bg += 0.02 * (db - self.bg)  # follow a background that drifts
        if run:
            ongoing = run_last + block >= t0 + len(levels) * block - 0.1
            close_run(ongoing_since if ongoing else None)
        close_event()
        now = self.bg is not None
        return events, (None if now == was else "started" if now else "stopped")

    @staticmethod
    def _speechy(start: float, n: int, in_speech) -> bool:
        block = EAR_BLOCK / EAR_RATE
        return sum(1 for k in range(n) if in_speech(start + k * block)) >= 0.5 * n


@dataclass
class Heard:
    """What the app played over a stretch of time."""
    start: float  # monotonic
    end: float
    sounds: list[Sound]  # events: speech, chimes, effects (not the background)
    lines: list[Line] = field(default_factory=list)
    language: Optional[str] = None
    transcribed: bool = False  # was speech recognition run
    background_db: Optional[float] = None  # a steady sound under everything (music, ambience), if any
    background_change: Optional[str] = None  # "started" | "stopped" since the listener last heard
    clipped: float = 0.0  # share of the samples at full scale
    samples: array = field(default_factory=lambda: array("f"), repr=False)

    @property
    def silent(self) -> bool:
        """Nothing worth telling: no speech, no events, no change in the background."""
        return not self.sounds and not self.lines and not self.background_change

    def describe(self) -> str:
        """For an agent: what it heard, in a sentence or two ("" for nothing new)."""
        if self.silent:
            return ""
        parts = []
        if self.lines:
            # Consecutive lines in one language read as one quote; a change of language is named.
            runs: list[tuple[Optional[str], list[str]]] = []
            for line in self.lines:
                if runs and runs[-1][0] == line.language:
                    runs[-1][1].append(line.text)
                else:
                    runs.append((line.language, [line.text]))
            quotes = []
            for i, (code, texts) in enumerate(runs):
                name = LANGUAGES.get(code or "", code) if code and code != "en" else None
                if i == 0:
                    lead = f"You heard the app say{f' (in {name})' if name else ''}: "
                else:
                    lead = f"then{f' in {name}' if name else (' in English' if code == 'en' else '')}: "
                quotes.append(lead + '"' + " ".join(texts) + '"')
            parts.append(", ".join(quotes))
        others = [s for s in self.sounds if not s.speech]
        if others:
            durations = ", ".join(f"{s.duration:.1f}s" for s in others[:4])
            what = "a short sound" if len(others) == 1 and others[0].duration < 1.0 else \
                f"{len(others)} sound{'s' if len(others) > 1 else ''} ({durations})"
            parts.append(("Also " if self.lines else "You heard ") + what + ".")
        if self.background_change == "started":
            parts.append("Music or ambience is playing in the background.")
        elif self.background_change == "stopped":
            parts.append("The background sound stopped.")
        if not self.transcribed and any(s.duration >= 0.5 for s in self.sounds):
            parts.append("(It may have spoken — without speech recognition its words can't be made out.)")
        if self.clipped > 0.001:
            parts.append(f"The audio was distorted: {self.clipped:.1%} of it clipped at full volume.")
        return " ".join(parts)

    def save(self, path: str | Path) -> Path:
        """The audio as a 16 kHz mono WAV."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.wav())
        return path

    def wav(self) -> bytes:
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(EAR_RATE)
            w.writeframes(pcm16(self.samples))
        return buf.getvalue()

    def to_json(self) -> dict:
        def rel(t: float) -> float:
            return round(t - self.start, 2)

        return {
            "seconds": round(self.end - self.start, 2),
            "said": " ".join(line.text for line in self.lines),
            "language": self.language,
            "lines": [{"start": rel(x.start), "end": rel(x.end), "text": x.text, "language": x.language}
                      for x in self.lines],
            "sounds": [{"start": rel(s.start), "end": rel(s.end), "level_db": round(s.level_db, 1), "speech": s.speech}
                       for s in self.sounds],
            "background_db": None if self.background_db is None else round(self.background_db, 1),
            "background_change": self.background_change,
            "transcribed": self.transcribed,
            "clipped": round(self.clipped, 5),
        }


class Ears:
    """Listens to one app for one listener, picking up where it left off."""

    def __init__(self, mic: MicClient, since: Optional[float] = None, language: Optional[str] = None,
                 transcribe: bool = True, model: Optional[str] = None, log: Callable[[str], None] = lambda m: None,
                 clock: Callable[[], float] = time.monotonic):
        self.mic = mic
        # The helper's clock: the monotonic clock every process on this machine shares.
        self.clock = clock
        self.cursor = clock() if since is None else since
        self.language = language
        self.transcribe = transcribe and available()
        self.model = model
        self.log = log
        self.scene = Scene()

    def utterance_start(self, lookback: float = 20.0) -> Optional[float]:
        """If the app is talking now, when it started (None if it isn't)."""
        if not self.talking():
            return None
        now = self.clock()
        if self.transcribe:
            r = self.mic.heard(now - lookback, now)
            regions = speech_regions(r["samples"], r["from"])
            if regions:
                return regions[-1][0]
        since = self.mic.speakers().get("sound_since")  # no voice detection: by loudness
        return since if since is not None and now - since < LONG_SOUND else now - 2.0

    def talking(self) -> bool:
        """Is the app in the middle of saying something?"""
        st = self.mic.speakers()
        if not st.get("playing"):
            return False
        now = self.clock()
        if self.transcribe:  # sound isn't speech: don't wait out the music
            tail = self.mic.heard(now - 0.6, now)["samples"]
            return any(p > 0.5 for p in speech_probability(tail))
        since = st.get("sound_since")
        return since is not None and now - since < LONG_SOUND

    def hear(self, wait: float = 0.0, step: Optional[Callable[[float], None]] = None, settle: bool = True,
             expect: float = 0.0) -> Heard:
        """Everything the app played since the last call. With ``wait``, first
        let it finish what it's saying, for up to that many seconds — a pause
        between sentences isn't the end — and with ``expect``, give a reply
        that long to begin (apps answer an action a moment later). ``step``
        runs a paused world forward (it's given seconds); without it, real
        time passes. With ``settle``, a sentence still being said is kept for
        the next call rather than cut in two."""
        deadline = self.clock() + wait

        def tick() -> None:
            if step is not None:
                step(0.25)
            else:
                time.sleep(0.25)

        talked = wait > 0 and self.talking()
        reply_by = self.clock() + min(expect, wait)
        while wait > 0 and not talked and self.clock() < reply_by:
            tick()
            talked = self.talking()
        quiet_since = None
        while talked and self.clock() < deadline:
            if self.talking():
                quiet_since = None
            elif quiet_since is None:
                quiet_since = self.clock()
            elif self.clock() - quiet_since >= SENTENCE_PAUSE:
                break
            tick()
        until = self.clock() - 0.05  # the last moment may still be mixing
        st = self.mic.speakers()
        since = st.get("sound_since") if st.get("playing") else None
        if settle and since is not None and self.scene.bg is None and until - since < BACKGROUND_MIN:
            until = max(self.cursor, since - 0.05)  # a fresh sound: wait to know if it's background
        if settle and not self.transcribe and since is not None and until - since < LONG_SOUND:
            until = max(self.cursor, since - 0.05)  # no voice detection: hold back a fresh stretch of sound
        # Loudness first: silence needs no samples.
        r = self.mic.heard(self.cursor, until, pcm=False)
        levels, start, end = r["levels"], r["from"], r["to"]
        block = EAR_BLOCK / EAR_RATE
        samples, regions = array("f"), []
        if any(db > SOUND_DB for db in levels):
            samples = self.mic.heard(start, end)["samples"]
            if self.transcribe:
                regions = speech_regions(samples, start, min_gap=0.6)
                # A sentence still being said: keep it (and what follows) for the next listen.
                if settle and regions and end - regions[-1][1] < 0.3 and end - regions[-1][0] < HOLD:
                    end = max(start, regions[-1][0] - 0.3)
                    samples = samples[:round((end - start) * EAR_RATE)]
                    levels = levels[:int((end - start) / block)]
                    regions = regions[:-1]
        self.cursor = end
        events, change = self.scene.feed(levels, start, regions, ongoing_since=since)
        heard = Heard(start=start, end=end, sounds=events, background_db=self.scene.bg, background_change=change,
                      samples=samples)
        if not len(samples):
            return heard
        heard.clipped = sum(1 for v in samples if v >= CLIPPED or v <= -CLIPPED) / len(samples)
        if self.transcribe:
            # Each stretch of speech on its own, so each finds its own language: a tutor
            # switches between sentences, and Whisper picks one language per call.
            for a_t, b_t in regions:
                a = max(0, round((a_t - start - 0.25) * EAR_RATE))
                b = round((b_t - start + 0.25) * EAR_RATE)
                lines, lang = transcribe(samples[a:b], language=self.language, model=self.model, log=self.log)
                at = start + a / EAR_RATE
                heard.lines += [Line(at + x, at + y, text, lang) for x, y, text in lines]
            for e in heard.sounds:
                e.speech = any(line.start < e.end + 0.3 and line.end > e.start - 0.3 for line in heard.lines)
            langs = [line.language for line in heard.lines if line.language]
            heard.language = max(set(langs), key=langs.count) if langs else None
            heard.transcribed = True
        return heard


def level_bar(levels: list[float], width: int = 48) -> str:
    """A text sparkline of loudness readings (for terminals)."""
    if not levels:
        return ""
    ticks = " ▁▂▃▄▅▆▇█"
    per = max(1, math.ceil(len(levels) / width))
    out = []
    for i in range(0, len(levels), per):
        db = max(levels[i:i + per])
        out.append(ticks[0] if db <= SOUND_DB else ticks[min(8, 1 + int((db - SOUND_DB) / 60 * 8))])
    return "".join(out)
