"""Tests for the bottleneck diagnosis engine and session diffing."""

import pytest

from visor.adb.diagnosis import diagnose_session, diff_sessions
from visor.adb.perf_session import PerfSession
from visor.adb.vrapi import VrApiSample


def sample(elapsed, **kw):
    base = dict(
        timestamp="t", pid=1, fps=72, target_fps=72, stale_frames=0,
        gpu_level=3, cpu_level=2, gpu_util=0.55, cpu_util=0.35,
        cpu_util_worst_core=0.45, app_gpu_ms=6.0, free_memory_mb=3000,
        gpu_mhz=525, temperature_c=30.0,
    )
    base.update(kw)
    d = VrApiSample(**base).to_dict()
    d["elapsed_s"] = elapsed
    return d


def make_session(samples, thermal=None, memory=None, events=None, package="com.my.game"):
    return PerfSession(
        device="S", package=package, started_at="t",
        duration_s=samples[-1]["elapsed_s"] + 1 if samples else 0,
        vrapi=samples, thermal=thermal or [], memory=memory or [],
        events=events or [],
    )


class TestVerdicts:
    def test_healthy_session(self):
        session = make_session([sample(i) for i in range(30)])
        d = diagnose_session(session)
        assert d["verdict"] == "healthy"
        assert d["findings"] == []
        assert "72" in d["headline"]  # mentions hitting target

    def test_gpu_bound(self):
        samples = [
            sample(i, fps=61, stale_frames=11, gpu_level=5, gpu_util=0.97,
                   app_gpu_ms=15.2)
            for i in range(30)
        ]
        d = diagnose_session(make_session(samples))
        assert d["verdict"] == "gpu_bound"
        f = d["findings"][0]
        assert f["kind"] == "gpu_bound"
        assert f["severity"] in ("warning", "critical")
        # evidence cites utilization and frame budget overrun
        evidence = " ".join(f["evidence"])
        assert "0.97" in evidence or "97" in evidence
        assert "budget" in evidence.lower()

    def test_cpu_bound_single_thread(self):
        samples = [
            sample(i, fps=58, stale_frames=14, cpu_level=4, cpu_util=0.40,
                   cpu_util_worst_core=0.97, gpu_util=0.45)
            for i in range(30)
        ]
        d = diagnose_session(make_session(samples))
        assert d["verdict"] == "cpu_bound"
        f = d["findings"][0]
        assert "single" in f["summary"].lower() or "one core" in f["summary"].lower()

    def test_thermal_throttling(self):
        samples = (
            [sample(i, gpu_mhz=599, gpu_level=5, gpu_util=0.9) for i in range(10)]
            + [sample(10 + i, fps=60, stale_frames=8, gpu_mhz=420, gpu_level=5,
                      gpu_util=0.99, temperature_c=44.0) for i in range(10)]
        )
        thermal = (
            [{"elapsed_s": i, "status_label": "NONE", "max_skin_temp_c": 38.0}
             for i in range(10)]
            + [{"elapsed_s": 10 + i, "status_label": "SEVERE", "max_skin_temp_c": 44.5}
               for i in range(10)]
        )
        d = diagnose_session(make_session(samples, thermal=thermal))
        kinds = [f["kind"] for f in d["findings"]]
        assert "thermal_throttling" in kinds
        assert d["verdict"] == "thermal_throttling"  # trumps gpu_bound

    def test_memory_pressure(self):
        samples = [sample(i, free_memory_mb=350) for i in range(30)]
        events = [{"elapsed_s": 12.0, "kind": "lmkd_kill",
                   "detail": "low-memory killer killed com.other.app"}]
        d = diagnose_session(make_session(samples, events=events))
        kinds = [f["kind"] for f in d["findings"]]
        assert "memory_pressure" in kinds

    def test_graphics_leak_suspicion(self):
        samples = [sample(i) for i in range(60)]
        memory = [
            {"elapsed_s": e, "graphics_kb": 400_000 + e * 2_000,
             "total_pss_kb": 900_000 + e * 2_500}
            for e in range(0, 60, 2)
        ]
        d = diagnose_session(make_session(samples, memory=memory))
        kinds = [f["kind"] for f in d["findings"]]
        assert "memory_growth" in kinds

    def test_hitching_without_obvious_bound(self):
        # frames dropping but neither CPU nor GPU saturated -> hitches
        samples = [sample(i) for i in range(20)]
        samples[5].update(stale_frames=9, fps=63)
        samples[12].update(stale_frames=8, fps=64)
        d = diagnose_session(make_session(samples))
        assert d["verdict"] == "hitching"
        assert any("burst" in e.lower() or "hitch" in e.lower()
                   for f in d["findings"] for e in [f["summary"]])

    def test_wake_transient_burst_not_flagged_as_hitching(self):
        # a stale burst inside the warm-up grace window is app-load noise
        samples = [sample(i) for i in range(20)]
        samples[1].update(stale_frames=15, fps=50)
        d = diagnose_session(make_session(samples))
        assert d["verdict"] == "healthy"

    def test_short_session_carries_low_confidence_note(self):
        d = diagnose_session(make_session([sample(i) for i in range(3)]))
        assert "note" in d

    def test_no_vrapi_data(self):
        d = diagnose_session(make_session([]))
        assert d["verdict"] == "no_vr_data"

    def test_all_findings_have_recommendations(self):
        samples = [
            sample(i, fps=61, stale_frames=11, gpu_level=5, gpu_util=0.97,
                   app_gpu_ms=15.2)
            for i in range(30)
        ]
        d = diagnose_session(make_session(samples))
        for f in d["findings"]:
            assert f["recommendations"], f"{f['kind']} needs recommendations"


class TestDiff:
    def _session(self, fps, stale, gpu_util, graphics_end=402_000):
        samples = [
            sample(i, fps=fps, stale_frames=stale, gpu_util=gpu_util)
            for i in range(20)
        ]
        memory = [
            {"elapsed_s": 0, "graphics_kb": 400_000, "total_pss_kb": 900_000},
            {"elapsed_s": 19, "graphics_kb": graphics_end, "total_pss_kb": 950_000},
        ]
        return make_session(samples, memory=memory)

    def test_diff_reports_deltas(self):
        before = self._session(fps=61, stale=10, gpu_util=0.97)
        after = self._session(fps=72, stale=0, gpu_util=0.72)
        diff = diff_sessions(before, after)
        assert diff["fps_avg"]["before"] == 61
        assert diff["fps_avg"]["after"] == 72
        assert diff["fps_avg"]["delta"] == 11
        assert diff["stale_percent"]["delta"] < 0  # improved
        assert diff["verdict"] == {"before": "gpu_bound", "after": "healthy"}
        assert "improved" in diff["headline"].lower()

    def test_diff_regression_flagged(self):
        before = self._session(fps=72, stale=0, gpu_util=0.7)
        after = self._session(fps=60, stale=12, gpu_util=0.98)
        diff = diff_sessions(before, after)
        assert "regress" in diff["headline"].lower()
