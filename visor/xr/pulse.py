"""A microphone any Linux app can open: just enough of PulseAudio's protocol.

Every app in the virtual headset gets its own audio server: a Unix socket that
speaks PulseAudio's native wire protocol, so pointing ``PULSE_SERVER`` at it is
all an app needs (libpulse, ``pa_simple``, SDL's pulse backend and the ALSA
pulse plugin all honour it). It offers one source, "Visor microphone", whose
sound Visor decides, and one sink, "Visor speakers", which takes what the app
plays at real-time pace and lets it go. Nothing here touches the computer's
real audio system: the app can't hear the room, and the room can't hear it.

The speakers matter even though nobody listens: engines open output and input
through one driver (SDL, FMOD, Godot), and real servers always have a sink, so
an app shown only a microphone may never get as far as recording — SDL 2
crashes on a server without a default sink.

Only what recording and plain playback need is implemented — connect, look up
devices, open, cork, flush, drain, time and close streams. Everything else gets
a polite "not supported", which apps already handle for devices that can't do
something. Audio goes inline over the socket (no shared memory is negotiated).

Written against PulseAudio 16.1's src/pulsecore/{protocol-native,tagstruct,
pstream}.c and src/pulse/{context,stream,introspect}.c. Each side formats its
messages for the other's protocol version, so everything here branches on
``min(ours, theirs)``.
"""

from __future__ import annotations

import os
import socket
import struct
import sys
import threading
import time
from array import array
from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol

PROTOCOL_VERSION = 35
VERSION_MASK = 0x0000FFFF
INVALID_INDEX = 0xFFFFFFFF
VOLUME_NORM = 0x10000
FRAME_SIZE_MAX = 16 * 1024 * 1024
DESCRIPTOR = struct.Struct(">5I")  # length, channel, offset hi, offset lo, flags

# Commands (enum in pulsecore/native-common.h).
ERROR, REPLY = 0, 2
CREATE_PLAYBACK_STREAM, DELETE_PLAYBACK_STREAM = 3, 4
CREATE_RECORD_STREAM, DELETE_RECORD_STREAM = 5, 6
EXIT, AUTH, SET_CLIENT_NAME, LOOKUP_SINK, LOOKUP_SOURCE = 7, 8, 9, 10, 11
DRAIN_PLAYBACK_STREAM, STAT, GET_PLAYBACK_LATENCY = 12, 13, 14
GET_SERVER_INFO, GET_SINK_INFO, GET_SINK_INFO_LIST, GET_SOURCE_INFO, GET_SOURCE_INFO_LIST = 20, 21, 22, 23, 24
GET_MODULE_INFO, GET_MODULE_INFO_LIST, GET_CLIENT_INFO, GET_CLIENT_INFO_LIST = 25, 26, 27, 28
GET_SINK_INPUT_INFO, GET_SINK_INPUT_INFO_LIST, GET_SOURCE_OUTPUT_INFO, GET_SOURCE_OUTPUT_INFO_LIST = 29, 30, 31, 32
GET_SAMPLE_INFO, GET_SAMPLE_INFO_LIST, SUBSCRIBE = 33, 34, 35
SET_SINK_VOLUME, SET_SINK_INPUT_VOLUME, SET_SOURCE_VOLUME, SET_SINK_MUTE, SET_SOURCE_MUTE = 36, 37, 38, 39, 40
CORK_PLAYBACK_STREAM, FLUSH_PLAYBACK_STREAM, TRIGGER_PLAYBACK_STREAM = 41, 42, 43
SET_DEFAULT_SINK, SET_DEFAULT_SOURCE, SET_PLAYBACK_STREAM_NAME, SET_RECORD_STREAM_NAME = 44, 45, 46, 47
GET_RECORD_LATENCY, CORK_RECORD_STREAM, FLUSH_RECORD_STREAM, PREBUF_PLAYBACK_STREAM, REQUEST = 57, 58, 59, 60, 61
SET_SINK_INPUT_MUTE, SUSPEND_SINK, SUSPEND_SOURCE = 69, 70, 71
SET_PLAYBACK_STREAM_BUFFER_ATTR, SET_RECORD_STREAM_BUFFER_ATTR = 72, 73
UPDATE_PLAYBACK_STREAM_SAMPLE_RATE, UPDATE_RECORD_STREAM_SAMPLE_RATE = 74, 75
UPDATE_RECORD_STREAM_PROPLIST, UPDATE_PLAYBACK_STREAM_PROPLIST, UPDATE_CLIENT_PROPLIST = 80, 81, 82
REMOVE_RECORD_STREAM_PROPLIST, REMOVE_PLAYBACK_STREAM_PROPLIST, REMOVE_CLIENT_PROPLIST = 83, 84, 85
GET_CARD_INFO, GET_CARD_INFO_LIST = 88, 89
SET_SOURCE_OUTPUT_VOLUME, SET_SOURCE_OUTPUT_MUTE = 98, 99

# Errors (enum pa_error_code in pulse/def.h).
ERR_INVALID, ERR_NOENTITY, ERR_PROTOCOL, ERR_NOTSUPPORTED = 3, 5, 7, 19

# Sample formats (pulse/sample.h) -> (name, bytes per sample). The microphone
# can produce these; the speakers also take a-law and mu-law.
FORMATS = {
    0: ("u8", 1), 3: ("s16le", 2), 4: ("s16be", 2), 5: ("float32le", 4), 6: ("float32be", 4),
    7: ("s32le", 4), 8: ("s32be", 4), 9: ("s24le", 3), 10: ("s24be", 3), 11: ("s24-32le", 4), 12: ("s24-32be", 4),
}
SAMPLE_BYTES = {**{k: v[1] for k, v in FORMATS.items()}, 1: 1, 2: 1}
FORMAT_BY_NAME = {**{v[0]: k for k, v in FORMATS.items()}, "aLaw": 1, "uLaw": 2}
FLOAT32LE = 5
ENCODING_ANY, ENCODING_PCM = 0, 1
CHANNEL_MONO, CHANNEL_LEFT, CHANNEL_RIGHT = 0, 1, 2
STATE_RUNNING, STATE_IDLE = 0, 1

