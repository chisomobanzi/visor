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
from visor.adb.capture import screen_record, screenshot, start_cast
from visor.adb.device import (
    connect_wireless,
    discover_devices,
    get_device_info,
    pair_device,
)
from visor.adb.files import delete_file, list_files, pull_file, push_file
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

__all__ = [name for name in dir() if not name.startswith("_")]
