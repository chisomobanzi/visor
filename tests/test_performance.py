"""Tests for performance monitoring and Quest-specific features."""

from unittest import mock

import pytest

from visor.adb.performance import (
    get_frame_timing,
    get_performance_snapshot,
    get_thermal_state,
)
from visor.adb.quest_specific import (
    get_guardian_status,
    get_tracking_status,
    set_refresh_rate,
)

THERMALSERVICE = """Thermal Status: 0
Cached temperatures:
\tTemperature{mValue=31.706001, mType=3, mName=soc-usr, mStatus=0}
\tTemperature{mValue=41.5, mType=3, mName=pcb1-usr, mStatus=0}
\tTemperature{mValue=25.000002, mType=2, mName=battery, mStatus=0}
"""

TOP_OUTPUT = """Tasks: 412 total,   1 running, 411 sleeping,   0 stopped,   0 zombie
  Mem:      7.4G total,      6.9G used,      500M free,       18M buffers
 Swap:      2.0G total,      1.1G used,      900M free,      3.4G cached
800%cpu  42%user   8%nice  30%sys 700%idle   0%iow  15%irq   5%sirq   0%host
  PID USER         PR  NI VIRT  RES  SHR S[%CPU] %MEM     TIME+ ARGS
 1234 u0_a123      20   0  28G  1.1G 400M S  85.3   14.8  12:34.56 com.mycompany.mygame
  800 system       18  -2  20G  800M 300M S  12.0   10.5 123:45.67 system_server
"""

BATTERY_CURRENT = "-512000\n"  # microamps, negative = discharging

GFXINFO = """Applications Graphics Acceleration Info:
** Graphics info for pid 1234 [com.mycompany.mygame] **

Total frames rendered: 12345
Janky frames: 123 (1.00%)
50th percentile: 11ms
90th percentile: 13ms
95th percentile: 14ms
99th percentile: 22ms
"""


@pytest.fixture
def adb():
    return mock.Mock()


def wire(adb, responses):
    def fake_shell(cmd, device_id=None, timeout=None):
        for key, value in responses.items():
            if key in cmd:
                return value
        return ""

    adb.shell.side_effect = fake_shell


class TestThermal:
    def test_flags_sensors_approaching_throttle(self, adb):
        wire(adb, {"thermalservice": THERMALSERVICE})
        state = get_thermal_state("S", adb=adb)
        assert state["status_label"] == "NONE"
        hot = {s["name"]: s for s in state["sensors"]}
        assert hot["pcb1-usr"]["warning"] is True  # 41.5C > 40C warn threshold
        assert hot["soc-usr"]["warning"] is False
        assert state["max_skin_temp_c"] == 41.5


class TestSnapshot:
    def test_snapshot_structure(self, adb):
        wire(
            adb,
            {
                "thermalservice": THERMALSERVICE,
                "top -b": TOP_OUTPUT,
                "current_now": BATTERY_CURRENT,
                "dumpsys battery": "  level: 55\n  AC powered: false\n  temperature: 280\n",
                "ResumedActivity": "  ResumedActivity: ActivityRecord{f006272 u0 com.mycompany.mygame/.MainActivity t4104}\n",
            },
        )
        snap = get_performance_snapshot("S", adb=adb)
        assert snap["cpu"]["usage_percent"] == pytest.approx(12.5)  # 100 - 700/8
        assert snap["memory"]["total"] == "7.4G"
        assert snap["memory"]["used"] == "6.9G"
        assert snap["battery"]["level"] == 55
        assert snap["battery"]["current_ma"] == pytest.approx(-512.0)
        assert snap["foreground_app"] == "com.mycompany.mygame"
        assert snap["thermal"]["status_label"] == "NONE"
        top_procs = snap["top_processes"]
        assert top_procs[0]["name"] == "com.mycompany.mygame"
        assert top_procs[0]["cpu_percent"] == 85.3


class TestFrameTiming:
    def test_parses_gfxinfo_percentiles(self, adb):
        wire(adb, {"gfxinfo": GFXINFO})
        ft = get_frame_timing("S", "com.mycompany.mygame", adb=adb)
        assert ft["total_frames"] == 12345
        assert ft["janky_frames"] == 123
        assert ft["janky_percent"] == 1.0
        assert ft["percentiles_ms"]["p90"] == 13
        assert ft["source"] == "gfxinfo"


class TestQuestSpecific:
    def test_guardian_status(self, adb):
        wire(adb, {"GuardianState": "GuardianState: 2\n"})
        status = get_guardian_status("S", adb=adb)
        assert isinstance(status, dict)

    def test_set_refresh_rate_validates_rate(self, adb):
        wire(adb, {"ro.product.device": "eureka\n"})
        with pytest.raises(ValueError, match="72"):
            set_refresh_rate("S", 45, adb=adb)

    def test_set_refresh_rate_sets_prop(self, adb):
        wire(adb, {"ro.product.device": "eureka\n"})
        result = set_refresh_rate("S", 90, adb=adb)
        assert result["rate"] == 90
        set_cmd = [
            c.args[0] for c in adb.shell.call_args_list if "setprop" in c.args[0]
        ]
        assert any("debug.oculus.refreshRate 90" in c for c in set_cmd)

    def test_tracking_status_returns_dict(self, adb):
        adb.shell.return_value = ""
        status = get_tracking_status("S", adb=adb)
        assert "hand_tracking" in status

    def test_tracking_status_reads_registered_services(self, adb):
        wire(
            adb,
            {
                "dumpsys -l": (
                    "  oculus.internal.tracking.IHandTrackingService/default\n"
                    "  oculus.internal.tracking.IControllerTrackingService/default\n"
                ),
            },
        )
        status = get_tracking_status("S", adb=adb)
        assert status["hand_tracking"] == "service registered"
        assert status["eye_tracking"] == "service not registered"

    def test_headset_sensors_from_grepped_types(self, adb):
        from visor.adb.quest_specific import get_headset_sensors

        wire(
            adb,
            {
                "sensorservice": (
                    "android.sensor.accelerometer\n"
                    "oculus.sensor.doubletap\n"
                    "oculus.sensor.hmd_imu.2\n"
                ),
            },
        )
        sensors = get_headset_sensors("S", adb=adb)
        types = [s["type"] for s in sensors]
        assert "oculus.sensor.hmd_imu.2" in types
        assert len(sensors) == 3