SOURCE_INDEX = 0
SOURCE_NAME = "visor_mic"
SOURCE_DESCRIPTION = "Visor microphone"
SOURCE_RATE = 48000
SINK_INDEX = 0
SINK_NAME = "visor_speakers"
SINK_DESCRIPTION = "Visor speakers"
SINK_BUFFER = 0.04  # seconds of audio the "hardware" holds ahead of real time, as a sound card does
# An idle microphone is suspended, and real servers take a moment to wake it:
# the first audio for a stream arrives this long after it opens (less a
# fragment). SDL 2 depends on it — its capture flush loops forever if
# audio is already queued when it first runs.
MIC_WAKE = 0.25


class TagError(ValueError):
    pass


# --------------------------------------------------------------------------
# Tagstructs: PulseAudio's typed, big-endian message encoding


class Tags:
    """Reads one tagstruct."""

    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def eof(self) -> bool:
        return self.pos >= len(self.data)

    def _take(self, n: int) -> bytes:
        if self.pos + n > len(self.data):
            raise TagError("tagstruct ends early")
        b = self.data[self.pos:self.pos + n]
        self.pos += n
        return b

    def _tag(self, *expected: bytes) -> bytes:
        t = self._take(1)
        if t not in expected:
            raise TagError(f"expected tag {expected}, got {t!r}")
        return t

    def u32(self) -> int:
        self._tag(b"L")
        return struct.unpack(">I", self._take(4))[0]

    def u8(self) -> int:
        self._tag(b"B")
        return self._take(1)[0]

    def u64(self) -> int:
        self._tag(b"R")
        return struct.unpack(">Q", self._take(8))[0]

    def s64(self) -> int:
        self._tag(b"r")
        return struct.unpack(">q", self._take(8))[0]

    def usec(self) -> int:
        self._tag(b"U")
        return struct.unpack(">Q", self._take(8))[0]

    def boolean(self) -> bool:
        return self._tag(b"1", b"0") == b"1"

    def string(self) -> Optional[str]:
        if self._tag(b"t", b"N") == b"N":
            return None
        end = self.data.find(b"\0", self.pos)
        if end < 0:
            raise TagError("unterminated string")
        s = self.data[self.pos:end].decode("utf-8", "replace")
        self.pos = end + 1
        return s

    def arbitrary(self) -> bytes:
        self._tag(b"x")
        n = struct.unpack(">I", self._take(4))[0]
        return self._take(n)

    def sample_spec(self) -> tuple[int, int, int]:
        self._tag(b"a")
        fmt, channels = self._take(2)
        return fmt, channels, struct.unpack(">I", self._take(4))[0]

    def channel_map(self) -> list[int]:
        self._tag(b"m")
        return list(self._take(self._take(1)[0]))

    def cvolume(self) -> list[int]:
        self._tag(b"v")
        n = self._take(1)[0]
        return list(struct.unpack(f">{n}I", self._take(4 * n)))

    def timeval(self) -> tuple[int, int]:
        self._tag(b"T")
        return struct.unpack(">II", self._take(8))

    def proplist(self) -> dict[str, bytes]:
        self._tag(b"P")
        props = {}
        while True:
            key = self.string()
            if key is None:
                return props
            n = self.u32()
            value = self.arbitrary()
            if len(value) != n:
                raise TagError("proplist value length mismatch")
            props[key] = value

    def format_info(self) -> tuple[int, dict]:
        self._tag(b"f")
        return self.u8(), self.proplist()


class Out:
    """Writes one tagstruct."""

    def __init__(self):
        self.buf = bytearray()

    def u32(self, v: int) -> "Out":
        self.buf += b"L" + struct.pack(">I", v & 0xFFFFFFFF)
        return self

    def u8(self, v: int) -> "Out":
        self.buf += b"B" + bytes([v & 0xFF])
        return self

    def s64(self, v: int) -> "Out":
        self.buf += b"r" + struct.pack(">q", v)
        return self

    def u64(self, v: int) -> "Out":
        self.buf += b"R" + struct.pack(">Q", max(0, int(v)))
        return self

    def usec(self, v: int) -> "Out":
        self.buf += b"U" + struct.pack(">Q", max(0, int(v)))
        return self

    def boolean(self, v: bool) -> "Out":
        self.buf += b"1" if v else b"0"
        return self

    def string(self, s: Optional[str]) -> "Out":
        self.buf += b"N" if s is None else b"t" + s.encode() + b"\0"
        return self

    def sample_spec(self, fmt: int, channels: int, rate: int) -> "Out":
        self.buf += b"a" + bytes([fmt, channels]) + struct.pack(">I", rate)
        return self

    def channel_map(self, positions: list[int]) -> "Out":
        self.buf += b"m" + bytes([len(positions), *positions])
        return self

    def cvolume(self, values: list[int]) -> "Out":
        self.buf += b"v" + bytes([len(values)]) + struct.pack(f">{len(values)}I", *values)
        return self

    def volume(self, v: int) -> "Out":
        self.buf += b"V" + struct.pack(">I", v)
        return self

    def timeval(self, sec: int, usec: int) -> "Out":
        self.buf += b"T" + struct.pack(">II", sec & 0xFFFFFFFF, usec)
        return self

    def proplist(self, props: dict[str, str]) -> "Out":
        self.buf += b"P"
        for k, v in props.items():
            data = v.encode() + b"\0"
            self.string(k).u32(len(data))
            self.buf += b"x" + struct.pack(">I", len(data)) + data
        self.buf += b"N"
        return self

    def format_info(self, encoding: int, props: dict[str, str]) -> "Out":
        self.buf += b"f"
        return self.u8(encoding).proplist(props)


