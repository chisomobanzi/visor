"""Quest model info, thermal thresholds, and other constants."""

# Product codename (ro.product.device / `adb devices -l` product:) -> marketing name
QUEST_PRODUCTS = {
    "monterey": "Quest",
    "hollywood": "Quest 2",
    "eureka": "Quest 3",
    "panther": "Quest 3S",
    "seacliff": "Quest Pro",
    "pacific": "Oculus Go",
}

# Supported refresh rates per product codename
REFRESH_RATES = {
    "monterey": [72],
    "hollywood": [60, 72, 90, 120],
    "eureka": [72, 80, 90, 120],
    "panther": [72, 80, 90, 120],
    "seacliff": [72, 90],
}

# Android ThermalStatus enum (dumpsys thermalservice "Thermal Status: N")
THERMAL_STATUS_LABELS = {
    0: "NONE",
    1: "LIGHT",
    2: "MODERATE",
    3: "SEVERE",
    4: "CRITICAL",
    5: "EMERGENCY",
    6: "SHUTDOWN",
}

# Android Temperature.TYPE_* enum (mType in thermalservice output)
TEMPERATURE_TYPES = {
    0: "CPU",
    1: "GPU",
    2: "BATTERY",
    3: "SKIN",
    4: "USB_PORT",
    5: "POWER_AMPLIFIER",
    6: "BCL_VOLTAGE",
    7: "BCL_CURRENT",
    8: "BCL_PERCENTAGE",
    9: "NPU",
    10: "TPU",
    11: "DISPLAY",
    12: "MODEM",
    13: "SOC",
    14: "WIFI",
    15: "CAMERA",
    16: "NFC",
    17: "SENSOR",
}

# Temperature types that are actual temperatures in Celsius (BCL types are
# voltage/current trip levels and would pollute thermal readings)
CELSIUS_TEMPERATURE_TYPES = {0, 1, 2, 3, 4, 5, 9, 10, 11, 12, 13, 14, 15, 16, 17}

# Approximate skin-sensor temps (C) at which Quest starts throttling / shuts down.
# Quest throttles CPU/GPU levels well before hard limits; warn early.
THERMAL_WARN_C = 40.0
THERMAL_CRITICAL_C = 46.0

# Android screenrecord hard limit
MAX_SCREENRECORD_SECONDS = 180

DEFAULT_WIRELESS_PORT = 5555

# Packages that are part of the OS image; used to identify "sideloaded" apps
SYSTEM_PACKAGE_PREFIXES = (
    "com.oculus.",
    "com.meta.",
    "com.facebook.",
    "com.android.",
    "android",
    "com.qualcomm.",
    "oculus.",
    "com.qti.",
    "com.google.android.",
    "com.whatsapp",  # preinstalled on Quest 3
)

LOG_LEVELS = ["V", "D", "I", "W", "E", "F"]
