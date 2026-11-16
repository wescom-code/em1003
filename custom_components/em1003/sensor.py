"""Sensor platform for EM1003 BLE Sensor integration."""
from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timedelta

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)

from .const import (
    DOMAIN,
    CONF_MAC_ADDRESS,
    SENSOR_ID_05,
    SENSOR_TYPES,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    WIFI_STATE_MAP,
    WIFI_STATE_DESC,
    CLOUD_STATE_MAP,
    CLOUD_STATE_DESC,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up EM1003 sensor based on a config entry."""
    data = hass.data[DOMAIN][config_entry.entry_id]
    mac_address = data[CONF_MAC_ADDRESS]
    device_name = data.get("device_name", config_entry.title)
    em1003_device = data["device"]

    _LOGGER.info("Setting up EM1003 sensors for device: %s (%s)", device_name, mac_address)

    # Get scan interval from options or use default
    scan_interval_seconds = config_entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    scan_interval = timedelta(seconds=scan_interval_seconds)
    _LOGGER.info("Using scan interval: %d seconds", scan_interval_seconds)

    # Create coordinator for updating sensor data
    coordinator = EM1003DataUpdateCoordinator(hass, em1003_device, mac_address, scan_interval)

    # Store coordinator in hass.data so switch can access it
    hass.data[DOMAIN][config_entry.entry_id]["coordinator"] = coordinator

    # Stagger initial refresh across devices to avoid startup contention
    init_delay = random.uniform(1.0, 4.0)
    _LOGGER.debug("Waiting %.1fs before initial refresh...", init_delay)
    await asyncio.sleep(init_delay)

    # Fetch initial data with better error handling
    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception as err:
        _LOGGER.warning(
            "Initial data fetch failed for %s, sensors will retry automatically: %s",
            mac_address,
            err
        )
        # Don't fail setup - let sensors show unavailable and retry later

    # Create sensor entities for each sensor type
    entities = []
    for sensor_id, sensor_info in SENSOR_TYPES.items():
        entities.append(
            EM1003Sensor(
                coordinator,
                config_entry,
                mac_address,
                device_name,
                sensor_id,
                sensor_info,
            )
        )

    # Wi-Fi and Cloud status sensors (disabled by default)
    entities.append(
        EM1003WifiStatusSensor(
            coordinator,
            config_entry,
            mac_address,
            device_name,
            em1003_device,
        )
    )
    entities.append(
        EM1003CloudStatusSensor(
            coordinator,
            config_entry,
            mac_address,
            device_name,
            em1003_device,
        )
    )

    async_add_entities(entities)


class EM1003DataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching EM1003 data from the device."""

    def __init__(
        self,
        hass: HomeAssistant,
        em1003_device,
        mac_address: str,
        update_interval: timedelta,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=f"EM1003 {mac_address}",
            update_interval=update_interval,
        )
        self.em1003_device = em1003_device
        self.mac_address = mac_address

    async def _async_update_data(self) -> dict:
        """Fetch data from the device."""
        try:
            _LOGGER.debug("Updating sensor data for %s", self.mac_address)
            data = await self.em1003_device.read_all_sensors()

            # Check if we actually received any valid data
            valid_count = sum(1 for k, v in data.items() if k in SENSOR_TYPES and v is not None)
            if valid_count == 0:
                raise UpdateFailed(
                    f"Failed to read any valid sensor data from {self.mac_address} - "
                    "device offline or all sensors unready"
                )

            _LOGGER.debug("Sensor data updated: %s (valid: %d/%d)", data, valid_count, len(data))

            # Log if problematic sensors have no data
            if data:
                for sensor_id in [0x11, 0x12, 0x13]:  # PM10, TVOC, eCO2
                    value = data.get(sensor_id)
                    if value is None:
                        sensor_info = SENSOR_TYPES.get(sensor_id, {})
                        sensor_name = sensor_info.get("name", f"0x{sensor_id:02x}")
                        _LOGGER.info("[%s] No data received", sensor_name)

            return data
        except Exception as err:
            raise UpdateFailed(f"Error communicating with device: {err}") from err


class EM1003Sensor(CoordinatorEntity, SensorEntity):
    """Representation of an EM1003 BLE sensor."""

    _attr_has_entity_name = True
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: EM1003DataUpdateCoordinator,
        config_entry: ConfigEntry,
        mac_address: str,
        device_name: str,
        sensor_id: int,
        sensor_info: dict,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._config_entry = config_entry
        self._mac_address = mac_address
        self._device_name = device_name
        self._sensor_id = sensor_id
        self._sensor_info = sensor_info

        # Set entity attributes
        self._attr_unique_id = f"{mac_address}_{sensor_info['key']}"
        self._attr_name = sensor_info["name"]
        self._attr_icon = sensor_info["icon"]
        self._attr_native_unit_of_measurement = sensor_info.get("unit")

        # Set device class if available - use explicit mapping with getattr for compatibility
        device_class_str = sensor_info.get("device_class")
        if device_class_str:
            # Map string device classes to SensorDeviceClass enum
            # Use getattr to handle older HA versions that may not have all device classes
            device_class_map = {
                "temperature": getattr(SensorDeviceClass, "TEMPERATURE", None),
                "humidity": getattr(SensorDeviceClass, "HUMIDITY", None),
                "pm25": getattr(SensorDeviceClass, "PM25", None),
                "pm10": getattr(SensorDeviceClass, "PM10", None),
                "carbon_dioxide": getattr(SensorDeviceClass, "CARBON_DIOXIDE", None),
                "volatile_organic_compounds": getattr(SensorDeviceClass, "VOLATILE_ORGANIC_COMPOUNDS", None),
                "battery": getattr(SensorDeviceClass, "BATTERY", None),
            }
            self._attr_device_class = device_class_map.get(device_class_str)
            if self._attr_device_class:
                _LOGGER.debug(
                    "Set device_class for sensor %s (0x%02x): %s",
                    sensor_info["name"],
                    sensor_id,
                    self._attr_device_class
                )
            elif device_class_str:
                _LOGGER.debug(
                    "Device class '%s' not available in this Home Assistant version for sensor %s (0x%02x)",
                    device_class_str,
                    sensor_info["name"],
                    sensor_id
                )

        # Set precision based on sensor type
        if sensor_id == 0x01:  # Temperature
            self._attr_suggested_display_precision = 2
        elif sensor_id == 0x06:  # Humidity
            self._attr_suggested_display_precision = 1
        elif sensor_id == 0x0A:  # Formaldehyde
            self._attr_suggested_display_precision = 3
        else:  # Other sensors (no decimals): PM2.5, PM10, Noise, TVOC, eCO2
            self._attr_suggested_display_precision = 0

        # Cache for last valid value and timestamp
        self._last_valid_value: float | None = None
        self._last_update_time: datetime | None = None

        # Stale threshold: 3 polling cycles, at least 3 minutes (180s)
        # Prevents transient glitches/reboots (e.g. 77s) from dropping to 0 or -40 on charts,
        # but ensures failed sensors become Unavailable within 3 minutes instead of masking failure.
        scan_interval_sec = coordinator.update_interval.total_seconds() if coordinator.update_interval else 60
        self._stale_threshold = timedelta(seconds=max(scan_interval_sec * 3, 180))

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information about this EM1003 device."""
        dev = getattr(self.coordinator, "em1003_device", None)
        return DeviceInfo(
            identifiers={(DOMAIN, self._mac_address)},
            name=self._device_name,
            manufacturer="720",
            model=getattr(dev, "model", "EM1003"),
            sw_version=getattr(dev, "firmware_version", None),
            serial_number=getattr(dev, "sn", None),
            connections={("mac", self._mac_address)},
        )

    @property
    def available(self) -> bool:
        """Return if entity is available.

        Consider sensor available if:
        1. Device is actively measuring (for environmental sensors). If running on battery
           and internal sensors are dormant, environmental entities are marked unavailable
           to prevent displaying frozen stale values.
        2. Coordinator has fresh data for this specific sensor, OR
        3. We have cached data that is within the 3-minute grace threshold.
        """
        # Battery sensor (0x05) is a device metric and remains available as long as coordinator updates
        if self._sensor_id != SENSOR_ID_05:
            dev = getattr(self.coordinator, "em1003_device", None)
            if dev and not dev.is_measuring:
                return False

        # Check if we have fresh data from coordinator
        if self.coordinator.last_update_success:
            if self.coordinator.data is not None:
                current_value = self.coordinator.data.get(self._sensor_id)
                if current_value is not None:
                    return True

        # Check if we have recent cached data within the grace period
        if self._last_valid_value is not None and self._last_update_time is not None:
            time_since_update = datetime.now() - self._last_update_time
            if time_since_update < self._stale_threshold:
                return True

        # No fresh or valid cached data available
        return False

    @property
    def native_value(self) -> float | None:
        """Return the state of the sensor."""
        # For environmental sensors, if device is not measuring, do not return cached/stale values
        if self._sensor_id != SENSOR_ID_05:
            dev = getattr(self.coordinator, "em1003_device", None)
            if dev and not dev.is_measuring:
                return None

        # Get current value from coordinator
        current_value = None
        if self.coordinator.data is not None:
            current_value = self.coordinator.data.get(self._sensor_id)

        # If we have a valid new value, update cache
        if current_value is not None:
            self._last_valid_value = current_value
            self._last_update_time = datetime.now()

            _LOGGER.debug(
                "[VALUE] %s (0x%02x): Fresh value = %s",
                self._sensor_info["name"],
                self._sensor_id,
                current_value
            )
            return current_value

        # No new data available, check if we should use cached value within grace period
        if self._last_valid_value is not None and self._last_update_time is not None:
            time_since_update = datetime.now() - self._last_update_time

            if time_since_update < self._stale_threshold:
                _LOGGER.debug(
                    "[VALUE] %s (0x%02x): Using cached value %s (age: %d seconds)",
                    self._sensor_info["name"],
                    self._sensor_id,
                    self._last_valid_value,
                    int(time_since_update.total_seconds())
                )
                return self._last_valid_value
            else:
                _LOGGER.warning(
                    "[VALUE] %s (0x%02x): Data stale for %d seconds (> %d s threshold), returning None",
                    self._sensor_info["name"],
                    self._sensor_id,
                    int(time_since_update.total_seconds()),
                    int(self._stale_threshold.total_seconds())
                )

        # No valid data available
        _LOGGER.debug(
            "[VALUE] %s (0x%02x): No data available (coordinator_data=%s, cached=%s)",
            self._sensor_info["name"],
            self._sensor_id,
            self.coordinator.data is not None,
            self._last_valid_value is not None
        )
        return None

    @property
    def extra_state_attributes(self) -> dict:
        """Return additional attributes."""
        attrs = {
            "sensor_id": f"0x{self._sensor_id:02x}",
            "note": self._sensor_info.get("note", "Unknown sensor type"),
            "mac_address": self._mac_address,
        }

        # Add last update information and data freshness indicator
        if self._last_update_time is not None:
            attrs["last_update"] = self._last_update_time.isoformat()
            time_since_update = datetime.now() - self._last_update_time
            attrs["data_age_seconds"] = int(time_since_update.total_seconds())
            is_stale = (
                self.coordinator.data is None
                or self.coordinator.data.get(self._sensor_id) is None
            )
            attrs["data_source"] = "cached" if is_stale else "live"

        if self._sensor_id == 0x05:
            charging_state = getattr(self.coordinator.em1003_device, "battery_charging", None)
            if charging_state is not None:
                attrs["is_charging"] = charging_state

        return attrs


class EM1003WifiStatusSensor(CoordinatorEntity, SensorEntity):
    """Representation of an EM1003 Wi-Fi status sensor."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:wifi"
    _attr_entity_registry_enabled_default = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: EM1003DataUpdateCoordinator,
        config_entry: ConfigEntry,
        mac_address: str,
        device_name: str,
        em1003_device,
    ) -> None:
        """Initialize the Wi-Fi status sensor."""
        super().__init__(coordinator)
        self._config_entry = config_entry
        self._mac_address = mac_address
        self._device_name = device_name
        self._em1003_device = em1003_device

        self._attr_unique_id = f"{mac_address}_wifi_status"
        self._attr_name = "Wi-Fi Status"

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information about this EM1003 device."""
        dev = getattr(self.coordinator, "em1003_device", None)
        return DeviceInfo(
            identifiers={(DOMAIN, self._mac_address)},
            name=self._device_name,
            manufacturer="720",
            model=getattr(dev, "model", "EM1003"),
            sw_version=getattr(dev, "firmware_version", None),
            serial_number=getattr(dev, "sn", None),
            connections={("mac", self._mac_address)},
        )

    @property
    def native_value(self) -> str | None:
        """Return the state of the Wi-Fi connection."""
        state_code = None
        if self.coordinator.data and "wifi_state" in self.coordinator.data:
            state_code = self.coordinator.data.get("wifi_state")
        if state_code is None and self._em1003_device.wifi_state is not None:
            state_code = self._em1003_device.wifi_state

        if state_code is None:
            return None
        return WIFI_STATE_MAP.get(state_code, f"unknown_{state_code}")

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return self.coordinator.last_update_success and self.native_value is not None

    @property
    def extra_state_attributes(self) -> dict:
        """Return additional attributes."""
        attrs = {
            "mac_address": self._mac_address,
        }
        state_code = None
        if self.coordinator.data and "wifi_state" in self.coordinator.data:
            state_code = self.coordinator.data.get("wifi_state")
        if state_code is None:
            state_code = self._em1003_device.wifi_state

        if state_code is not None:
            attrs["status_code"] = state_code
            attrs["description"] = WIFI_STATE_DESC.get(state_code, "未知")

        wifi_sum = None
        current_index = None
        if self.coordinator.data:
            wifi_sum = self.coordinator.data.get("wifi_sum_number")
            current_index = self.coordinator.data.get("current_wifi_index")
        if wifi_sum is None:
            wifi_sum = self._em1003_device.wifi_sum_number
        if current_index is None:
            current_index = self._em1003_device.current_wifi_index

        if wifi_sum is not None:
            attrs["configured_profiles"] = wifi_sum
        if current_index is not None:
            attrs["active_profile_index"] = current_index

        if self._em1003_device.sn:
            attrs["serial_number"] = self._em1003_device.sn
        if self._em1003_device.place_id:
            attrs["place_id"] = self._em1003_device.place_id
        if self._em1003_device.model:
            attrs["model"] = self._em1003_device.model
        if self._em1003_device.wifi_version:
            attrs["wifi_firmware_version"] = self._em1003_device.wifi_version
        if self._em1003_device.ble_version:
            attrs["ble_firmware_version"] = self._em1003_device.ble_version

        return attrs


class EM1003CloudStatusSensor(CoordinatorEntity, SensorEntity):
    """Representation of an EM1003 Cloud reporting status sensor."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:cloud-upload"
    _attr_entity_registry_enabled_default = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: EM1003DataUpdateCoordinator,
        config_entry: ConfigEntry,
        mac_address: str,
        device_name: str,
        em1003_device,
    ) -> None:
        """Initialize the Cloud status sensor."""
        super().__init__(coordinator)
        self._config_entry = config_entry
        self._mac_address = mac_address
        self._device_name = device_name
        self._em1003_device = em1003_device

        self._attr_unique_id = f"{mac_address}_cloud_status"
        self._attr_name = "Cloud Status"

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information about this EM1003 device."""
        dev = getattr(self.coordinator, "em1003_device", None)
        return DeviceInfo(
            identifiers={(DOMAIN, self._mac_address)},
            name=self._device_name,
            manufacturer="720",
            model=getattr(dev, "model", "EM1003"),
            sw_version=getattr(dev, "firmware_version", None),
            serial_number=getattr(dev, "sn", None),
            connections={("mac", self._mac_address)},
        )

    @property
    def native_value(self) -> str | None:
        """Return the state of cloud reporting."""
        cloud_code = None
        if self.coordinator.data and "cloud_state" in self.coordinator.data:
            cloud_code = self.coordinator.data.get("cloud_state")
        if cloud_code is None and self._em1003_device.cloud_state is not None:
            cloud_code = self._em1003_device.cloud_state

        if cloud_code is None:
            return None
        return CLOUD_STATE_MAP.get(cloud_code, f"unknown_{cloud_code}")

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return self.coordinator.last_update_success and self.native_value is not None

    @property
    def extra_state_attributes(self) -> dict:
        """Return additional attributes."""
        attrs = {
            "mac_address": self._mac_address,
        }
        cloud_code = None
        if self.coordinator.data and "cloud_state" in self.coordinator.data:
            cloud_code = self.coordinator.data.get("cloud_state")
        if cloud_code is None:
            cloud_code = self._em1003_device.cloud_state

        if cloud_code is not None:
            attrs["status_code"] = cloud_code
            attrs["description"] = CLOUD_STATE_DESC.get(cloud_code, "未知")

        if self._em1003_device.sn:
            attrs["serial_number"] = self._em1003_device.sn
        if self._em1003_device.place_id:
            attrs["place_id"] = self._em1003_device.place_id
        if self._em1003_device.model:
            attrs["model"] = self._em1003_device.model
        if self._em1003_device.wifi_version:
            attrs["wifi_firmware_version"] = self._em1003_device.wifi_version
        if self._em1003_device.ble_version:
            attrs["ble_firmware_version"] = self._em1003_device.ble_version

        return attrs
