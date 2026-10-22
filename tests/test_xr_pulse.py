"""The PulseAudio-protocol microphone server, spoken to the way libpulse does.

A small client here speaks the same wire format (it's what the server was
written against), so these run without libpulse; one test uses the real
libpulse-simple when it's installed, since that is what apps actually link.
"""

import ctypes
import ctypes.util
import math
import os
import shutil
import socket
import struct
import sys
import tempfile
import threading
import time

import pytest

from visor.xr import pulse
from visor.xr.pulse import DESCRIPTOR, INVALID_INDEX, Out, PulseServer, Tags, pcm_bytes

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Unix sockets")


class Tone:
    """A 440 Hz source at half scale, timed like Visor's mic (by absolute time)."""

    def tick(self, now):
        pass

    def render(self, t0, rate, n):
        return [0.5 * math.sin(2 * math.pi * 440 * (t0 + i / rate)) for i in range(n)]


@pytest.fixture
def server():
    d = tempfile.mkdtemp(prefix="vpa")  # not tmp_path: socket paths max out at 108 bytes
    srv = PulseServer(os.path.join(d, "p.sock"), Tone(), mic_wake=0).start()
    yield srv
    srv.stop()
    shutil.rmtree(d, ignore_errors=True)


class Client:
    def __init__(self, path):
        self.s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.s.settimeout(5)
        self.s.connect(path)
        self.tag = 0
        self.audio = bytearray()
        self.packets: list[int] = []  # sizes of the audio packets received
        self.requests: list[tuple[int, int]] = []  # (stream, bytes) the speakers asked for

    def _read(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.s.recv(n - len(buf))
            assert chunk, "server closed the connection"
            buf += chunk
        return buf

    def _packet(self):
        """One packet: audio and requests are collected; anything else is returned."""
        length, channel, _, _, _ = DESCRIPTOR.unpack(self._read(DESCRIPTOR.size))
        data = self._read(length)
        if channel != INVALID_INDEX:
            self.audio += data
            self.packets.append(len(data))
            return None
        t = Tags(data)
        kind, tag = t.u32(), t.u32()
        if kind == pulse.REQUEST:
            assert tag == INVALID_INDEX
            self.requests.append((t.u32(), t.u32()))
            return None
        return kind, tag, t

    def command(self, cmd, body=b""):
        self.tag += 1
        payload = bytes(Out().u32(cmd).u32(self.tag).buf) + bytes(body)
        self.s.sendall(DESCRIPTOR.pack(len(payload), INVALID_INDEX, 0, 0, 0) + payload)
        while True:  # audio and requests may arrive before the reply
            got = self._packet()
            if got is not None:
                kind, tag, t = got
                assert tag == self.tag
                return kind, t

    def audio_for(self, seconds):
        self.s.settimeout(0.05)
        deadline = time.time() + seconds
        while time.time() < deadline:
            try:
                self._packet()
            except socket.timeout:
                pass
        self.s.settimeout(5)

    def write(self, stream, nbytes):
        """Play ``nbytes`` of silence into a playback stream."""
        self.s.sendall(DESCRIPTOR.pack(nbytes, stream, 0, 0, 0) + bytes(nbytes))

    def requested(self, stream):
        return sum(n for s, n in self.requests if s == stream)

    def hello(self, version=35):
        kind, t = self.command(pulse.AUTH, bytes(Out().u32(version | 0x80000000).buf) + b"x" + struct.pack(">I", 256)
                               + bytes(256))
        assert kind == pulse.REPLY
        server_version = t.u32()
        kind, t = self.command(pulse.SET_CLIENT_NAME, bytes(Out().proplist({"application.name": "test-app"}).buf))
        assert kind == pulse.REPLY
        return server_version, t.u32()

    def record(self, fmt, channels, rate, corked=False):
        body = (Out().sample_spec(fmt, channels, rate).channel_map(list(range(1, channels + 1)) if channels > 1 else [0])
                .u32(INVALID_INDEX).string(None).u32(INVALID_INDEX).boolean(corked).u32(INVALID_INDEX))
        for _ in range(7):
            body.boolean(False)
        body.boolean(False).boolean(True).proplist({"media.name": "capture"}).u32(INVALID_INDEX)
        body.boolean(False)  # early requests (v14)
        body.boolean(False).boolean(False)  # v15
        body.u8(0)  # no formats (v22)
        return self.command(pulse.CREATE_RECORD_STREAM, bytes(body.buf))

    def play(self, fmt, channels, rate, corked=False, formats=()):
        """CREATE_PLAYBACK_STREAM as libpulse 16 sends it."""
        body = (Out().sample_spec(fmt, channels, rate).channel_map(list(range(1, channels + 1)) if channels > 1 else [0])
                .u32(INVALID_INDEX).string(None).u32(INVALID_INDEX).boolean(corked)
                .u32(INVALID_INDEX).u32(INVALID_INDEX).u32(INVALID_INDEX).u32(0).cvolume([0x10000] * max(channels, 1)))
        for _ in range(7):
            body.boolean(False)
        body.boolean(False).boolean(False).proplist({"media.name": "music"})  # v13
        body.boolean(False).boolean(False)  # v14
        body.boolean(False).boolean(False).boolean(False)  # v15
        body.boolean(False)  # v17
        body.boolean(False)  # v18
        body.u8(len(formats))  # v21
        for encoding, props in formats:
            body.format_info(encoding, props)
        return self.command(pulse.CREATE_PLAYBACK_STREAM, bytes(body.buf))


def test_tagstruct_round_trip():
    out = (Out().u32(7).string("hi").string(None).boolean(True).usec(1234).sample_spec(5, 2, 48000)
           .channel_map([1, 2]).cvolume([65536]).proplist({"a": "b"}).format_info(1, {}))
    t = Tags(bytes(out.buf))
    assert t.u32() == 7 and t.string() == "hi" and t.string() is None and t.boolean() is True
    assert t.usec() == 1234 and t.sample_spec() == (5, 2, 48000) and t.channel_map() == [1, 2]
    assert t.cvolume() == [65536] and t.proplist() == {"a": b"b\0"} and t.format_info() == (1, {})
    assert t.eof()


def test_pcm_formats():
    assert struct.unpack("<2h", pcm_bytes([0.5, -1.0], 3, 1)) == (16384, -32767)
    assert struct.unpack("<2f", pcm_bytes([0.25], 5, 2)) == (0.25, 0.25)  # mono to stereo
    assert struct.unpack(">h", pcm_bytes([1.5], 4, 1)) == (32767,)  # clipped, big-endian
    assert pcm_bytes([0.0], 0, 1) == bytes([128])  # u8 silence
    assert pcm_bytes([1.0], 9, 1) == (8388607).to_bytes(3, "little", signed=True)


def test_handshake_and_the_one_microphone(server):
    c = Client(server.path)
    version, index = c.hello()
    assert version == 35 and index >= 1
    kind, t = c.command(pulse.GET_SERVER_INFO)
    assert kind == pulse.REPLY
    assert [t.string() for _ in range(4)][0] == "pulseaudio"
    t.sample_spec()
    assert t.string() == "visor_speakers" and t.string() == "visor_mic"
    kind, t = c.command(pulse.GET_SOURCE_INFO_LIST)
    assert kind == pulse.REPLY and t.u32() == 0 and t.string() == "visor_mic" and t.string() == "Visor microphone"


def test_there_are_always_speakers(server):
    """Real servers always have a sink; SDL 2 crashes on one without a default."""
    c = Client(server.path)
    c.hello()
    kind, t = c.command(pulse.GET_SERVER_INFO)
    [t.string() for _ in range(4)]
    t.sample_spec()
    assert t.string() == "visor_speakers" and t.string() == "visor_mic"
    kind, t = c.command(pulse.GET_SINK_INFO_LIST)
    assert kind == pulse.REPLY and t.u32() == 0 and t.string() == "visor_speakers"
    kind, t = c.command(pulse.LOOKUP_SINK, bytes(Out().string("@DEFAULT_SINK@").buf))
    assert kind == pulse.REPLY and t.u32() == 0


def test_speakers_play_at_real_time_pace(server):
    c = Client(server.path)
    c.hello()
    kind, t = c.play(3, 2, 44100)  # s16le stereo
    assert kind == pulse.REPLY
    index, _, first = t.u32(), t.u32(), t.u32()
    maxlength, tlength, prebuf, minreq = t.u32(), t.u32(), t.u32(), t.u32()
    second = 44100 * 4
    assert first == tlength == second // 4 and minreq <= prebuf <= tlength <= maxlength  # 250 ms to fill
    assert t.sample_spec() == (3, 2, 44100)
    c.write(index, first)
    sent, start = first, time.monotonic()
    while time.monotonic() - start < 0.6:  # write what's asked for, as libpulse does
        c.audio_for(0.03)
        more = c.requested(index) - (sent - first)
        if more > 0:
            c.write(index, more)
            sent += more
    played = c.requested(index) / second
    assert 0.5 < played < 0.75  # 0.6 s, plus the 40 ms the "sound card" holds
    t0 = time.monotonic()
    kind, _ = c.command(pulse.DRAIN_PLAYBACK_STREAM, bytes(Out().u32(index).buf))
    assert kind == pulse.REPLY and time.monotonic() - t0 < 0.5  # once what was left has played
    kind, t = c.command(pulse.GET_PLAYBACK_LATENCY, bytes(Out().u32(index).timeval(5, 6).buf))
    assert kind == pulse.REPLY
    t.usec(), t.usec(), t.boolean()
    assert t.timeval() == (5, 6)
    t.timeval()
    assert t.s64() == sent and t.s64() == sent  # everything written has been played
    t.u64(), t.u64()
    assert t.eof()


def test_corked_speakers_wait_and_flush_frees_the_buffer(server):
    c = Client(server.path)
    c.hello()
    _, t = c.play(5, 1, 48000, corked=True)
    index, _, first = t.u32(), t.u32(), t.u32()
    c.write(index, first)
    c.audio_for(0.2)
    assert c.requested(index) == 0  # corked: nothing plays, nothing more is wanted
    kind, _ = c.command(pulse.FLUSH_PLAYBACK_STREAM, bytes(Out().u32(index).buf))
    assert kind == pulse.REPLY
    c.audio_for(0.1)
    assert c.requested(index) == first  # what was dropped can be written again


def test_extended_api_streams_get_a_pcm_format(server):
    c = Client(server.path)
    c.hello()
    props = {"format.sample_format": '"s16le"', "format.rate": "22050", "format.channels": "1"}
    kind, t = c.play(0xFF, 0, 0, formats=[(1, props)])  # sample spec left unset, as pa_stream_new_extended does
    assert kind == pulse.REPLY
    [t.u32() for _ in range(7)]
    assert t.sample_spec() == (3, 1, 22050)
    kind, t = c.play(0xFF, 0, 0, formats=[(2, {})])  # AC3 passthrough only: not these speakers
    assert kind == pulse.ERROR and t.u32() == pulse.ERR_NOTSUPPORTED


def test_negotiate():
    from visor.xr.pulse import negotiate

    assert negotiate(5, 2, 48000, [1, 2], []) == (5, 2, 48000, [1, 2])
    assert negotiate(255, 0, 0, [], [(1, {"format.sample_format": b'"float32le"\0'})]) == (5, 2, 48000, [1, 2])
    assert negotiate(255, 0, 0, [], []) is None


def test_recording_delivers_real_time_audio(server):
    c = Client(server.path)
    c.hello()
    kind, t = c.record(5, 1, 48000)
    assert kind == pulse.REPLY
    index = t.u32()
    assert t.u32() == index
    t.u32()
    fragsize = t.u32()
    assert t.sample_spec() == (5, 1, 48000) and fragsize == 960 * 4  # 20 ms by default
    assert len(server.listening()) == 1
    c.audio_for(0.5)
    assert set(c.packets) == {fragsize}  # whole fragments, as PulseAudio sends them
    n = len(c.audio) // 4
    samples = struct.unpack(f"<{n}f", bytes(c.audio[: n * 4]))
    assert 0.35 * 48000 < n < 0.7 * 48000  # about real time
    rms = math.sqrt(sum(v * v for v in samples) / n)
    assert abs(rms - 0.5 / math.sqrt(2)) < 0.02
    kind, _ = c.command(pulse.DELETE_RECORD_STREAM, bytes(Out().u32(index).buf))
    assert kind == pulse.REPLY and server.listening() == []


def test_an_idle_mic_takes_a_moment_to_wake():
    d = tempfile.mkdtemp(prefix="vpa")
    srv = PulseServer(os.path.join(d, "p.sock"), Tone(), mic_wake=0.3).start()
    try:
        c = Client(srv.path)
        c.hello()
        c.record(5, 1, 48000)
        c.audio_for(0.2)
        assert not c.audio  # still waking up
        c.audio_for(0.3)
        assert c.audio
        c2 = Client(srv.path)  # the mic is running now: a second stream starts within a fragment
        c2.hello()
        c2.record(5, 1, 48000)
        c2.audio_for(0.1)
        assert c2.audio
    finally:
        srv.stop()
        shutil.rmtree(d, ignore_errors=True)


def test_corked_streams_aren_t_listening(server):
    c = Client(server.path)
    c.hello()
    kind, t = c.record(3, 2, 16000, corked=True)
    index = t.u32()
    assert kind == pulse.REPLY and server.listening() == []
    kind, _ = c.command(pulse.CORK_RECORD_STREAM, bytes(Out().u32(index).boolean(False).buf))
    assert kind == pulse.REPLY and len(server.listening()) == 1


def test_a_client_that_goes_away_takes_its_streams(server):
    c = Client(server.path)
    c.hello()
    c.record(5, 1, 48000)
    c.s.close()
    for _ in range(50):
        if not server.streams:
            break
        time.sleep(0.02)
    assert not server.streams


SDL_SCRIPT = r"""
import ctypes, math, sys, time
sdl = ctypes.CDLL(sys.argv[1])
class Spec(ctypes.Structure):
    _fields_ = [("freq", ctypes.c_int), ("format", ctypes.c_uint16), ("channels", ctypes.c_uint8),
                ("silence", ctypes.c_uint8), ("samples", ctypes.c_uint16), ("padding", ctypes.c_uint16),
                ("size", ctypes.c_uint32), ("callback", ctypes.c_void_p), ("userdata", ctypes.c_void_p)]
sdl.SDL_GetCurrentAudioDriver.restype = ctypes.c_char_p
assert sdl.SDL_Init(0x10) == 0
print(sdl.SDL_GetCurrentAudioDriver().decode())
cap = sdl.SDL_OpenAudioDevice(None, 1, ctypes.byref(Spec(16000, 0x8010, 1, 0, 512, 0, 0, None, None)), None, 0)
out = sdl.SDL_OpenAudioDevice(None, 0, ctypes.byref(Spec(48000, 0x8010, 2, 0, 1024, 0, 0, None, None)), None, 0)
assert cap and out
second = (ctypes.c_int16 * (48000 * 2))()
sdl.SDL_QueueAudio(out, second, ctypes.sizeof(second))
sdl.SDL_PauseAudioDevice(cap, 0)
sdl.SDL_PauseAudioDevice(out, 0)
time.sleep(1.0)
buf = (ctypes.c_int16 * 32000)()
n = sdl.SDL_DequeueAudio(cap, buf, ctypes.sizeof(buf)) // 2
print(n, math.sqrt(sum(buf[i] ** 2 for i in range(n)) / max(n, 1)) / 32768)
print(sdl.SDL_GetQueuedAudioSize(out))
sdl.SDL_CloseAudioDevice(out)
sdl.SDL_CloseAudioDevice(cap)
sdl.SDL_Quit()
"""


@pytest.mark.skipif(sys.platform != "linux" or not ctypes.util.find_library("SDL2-2.0")
                    or not ctypes.util.find_library("pulse"), reason="needs SDL 2 and libpulse")
def test_sdl2_records_and_plays():
    """SDL 2.30 segfaults on a server with no default sink, and its capture
    hangs if audio is queued before its first flush."""
    import subprocess

    d = tempfile.mkdtemp(prefix="vpa")
    srv = PulseServer(os.path.join(d, "p.sock"), Tone()).start()  # the real wake-up delay
    try:
        env = dict(os.environ, PULSE_SERVER=f"unix:{srv.path}", PIPEWIRE_REMOTE="/nonexistent",
                   SDL_AUDIODRIVER="pulseaudio")
        r = subprocess.run([sys.executable, "-c", SDL_SCRIPT, ctypes.util.find_library("SDL2-2.0")], env=env,
                           capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, r.stderr
        driver, captured, queued = r.stdout.split("\n")[:3]
        n, level = captured.split()
        assert driver == "pulseaudio"
        assert int(n) > 8000 and abs(float(level) - 0.5 / math.sqrt(2)) < 0.03  # it heard the tone
        assert int(queued) < 48000 * 4 // 2  # and played what it was given, in real time
    finally:
        srv.stop()
        shutil.rmtree(d, ignore_errors=True)


def _libpulse_simple():
    name = ctypes.util.find_library("pulse-simple")
    return ctypes.CDLL(name) if name else None


@pytest.mark.skipif(sys.platform != "linux" or _libpulse_simple() is None, reason="needs libpulse-simple")
def test_real_libpulse_records_from_it(server, monkeypatch):
    """What Speakeasy3D does: pa_simple, float32 48 kHz, 20 ms reads."""
    monkeypatch.setenv("PULSE_SERVER", f"unix:{server.path}")
    lib = _libpulse_simple()

    class Spec(ctypes.Structure):
        _fields_ = [("format", ctypes.c_int), ("rate", ctypes.c_uint32), ("channels", ctypes.c_uint8)]

    lib.pa_simple_new.restype = ctypes.c_void_p
    lib.pa_simple_new.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_char_p,
                                  ctypes.POINTER(Spec), ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
    lib.pa_simple_read.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_int)]
    lib.pa_simple_free.argtypes = [ctypes.c_void_p]
    err = ctypes.c_int(0)
    spec = Spec(5, 48000, 1)
    result: dict = {}

    def run():
        s = lib.pa_simple_new(None, b"test", 2, None, b"capture", ctypes.byref(spec), None, None, ctypes.byref(err))
        if not s:
            result["error"] = err.value
            return
        buf = (ctypes.c_float * 960)()
        total = []
        for _ in range(10):
            assert lib.pa_simple_read(s, buf, ctypes.sizeof(buf), ctypes.byref(err)) == 0
            total.extend(buf)
        lib.pa_simple_free(s)
        result["samples"] = total

    t = threading.Thread(target=run)
    t.start()
    t.join(timeout=10)
    assert "error" not in result, f"pa_simple_new failed: {result.get('error')}"
    rms = math.sqrt(sum(v * v for v in result["samples"]) / len(result["samples"]))
    assert abs(rms - 0.5 / math.sqrt(2)) < 0.03
