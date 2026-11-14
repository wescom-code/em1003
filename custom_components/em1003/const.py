"""Constants for the EM1003 BLE Sensor integration."""

DOMAIN = "em1003"
CONF_MAC_ADDRESS = "mac_address"

# Service names
SERVICE_SCAN_DEVICE = "scan_device"
SERVICE_READ_CHARACTERISTIC = "read_characteristic"
SERVICE_WRITE_CHARACTERISTIC = "write_characteristic"
SERVICE_LIST_SERVICES = "list_services"
SERVICE_DISCOVER_ALL = "discover_all"
SERVICE_READ_DEVICE_NAME = "read_device_name"

# Attribute names
ATTR_MAC_ADDRESS = "mac_address"
ATTR_SERVICE_UUID = "service_uuid"
ATTR_CHARACTERISTIC_UUID = "characteristic_uuid"
ATTR_DATA = "data"

# BLE UUIDs
# Standard BLE GATT Characteristics
DEVICE_NAME_UUID = "00002A00-0000-1000-8000-00805F9B34FB"  # Device Name characteristic

# EM1003 Custom Service and Characteristics
EM1003_SERVICE_UUID = "09de2880-1415-4e2c-a48a-3938e3288537"  # Unknown service for sensor data
EM1003_WRITE_CHAR_UUID = "0000FFF1-0000-1000-8000-00805F9B34FB"  # Write characteristic (0xFFF1)
EM1003_NOTIFY_CHAR_UUID = "0000FFF4-0000-1000-8000-00805F9B34FB"  # Notify characteristic (0xFFF4)

# Protocol Commands
CMD_READ_SENSOR = 0x06  # Command to read sensor data
CMD_BUZZER = 0x50  # Command for buzzer control
CMD_BUZZER_SET_RESPONSE = 0x05  # Response command for buzzer set operations
CMD_READ_WIFI_CURRENT_STATE = 0x19  # 25: Read WiFi & Cloud status
CMD_READ_USB_STATE = 0x26  # 38: Read USB power status
CMD_ACTIVATE_WIFI = 0x2E  # 46: Activate WiFi profile

# WiFi and Cloud status mappings
WIFI_STATE_MAP = {
    0: "idle",
    1: "connecting",
    2: "wrong_password",
    3: "ssid_not_found",
    4: "connection_failed",
    5: "connected",
    6: "timeout",
}

WIFI_STATE_DESC = {
    0: "闲置",
    1: "正在连接",
    2: "密码错误",
    3: "未找到该名称wifi",
    4: "连接失败",
    5: "连接成功",
    6: "连接超时",
}

CLOUD_STATE_MAP = {
    -1: "unknown",
    0: "disconnected",
    1: "connected",
    101: "file_not_found",
    102: "data_source_error",
}

CLOUD_STATE_DESC = {
    -1: "网络情况未知",
    0: "网络到云端不通",
    1: "网络连接成功",
    101: "自动监测文件不存在",
    102: "数据源错误",
}

# Buzzer states
BUZZER_OFF = 0x00  # Buzzer off
BUZZER_ON = 0x01  # Buzzer on

# Sensor IDs (all confirmed through testing)
SENSOR_ID_05 = 0x05  # ✓ Confirmed: Battery Level (%) and Charging Status
SENSOR_ID_01 = 0x01  # ✓ Confirmed: Temperature (°C) - formula: (raw - 4000) / 100
SENSOR_ID_06 = 0x06  # ✓ Confirmed: Humidity (%) - formula: raw / 100
SENSOR_ID_08 = 0x08  # ✓ Confirmed: Noise Level (dB)
SENSOR_ID_09 = 0x09  # ✓ Confirmed: PM2.5 (µg/m³)
SENSOR_ID_0A = 0x0A  # ✓ Confirmed: Formaldehyde (mg/m³) - formula: (raw - 16384) / 1000
SENSOR_ID_11 = 0x11  # ✓ Confirmed: PM10 (µg/m³)
SENSOR_ID_12 = 0x12  # ✓ Confirmed: TVOC (mg/m³) - formula: raw × 0.001
SENSOR_ID_13 = 0x13  # ✓ Confirmed: eCO2 (ppm)

# Sensor definitions
SENSOR_TYPES = {
    SENSOR_ID_05: {
        "id": SENSOR_ID_05,
        "name": "Battery",
        "key": "battery",
        "icon": "mdi:battery",
        "device_class": "battery",
        "unit": "%",
        "note": "Confirmed: Battery level percentage (0-100%)",
    },
    SENSOR_ID_01: {
        "id": SENSOR_ID_01,
        "name": "Temperature",
        "key": "temperature",
        "icon": "mdi:thermometer",
        "device_class": "temperature",
        "unit": "°C",
        "note": "Confirmed: Temperature with offset encoding - formula: (raw - 4000) / 100",
    },
    SENSOR_ID_06: {
        "id": SENSOR_ID_06,
        "name": "Humidity",
        "key": "humidity",
        "icon": "mdi:water-percent",
        "device_class": "humidity",
        "unit": "%",
        "note": "Confirmed: Relative humidity - formula: raw / 100",
    },
    SENSOR_ID_08: {
        "id": SENSOR_ID_08,
        "name": "Noise Level",
        "key": "noise",
        "icon": "mdi:volume-high",
        "device_class": None,
        "unit": "dB",
        "note": "Confirmed: Noise level in decibels",
    },
    SENSOR_ID_09: {
        "id": SENSOR_ID_09,
        "name": "PM2.5",
        "key": "pm25",
        "icon": "mdi:air-filter",
        "device_class": "pm25",
        "unit": "µg/m³",
        "note": "Confirmed: Particulate Matter 2.5µm",
    },
    SENSOR_ID_0A: {
        "id": SENSOR_ID_0A,
        "name": "Formaldehyde",
        "key": "formaldehyde",
        "icon": "mdi:chemical-weapon",
        "device_class": None,
        "unit": "mg/m³",
        "note": "Confirmed: Formaldehyde (HCHO) with offset - formula: (raw - 16384) / 1000",
    },
    SENSOR_ID_11: {
        "id": SENSOR_ID_11,
        "name": "PM10",
        "key": "pm10",
        "icon": "mdi:air-filter",
        "device_class": "pm10",
        "unit": "µg/m³",
        "note": "Confirmed: Particulate Matter 10µm",
    },
    SENSOR_ID_12: {
        "id": SENSOR_ID_12,
        "name": "TVOC",
        "key": "tvoc",
        "icon": "mdi:molecule",
        "device_class": "volatile_organic_compounds",
        "unit": "µg/m³",
        "note": "Confirmed: Total Volatile Organic Compounds (raw value is µg/m³)",
    },
    SENSOR_ID_13: {
        "id": SENSOR_ID_13,
        "name": "eCO2",
        "key": "eco2",
        "icon": "mdi:molecule-co2",
        "device_class": "carbon_dioxide",
        "unit": "ppm",
        "note": "Confirmed: Equivalent CO2",
    },
}

# Configuration keys
CONF_SCAN_INTERVAL = "scan_interval"

# Default values
DEFAULT_NAME = "EM1003"
DEFAULT_SCAN_INTERVAL = 60  # Default polling interval in seconds
DEVICE_TIMEOUT = 30.0

# Version
VERSION = "1.0.4"
