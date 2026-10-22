"""Hearing the app: what its speakers played, as words, sounds and background."""

import math
import wave

import pytest

from visor.xr import hearing
from visor.xr.hearing import Heard, Line, Scene, Sound
from visor.xr.mic import EAR_BLOCK, EAR_RATE, _db, _Downsampler

BLOCK = EAR_BLOCK / EAR_RATE  # 50 ms


def levels_of(spec):
    """[(seconds, dB)] -> 50 ms loudness readings."""
    out = []
    for seconds, db in spec:
        out += [db] * round(seconds / BLOCK)
    return out


class TestScene:
    def test_silence_says_nothing(self):
        events, change = Scene().feed(levels_of([(3, -120)]), 0.0)
        assert events == [] and change is None

    def test_a_chime_in_silence(self):
        events, change = Scene().feed(levels_of([(1, -120), (0.4, -20), (1, -120)]), 0.0)
        assert [(round(e.start, 2), round(e.duration, 2)) for e in events] == [(1.0, 0.4)] and change is None

    def test_ambience_becomes_the_background_then_events_rise_above_it(self):
        scene = Scene()
        events, change = scene.feed(levels_of([(0.5, -120), (3, -38)]), 0.0)
        assert events == [] and change == "started" and scene.bg == pytest.approx(-38, abs=0.5)
        events, change = scene.feed(levels_of([(1, -38), (0.4, -22), (1, -38)]), 3.5)
        assert [round(e.start, 2) for e in events] == [4.5] and change is None  # the chime, over it
        events, change = scene.feed(levels_of([(0.5, -38), (1.5, -120)]), 6.0)
        assert events == [] and change == "stopped"

    def test_a_long_sentence_isn_t_music(self):
        speech = [(0.0, 4.0)]
        events, change = Scene().feed(levels_of([(4, -25), (1, -120)]), 0.0, speech)
        assert change is None and len(events) == 1 and events[0].duration == pytest.approx(4.0)

    def test_ongoing_sound_that_began_long_ago_is_background(self):
        events, change = Scene().feed(levels_of([(1, -40)]), 10.0, ongoing_since=5.0)
        assert events == [] and change == "started"


class TestDescribe:
    def heard(self, **kw):
        return Heard(start=0.0, end=5.0, **({"sounds": []} | kw))

    def test_nothing_new(self):
        assert self.heard().describe() == "" and self.heard().silent

    def test_languages_are_named_when_they_change(self):
        h = self.heard(lines=[Line(0, 1, "Welcome aboard.", "en"), Line(1, 2, "Bienvenido.", "es"),
                              Line(2, 3, "Now you.", "en")], transcribed=True)
        assert h.describe() == ('You heard the app say: "Welcome aboard.", then in Spanish: "Bienvenido.", '
                                'then in English: "Now you."')

    def test_other_sounds_and_the_background(self):
        h = self.heard(sounds=[Sound(1, 1.3, -20), Sound(2, 4, -18)], background_change="started", transcribed=True)
        assert h.describe() == ("You heard 2 sounds (0.3s, 2.0s). "
                                "Music or ambience is playing in the background.")
        assert self.heard(background_change="stopped").describe() == "The background sound stopped."

    def test_without_speech_recognition_it_says_words_were_missed(self):
        h = self.heard(sounds=[Sound(1, 3, -20)])
        assert "without speech recognition" in h.describe()

    def test_clipping_is_reported(self):
        h = self.heard(sounds=[Sound(1, 3, -1)], clipped=0.02, transcribed=True)
        assert "2.0% of it clipped" in h.describe()

    def test_wav(self, tmp_path):
        from array import array

        h = self.heard(samples=array("f", [0.5] * 1600))
        with wave.open(str(h.save(tmp_path / "a.wav"))) as w:
            assert (w.getframerate(), w.getnchannels(), w.getnframes()) == (EAR_RATE, 1, 1600)


class FakeSpeakers:
    """The audio helper's speaker side, serving a scripted soundscape on the monotonic clock."""

    def __init__(self, samples, origin):
        self.samples, self.origin = samples, origin
        self.sound_since = None

    def speakers(self):
        return {"playing": self.sound_since is not None, "sound_since": self.sound_since}

    def heard(self, t_from, t_to=None, pcm=True):
        a = max(0, round((t_from - self.origin) * EAR_RATE))
        b = min(len(self.samples), round(((t_to or 1e18) - self.origin) * EAR_RATE))
        b = max(a, b)
        seg = self.samples[a:b]
        out = {"from": self.origin + a / EAR_RATE, "to": self.origin + b / EAR_RATE,
               "levels": [_db(seg[i:i + EAR_BLOCK]) for i in range(0, len(seg), EAR_BLOCK)]}
        if pcm:
            out["samples"] = seg
        return out


class TestEars:
    def soundscape(self, spec):
        from array import array

        out = array("f")
        for seconds, amp in spec:
            n = round(seconds * EAR_RATE)
            out.extend(array("f", (amp * math.sin(2 * math.pi * 440 * i / EAR_RATE) for i in range(n))))
        return out

    def test_picks_up_where_it_left_off(self):
        samples = self.soundscape([(1, 0.0), (0.4, 0.3), (1, 0.0), (0.4, 0.3), (1, 0.0)])
        mic = FakeSpeakers(samples, origin=1000.0)
        ears = hearing.Ears(mic, since=1000.0, transcribe=False, clock=lambda: 1003.8)
        first = ears.hear()
        assert len(first.sounds) == 2 and first.end == pytest.approx(1003.75, abs=0.01)
        assert ears.hear().silent  # nothing new

    def test_a_fresh_sound_waits_to_be_known(self):
        samples = self.soundscape([(1, 0.0), (1, 0.05)])  # a sound that began 1 s ago and is still going
        mic = FakeSpeakers(samples, origin=1000.0)
        mic.sound_since = 1001.0
        ears = hearing.Ears(mic, since=1000.0, transcribe=False, clock=lambda: 1002.05)
        assert ears.hear().silent and ears.cursor == pytest.approx(1000.95, abs=0.01)  # held back
        assert len(ears.hear(settle=False).sounds) == 1  # unless told not to

    def test_clipping_measured(self):
        from array import array

        samples = array("f", [0.0] * 16000) + array("f", [1.0, -1.0] * 4000)
        ears = hearing.Ears(FakeSpeakers(samples, origin=1000.0), since=1000.0, transcribe=False,
                            clock=lambda: 1001.6)
        assert ears.hear().clipped > 0.3


@pytest.mark.skipif(not (hearing.available() and hearing.installed()),
                    reason="needs faster-whisper and its base model (no downloads in tests)")
def test_real_words():
    from array import array

    from visor.xr import voice as tts

    if not (tts.available() and tts.DEFAULT_VOICE in tts.installed()):
        pytest.skip("needs Piper and its default voice")
    s = tts.synthesize("Welcome aboard. Say anything to test your microphone.")
    with wave.open(str(s.path)) as w:
        rate, raw = w.getframerate(), w.readframes(w.getnframes())
    pcm = array("h")
    pcm.frombytes(raw)
    d = _Downsampler(rate)
    speech = d.feed(array("f", (v / 32768 for v in pcm)))
    samples = array("f", [0.0] * 8000) + speech + array("f", [0.0] * 16000)
    now = 1000.0 + len(samples) / EAR_RATE
    heard = hearing.Ears(FakeSpeakers(samples, origin=1000.0), since=1000.0, clock=lambda: now).hear()
    assert heard.transcribed and "microphone" in heard.describe().lower()
    assert heard.lines and heard.lines[0].language == "en" and heard.sounds[0].speech
