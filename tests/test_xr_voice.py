"""Speech for the virtual headset: choosing voices, and saying things in a playtest."""

import json
import math
import struct
import wave

import pytest

from visor.xr import voice as tts
from visor.xr.mic import MicError


class TestVoices:
    def test_default_is_english(self):
        assert tts.resolve() == tts.DEFAULT_VOICE

    def test_languages(self):
        assert tts.resolve(language="es") == "es_ES-davefx-medium"
        assert tts.resolve(language="pt-BR").startswith("pt_")

    def test_regional_voice_preferred_when_installed(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        tts.voices_dir().mkdir(parents=True)
        (tts.voices_dir() / "es_MX-ald-medium.onnx").write_bytes(b"")
        assert tts.resolve(language="es-MX") == "es_MX-ald-medium"
        assert tts.resolve(language="es") == "es_ES-davefx-medium"

    def test_unknown_language_says_what_there_is(self):
        with pytest.raises(tts.VoiceError, match="no built-in voice for 'tlh'.*es"):
            tts.resolve(language="tlh")

    @pytest.mark.parametrize("bad", ["lessac", "en-lessac", "../../etc/passwd-x-y", "en_US-a/b-medium"])
    def test_voice_names_are_checked(self, bad):
        with pytest.raises(tts.VoiceError):
            tts.resolve(voice=bad)

    def test_download_url_follows_piper_layout(self):
        model, config = tts.voice_urls("en_GB-northern_english_male-medium")
        assert model == (f"{tts.VOICES_URL}/en/en_GB/northern_english_male/medium/"
                         "en_GB-northern_english_male-medium.onnx")
        assert config == model + ".json"


class TestSynthesize:
    def test_nothing_to_say(self):
        with pytest.raises(tts.VoiceError, match="nothing to say"):
            tts.synthesize("   ")

    def test_without_an_engine_it_explains(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        monkeypatch.setattr(tts, "available", lambda: False)
        with pytest.raises(tts.VoiceError, match=r"visor-dev\[voice\]"):
            tts.synthesize("hello there")

    def test_cached_lines_need_no_engine(self, tmp_path, monkeypatch):
        """A line said once is a file: replays and repeats don't synthesize again."""
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        monkeypatch.setattr(tts, "available", lambda: False)
        import hashlib

        key = hashlib.sha256(f"{tts.DEFAULT_VOICE}\n1.00\nhello there".encode()).hexdigest()[:20]
        cached = tmp_path / "visor" / "xr" / "speech" / f"{key}.wav"
        cached.parent.mkdir(parents=True)
        _wav(cached, seconds=0.5)
        s = tts.synthesize("  hello   there ")
        assert s.path == cached and s.duration == pytest.approx(0.5) and s.text == "hello there"

    @pytest.mark.skipif(not (tts.available() and tts.DEFAULT_VOICE in tts.installed()),
                        reason="needs Piper and its default voice (no downloads in tests)")
    def test_real_speech(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        s = tts.synthesize("Testing, one, two, three.")
        with wave.open(str(s.path)) as w:
            assert w.getnchannels() == 1 and w.getsampwidth() == 2
        assert 1.0 < s.duration < 4.0


def _wav(path, seconds=1.0, rate=16000):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        n = int(seconds * rate)
        w.writeframes(struct.pack(f"<{n}h", *(int(8000 * math.sin(i / 10)) for i in range(n))))
    return path


class FakeMic:
    def __init__(self, listening=True):
        self.listening = listening
        self.played = []

    def play(self, path, volume=1.0, label="", kind="audio", **_):
        self.played.append((path, volume, label, kind))
        return {"duration": 1.0, "starts_in": 0.05, "listening": self.listening, "world": "paused"}

    def status(self):
        return {"listening": self.listening}


class FakeXr:
    def __init__(self, mic):
        self._mic = mic

    def mic(self):
        if self._mic is None:
            raise MicError("this app has no Visor microphone")
        return self._mic

    def state(self):
        return {"frame": 500, "render_frame": 500, "paused": True}


@pytest.fixture
def playtest(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    from visor.xr.playtest.brains import MonkeyBrain
    from visor.xr.playtest.session import Playtest, PlaytestConfig

    t = Playtest(PlaytestConfig(target="demo", persona="soft-spoken", brain="monkey"), brain=MonkeyBrain())
    t.dir.mkdir(parents=True)
    speech = tmp_path / "line.wav"
    _wav(speech, seconds=1.5)
    monkeypatch.setattr(tts, "synthesize", lambda text, **kw: tts.Speech(text=text, voice=tts.DEFAULT_VOICE,
                                                                          path=speech, duration=1.5))
    return t


class TestPlaytestVoice:
    def test_say_speaks_in_the_persona_s_voice_and_runs_the_world(self, playtest):
        mic = FakeMic()
        playtest.xr = FakeXr(mic)
        text, frames = playtest._say("Hi. Can you hear me?", None)
        assert text == 'You said "Hi. Can you hear me?" (1.5s).'
        assert frames == round((1.5 + 0.8) * 72)  # the world runs while it's said
        path, volume, label, kind = mic.played[0]
        assert volume == 0.35 and label == "Hi. Can you hear me?" and kind == "speech"  # soft-spoken
        ev = playtest.voice_events[0]
        assert ev["frame"] == 500 and ev["audio"] == "voice/001.wav" and (playtest.dir / ev["audio"]).exists()

    def test_saying_to_an_app_that_isn_t_listening(self, playtest):
        playtest.xr = FakeXr(FakeMic(listening=False))
        text, _ = playtest._say("hello", None)
        assert "wasn't listening" in text

    def test_no_microphone_is_a_result_not_a_crash(self, playtest):
        playtest.xr = FakeXr(None)
        text, frames = playtest._say("hello", None)
        assert text.startswith("You can't speak here") and frames == 0

    def test_the_agent_hears_when_the_mic_opens_and_closes(self, playtest):
        mic = FakeMic(listening=True)
        playtest.xr = FakeXr(mic)
        assert "it's listening" in playtest._mic_note()
        assert playtest._mic_note() == ""  # said once, not every step
        mic.listening = False
        assert "turned its microphone off" in playtest._mic_note()

    def test_a_mic_that_was_never_on_isn_t_mentioned(self, playtest):
        playtest.xr = FakeXr(FakeMic(listening=False))
        assert playtest._mic_note() == ""


def test_report_plays_back_what_was_said(tmp_path):
    from visor.xr.playtest.report import write_html

    report = {
        "name": "pt", "created": "2026-10-22T10:00:00", "duration_s": 30,
        "app": {"name": "Game"}, "persona": {"name": "Soft-spoken player", "tagline": "quiet"}, "goal": "talk",
        "brain": {"kind": "monkey"}, "config": {}, "verdict": {}, "issues": [], "perf": {}, "artifacts": {},
        "steps": [{"step": 1, "frame": 10, "action": "say", "args": {"text": "<hi>"}, "thought": "", "t": 0,
                   "result": 'You said "<hi>" (1.0s).', "screenshot": "shots/001.jpg", "audio": "voice/001.wav"}],
    }
    html = write_html(report, tmp_path / "report.html").read_text(encoding="utf-8")
    assert '<audio controls preload="none" src="voice/001.wav">' in html
    assert "“&lt;hi&gt;”" in html  # what was said is escaped


def test_replay_scripts_carry_speech(tmp_path):
    """XrClient.replay plays recorded lines into the mic at their frames."""
    from visor.xr.client import XrClient

    calls = []

    class Mic:
        def play(self, path, **kw):
            calls.append((str(path), kw))
            return {}

    xr = XrClient("/nonexistent.sock")
    xr._mic = Mic()
    xr.state = lambda: {"frame": 0}
    xr.schedule = lambda events, relative=True: {"scheduled": len(events)}
    script = json.loads(json.dumps({"start_frame": 352, "frames": 600, "events": [{"frame": 5, "set": {}}],
                                    "voice": [{"frame": 103, "audio": "voice/001.wav", "text": "hi", "volume": 0.35}]}))
    r = xr.replay(script, base_dir=tmp_path)
    assert r["voice"] == 1
    path, kw = calls[0]
    assert path == str(tmp_path / "voice/001.wav") and kw["at_frame"] == 455 and kw["volume"] == 0.35
    assert kw["kind"] == "speech"


def test_monkeys_talk_too():
    from visor.xr.playtest.brains import BrainContext, MonkeyBrain, Observation
    from visor.xr.playtest.personas import get_persona

    m = MonkeyBrain(seed=3)
    m.start(BrainContext(persona=get_persona("first-timer"), goal="", app_name="x", controls="",
                         image_size=(960, 1006), max_steps=400))
    obs = Observation(image=None, text="", step=0, steps_left=400)
    said = [c for _ in range(400) for c in m.next([], obs) if c.name == "say"]
    assert said and all(c.args["text"] for c in said)


def test_tools_include_say():
    from visor.xr.playtest.brains import ACTION_TOOLS, TOOLS

    say = next(t for t in TOOLS if t["name"] == "say")
    assert "say" in ACTION_TOOLS and say["input_schema"]["required"] == ["text"]
