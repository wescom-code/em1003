"""Switch platform for EM1003 BLE Sensor integration."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, CONF_MAC_ADDRESS

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up EM1003 switch based on a config entry."""
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

    _LOGGER.info("Setting up EM1003 switches for device: %s (%s)", device_name, mac_address)

    # Create switch entities
    async_add_entities([
        EM1003BuzzerSwitch(
            config_entry,
            mac_address,
            device_name,
            em1003_device,
            coordinator,
        ),
        EM1003MeasurementSwitch(
            config_entry,
            mac_address,
            device_name,
            em1003_device,
            coordinator,
        ),
    ])


class EM1003BuzzerSwitch(SwitchEntity):
    """Representation of an EM1003 buzzer switch."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        config_entry: ConfigEntry,
        mac_address: str,
        device_name: str,
        em1003_device,
        coordinator=None,
    ) -> None:
        """Initialize the buzzer switch."""
        self._config_entry = config_entry
        self._mac_address = mac_address
        self._device_name = device_name
        self._em1003_device = em1003_device
        self._coordinator = coordinator

        # Set entity attributes
        self._attr_unique_id = f"{mac_address}_buzzer"
        self._attr_name = "Buzzer"
        self._attr_icon = "mdi:volume-high"

        # State tracking
        self._attr_is_on = None
        self._attr_available = True

        # Subscribe to coordinator updates if available
        if self._coordinator:
            self._coordinator_listener = None

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information about this EM1003 device."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._mac_address)},
            name=self._device_name,
            manufacturer="720",
            model=getattr(self._em1003_device, "model", "EM1003"),
            sw_version=getattr(self._em1003_device, "firmware_version", None),
            serial_number=getattr(self._em1003_device, "sn", None),
            connections={(dr.CONNECTION_BLUETOOTH, self._mac_address)},
        )

    async def async_added_to_hass(self) -> None:
        """Run when entity about to be added to hass."""
        await super().async_added_to_hass()

        # Subscribe to coordinator updates
        if self._coordinator:
            self._coordinator_listener = self._coordinator.async_add_listener(
                self._handle_coordinator_update
            )

        # Check if buzzer state is already populated
        if self._em1003_device.buzzer_state is not None:
            self._attr_is_on = self._em1003_device.buzzer_state
            _LOGGER.info(
                "Initial buzzer state for %s: %s (from device cache)",
                self._mac_address,
                "ON" if self._attr_is_on else "OFF"
            )
            return

        # Try to read initial buzzer state if not yet cached
        try:
            _LOGGER.debug("Reading initial buzzer state for %s", self._mac_address)
            state = await self._em1003_device.read_buzzer_state()
            if state is not None:
                self._attr_is_on = state
                _LOGGER.info(
                    "Initial buzzer state for %s: %s",
                    self._mac_address,
                    "ON" if state else "OFF"
                )
            else:
                _LOGGER.warning(
                    "Could not read initial buzzer state for %s, will retry on coordinator update",
                    self._mac_address
                )
                self._attr_is_on = None
        except Exception as err:
            _LOGGER.warning(
                "Error reading initial buzzer state for %s: %s",
                self._mac_address,
                err
            )
            self._attr_is_on = None

    async def async_will_remove_from_hass(self) -> None:
        """Run when entity will be removed from hass."""
        if self._coordinator_listener:
            self._coordinator_listener()
            self._coordinator_listener = None

    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        # Sync state from device object (updated by coordinator's buzzer query)
        if self._em1003_device.buzzer_state is not None:
            if self._attr_is_on != self._em1003_device.buzzer_state:
                _LOGGER.debug(
                    "Syncing buzzer state from coordinator for %s: %s -> %s",
                    self._mac_address,
                    "ON" if self._attr_is_on else "OFF" if self._attr_is_on is not None else "Unknown",
                    "ON" if self._em1003_device.buzzer_state else "OFF"
                )
                self._attr_is_on = self._em1003_device.buzzer_state
                self._attr_available = True
                self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the buzzer on."""
        _LOGGER.debug("Turning on buzzer for %s", self._mac_address)

        try:
            success = await self._em1003_device.set_buzzer_state(True)

            if success:
                self._attr_is_on = True
                self._attr_available = True
                _LOGGER.info("Successfully turned on buzzer for %s", self._mac_address)
            else:
                _LOGGER.error("Failed to turn on buzzer for %s", self._mac_address)
                self._attr_available = False

            # Trigger state update
            self.async_write_ha_state()

        except Exception as err:
            _LOGGER.error("Error turning on buzzer for %s: %s", self._mac_address, err)
            self._attr_available = False
            self.async_write_ha_state()
            raise

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the buzzer off."""
        _LOGGER.debug("Turning off buzzer for %s", self._mac_address)

        try:
            success = await self._em1003_device.set_buzzer_state(False)

            if success:
                self._attr_is_on = False
                self._attr_available = True
                _LOGGER.info("Successfully turned off buzzer for %s", self._mac_address)
            else:
                _LOGGER.error("Failed to turn off buzzer for %s", self._mac_address)
                self._attr_available = False

            # Trigger state update
            self.async_write_ha_state()

        except Exception as err:
            _LOGGER.error("Error turning off buzzer for %s: %s", self._mac_address, err)
            self._attr_available = False
            self.async_write_ha_state()
            raise

    @property
    def extra_state_attributes(self) -> dict:
        """Return additional attributes."""
        return {
            "mac_address": self._mac_address,
        }


class EM1003MeasurementSwitch(SwitchEntity):
    """Representation of an EM1003 measurement switch (active measurement vs sleep mode)."""

    _attr_has_entity_name = True

    def __init__(
        self,
        config_entry: ConfigEntry,
        mac_address: str,
        device_name: str,
        em1003_device,
        coordinator=None,
    ) -> None:
        """Initialize the measurement switch."""
        self._config_entry = config_entry
        self._mac_address = mac_address
        self._device_name = device_name
        self._em1003_device = em1003_device
        self._coordinator = coordinator

        self._attr_unique_id = f"{mac_address}_measurement"
        self._attr_name = "Measuring"
        self._attr_is_on = bool(self._em1003_device.is_measuring)
        self._coordinator_listener = None

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information about this EM1003 device."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._mac_address)},
            name=self._device_name,
            manufacturer="720",
            model=getattr(self._em1003_device, "model", "EM1003"),
            sw_version=getattr(self._em1003_device, "firmware_version", None),
            serial_number=getattr(self._em1003_device, "sn", None),
            connections={(dr.CONNECTION_BLUETOOTH, self._mac_address)},
        )

    @property
    def icon(self) -> str:
        """Return icon based on measurement state."""
        return "mdi:fan" if self.is_on else "mdi:fan-off"

    @property
    def is_on(self) -> bool:
        """Return True if device is actively measuring."""
        if self._attr_is_on is not None:
            return self._attr_is_on
        if self._coordinator and self._coordinator.data and "is_measuring" in self._coordinator.data:
            return bool(self._coordinator.data["is_measuring"])
        return bool(self._em1003_device.is_measuring)

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        if self._coordinator:
            return self._coordinator.last_update_success
        return True

    async def async_added_to_hass(self) -> None:
        """Run when entity about to be added to hass."""
        await super().async_added_to_hass()
        if self._coordinator:
            self._coordinator_listener = self._coordinator.async_add_listener(
                self._handle_coordinator_update
            )
            if self._coordinator.data and "is_measuring" in self._coordinator.data:
                self._attr_is_on = bool(self._coordinator.data["is_measuring"])

    async def async_will_remove_from_hass(self) -> None:
        """Run when entity will be removed from hass."""
        if self._coordinator_listener:
            self._coordinator_listener()
            self._coordinator_listener = None

    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        if self._coordinator and self._coordinator.data and "is_measuring" in self._coordinator.data:
            self._attr_is_on = bool(self._coordinator.data["is_measuring"])
        else:
            self._attr_is_on = bool(self._em1003_device.is_measuring)
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on measurement (wake sensors & fan)."""
        _LOGGER.debug("Starting measurement for %s", self._mac_address)
        try:
            success = await self._em1003_device.set_measurement_state(True)
            if success:
                self._attr_is_on = True
                if self._coordinator and self._coordinator.data:
                    self._coordinator.data["is_measuring"] = True
                self.async_write_ha_state()
                _LOGGER.info("Successfully started measurement for %s", self._mac_address)
                if self._coordinator:
                    await self._coordinator.async_request_refresh()
            else:
                _LOGGER.error("Failed to start measurement for %s", self._mac_address)
                self._attr_is_on = self._em1003_device.is_measuring
                self.async_write_ha_state()
        except Exception as err:
            _LOGGER.error("Error starting measurement for %s: %s", self._mac_address, err)
            self._attr_is_on = self._em1003_device.is_measuring
            self.async_write_ha_state()
            raise

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off measurement (enter dormant sleep)."""
        _LOGGER.debug("Stopping measurement for %s", self._mac_address)
        try:
            success = await self._em1003_device.set_measurement_state(False)
            if success:
                self._attr_is_on = False
                if self._coordinator and self._coordinator.data:
                    self._coordinator.data["is_measuring"] = False
                    for env_sid in [0x01, 0x06, 0x08, 0x09, 0x0A, 0x11, 0x12, 0x13]:
                        self._coordinator.data[env_sid] = None
                self.async_write_ha_state()
                _LOGGER.info("Successfully stopped measurement for %s", self._mac_address)
                if self._coordinator:
                    await self._coordinator.async_request_refresh()
            else:
                _LOGGER.error("Failed to stop measurement for %s", self._mac_address)
                self._attr_is_on = self._em1003_device.is_measuring
                self.async_write_ha_state()
        except Exception as err:
            _LOGGER.error("Error stopping measurement for %s: %s", self._mac_address, err)
            self._attr_is_on = self._em1003_device.is_measuring
            self.async_write_ha_state()
            raise

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes."""
        return {
            "mac_address": self._mac_address,
            "usb_power": getattr(self._em1003_device, "usb_power", None),
            "hcho_state": getattr(self._em1003_device, "hcho_state", None),
            "battery_pct": getattr(self._em1003_device, "battery_pct", None),
        }