def pcm_bytes(samples: list[float], fmt: int, channels: int) -> bytes:
    """Mono float samples in [-1, 1] as interleaved PCM in a PulseAudio format."""
    clipped = [1.0 if s > 1.0 else -1.0 if s < -1.0 else s for s in samples]
    if channels > 1:
        clipped = [s for s in clipped for _ in range(channels)]
    little = fmt in (3, 5, 7, 9, 11)
    swap = little != (sys.byteorder == "little")
    if fmt in (5, 6):
        a = array("f", clipped)
    elif fmt in (3, 4):
        a = array("h", [int(round(s * 32767.0)) for s in clipped])
    elif fmt in (7, 8):
        a = array("i", [int(round(s * 2147483647.0)) for s in clipped])
    elif fmt in (11, 12):
        a = array("i", [int(round(s * 8388607.0)) for s in clipped])
    elif fmt in (9, 10):
        order = "little" if little else "big"
        return b"".join(int(round(s * 8388607.0)).to_bytes(3, order, signed=True) for s in clipped)
    elif fmt == 0:
        return bytes(int(round(s * 127.0)) + 128 for s in clipped)
    else:
        raise ValueError(f"unsupported sample format {fmt}")
    if swap:
        a.byteswap()
    return a.tobytes()


def negotiate(fmt: int, channels: int, rate: int, cmap: list[int],
              formats: list[tuple[int, dict]]) -> Optional[tuple[int, int, int, list[int]]]:
    """The PCM format a new stream gets: what it asked for, or — clients using
    the extended API send an unset sample spec plus format infos — the first
    PCM format info. None when it wants only something else (AC3 passthrough)."""
    if fmt in SAMPLE_BYTES and 1 <= channels <= 32 and 1000 <= rate <= 384000 and len(cmap) == channels:
        return fmt, channels, rate, cmap
    for encoding, props in formats:
        if encoding not in (ENCODING_ANY, ENCODING_PCM):
            continue

        def prop(key: str) -> str:
            return props.get(key, b"").rstrip(b"\0").decode("utf-8", "replace").strip().strip('"')

        f = FORMAT_BY_NAME.get(prop("format.sample_format"), FLOAT32LE)  # lists or ranges: our choice
        try:
            ch = int(prop("format.channels") or 2)
            r = int(prop("format.rate") or SOURCE_RATE)
        except ValueError:
            ch, r = 2, SOURCE_RATE
        if not (1 <= ch <= 32 and 1000 <= r <= 384000):
            return None
        return f, ch, r, [CHANNEL_MONO] if ch == 1 else list(range(1, ch + 1))
    return None


# --------------------------------------------------------------------------
# The server


class Source(Protocol):
    """What the server records from: Visor's microphone (visor.xr.mic)."""

    def tick(self, now: float) -> None:
        """Called once per clock tick, before any stream renders for it."""

    def render(self, t0: float, rate: int, n: int) -> list[float]:
        """``n`` mono samples at ``rate`` starting at monotonic time ``t0``.
        Every stream asks by time, so all of them hear the same moment."""


@dataclass
class RecordStream:
    index: int  # also the channel its audio travels on
    client: "Connection"
    fmt: int
    channels: int
    rate: int
    channel_map: list[int]
    maxlength: int
    fragsize: int
    corked: bool
    name: str = ""
    muted: bool = False
    started: float = 0.0
    frames_sent: int = 0
    bytes_sent: int = 0

    @property
    def frame_bytes(self) -> int:
        return FORMATS[self.fmt][1] * self.channels


@dataclass
class PlaybackStream:
    index: int  # also the channel its audio arrives on
    client: "Connection"
    fmt: int
    channels: int
    rate: int
    channel_map: list[int]
    maxlength: int
    tlength: int
    prebuf: int
    minreq: int
    corked: bool
    name: str = ""
    written: int = 0  # bytes the app has sent: its write index
    played: int = 0  # bytes taken from its buffer by the speakers: the read index
    hw: float = 0.0  # frames inside the "sound card", playing out in real time
    owed: int = 0  # taken since we last asked for more
    last: float = 0.0  # when the speakers last ran
    drains: list = field(default_factory=list)  # tags to answer once everything has played

    @property
    def frame_bytes(self) -> int:
        return SAMPLE_BYTES[self.fmt] * self.channels

    def usec(self, nbytes: int) -> int:
        return nbytes * 1_000_000 // (self.frame_bytes * self.rate)


@dataclass
class Connection:
    sock: socket.socket
    index: int
    version: int = PROTOCOL_VERSION
    name: str = ""
    send_lock: threading.Lock = field(default_factory=threading.Lock)
    alive: bool = True

    def send(self, payload: bytes, channel: int = INVALID_INDEX) -> None:
        with self.send_lock:
            self.write(payload, channel)

    def write(self, payload: bytes, channel: int = INVALID_INDEX) -> None:
        """Send one packet; the caller holds ``send_lock``."""
        if not self.alive:
            return
        try:
            self.sock.sendall(DESCRIPTOR.pack(len(payload), channel, 0, 0, 0) + payload)
        except OSError:
            self.alive = False

    def reply(self, tag: int, out: Optional[Out] = None) -> None:
        self.send(bytes(Out().u32(REPLY).u32(tag).buf + (out.buf if out else b"")))

    def error(self, tag: int, code: int) -> None:
        self.send(bytes(Out().u32(ERROR).u32(tag).u32(code).buf))


