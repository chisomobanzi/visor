"""Speech for the virtual headset: choosing voices and saying things."""

import json
import math
import struct
import wave

import pytest

from visor.xr import voice as tts


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
