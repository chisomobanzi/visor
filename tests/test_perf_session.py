"""Tests for the performance session recorder and its derived data."""

import json
from pathlib import Path

import pytest

from visor.adb.perf_session import (
    PerfSession,
    derive_events,
    load_session,
    save_session,
    summarize_session,
)
from visor.adb.vrapi import VrApiSample
from visor.utils.parser import parse_meminfo_summary

MEMINFO_FIXTURE = (Path(__file__).parent / "fixtures" / "meminfo_v14.txt").read_text()


def sample(elapsed, **kw):
    base = dict(
        timestamp="07-12 12:00:00.000", pid=8864, fps=72, target_fps=72,
        stale_frames=0, gpu_level=3, cpu_level=2, gpu_util=0.5, cpu_util=0.3,
        app_gpu_ms=5.0, free_memory_mb=3000, temperature_c=30.0,
    )
    base.update(kw)
    d = VrApiSample(**base).to_dict()
    d["elapsed_s"] = elapsed
    return d


class TestMeminfoParser:
    def test_app_summary_parsed(self):
        m = parse_meminfo_summary(MEMINFO_FIXTURE)
        assert m["java_heap_kb"] == 6224
        assert m["native_heap_kb"] == 24464
        assert m["graphics_kb"] == 436294
        assert m["code_kb"] == 200016
        assert m["total_pss_kb"] == 992571

    def test_missing_process_returns_none(self):
        assert parse_meminfo_summary("No process found for: com.nope\n") is None


class TestDeriveEvents:
    def test_gpu_level_change_detected(self):
        samples = [sample(0), sample(1), sample(2, gpu_level=5), sample(3, gpu_level=5)]
        events = derive_events(vrapi=samples, thermal=[])
        kinds = [(e["kind"], e["elapsed_s"]) for e in events]
        assert ("gpu_level_change", 2) in kinds

    def test_thermal_transition_detected(self):
        thermal = [
            {"elapsed_s": 0, "status_label": "NONE", "max_skin_temp_c": 35.0},
            {"elapsed_s": 10, "status_label": "LIGHT", "max_skin_temp_c": 41.0},
        ]
        events = derive_events(vrapi=[], thermal=thermal)
        assert any(
            e["kind"] == "thermal_status" and "LIGHT" in e["detail"] for e in events
        )

    def test_stale_burst_detected(self):
        samples = [sample(0), sample(1, stale_frames=12, fps=60), sample(2)]
        events = derive_events(vrapi=samples, thermal=[])
        assert any(e["kind"] == "stale_burst" for e in events)

    def test_steady_state_yields_no_events(self):
        samples = [sample(i) for i in range(5)]
        assert derive_events(vrapi=samples, thermal=[]) == []

    def test_level_oscillation_debounced(self):
        # governor bouncing 2-3-2-3 every second is noise, not events
        samples = [sample(i, cpu_level=2 + (i % 2)) for i in range(10)]
        events = derive_events(vrapi=samples, thermal=[])
        assert [e for e in events if e["kind"] == "cpu_level_change"] == []

    def test_sustained_level_change_still_reported(self):
        samples = [sample(i, gpu_level=3) for i in range(4)] + \
                  [sample(4 + i, gpu_level=5) for i in range(4)]
        events = derive_events(vrapi=samples, thermal=[])
        changes = [e for e in events if e["kind"] == "gpu_level_change"]
        assert len(changes) == 1
        assert changes[0]["detail"] == "3 -> 5"


class TestSummary:
    def test_summary_stats(self):
        samples = (
            [sample(i, fps=72) for i in range(8)]
            + [sample(8, fps=60, stale_frames=12, gpu_util=0.95, gpu_level=5)]
            + [sample(9, fps=72)]
        )
        session = PerfSession(
            device="S", package="com.my.game", started_at="2026-07-12T12:00:00",
            duration_s=10, vrapi=samples, memory=[], thermal=[], events=[],
        )
        summary = summarize_session(session)
        assert summary["sample_count"] == 10
        assert summary["fps"]["avg"] == pytest.approx(70.8, abs=0.1)
        assert summary["fps"]["min"] == 60
        assert summary["fps"]["target"] == 72
        assert summary["stale_frames"]["total"] == 12
        assert summary["stale_frames"]["percent_of_frames"] > 0
        assert summary["gpu"]["max_level"] == 5
        assert summary["gpu"]["avg_util"] > 0.5

    def test_memory_trend(self):
        memory = [
            {"elapsed_s": 0, "graphics_kb": 100_000, "total_pss_kb": 500_000},
            {"elapsed_s": 30, "graphics_kb": 160_000, "total_pss_kb": 580_000},
        ]
        session = PerfSession(
            device="S", package="p", started_at="t", duration_s=30,
            vrapi=[sample(0)], memory=memory, thermal=[], events=[],
        )
        summary = summarize_session(session)
        assert summary["memory"]["graphics_growth_kb"] == 60_000

    def test_empty_vrapi_degrades_gracefully(self):
        session = PerfSession(
            device="S", package=None, started_at="t", duration_s=10,
            vrapi=[], memory=[], thermal=[
                {"elapsed_s": 0, "status_label": "NONE", "max_skin_temp_c": 30.0}
            ], events=[],
        )
        summary = summarize_session(session)
        assert summary["sample_count"] == 0
        assert "note" in summary  # explains no VR app was emitting metrics


class TestPersistence:
    def test_json_roundtrip(self, tmp_path):
        session = PerfSession(
            device="S", package="com.my.game", started_at="2026-07-12T12:00:00",
            duration_s=5, vrapi=[sample(i) for i in range(5)],
            memory=[{"elapsed_s": 0, "graphics_kb": 1, "total_pss_kb": 2}],
            thermal=[{"elapsed_s": 0, "status_label": "NONE", "max_skin_temp_c": 30.0}],
            events=[{"elapsed_s": 2, "kind": "gpu_level_change", "detail": "3 -> 5"}],
        )
        path = tmp_path / "session.json"
        save_session(session, str(path))
        loaded = load_session(str(path))
        assert loaded.package == "com.my.game"
        assert len(loaded.vrapi) == 5
        assert loaded.events[0]["kind"] == "gpu_level_change"
        # file is valid JSON with a schema marker
        raw = json.loads(path.read_text())
        assert raw["schema"] == "visor-perf-session/1"
