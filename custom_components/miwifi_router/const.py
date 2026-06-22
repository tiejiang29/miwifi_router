"""Constants for the MiWiFi Router integration."""

DOMAIN = "miwifi_router"

# Config entry fields
CONF_HOST = "host"
CONF_PASSWORD = "password"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_DEVICE_SCAN_INTERVAL = "device_scan_interval"
CONF_FORCE_HASH_ALGO = "force_hash_algo"  # Optional: "SHA1" | "SHA256" | None
CONF_SPEED_UNIT = "speed_unit"  # Optional: unit for speed sensors
CONF_TOTAL_UNIT = "total_unit"  # Optional: unit for total traffic sensors

# Speed unit options (for CONF_SPEED_UNIT)
# "auto" = use B/s as native unit (legacy v1.3.10 behavior, no conversion)
# Other values = use that unit as native_unit, value will be converted from bytes
SPEED_UNIT_AUTO = "auto"
SPEED_UNIT_OPTIONS: dict[str, str] = {
    "auto": "自动（B/s，不换算）",
    "B/s": "B/s（字节/秒）",
    "kB/s": "kB/s（千字节/秒，1000 进制）",
    "MB/s": "MB/s（兆字节/秒，1000 进制）",
    "GB/s": "GB/s（吉字节/秒，1000 进制）",
    "KiB/s": "KiB/s（千比字节/秒，1024 进制）",
    "MiB/s": "MiB/s（兆比字节/秒，1024 进制）",
    "GiB/s": "GiB/s（吉比字节/秒，1024 进制）",
}

# Total unit options (for CONF_TOTAL_UNIT)
# "auto" = use B as native unit (legacy v1.3.10 behavior, no conversion)
TOTAL_UNIT_AUTO = "auto"
TOTAL_UNIT_OPTIONS: dict[str, str] = {
    "auto": "自动（B，不换算）",
    "B": "B（字节）",
    "kB": "kB（千字节，1000 进制）",
    "MB": "MB（兆字节，1000 进制）",
    "GB": "GB（吉字节，1000 进制）",
    "TB": "TB（太字节，1000 进制）",
    "KiB": "KiB（千比字节，1024 进制）",
    "MiB": "MiB（兆比字节，1024 进制）",
    "GiB": "GiB（吉比字节，1024 进制）",
    "TiB": "TiB（太比字节，1024 进制）",
}

# Unit conversion factors (number of bytes per unit)
# 1000进制 (SI) and 1024进制 (IEC) both supported
SPEED_UNIT_FACTORS: dict[str, float] = {
    "B/s": 1.0,
    "kB/s": 1_000.0,
    "MB/s": 1_000_000.0,
    "GB/s": 1_000_000_000.0,
    "KiB/s": 1024.0,
    "MiB/s": 1024.0 * 1024.0,
    "GiB/s": 1024.0 * 1024.0 * 1024.0,
}

TOTAL_UNIT_FACTORS: dict[str, float] = {
    "B": 1.0,
    "kB": 1_000.0,
    "MB": 1_000_000.0,
    "GB": 1_000_000_000.0,
    "TB": 1_000_000_000_000.0,
    "KiB": 1024.0,
    "MiB": 1024.0 * 1024.0,
    "GiB": 1024.0 * 1024.0 * 1024.0,
    "TiB": 1024.0 * 1024.0 * 1024.0 * 1024.0,
}

# Default values
DEFAULT_SCAN_INTERVAL = 10  # seconds - for realtime data (speeds, counts)
DEFAULT_DEVICE_SCAN_INTERVAL = 30  # seconds - for device list details

# Per-device sensor configuration
CONF_TRACKED_DEVICES = "tracked_devices"  # dict: {mac: device_name}

# Router API endpoints
# Login endpoint - does NOT use stok, accessed directly
API_LOGIN = "/cgi-bin/luci/api/xqsystem/login"

# Logout endpoint - uses stok to end the session
API_LOGOUT = "/web/logout"

# Authenticated endpoints - accessed via /cgi-bin/luci/;stok=XXX{endpoint}
# These paths are based on diagnostic results from BE5000 (RD18) firmware 1.0.53
API_STATUS = "/api/misystem/status"              # Device speeds + WAN stats
API_DEVICE_LIST = "/api/xqsystem/device_list"    # Detailed device list
API_INIT_INFO = "/api/xqsystem/init_info"        # Router hardware/firmware info
API_NEWSTATUS = "/api/misystem/newstatus"        # Extended status with hardware info
API_WIFI_DETAIL = "/api/xqnetwork/wifi_detail_all"  # WiFi band details
API_SYSTEM_STATUS = "/api/xqsystem/status"       # System status with WAN statistics
API_REBOOT = "/api/xqsystem/reboot"               # Reboot router

# Login algorithm constants
PUBLIC_KEY = "a2ffa5c9be07488bbb04a3a47d3c5f6a"

# Device tracker attributes
DEVICE_ATTR_MAC = "mac"
DEVICE_ATTR_NAME = "devname"
DEVICE_ATTR_ONLINE = "online"
DEVICE_ATTR_UPSPEED = "upspeed"
DEVICE_ATTR_DOWNSPEED = "downspeed"
DEVICE_ATTR_UPLOAD = "upload"
DEVICE_ATTR_DOWNLOAD = "download"
DEVICE_ATTR_MAX_UPSPEED = "maxuploadspeed"
DEVICE_ATTR_MAX_DOWNSPEED = "maxdownloadspeed"
DEVICE_ATTR_ISAP = "isap"
DEVICE_ATTR_IP = "ip"
DEVICE_ATTR_AUTHED = "authority"

# Sensor types for router stats
SENSOR_TYPES = {
    "download_speed": {
        "name": "Download Speed",
        "native_unit_of_measurement": "B/s",
        "icon": "mdi:download",
        "device_class": None,
        "state_class": "measurement",
    },
    "upload_speed": {
        "name": "Upload Speed",
        "native_unit_of_measurement": "B/s",
        "icon": "mdi:upload",
        "device_class": None,
        "state_class": "measurement",
    },
    "download_total": {
        "name": "Download Total",
        "native_unit_of_measurement": "B",
        "icon": "mdi:download-circle",
        "device_class": None,
        "state_class": "total_increasing",
    },
    "upload_total": {
        "name": "Upload Total",
        "native_unit_of_measurement": "B",
        "icon": "mdi:upload-circle",
        "device_class": None,
        "state_class": "total_increasing",
    },
    "online_devices": {
        "name": "Online Devices",
        "native_unit_of_measurement": "devices",
        "icon": "mdi:devices",
        "device_class": None,
        "state_class": "measurement",
    },
    "cpu_load": {
        "name": "CPU Load",
        "native_unit_of_measurement": "%",
        "icon": "mdi:cpu-64-bit",
        "device_class": None,
        "state_class": "measurement",
    },
    "memory_usage": {
        "name": "Memory Usage",
        "native_unit_of_measurement": "%",
        "icon": "mdi:memory",
        "device_class": None,
        "state_class": "measurement",
    },
    "temperature": {
        "name": "Temperature",
        "native_unit_of_measurement": "°C",
        "icon": "mdi:thermometer",
        "device_class": "temperature",
        "state_class": "measurement",
    },
}
