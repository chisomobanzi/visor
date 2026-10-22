"""Speech for the virtual headset's microphone: text in, a voice out.

Uses Piper, offline neural text-to-speech (``pip install 'visor-dev[voice]'``).
Each voice is a ~60 MB model, downloaded once on first use into
``~/.local/share/visor/xr/voices`` from the Piper project's v1.0.0 release.
Synthesised lines are cached by voice, speed and text, so an agent repeating
itself, or a replay, costs nothing.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import threading
import urllib.request
import wave
from dataclasses import dataclass
from importlib.util import find_spec
from pathlib import Path
from typing import Callable, Optional

from visor.xr import paths

VOICES_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0"
DEFAULT_VOICE = "en_US-lessac-medium"

# A good default voice per language (ISO 639-1), all in Piper's v1.0.0 set.
LANGUAGE_VOICES = {
    "en": "en_US-lessac-medium",
    "es": "es_ES-davefx-medium",
    "fr": "fr_FR-siwis-medium",
    "de": "de_DE-thorsten-medium",
    "it": "it_IT-paola-medium",
    "pt": "pt_BR-faber-medium",
    "nl": "nl_NL-pim-medium",
    "pl": "pl_PL-gosia-medium",
    "ru": "ru_RU-irina-medium",
    "uk": "uk_UA-ukrainian_tts-medium",
    "tr": "tr_TR-fettah-medium",
    "sv": "sv_SE-nst-medium",
    "ar": "ar_JO-kareem-medium",
    "hi": "hi_IN-priyamvada-medium",
    "sw": "sw_CD-lanfrica-medium",
    "vi": "vi_VN-vais1000-medium",
    "zh": "zh_CN-huayan-medium",
}

Log = Callable[[str], None]


class VoiceError(RuntimeError):
    pass


@dataclass
class Speech:
    text: str
    voice: str
    path: Path  # a 16-bit mono WAV
    duration: float


def available() -> bool:
    """Is a text-to-speech engine installed?"""
    return find_spec("piper") is not None


def voices_dir() -> Path:
    return paths.xr_home() / "voices"


def installed() -> list[str]:
    d = voices_dir()
    return sorted(p.name[: -len(".onnx")] for p in d.glob("*.onnx")) if d.is_dir() else []


def resolve(voice: Optional[str] = None, language: Optional[str] = None) -> str:
    """The voice to use: an explicit Piper voice name, else the default for a
    language ("es", "pt-BR", "zh_CN"), else English."""
    if voice:
        _split(voice)
        return voice
    if language:
        code = language.replace("-", "_")
        family = code.split("_")[0].lower()
        if family not in LANGUAGE_VOICES:
            have = ", ".join(sorted(LANGUAGE_VOICES))
            raise VoiceError(f"no built-in voice for '{language}' (have: {have}); name a Piper voice instead")
        if "_" in code:  # a regional voice, if we can find one already installed
            for name in installed():
                if name.lower().startswith(code.lower() + "-"):
                    return name
        return LANGUAGE_VOICES[family]
    return DEFAULT_VOICE


def _split(name: str) -> tuple[str, str, str, str]:
    """en_US-lessac-medium -> (en, en_US, lessac, medium)"""
    try:
        code, rest = name.split("-", 1)
        speaker, quality = rest.rsplit("-", 1)
    except ValueError:
        raise VoiceError(f"'{name}' isn't a Piper voice name (like {DEFAULT_VOICE})") from None
    if "_" not in code or not speaker or "/" in name or ".." in name:
        raise VoiceError(f"'{name}' isn't a Piper voice name (like {DEFAULT_VOICE})")
    return code.split("_")[0], code, speaker, quality


def voice_urls(name: str) -> tuple[str, str]:
    family, code, speaker, quality = _split(name)
    base = f"{VOICES_URL}/{family}/{code}/{speaker}/{quality}/{name}"
    return base + ".onnx", base + ".onnx.json"


def ensure(name: str, log: Log = lambda m: None) -> Path:
    """The voice's model on disk, downloading it the first time."""
    model = voices_dir() / f"{name}.onnx"
    config = voices_dir() / f"{name}.onnx.json"
    if model.exists() and config.exists():
        return model
    model_url, config_url = voice_urls(name)
    voices_dir().mkdir(parents=True, exist_ok=True)
    log(f"downloading the {name} voice (~60 MB, once)")
    for url, dest in ((config_url, config), (model_url, model)):
        part = dest.with_name(dest.name + ".part")
        try:
            with urllib.request.urlopen(url, timeout=120) as r, open(part, "wb") as f:
                shutil.copyfileobj(r, f)
        except OSError as e:
            part.unlink(missing_ok=True)
            raise VoiceError(f"couldn't download the {name} voice: {e}") from None
        part.replace(dest)
    try:
        json.loads(config.read_text(encoding="utf-8"))
    except ValueError:
        model.unlink(missing_ok=True)
        config.unlink(missing_ok=True)
        raise VoiceError(f"the {name} voice downloaded incomplete; try again") from None
    return model


_voices: dict = {}
_voices_lock = threading.Lock()


def _load(name: str, log: Log):
    with _voices_lock:
        if name not in _voices:
            from piper import PiperVoice

            _voices[name] = (PiperVoice.load(ensure(name, log)), threading.Lock())
        return _voices[name]


def synthesize(text: str, voice: Optional[str] = None, language: Optional[str] = None, speed: float = 1.0,
               log: Log = lambda m: None) -> Speech:
    """Speak ``text``; returns a WAV of it (cached)."""
    text = " ".join(text.split())
    if not text:
        raise VoiceError("nothing to say")
    if len(text) > 2000:
        raise VoiceError("that's a speech, not a line — keep it under 2000 characters")
    name = resolve(voice, language)
    speed = max(0.5, min(2.0, float(speed)))
    key = hashlib.sha256(f"{name}\n{speed:.2f}\n{text}".encode()).hexdigest()[:20]
    out = paths.cache_home() / "speech" / f"{key}.wav"
    if not out.exists():
        if not available():
            raise VoiceError("speaking needs a text-to-speech engine: pip install 'visor-dev[voice]' "
                             "(or play a recording with --file)")
        from piper.config import SynthesisConfig

        engine, lock = _load(name, log)
        out.parent.mkdir(parents=True, exist_ok=True)
        part = out.with_name(out.name + ".part")
        with lock, wave.open(str(part), "wb") as w:
            engine.synthesize_wav(text, w, syn_config=SynthesisConfig(length_scale=1.0 / speed))
        part.replace(out)
    with wave.open(str(out), "rb") as w:
        duration = w.getnframes() / float(w.getframerate())
    return Speech(text=text, voice=name, path=out, duration=duration)
