"""Binary sensor platform for EM1003 BLE Sensor integration."""
from __future__ import annotations

import asyncio
import logging

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, CONF_MAC_ADDRESS

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up EM1003 binary sensor based on a config entry."""
    data = hass.data[DOMAIN][config_entry.entry_id]
    mac_address = data[CONF_MAC_ADDRESS]
    device_name = data.get("device_name", config_entry.title)
    em1003_device = data["device"]

    coordinator = data.get("coordinator")
    if coordinator is None:
        # Wait up to 5 seconds for coordinator to be created by sensor platform
        for _ in range(50):
            await asyncio.sleep(0.1)
            coordinator = data.get("coordinator")
            if coordinator is not None:
                break

    if coordinator is None:
        _LOGGER.error("Coordinator not found for %s, cannot set up binary sensors", mac_address)
        return

    _LOGGER.info("Setting up EM1003 binary sensors for device: %s (%s)", device_name, mac_address)

    async_add_entities([
        EM1003BatteryChargingBinarySensor(
            coordinator,
            config_entry,
            mac_address,
            device_name,
            em1003_device,
        )
    ])


class EM1003BatteryChargingBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """Binary sensor for EM1003 battery charging state."""

    _attr_device_class = BinarySensorDeviceClass.BATTERY_CHARGING
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator,
        config_entry: ConfigEntry,
        mac_address: str,
        device_name: str,
        em1003_device,
    ) -> None:
        """Initialize the battery charging binary sensor."""
        super().__init__(coordinator)
        self._config_entry = config_entry
        self._mac_address = mac_address
        self._device_name = device_name
        self._em1003_device = em1003_device

        self._attr_unique_id = f"{mac_address}_battery_charging"
        self._attr_name = "Battery Charging"

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information about this EM1003 device."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._mac_address)},
            name=self._device_name,
            manufacturer="EM1003",
            model="BLE Air Quality Sensor",
            connections={("mac", self._mac_address)},
        )

    @property
    def is_on(self) -> bool | None:
        """Return True if the battery is charging."""
        if self.coordinator.data and "battery_charging" in self.coordinator.data:
            charging = self.coordinator.data["battery_charging"]
            if charging is not None:
                return bool(charging)
        if self._em1003_device.battery_charging is not None:
            return bool(self._em1003_device.battery_charging)
        return None

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        if self.coordinator.last_update_success and self.is_on is not None:
            return True
        return False

    @property
    def extra_state_attributes(self) -> dict:
        """Return additional attributes."""
        attrs = {
            "mac_address": self._mac_address,
        }
        if self.coordinator.data and 0x05 in self.coordinator.data:
            attrs["battery_level"] = self.coordinator.data[0x05]
        return attrs
