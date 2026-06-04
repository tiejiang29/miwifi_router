"""Constants for the MiWiFi Router integration."""

DOMAIN = "miwifi_router"

# Config entry fields
CONF_HOST = "host"
CONF_PASSWORD = "password"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_DEVICE_SCAN_INTERVAL = "device_scan_interval"

# Default values
DEFAULT_SCAN_INTERVAL = 10  # seconds - for realtime data (speeds, counts)
DEFAULT_DEVICE_SCAN_INTERVAL = 30  # seconds - for device list details

# Router API endpoints
API_LOGIN = "/cgi-bin/luci/api/xqsystem/login"
API_STATUS = "/api/misystem/status"
API_DEVICE_LIST = "/api/misystem/device_list"
API_INIT_INFO = "/api/misystem/init_info"
API_NEWSTATUS = "/api/misystem/newstatus"
API_WIFI_DETAIL = "/api/misystem/wifi_detail_all"
API_SYSTEM_STATUS = "/api/misystem/status"

# Stok cache duration (seconds)
STOK_CACHE_SECONDS = 600  # 10 minutes

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
