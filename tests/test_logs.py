"""Tests for logcat dump/stream/crash extraction."""

from unittest import mock

import pytest

from visor.adb.logs import crash_log, logcat_clear, logcat_dump

LOGCAT_OUTPUT = """--------- beginning of main
07-10 14:23:44.100  1234  5678 I Unity   : Loading scene 'Main'
07-10 14:23:45.123  1234  5678 W Unity   : Missing reference on GameObject 'Player'
07-10 14:23:46.500  1234  5678 E Unity   : NullReferenceException: Object reference not set to an instance of an object
07-10 14:23:46.501  1234  5678 E Unity   :   at GameManager.Update () [0x00000] in <filename unknown>:0
07-10 14:23:47.000   800   900 W VrApi   : FPS=72,Prd=32ms,Tear=0,Early=0
"""

DROPBOX_CRASH = """Drop box contents: 2 entries
========================================
2026-07-10 14:23:46 data_app_crash (text, 4056 bytes)
Process: com.mycompany.mygame
PID: 1234
Flags: 0x30c8be44
Package: com.mycompany.mygame v42 (1.3.7)
Build: oculus/eureka/eureka:14/UP1A.231005.007.A1

java.lang.RuntimeException: Unable to resume activity
\tat android.app.ActivityThread.performResumeActivity(ActivityThread.java:4200)
\tat android.app.ActivityThread.handleResumeActivity(ActivityThread.java:4240)
Caused by: java.lang.NullPointerException: Attempt to invoke virtual method
\tat com.mycompany.mygame.MainActivity.onResume(MainActivity.java:55)
"""


@pytest.fixture
def adb():
    return mock.Mock()


class TestLogcatDump:
    def test_returns_structured_entries(self, adb):
        adb.run.return_value = LOGCAT_OUTPUT
        entries = logcat_dump("S", adb=adb)
        assert len(entries) == 5  # marker line skipped
        assert entries[0].tag == "Unity"
        assert entries[2].level == "E"

    def test_level_filter_passed_to_logcat(self, adb):
        adb.run.return_value = ""
        logcat_dump("S", level="E", adb=adb)
        args = adb.run.call_args.args[0]
        assert "*:E" in args

    def test_tag_filter(self, adb):
        adb.run.return_value = ""
        logcat_dump("S", tag="Unity", level="D", adb=adb)
        args = adb.run.call_args.args[0]
        assert "Unity:D" in args and "*:S" in args

    def test_pattern_filter_applied_client_side(self, adb):
        adb.run.return_value = LOGCAT_OUTPUT
        entries = logcat_dump("S", pattern="NullReference", adb=adb)
        assert len(entries) == 1
        assert "NullReferenceException" in entries[0].message

    def test_clear(self, adb):
        logcat_clear("S", adb=adb)
        assert "-c" in adb.run.call_args.args[0]


class TestCrashLog:
    def test_extracts_crash_for_package(self, adb):
        adb.shell.return_value = DROPBOX_CRASH
        crashes = crash_log("S", "com.mycompany.mygame", adb=adb)
        assert len(crashes) >= 1
        crash = crashes[0]
        assert crash.package == "com.mycompany.mygame"
        assert crash.crash_type == "data_app_crash"
        assert crash.timestamp == "2026-07-10 14:23:46"
        assert "NullPointerException" in crash.stack_trace
        assert "MainActivity.onResume" in crash.stack_trace

    def test_no_crashes_returns_empty(self, adb):
        adb.shell.return_value = "Drop box contents: 0 entries\n"
        assert crash_log("S", "com.mycompany.mygame", adb=adb) == []
