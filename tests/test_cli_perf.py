"""Tests for the perf/bench/trace CLI commands."""


import pytest
from typer.testing import CliRunner

from visor import adb as adb_module
from visor.adb.perf_session import PerfSession
from visor.cli.main import app
from visor.utils.parser import Device

runner = CliRunner()

QUEST = Device(serial="Q1", state="device", product="eureka",
               model="Quest 3", is_quest=True)


@pytest.fixture
def patch_adb(monkeypatch):
    def _patch(name, value):
        monkeypatch.setattr(adb_module, name, value)
    return _patch


def fake_session(fps=72, stale=0):
    return PerfSession(
        device="Q1", package="com.my.game", started_at="2026-07-12T12:00:00",
        duration_s=10,
        vrapi=[{"timestamp": "t", "pid": 1, "fps": fps, "target_fps": 72,
                "stale_frames": stale, "elapsed_s": float(i)} for i in range(10)],
    )


class TestPerfSubcommands:
    def test_perf_without_subcommand_still_snapshots(self, patch_adb):
        patch_adb("discover_devices", lambda: [QUEST])
        patch_adb("get_performance_snapshot", lambda s: {
            "cpu": {"cores": 6, "usage_percent": 20.0},
            "memory": {}, "thermal": {}, "battery": {},
            "foreground_app": "com.my.game", "top_processes": [],
        })
        result = runner.invoke(app, ["perf"])
        assert result.exit_code == 0
        assert "com.my.game" in result.output

    def test_perf_record_prints_diagnosis_and_saves(self, patch_adb, tmp_path):
        patch_adb("discover_devices", lambda: [QUEST])
        patch_adb("record_session", lambda *a, **k: fake_session())
        out = tmp_path / "s.json"
        result = runner.invoke(app, ["perf", "record", "-t", "5", "-o", str(out)])
        assert result.exit_code == 0
        assert "HEALTHY" in result.output
        assert out.exists()

    def test_perf_report(self, patch_adb, tmp_path):
        from visor.adb.perf_session import save_session

        path = tmp_path / "s.json"
        save_session(fake_session(fps=61, stale=12), str(path))
        patch_adb("discover_devices", lambda: [QUEST])
        result = runner.invoke(app, ["perf", "report", str(path)])
        assert result.exit_code == 0
        assert "61" in result.output

    def test_perf_diff(self, patch_adb, tmp_path):
        from visor.adb.perf_session import save_session

        a, b = tmp_path / "a.json", tmp_path / "b.json"
        save_session(fake_session(fps=61, stale=10), str(a))
        save_session(fake_session(fps=72, stale=0), str(b))
        result = runner.invoke(app, ["perf", "diff", str(a), str(b)])
        assert result.exit_code == 0
        assert "improved" in result.output.lower()


class TestBenchTrace:
    def test_bench_table(self, patch_adb):
        patch_adb("discover_devices", lambda: [QUEST])
        patch_adb("launch_benchmark", lambda *a, **k: {
            "package": "com.my.game", "component": "com.my.game/.Main",
            "runs_completed": 3, "runs_failed": 0,
            "total_time_ms": {"min": 2100, "median": 2345, "max": 2600},
            "launch_state": "COLD", "runs": [],
        })
        result = runner.invoke(app, ["bench", "com.my.game"])
        assert result.exit_code == 0
        assert "2345" in result.output

    def test_bench_all_failed(self, patch_adb):
        patch_adb("discover_devices", lambda: [QUEST])
        patch_adb("launch_benchmark", lambda *a, **k: {
            "package": "com.nope", "component": "x", "runs_completed": 0,
            "runs_failed": 3, "total_time_ms": None, "launch_state": None,
            "runs": [{"status": "error", "error": "Activity does not exist"}],
        })
        result = runner.invoke(app, ["bench", "com.nope"])
        assert result.exit_code == 1
        assert "does not exist" in result.output

    def test_trace(self, patch_adb, tmp_path):
        from visor.adb.bench import TraceResult

        patch_adb("discover_devices", lambda: [QUEST])
        patch_adb("capture_trace", lambda *a, **k: TraceResult(
            path=str(tmp_path / "t.pftrace"), duration_s=10, size_bytes=1024,
            message="Trace saved. Open it at https://ui.perfetto.dev",
        ))
        result = runner.invoke(app, ["trace"])
        assert result.exit_code == 0
        assert "perfetto.dev" in result.output