def _recv_exact(sock: socket.socket, n: int) -> Optional[bytes]:
    buf = bytearray()
    while len(buf) < n:
        try:
            chunk = sock.recv(n - len(buf))
        except OSError:
            return None
        if not chunk:
            return None
        buf += chunk
    return bytes(buf)


class PulseServer:
    """Serves Visor's microphone to apps over one Unix socket.

    One thread accepts, one per client reads commands, and a clock thread
    sends every open record stream its audio in real time, about 10 ms at a
    time — a microphone never stalls, whatever the game is doing.
    """

    TICK = 0.01

    def __init__(self, path: str, source: Source, log: Callable[[str], None] = lambda m: None,
                 clock: Callable[[], float] = time.monotonic, mic_wake: float = MIC_WAKE):
        self.path = str(path)
        self.source = source
        self.log = log
        # Streams are timed on this clock: real time by default; Visor's mic
        # passes one that stops while the world is paused.
        self.clock = clock
        self.mic_wake = mic_wake
        self.streams: dict[int, RecordStream] = {}
        self.playback: dict[int, PlaybackStream] = {}
        self.clients: list[Connection] = []
        self.source_muted = False
        self._lock = threading.Lock()
        self._next_client = 1
        self._next_stream = 1
        self._stop = threading.Event()
        self._sock: Optional[socket.socket] = None
        self._threads: list[threading.Thread] = []

    # -- lifecycle --------------------------------------------------------

    def start(self) -> "PulseServer":
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(self.path)
        os.chmod(self.path, 0o600)
        s.listen(8)
        s.settimeout(0.5)
        self._sock = s
        for target in (self._accept_loop, self._clock_loop):
            t = threading.Thread(target=target, daemon=True)
            t.start()
            self._threads.append(t)
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            self._sock.close()
        with self._lock:
            clients = list(self.clients)
        for c in clients:
            c.alive = False
            try:
                c.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            c.sock.close()
        for t in self._threads:
            t.join(timeout=2)
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass

    def listening(self) -> list[RecordStream]:
        """Streams that are open and uncorked: the app is listening right now."""
        with self._lock:
            return [s for s in self.streams.values() if not s.corked]

    # -- threads ----------------------------------------------------------

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            conn.settimeout(None)
            with self._lock:
                c = Connection(conn, self._next_client)
                self._next_client += 1
                self.clients.append(c)
            threading.Thread(target=self._client_loop, args=(c,), daemon=True).start()

    def _client_loop(self, c: Connection) -> None:
        try:
            while not self._stop.is_set():
                head = _recv_exact(c.sock, DESCRIPTOR.size)
                if head is None:
                    break
                length, channel, _, _, flags = DESCRIPTOR.unpack(head)
                if length > FRAME_SIZE_MAX or flags & 0xFF000000:
                    break  # shared memory was never offered, so this isn't a client we speak to
                payload = _recv_exact(c.sock, length)
                if payload is None:
                    break
                if channel == INVALID_INDEX:
                    self._command(c, payload)
                else:  # what the app plays: counted, so it's paced like real speakers, then let go
                    with self._lock:
                        s = self.playback.get(channel)
                        if s is not None and s.client is c:
                            s.written += len(payload)
        finally:
            c.alive = False
            with self._lock:
                for idx in [i for i, s in self.streams.items() if s.client is c]:
                    del self.streams[idx]
                for idx in [i for i, s in self.playback.items() if s.client is c]:
                    del self.playback[idx]
                if c in self.clients:
                    self.clients.remove(c)
            c.sock.close()
            self.log(f"client {c.index} ({c.name or 'unnamed'}) disconnected")

    def _clock_loop(self) -> None:
        next_tick = time.monotonic()
        while not self._stop.is_set():
            self.source.tick(time.monotonic())
            now = self.clock()
            with self._lock:
                streams = list(self.streams.values())
            for s in streams:
                due = int((now - s.started) * s.rate) - s.frames_sent
                if due <= 0:
                    continue
                if s.corked:
                    s.frames_sent += due  # time passes; a corked stream just isn't sent it
                    continue
                # Whole fragments only, as PulseAudio delivers them: clients size their reads
                # by fragsize, and SDL 2's capture flush never ends if data comes any faster.
                frag = max(1, s.fragsize // s.frame_bytes)
                while due >= frag:
                    if s.muted or self.source_muted:
                        samples = [0.0] * frag
                    else:
                        samples = self.source.render(s.started + s.frames_sent / s.rate, s.rate, frag)
                    data = pcm_bytes(samples, s.fmt, s.channels)
                    s.client.send(data, channel=s.index)
                    s.frames_sent += frag
                    s.bytes_sent += len(data)
                    due -= frag
            self._play(time.monotonic())
            next_tick += self.TICK
            delay = next_tick - time.monotonic()
            if delay > 0:
                self._stop.wait(delay)
            else:
                next_tick = time.monotonic()  # fell behind (suspend, debugger): don't burst

    def _play(self, now: float) -> None:
        """The speakers: a small "sound card" buffer that plays out in real
        time (into nothing) and refills from each app's stream; ask the app for
        more as room frees up, and answer drains once it has all played. The
        buffer ahead of real time is what lets an app write its next chunk
        before the last one runs out, as on real hardware."""
        requests, drained = [], []
        with self._lock:
            for s in self.playback.values():
                elapsed, s.last = (now - s.last if s.last else 0.0), now
                fb = s.frame_bytes
                if not s.corked:
                    s.hw = max(0.0, s.hw - min(elapsed, 0.1) * s.rate)
                    room = int(SINK_BUFFER * s.rate - s.hw)
                    move = max(0, min(room, (s.written - s.played) // fb))
                    s.hw += move
                    s.played += move * fb
                    s.owed += move * fb
                if s.owed and (s.owed >= s.minreq or s.written - s.played < fb):
                    requests.append((s.client, s.index, s.owed))
                    s.owed = 0
                if s.drains and s.written - s.played < fb and s.hw < 1.0:
                    drained += [(s.client, tag) for tag in s.drains]
                    s.drains = []
        for client, index, nbytes in requests:
            client.send(bytes(Out().u32(REQUEST).u32(INVALID_INDEX).u32(index).u32(nbytes).buf))
        for client, tag in drained:
            client.reply(tag)

    # -- commands ---------------------------------------------------------

    def _command(self, c: Connection, payload: bytes) -> None:
        t = Tags(payload)
        try:
            cmd, tag = t.u32(), t.u32()
        except TagError:
            return
        handler = self._HANDLERS.get(cmd)
        if handler is None:
            c.error(tag, ERR_NOTSUPPORTED)
            return
        try:
            handler(self, c, tag, t)
        except TagError as e:
            self.log(f"malformed command {cmd} from client {c.index}: {e}")
            c.error(tag, ERR_PROTOCOL)

    def _auth(self, c: Connection, tag: int, t: Tags) -> None:
        theirs = t.u32() & VERSION_MASK
        t.arbitrary()  # the cookie: this socket is ours (0600), so who you are is already settled
        if theirs < 13:
            c.error(tag, ERR_NOTSUPPORTED)
            return
        c.version = min(theirs, PROTOCOL_VERSION)
        # No shared-memory flags in our version word: audio stays inline.
        c.reply(tag, Out().u32(PROTOCOL_VERSION))

    def _set_client_name(self, c: Connection, tag: int, t: Tags) -> None:
        props = t.proplist()
        c.name = props.get("application.name", b"").rstrip(b"\0").decode("utf-8", "replace")
        self.log(f"client {c.index} is {c.name or 'unnamed'} (protocol {c.version})")
        c.reply(tag, Out().u32(c.index))

    def _ack(self, c: Connection, tag: int, t: Tags) -> None:
        c.reply(tag)

    def _empty_list(self, c: Connection, tag: int, t: Tags) -> None:
        c.reply(tag)

    def _no_entity(self, c: Connection, tag: int, t: Tags) -> None:
        c.error(tag, ERR_NOENTITY)

    def _server_info(self, c: Connection, tag: int, t: Tags) -> None:
        out = (Out().string("pulseaudio").string("16.1").string(os.environ.get("USER", "visor")).string("visor")
               .sample_spec(FLOAT32LE, 2, SOURCE_RATE).string(SINK_NAME).string(SOURCE_NAME).u32(0x5649534f))
        if c.version >= 15:
            out.channel_map([CHANNEL_LEFT, CHANNEL_RIGHT])
        c.reply(tag, out)

    def _device_entry(self, c: Connection, out: Out, sink: bool) -> None:
        """One sink_info / source_info record (they share a layout)."""
        v = c.version
        name, desc = (SINK_NAME, SINK_DESCRIPTION) if sink else (SOURCE_NAME, SOURCE_DESCRIPTION)
        channels = [CHANNEL_LEFT, CHANNEL_RIGHT] if sink else [CHANNEL_MONO]
        muted = False if sink else self.source_muted
        busy = bool(self.playback) if sink else bool(self.listening())
        (out.u32(SINK_INDEX if sink else SOURCE_INDEX).string(name).string(desc)
            .sample_spec(FLOAT32LE, len(channels), SOURCE_RATE).channel_map(channels).u32(INVALID_INDEX)
            .cvolume([VOLUME_NORM] * len(channels)).boolean(muted).u32(INVALID_INDEX).string(None)
            .usec(10_000).string("visor").u32(0))
        if v >= 13:
            out.proplist({"device.description": desc, "device.class": "sound", "device.api": "visor",
                          "device.form_factor": "headset"}).usec(10_000)
        if v >= 15:
            out.volume(VOLUME_NORM).u32(STATE_RUNNING if busy else STATE_IDLE).u32(VOLUME_NORM + 1).u32(INVALID_INDEX)
        if v >= 16:
            out.u32(0).string(None)  # no ports, no active port
        if v >= (21 if sink else 22):
            out.u8(1).format_info(ENCODING_PCM, {})

    def _source_entry(self, c: Connection, out: Out) -> None:
        self._device_entry(c, out, sink=False)

    def _sink_info(self, c: Connection, tag: int, t: Tags) -> None:
        idx, name = t.u32(), t.string()
        if idx not in (SINK_INDEX, INVALID_INDEX) or (idx == INVALID_INDEX and name not in
                                                      (None, SINK_NAME, "@DEFAULT_SINK@")):
            c.error(tag, ERR_NOENTITY)
            return
        out = Out()
        self._device_entry(c, out, sink=True)
        c.reply(tag, out)

    def _sink_info_list(self, c: Connection, tag: int, t: Tags) -> None:
        out = Out()
        self._device_entry(c, out, sink=True)
        c.reply(tag, out)

    def _lookup_sink(self, c: Connection, tag: int, t: Tags) -> None:
        t.string()
        c.reply(tag, Out().u32(SINK_INDEX))  # one pair of speakers, whatever they're called

    # -- the speakers -------------------------------------------------------

    def _create_playback_stream(self, c: Connection, tag: int, t: Tags) -> None:
        v = c.version
        fmt, channels, rate = t.sample_spec()
        cmap = t.channel_map()
        t.u32()  # sink index: any, we have one
        t.string()  # sink name: likewise
        maxlength, corked = t.u32(), t.boolean()
        tlength, prebuf, minreq = t.u32(), t.u32(), t.u32()
        t.u32()  # sync id
        t.cvolume()
        name = ""
        if v >= 12:
            for _ in range(7):  # no_remap, no_remix, fix_format, fix_rate, fix_channels, no_move, variable_rate
                t.boolean()
        if v >= 13:
            t.boolean()  # muted
            t.boolean()  # adjust_latency
            props = t.proplist()
            name = props.get("media.name", b"").rstrip(b"\0").decode("utf-8", "replace")
        if v >= 14:
            t.boolean()  # volume_set
            t.boolean()  # early_requests
        if v >= 15:
            t.boolean()  # muted_set
            t.boolean()  # dont_inhibit_auto_suspend
            t.boolean()  # fail_on_suspend
        if v >= 17:
            t.boolean()  # relative_volume
        if v >= 18:
            t.boolean()  # passthrough
        formats = []
        if v >= 21:
            formats = [t.format_info() for _ in range(t.u8())]
        chosen = negotiate(fmt, channels, rate, cmap, formats)
        if chosen is None:
            c.error(tag, ERR_NOTSUPPORTED if formats else ERR_INVALID)
            return
        fmt, channels, rate, cmap = chosen
        frame = SAMPLE_BYTES[fmt] * channels

        def attr(value: int, default: int) -> int:
            return default if value in (0, INVALID_INDEX) else max(frame, value - value % frame)

        tlength = attr(tlength, max(1, rate // 4) * frame)  # 250 ms
        minreq = min(attr(minreq, max(1, rate // 50) * frame), tlength)  # 20 ms
        prebuf = min(attr(prebuf, tlength), tlength)
        maxlength = max(attr(maxlength, 4 * 1024 * 1024), tlength)
        with self._lock:
            index = self._next_stream
            self._next_stream += 1
        s = PlaybackStream(index=index, client=c, fmt=fmt, channels=channels, rate=rate, channel_map=cmap,
                           maxlength=maxlength, tlength=tlength, prebuf=prebuf, minreq=minreq, corked=corked,
                           name=name, last=time.monotonic())
        out = Out().u32(index).u32(index).u32(tlength)  # stream, sink input, and the first request: fill the buffer
        out.u32(maxlength).u32(tlength).u32(prebuf).u32(minreq)
        out.sample_spec(fmt, channels, rate).channel_map(cmap).u32(SINK_INDEX).string(SINK_NAME).boolean(False)
        out.usec(s.usec(tlength))
        if v >= 21:
            sample = next((k for k, n in FORMAT_BY_NAME.items() if n == fmt), "float32le")
            out.format_info(ENCODING_PCM, {"format.sample_format": f'"{sample}"', "format.rate": str(rate),
                                           "format.channels": str(channels)})
        with c.send_lock:
            c.write(bytes(Out().u32(REPLY).u32(tag).buf + out.buf))
            with self._lock:
                self.playback[index] = s
        self.log(f"client {c.index} opened the speakers: {rate} Hz × {channels}" + (f" ({name})" if name else ""))

    def _playback(self, c: Connection, idx: int) -> Optional[PlaybackStream]:
        with self._lock:
            s = self.playback.get(idx)
        return s if s is not None and s.client is c else None

    def _delete_playback_stream(self, c: Connection, tag: int, t: Tags) -> None:
        idx = t.u32()
        with self._lock:
            s = self.playback.get(idx)
            if s is not None and s.client is c:
                del self.playback[idx]
        if s is None or s.client is not c:
            c.error(tag, ERR_NOENTITY)
            return
        c.reply(tag)

    def _cork_playback(self, c: Connection, tag: int, t: Tags) -> None:
        idx, corked = t.u32(), t.boolean()
        s = self._playback(c, idx)
        if s is None:
            c.error(tag, ERR_NOENTITY)
            return
        with self._lock:
            s.corked, s.last = corked, time.monotonic()
        c.reply(tag)

    def _flush_playback(self, c: Connection, tag: int, t: Tags) -> None:
        s = self._playback(c, t.u32())
        if s is None:
            c.error(tag, ERR_NOENTITY)
            return
        with self._lock:
            s.owed += s.written - s.played  # the dropped audio's room is free again
            s.played = s.written
            s.hw = 0.0
        c.reply(tag)

    def _drain_playback(self, c: Connection, tag: int, t: Tags) -> None:
        s = self._playback(c, t.u32())
        if s is None:
            c.error(tag, ERR_NOENTITY)
            return
        with self._lock:
            s.drains.append(tag)  # answered by the speakers once it has all played

    def _playback_latency(self, c: Connection, tag: int, t: Tags) -> None:
        idx = t.u32()
        sec, usec = t.timeval()
        s = self._playback(c, idx)
        if s is None:
            c.error(tag, ERR_NOENTITY)
            return
        with self._lock:
            written, played, corked, hw = s.written, s.played, s.corked, s.hw
        now = time.time()
        # The device's own latency; the client adds what's still in the stream (written - played).
        out = (Out().usec(int(hw * 1_000_000 / s.rate)).usec(0).boolean(not corked).timeval(sec, usec)
               .timeval(int(now), int((now % 1) * 1_000_000)).s64(written).s64(played))
        if c.version >= 13:
            out.u64(0).u64(s.usec(played))  # underrun for, playing for
        c.reply(tag, out)

    def _set_playback_buffer_attr(self, c: Connection, tag: int, t: Tags) -> None:
        idx, maxlength, tlength, prebuf, minreq = t.u32(), t.u32(), t.u32(), t.u32(), t.u32()
        if c.version >= 13:
            t.boolean()  # adjust_latency
        if c.version >= 14:
            t.boolean()  # early_requests
        s = self._playback(c, idx)
        if s is None:
            c.error(tag, ERR_NOENTITY)
            return
        frame = s.frame_bytes
        with self._lock:
            if tlength not in (0, INVALID_INDEX):
                s.tlength = max(frame, tlength - tlength % frame)
            if minreq not in (0, INVALID_INDEX):
                s.minreq = min(max(frame, minreq - minreq % frame), s.tlength)
            if prebuf not in (INVALID_INDEX,):
                s.prebuf = min(prebuf - prebuf % frame, s.tlength)
            if maxlength not in (0, INVALID_INDEX):
                s.maxlength = max(maxlength, s.tlength)
        out = Out().u32(s.maxlength).u32(s.tlength).u32(s.prebuf).u32(s.minreq)
        if c.version >= 13:
            out.usec(s.usec(s.tlength))
        c.reply(tag, out)

    def _update_sample_rate(self, c: Connection, tag: int, t: Tags) -> None:
        idx, rate = t.u32(), t.u32()
        with self._lock:
            s = self.playback.get(idx) or self.streams.get(idx)
        if s is None or s.client is not c or not 1000 <= rate <= 384000:
            c.error(tag, ERR_NOENTITY if s is None or s.client is not c else ERR_INVALID)
            return
        s.rate = rate
        c.reply(tag)

    def _source_info(self, c: Connection, tag: int, t: Tags) -> None:
        idx, name = t.u32(), t.string()
        if idx not in (SOURCE_INDEX, INVALID_INDEX) or (idx == INVALID_INDEX and name not in
                                                        (None, SOURCE_NAME, "@DEFAULT_SOURCE@")):
            c.error(tag, ERR_NOENTITY)
            return
        out = Out()
        self._source_entry(c, out)
        c.reply(tag, out)

    def _source_info_list(self, c: Connection, tag: int, t: Tags) -> None:
        out = Out()
        self._source_entry(c, out)
        c.reply(tag, out)

    def _lookup_source(self, c: Connection, tag: int, t: Tags) -> None:
        t.string()
        c.reply(tag, Out().u32(SOURCE_INDEX))  # there is only one microphone, whatever it's called

    def _stat(self, c: Connection, tag: int, t: Tags) -> None:
        c.reply(tag, Out().u32(0).u32(0).u32(0).u32(0).u32(0))

    def _create_record_stream(self, c: Connection, tag: int, t: Tags) -> None:
        v = c.version
        fmt, channels, rate = t.sample_spec()
        cmap = t.channel_map()
        t.u32()  # source index: any, we have one
        t.string()  # source name: likewise
        maxlength, corked, fragsize = t.u32(), t.boolean(), t.u32()
        name = ""
        if v >= 12:
            for _ in range(7):  # no_remap, no_remix, fix_format, fix_rate, fix_channels, no_move, variable_rate
                t.boolean()
        if v >= 13:
            t.boolean()  # peak_detect
            t.boolean()  # adjust_latency
            props = t.proplist()
            name = props.get("media.name", b"").rstrip(b"\0").decode("utf-8", "replace")
            t.u32()  # direct_on_input
        if v >= 14:
            t.boolean()  # early_requests
        if v >= 15:
            t.boolean()  # dont_inhibit_auto_suspend
            t.boolean()  # fail_on_suspend
        formats = []
        if v >= 22:
            formats = [t.format_info() for _ in range(t.u8())]
        chosen = negotiate(fmt, channels, rate, cmap, formats)
        if chosen is None or chosen[0] not in FORMATS:  # compressed, a-law, mu-law: not things this mic speaks
            c.error(tag, ERR_NOTSUPPORTED if formats or chosen else ERR_INVALID)
            return
        fmt, channels, rate, cmap = chosen
        frame = FORMATS[fmt][1] * channels
        if fragsize in (0, INVALID_INDEX):
            fragsize = max(1, rate // 50) * frame  # 20 ms
        fragsize = max(frame, fragsize - fragsize % frame)
        if maxlength in (0, INVALID_INDEX) or maxlength < fragsize:
            maxlength = 4 * 1024 * 1024
        with self._lock:
            index = self._next_stream
            self._next_stream += 1
        s = RecordStream(index=index, client=c, fmt=fmt, channels=channels, rate=rate, channel_map=cmap,
                         maxlength=maxlength, fragsize=fragsize, corked=corked, name=name)
        out = Out().u32(index).u32(index).u32(maxlength).u32(fragsize)
        out.sample_spec(fmt, channels, rate).channel_map(cmap).u32(SOURCE_INDEX).string(SOURCE_NAME).boolean(False)
        latency = fragsize * 1_000_000 // (frame * rate)
        out.usec(latency)
        if v >= 22:
            out.format_info(ENCODING_PCM, {"format.sample_format": f'"{FORMATS[fmt][0]}"',
                                           "format.rate": str(rate), "format.channels": str(channels)})
        # Reply before the first audio, under the send lock, so the stream exists when its data lands.
        with c.send_lock:
            c.write(bytes(Out().u32(REPLY).u32(tag).buf + out.buf))
            with self._lock:
                awake = bool(self.streams)  # another stream is recording: the mic is already running
                s.started = self.clock() + (0.0 if awake else max(0.0, self.mic_wake - fragsize / (frame * rate)))
                self.streams[index] = s
        self.log(f"client {c.index} opened the microphone: {FORMATS[fmt][0]} {rate} Hz × {channels}"
                 + (f" ({name})" if name else "") + (" corked" if corked else ""))

    def _stream(self, c: Connection, idx: int) -> Optional[RecordStream]:
        with self._lock:
            s = self.streams.get(idx)
        return s if s is not None and s.client is c else None

    def _delete_record_stream(self, c: Connection, tag: int, t: Tags) -> None:
        idx = t.u32()
        with self._lock:
            s = self.streams.get(idx)
            if s is not None and s.client is c:
                del self.streams[idx]
        if s is None or s.client is not c:
            c.error(tag, ERR_NOENTITY)
            return
        self.log(f"client {c.index} closed the microphone")
        c.reply(tag)

    def _cork(self, c: Connection, tag: int, t: Tags) -> None:
        idx, corked = t.u32(), t.boolean()
        s = self._stream(c, idx)
        if s is None:
            c.error(tag, ERR_NOENTITY)
            return
        s.corked = corked
        c.reply(tag)

    def _flush(self, c: Connection, tag: int, t: Tags) -> None:
        if self._stream(c, t.u32()) is None:
            c.error(tag, ERR_NOENTITY)
            return
        c.reply(tag)

    def _record_latency(self, c: Connection, tag: int, t: Tags) -> None:
        idx = t.u32()
        sec, usec = t.timeval()
        s = self._stream(c, idx)
        if s is None:
            c.error(tag, ERR_NOENTITY)
            return
        now = time.time()
        (c.reply(tag, Out().usec(0).usec(10_000).boolean(not s.corked).timeval(sec, usec)
                 .timeval(int(now), int((now % 1) * 1_000_000)).s64(s.bytes_sent).s64(s.bytes_sent)))

    def _set_buffer_attr(self, c: Connection, tag: int, t: Tags) -> None:
        idx, maxlength, fragsize = t.u32(), t.u32(), t.u32()
        if c.version >= 13:
            t.boolean()  # adjust_latency
        if c.version >= 14:
            t.boolean()  # early_requests
        s = self._stream(c, idx)
        if s is None:
            c.error(tag, ERR_NOENTITY)
            return
        frame = s.frame_bytes
        if fragsize not in (0, INVALID_INDEX):
            s.fragsize = max(frame, fragsize - fragsize % frame)
        if maxlength not in (0, INVALID_INDEX) and maxlength >= s.fragsize:
            s.maxlength = maxlength
        c.reply(tag, Out().u32(s.maxlength).u32(s.fragsize).usec(s.fragsize * 1_000_000 // (frame * s.rate)))

    def _set_stream_mute(self, c: Connection, tag: int, t: Tags) -> None:
        idx, muted = t.u32(), t.boolean()
        with self._lock:
            s = self.streams.get(idx)
        if s is not None:
            s.muted = muted
        c.reply(tag)

    def _set_source_mute(self, c: Connection, tag: int, t: Tags) -> None:
        t.u32()
        t.string()
        self.source_muted = t.boolean()
        c.reply(tag)

    _HANDLERS = {
        AUTH: _auth,
        SET_CLIENT_NAME: _set_client_name,
        GET_SERVER_INFO: _server_info,
        GET_SOURCE_INFO: _source_info,
        GET_SOURCE_INFO_LIST: _source_info_list,
        LOOKUP_SOURCE: _lookup_source,
        LOOKUP_SINK: _lookup_sink,
        GET_SINK_INFO: _sink_info,
        GET_SINK_INFO_LIST: _sink_info_list,
        CREATE_PLAYBACK_STREAM: _create_playback_stream,
        DELETE_PLAYBACK_STREAM: _delete_playback_stream,
        CORK_PLAYBACK_STREAM: _cork_playback,
        FLUSH_PLAYBACK_STREAM: _flush_playback,
        DRAIN_PLAYBACK_STREAM: _drain_playback,
        TRIGGER_PLAYBACK_STREAM: _ack,
        PREBUF_PLAYBACK_STREAM: _ack,
        GET_PLAYBACK_LATENCY: _playback_latency,
        SET_PLAYBACK_STREAM_BUFFER_ATTR: _set_playback_buffer_attr,
        UPDATE_PLAYBACK_STREAM_SAMPLE_RATE: _update_sample_rate,
        UPDATE_RECORD_STREAM_SAMPLE_RATE: _update_sample_rate,
        SET_PLAYBACK_STREAM_NAME: _ack,
        UPDATE_PLAYBACK_STREAM_PROPLIST: _ack,
        REMOVE_PLAYBACK_STREAM_PROPLIST: _ack,
        SET_SINK_INPUT_VOLUME: _ack,
        SET_SINK_INPUT_MUTE: _ack,
        SET_SINK_VOLUME: _ack,
        SET_SINK_MUTE: _ack,
        GET_MODULE_INFO: _no_entity,
        GET_CLIENT_INFO: _no_entity,
        GET_SINK_INPUT_INFO: _no_entity,
        GET_SOURCE_OUTPUT_INFO: _no_entity,
        GET_SAMPLE_INFO: _no_entity,
        GET_CARD_INFO: _no_entity,
        GET_MODULE_INFO_LIST: _empty_list,
        GET_CLIENT_INFO_LIST: _empty_list,
        GET_SINK_INPUT_INFO_LIST: _empty_list,
        GET_SOURCE_OUTPUT_INFO_LIST: _empty_list,
        GET_SAMPLE_INFO_LIST: _empty_list,
        GET_CARD_INFO_LIST: _empty_list,
        STAT: _stat,
        SUBSCRIBE: _ack,
        EXIT: _ack,
        CREATE_RECORD_STREAM: _create_record_stream,
        DELETE_RECORD_STREAM: _delete_record_stream,
        CORK_RECORD_STREAM: _cork,
        FLUSH_RECORD_STREAM: _flush,
        GET_RECORD_LATENCY: _record_latency,
        SET_RECORD_STREAM_BUFFER_ATTR: _set_buffer_attr,
        SET_RECORD_STREAM_NAME: _ack,
        UPDATE_RECORD_STREAM_PROPLIST: _ack,
        UPDATE_CLIENT_PROPLIST: _ack,
        REMOVE_RECORD_STREAM_PROPLIST: _ack,
        REMOVE_CLIENT_PROPLIST: _ack,
        SET_SOURCE_OUTPUT_MUTE: _set_stream_mute,
        SET_SOURCE_OUTPUT_VOLUME: _ack,
        SET_SOURCE_VOLUME: _ack,
        SET_SOURCE_MUTE: _set_source_mute,
    }
