"""Tests for the VrApi metrics parser — fixture captured from a real
Quest 3 (OS v14) running Voyager under load."""

from pathlib import Path

import pytest

from visor.adb.vrapi import parse_vrapi_line, parse_vrapi_message

FIXTURE = Path(__file__).parent / "fixtures" / "vrapi_v14.log"

# Real line from Voyager (pid 8864) under GPU load
GAME_LINE = (
    "07-12 12:44:27.006  8864  9041 I VrApi   : FPS=72/72,Prd=36ms,Tear=0,"
    "Early=0,Stale=0,Stale2/5/10/max=0/0/0/0,VSnc=1,Lat=-4,Fov=0D,"
    "CPU4/GPU=2/5,1382/599MHz,OC=3F,TA=0/0/0,SP=N/N/N,Mem=2736MHz,"
    "Free=3247MB,PLS=0,Temp=32.0C/0.0C,TW=1.92ms,App=8.29ms,GD=0.37ms,"
    "CPU&GPU=21.81ms,LCnt=5(DR0,LM2),GPU%=0.81,CPU%=0.53(W0.55),"
    "CFL=12.49/16.38,ICFLp95=15.61,LD=0,SF=1.00,LP=0,DVFS=0,Preempt=432,"
    "PoseAgeP95=0.00"
)

# Shell/runtime line (pid 3207) — has extra fields the game line lacks
SHELL_LINE = (
    "07-12 12:42:20.006  3207  8166 I VrApi   : FPS=72/72,Prd=23ms,Tear=0,"
    "Early=0,Stale=0,Stale2/5/10/max=0/0/0/0,VSnc=1,Lat=-4,Fov=0,"
    "CPU4/GPU=3/2,1651/492MHz,OC=3F,TA=0/0/0,SP=N/N/N,Mem=2736MHz,"
    "Free=3503MB,PLS=0,Temp=31.0C/0.0C,TW=2.23ms,App=1.50ms,GD=0.00ms,"
    "CPU&GPU=5.03ms,LCnt=6(DR0,LM0),GPU%=0.29,CPU%=0.25(W0.28),"
    "CFL=12.20/16.23,ICFLp95=15.40,LD=0,SF=1.00,LP=0,DVFS=0,ShrpLCnt=3,"
    "ShrpR=0.500,SSLCnt=0/0,Preempt=156,PoseAgeP95=0.00"
)


class TestParseVrApiLine:
    def test_game_line_core_fields(self):
        s = parse_vrapi_line(GAME_LINE)
        assert s.pid == 8864
        assert s.timestamp == "07-12 12:44:27.006"
        assert s.fps == 72
        assert s.target_fps == 72
        assert s.stale_frames == 0
        assert s.early_frames == 0
        assert s.tears == 0
        assert s.cpu_level == 2
        assert s.gpu_level == 5
        assert s.cpu_mhz == 1382
        assert s.gpu_mhz == 599
        assert s.gpu_util == pytest.approx(0.81)
        assert s.cpu_util == pytest.approx(0.53)
        assert s.cpu_util_worst_core == pytest.approx(0.55)
        assert s.app_gpu_ms == pytest.approx(8.29)
        assert s.timewarp_gpu_ms == pytest.approx(1.92)
        assert s.free_memory_mb == 3247
        assert s.memory_mhz == 2736
        assert s.temperature_c == pytest.approx(32.0)
        assert s.prediction_ms == 36
        assert s.layer_count == 5

    def test_shell_line_with_extra_fields_still_parses(self):
        s = parse_vrapi_line(SHELL_LINE)
        assert s.pid == 3207
        assert s.gpu_util == pytest.approx(0.29)

    def test_stale_histogram(self):
        line = GAME_LINE.replace(
            "Stale=0,Stale2/5/10/max=0/0/0/0", "Stale=7,Stale2/5/10/max=3/1/0/12"
        )
        s = parse_vrapi_line(line)
        assert s.stale_frames == 7
        assert s.stale_histogram == {"2": 3, "5": 1, "10": 0, "max": 12}

    def test_non_vrapi_line_returns_none(self):
        assert parse_vrapi_line("07-12 12:00:00.000  1  2 I Unity : hello") is None
        assert parse_vrapi_line("--------- beginning of main") is None

    def test_missing_optional_fields_tolerated(self):
        # Field sets vary between emitters/OS builds — parser must not
        # require any particular optional key
        s = parse_vrapi_message(8864, "07-12 12:00:00.000", "FPS=90/90,Prd=30ms")
        assert s.fps == 90
        assert s.target_fps == 90
        assert s.gpu_util is None
        assert s.app_gpu_ms is None

    def test_fps_without_target_old_format(self):
        # Pre-v14 format logs FPS=72 without target
        s = parse_vrapi_message(1, "07-12 12:00:00.000", "FPS=72,Prd=32ms,Stale=1")
        assert s.fps == 72
        assert s.target_fps is None
        assert s.stale_frames == 1

    def test_to_dict_roundtrip(self):
        d = parse_vrapi_line(GAME_LINE).to_dict()
        assert d["fps"] == 72
        assert d["gpu_level"] == 5
        assert d["pid"] == 8864


class TestFixture:
    def test_parses_entire_real_capture(self):
        lines = FIXTURE.read_text().splitlines()
        samples = [s for s in (parse_vrapi_line(l) for l in lines) if s]
        assert len(samples) >= 60
        pids = {s.pid for s in samples}
        assert len(pids) == 2  # game + runtime both emit
        # every parsed sample has the essentials
        assert all(s.fps is not None for s in samples)
        assert all(s.timestamp for s in samples)
