"""Quest-aware ADB wrapper library — the core of visor."""

from visor.adb.apps import (
    clear_app_data,
    get_app_info,
    install_apk,
    launch_app,
    list_apps,
    stop_app,
    uninstall_app,
)
from visor.adb.capture import (
    screen_record,
    screenshot,
    start_cast,
    start_screen_stream,
    stop_stream_processes,
    undistort_eye,
)
from visor.adb.device import (
    connect_wireless,
    discover_devices,
    get_device_info,
    pair_device,
)
from visor.adb.bench import capture_trace, launch_benchmark
from visor.adb.diagnosis import diagnose_session, diff_sessions
from visor.adb.files import delete_file, list_files, pull_file, push_file
from visor.adb.perf_session import (
    LiveRecorder,
    PerfSession,
    load_session,
    record_session,
    save_session,
    summarize_session,
)
from visor.adb.vrapi import VrApiSample, parse_vrapi_line
from visor.adb.logs import crash_log, logcat_clear, logcat_dump, logcat_stream
from visor.adb.performance import (
    get_foreground_app,
    get_frame_timing,
    get_performance_snapshot,
    get_thermal_state,
    monitor_performance,
)
from visor.adb.quest_specific import (
    get_guardian_status,
    get_headset_sensors,
    get_tracking_status,
    set_refresh_rate,
)
from visor.adb.wrapper import (
    ADBCommandError,
    ADBNotFoundError,
    ADBWrapper,
    DeviceNotFoundError,
    DeviceOfflineError,
    DeviceUnauthorizedError,
    MultipleDevicesError,
    VisorError,
    get_adb,
)

__all__ = [
    # apps
    "clear_app_data",
    "get_app_info",
    "install_apk",
    "launch_app",
    "list_apps",
    "stop_app",
    "uninstall_app",
    # capture
    "screen_record",
    "screenshot",
    "start_cast",
    "start_screen_stream",
    "stop_stream_processes",
    "undistort_eye",
    # device
    "connect_wireless",
    "discover_devices",
    "get_device_info",
    "pair_device",
    # bench
    "capture_trace",
    "launch_benchmark",
    # diagnosis
    "diagnose_session",
    "diff_sessions",
    # files
    "delete_file",
    "list_files",
    "pull_file",
    "push_file",
    # perf_session
    "LiveRecorder",
    "PerfSession",
    "load_session",
    "record_session",
    "save_session",
    "summarize_session",
    # vrapi
    "VrApiSample",
    "parse_vrapi_line",
    # logs
    "crash_log",
    "logcat_clear",
    "logcat_dump",
    "logcat_stream",
    # performance
    "get_foreground_app",
    "get_frame_timing",
    "get_performance_snapshot",
    "get_thermal_state",
    "monitor_performance",
    # quest_specific
    "get_guardian_status",
    "get_headset_sensors",
    "get_tracking_status",
    "set_refresh_rate",
    # wrapper
    "ADBCommandError",
    "ADBNotFoundError",
    "ADBWrapper",
    "DeviceNotFoundError",
    "DeviceOfflineError",
    "DeviceUnauthorizedError",
    "MultipleDevicesError",
    "VisorError",
    "get_adb",
]
