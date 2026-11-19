"""Button platform for EM1003 BLE Sensor integration."""
from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
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
    """Set up EM1003 button based on a config entry."""
    data = hass.data[DOMAIN][config_entry.entry_id]
    mac_address = data[CONF_MAC_ADDRESS]
    device_name = data.get("device_name", config_entry.title)
    em1003_device = data["device"]
    coordinator = data.get("coordinator")

    _LOGGER.info("Setting up EM1003 activate wifi button for device: %s (%s)", device_name, mac_address)

    async_add_entities([
        EM1003ActivateWifiButton(
            config_entry,
            mac_address,
            device_name,
            em1003_device,
            coordinator,
        )
    ])


class EM1003ActivateWifiButton(ButtonEntity):
    """Button to activate Wi-Fi profile on EM1003 device."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:wifi-sync"
    _attr_entity_registry_enabled_default = False
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        config_entry: ConfigEntry,
        mac_address: str,
        device_name: str,
        em1003_device,
        coordinator=None,
    ) -> None:
        """Initialize the activate Wi-Fi button."""
        self._config_entry = config_entry
        self._mac_address = mac_address
        self._device_name = device_name
        self._em1003_device = em1003_device
        self._coordinator = coordinator

        self._attr_unique_id = f"{mac_address}_activate_wifi"
        self._attr_name = "Activate Wi-Fi"

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

    async def async_press(self) -> None:
        """Handle the button press to activate Wi-Fi."""
        _LOGGER.info("Activating Wi-Fi on device: %s", self._mac_address)
        success = await self._em1003_device.activate_wifi(wifi_index=0)
        if success:
            _LOGGER.info("Wi-Fi activation signal sent successfully to %s", self._mac_address)
            if self._coordinator:
                await self._coordinator.async_request_refresh()
        else:
            _LOGGER.warning("Failed to send Wi-Fi activation signal to %s", self._mac_address)
