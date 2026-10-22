"""Visor's microphone: the timeline an app hears, its clock, and the helper."""

import json
import math
import os
import shutil
import struct
import subprocess
import sys
import time
import wave
from array import array

import pytest

from visor.xr import mic as xr_mic
from visor.xr.mic import GAP, LEAD, Mic, MicClient, MicError, load_audio, normalize


def rms(xs):
    return math.sqrt(sum(v * v for v in xs) / len(xs))


def tone(seconds=0.5, rate=22050, hz=300.0, amp=0.8):
    return array("f", (amp * math.sin(2 * math.pi * hz * i / rate) for i in range(int(seconds * rate))))


def run_clock(m, seconds, running=True, step=0.01):
    m.world_running = lambda: running
    t = getattr(m, "_test_real", 1000.0)
    m.tick(t)
    for _ in range(int(round(seconds / step))):
        t += step
        m.tick(t)
    m._test_real = t


class TestTimeline:
    def test_quiet_room_between_lines(self):
        m = Mic()
        out = m.render(0.0, 48000, 48000)
        assert -63 < 20 * math.log10(rms(out)) < -57  # about -60 dBFS: real mics never read zero

    def test_a_line_lands_where_it_was_scheduled_at_any_rate(self):
        m = Mic(room_db=-120)
        clip = m.play(tone(), 22050)
        assert clip.start == pytest.approx(LEAD)
        for rate in (48000, 16000):
            before = m.render(0.0, rate, int(LEAD * rate) - 10)
            during = m.render(clip.start + 0.1, rate, rate // 10)
            after = m.render(clip.end + 0.05, rate, rate // 20)
            assert rms(before) < 1e-4 and rms(after) < 1e-4
            assert rms(during) == pytest.approx(xr_mic.SPEECH_RMS, rel=0.1)  # spoken at a normal level

    def test_lines_queue_with_a_breath_between(self):
        m = Mic()
        a = m.play(tone(0.4), 22050)
        b = m.play(tone(0.4), 22050)
        assert b.start == pytest.approx(a.end + GAP)
        c = m.play(tone(0.4), 22050, queue=False)
        assert c.start == pytest.approx(LEAD)

    def test_volume_scales_the_voice(self):
        m = Mic(room_db=-120)
        soft = m.play(tone(), 22050, volume=0.35, queue=False)
        level = rms(m.render(soft.start + 0.1, 48000, 4800))
        assert 20 * math.log10(level / xr_mic.SPEECH_RMS) == pytest.approx(20 * math.log10(0.35), abs=1.0)

    def test_stop_silences_everything(self):
        m = Mic(room_db=-120)
        m.play(tone(), 22050)
        m.stop()
        assert rms(m.render(0.1, 48000, 4800)) < 1e-4 and m.remaining() == 0


class TestClock:
    def test_time_stands_still_with_the_world(self):
        m = Mic()
        run_clock(m, 1.0)
        assert m.now() == pytest.approx(1.0, abs=0.02)
        run_clock(m, 2.0, running=False)
        assert m.now() == pytest.approx(1.0, abs=0.02)  # frozen while paused
        run_clock(m, 0.5)
        assert m.now() == pytest.approx(1.5, abs=0.03)

    def test_a_line_said_while_frozen_waits_for_the_world(self):
        m = Mic()
        run_clock(m, 1.0, running=False)
        clip = m.play(tone(0.3), 22050)
        run_clock(m, 5.0, running=False)
        assert m.remaining() == pytest.approx(clip.duration + LEAD, abs=0.02)  # not a sample spent
        run_clock(m, clip.duration + 0.2)
        assert m.remaining() == 0

    def test_replayed_lines_start_at_their_frame(self):
        m = Mic(room_db=-120)
        frame = {"n": 100}
        m.world_frame = lambda: frame["n"]
        m.play_at(110, tone(0.3), 22050, label="hello", kind="speech")
        run_clock(m, 0.1)
        assert not m.clips and m.busy()
        frame["n"] = 110
        run_clock(m, 0.02)
        assert len(m.clips) == 1 and m.history[-1]["label"] == "hello" and m.history[-1]["kind"] == "speech"

    def test_long_stalls_don_t_jump_the_clock(self):
        m = Mic()
        m.tick(0.0)
        m.tick(30.0)  # a suspended laptop, a debugger
        assert m.now() <= 0.1


class TestAudioFiles:
    def _wav(self, path, samples, rate=16000, channels=1, width=2):
        with wave.open(str(path), "wb") as w:
            w.setnchannels(channels)
            w.setsampwidth(width)
            w.setframerate(rate)
            frames = [int(v * 32767) for v in samples for _ in range(channels)]
            w.writeframes(struct.pack(f"<{len(frames)}h", *frames))
        return path

    def test_wav_to_mono(self, tmp_path):
        p = self._wav(tmp_path / "a.wav", [0.5, -0.5, 0.25], channels=2)
        samples, rate = load_audio(p)
        assert rate == 16000 and list(samples) == pytest.approx([0.5, -0.5, 0.25], abs=1e-3)

    @pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
    def test_other_formats_through_ffmpeg(self, tmp_path):
        src = self._wav(tmp_path / "a.wav", list(tone(0.5, 16000)), rate=16000)
        flac = tmp_path / "a.flac"
        subprocess.run(["ffmpeg", "-v", "error", "-i", str(src), str(flac)], check=True)
        samples, rate = load_audio(flac)
        assert rate == 48000 and len(samples) == pytest.approx(24000, abs=200)

    def test_missing_file(self, tmp_path):
        with pytest.raises(MicError, match="no audio file"):
            load_audio(tmp_path / "nope.wav")

    def test_normalize_to_speaking_level(self):
        out = normalize(tone(amp=0.9), 0.1)
        assert rms(out) == pytest.approx(0.1, rel=0.05) and max(abs(v) for v in out) < 0.99


@pytest.mark.skipif(sys.platform != "linux", reason="the simulator's helper runs on Linux")
def test_helper_process_lives_and_dies_with_its_app(tmp_path, monkeypatch):
    import tempfile

    run = tempfile.mkdtemp(prefix="vx", dir="/tmp")  # short: Unix socket paths max out at 108 bytes
    monkeypatch.setenv("XDG_RUNTIME_DIR", run)
    from visor.xr import paths

    name = f"mictest-{os.getpid()}"
    meta = paths.socket_dir() / f"{name}.json"
    paths.socket_dir().mkdir(parents=True, exist_ok=True)
    meta.write_text(json.dumps({"pid": os.getpid()}))
    proc = None
    try:
        proc = xr_mic.start_helper(name, tmp_path / "mic.log")
        client = MicClient(xr_mic.control_socket(name))
        for _ in range(40):  # until it has found whose microphone it is
            st = client.status()
            if st["app_pid"]:
                break
            time.sleep(0.05)
        assert st["app_pid"] == os.getpid()
        assert st["listening"] is False and st["speaking"] is False and st["world"] == "running"
        wav = tmp_path / "hi.wav"
        TestAudioFiles()._wav(wav, list(tone(0.3, 16000)))
        r = client.play(wav, label="hi", kind="speech")
        assert r["duration"] == pytest.approx(0.3, abs=0.01) and r["listening"] is False
        assert client.status()["history"][-1]["label"] == "hi"
        client.close()
        meta.unlink()  # the instance was stopped: the microphone goes with it
        assert proc.wait(timeout=5) == 0
        assert not xr_mic.pulse_socket(name).exists() and not xr_mic.control_socket(name).exists()
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait()
        shutil.rmtree(run, ignore_errors=True)


def test_helper_from_an_appimage_runs_on_its_own_copy(monkeypatch):
    """Not on ours: our mount goes away when this process exits (SIGBUS)."""
    monkeypatch.setenv("APPIMAGE", "/opt/Visor.AppImage")
    monkeypatch.delenv("PYTHONPATH", raising=False)
    cmd, env = xr_mic.helper_command("app1")
    assert cmd == ["/opt/Visor.AppImage", "sim", "mic-server", "app1"] and "PYTHONPATH" not in env
    monkeypatch.delenv("APPIMAGE")
    cmd, env = xr_mic.helper_command("app1")
    assert cmd[0] == sys.executable and env["PYTHONPATH"].split(os.pathsep)[0] == str(
        __import__("pathlib").Path(xr_mic.__file__).resolve().parents[2])


def test_an_app_without_visor_s_mic_says_so(tmp_path):
    with pytest.raises(MicError, match="real one"):
        MicClient.for_app(tmp_path / "app.sock")


def test_world_watch_follows_the_layer(tmp_path):
    if sys.platform == "win32":
        pytest.skip("Unix sockets")
    from tests.xrfake import FakeLayer

    state = {"paused": False, "render_frame": 10, "frame": 10}
    layer = FakeLayer(lambda cmd, args: dict(state) if cmd == "state" else {})
    from pathlib import Path

    watch = xr_mic.WorldWatch(Path(layer.path)).start()
    try:
        time.sleep(0.15)
        assert watch.running and watch.frame == 10
        state["paused"] = True
        time.sleep(0.15)
        assert not watch.running  # paused and no frames moving
        state["render_frame"] = 12  # a step: frames move while paused
        time.sleep(0.06)
        assert watch.running
    finally:
        watch.stop()
        layer.close()
