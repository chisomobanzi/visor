"""Tests for launch benchmarking and perfetto trace capture."""

from unittest import mock

import pytest

from visor.adb.bench import capture_trace, launch_benchmark, parse_am_start
from visor.adb.wrapper import ADBCommandError

AM_START_OK = """Stopping: com.speakeasy3d.voyager
Starting: Intent { act=android.intent.action.MAIN cmp=com.speakeasy3d.voyager/com.epicgames.unreal.GameActivity }
Status: ok
LaunchState: COLD
Activity: com.speakeasy3d.voyager/com.epicgames.unreal.GameActivity
TotalTime: 2345
WaitTime: 2352
Complete
"""

AM_START_ERROR = """Starting: Intent { cmp=com.nope/.Main }
Error type 3
Error: Activity class {com.nope/.Main} does not exist.
"""


@pytest.fixture
def adb():
    return mock.Mock(adb_path="/usr/bin/adb")


def wire(adb, responses):
    def fake_shell(cmd, device_id=None, timeout=None):
        for key, value in responses.items():
            if key in cmd:
                return value
        return ""
    adb.shell.side_effect = fake_shell


class TestParseAmStart:
    def test_ok(self):
        r = parse_am_start(AM_START_OK)
        assert r == {"status": "ok", "launch_state": "COLD",
                     "total_time_ms": 2345, "wait_time_ms": 2352}

    def test_error(self):
        r = parse_am_start(AM_START_ERROR)
        assert r["status"] == "error"
        assert "does not exist" in r["error"]


class TestLaunchBenchmark:
    def test_runs_and_stats(self, adb):
        wire(adb, {
            "resolve-activity": "com.speakeasy3d.voyager/com.epicgames.unreal.GameActivity\n",
            "am start": AM_START_OK,
            "am force-stop": "",
        })
        result = launch_benchmark("S", "com.speakeasy3d.voyager", runs=3, adb=adb)
        assert result["runs_completed"] == 3
        assert result["total_time_ms"]["median"] == 2345
        assert result["launch_state"] == "COLD"
        # -S in the am start command forces a cold start each run
        start_cmds = [c.args[0] for c in adb.shell.call_args_list if "am start" in c.args[0]]
        assert all("-S" in c and "-W" in c for c in start_cmds)

    def test_unresolvable_package_raises(self, adb):
        wire(adb, {"resolve-activity": "No activity found\n"})
        with pytest.raises(ADBCommandError, match="[Nn]o launchable"):
            launch_benchmark("S", "com.nope", adb=adb)

    def test_failed_runs_reported_not_fatal(self, adb):
        wire(adb, {
            "resolve-activity": "com.x/.Main\n",
            "am start": AM_START_ERROR,
        })
        result = launch_benchmark("S", "com.x", runs=2, adb=adb)
        assert result["runs_completed"] == 0
        assert result["runs_failed"] == 2


class TestCaptureTrace:
    def test_trace_command_and_pull(self, adb, tmp_path):
        adb.shell.return_value = ""
        adb.run.return_value = "1 file pulled\n"
        out = tmp_path / "trace.pftrace"
        result = capture_trace("S", duration_s=5, save_path=str(out), adb=adb)
        shell_cmd = adb.shell.call_args_list[0].args[0]
        assert "perfetto" in shell_cmd
        assert "-t 5s" in shell_cmd
        assert "sched" in shell_cmd  # default categories include scheduling
        pull_args = [c for c in adb.run.call_args_list if c.args[0][0] == "pull"]
        assert pull_args, "trace file must be pulled"
        assert result.path == str(out)
        assert "ui.perfetto.dev" in result.message
