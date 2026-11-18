"""BLE device client for EM1003 air quality sensor."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import logging
import random
import time
from dataclasses import dataclass

from bleak import BleakClient
from bleak.exc import BleakError
from bleak_retry_connector import establish_connection

from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import (
    DOMAIN,
    CMD_READ_SENSOR,
    CMD_START_MEASURE,
    CMD_STOP_MEASURE,
    CMD_READ_SN,
    CMD_READ_WIFI_CURRENT_STATE,
    CMD_READ_PLACEID,
    CMD_READ_USB_STATE,
    CMD_ACTIVATE_WIFI,
    CMD_READ_BLE_VERSION,
    CMD_READ_WIFI_VERSION,
    CMD_BUZZER,
    CMD_BUZZER_SET_RESPONSE,
    BUZZER_ON,
    BUZZER_OFF,
    EM1003_NOTIFY_CHAR_UUID,
    EM1003_WRITE_CHAR_UUID,
    SENSOR_TYPES,
)

_LOGGER = logging.getLogger(__name__)

# Per-proxy locks to serialize BLE operations per Bluetooth adapter/ESPHome proxy.
# This prevents ESP_GATT_ERROR from concurrent connections on the same proxy,
# while allowing devices on different proxies to communicate concurrently in parallel!
_PROXY_LOCKS: dict[str, asyncio.Lock] = {}
_PROXY_LAST_DISCONNECT: dict[str, float] = {}
_PROXY_BLE_SETTLING_DELAY: float = 0.5  # Seconds to let BLE adapter/proxy settle between devices


def get_device_proxy_source(hass: HomeAssistant, mac_address: str) -> str:
    """Get the current Bluetooth proxy/adapter source identifier for a MAC address."""
    try:
        service_info = bluetooth.async_last_service_info(hass, mac_address, connectable=True)
        if service_info and service_info.source:
            return str(service_info.source).upper()
    except Exception as err:
        _LOGGER.debug("Could not determine BLE source for %s: %s", mac_address, err)
    return "default"


def _get_proxy_lock(source: str) -> asyncio.Lock:
    """Get or create an asyncio.Lock for a specific proxy/adapter source."""
    if source not in _PROXY_LOCKS:
        _PROXY_LOCKS[source] = asyncio.Lock()
    return _PROXY_LOCKS[source]


@asynccontextmanager
async def _proxy_ble_session(source: str):
    """Acquire proxy-specific BLE lock and ensure radio settling delay for that proxy."""
    lock = _get_proxy_lock(source)
    async with lock:
        elapsed = time.time() - _PROXY_LAST_DISCONNECT.get(source, 0.0)
        if 0 < elapsed < _PROXY_BLE_SETTLING_DELAY:
            await asyncio.sleep(_PROXY_BLE_SETTLING_DELAY - elapsed)
        try:
            yield
        finally:
            _PROXY_LAST_DISCONNECT[source] = time.time()


@dataclass
class PendingRequest:
    """Represents a pending sensor read request."""

    seq_id: int
    sensor_id: int
    future: asyncio.Future
    timestamp: float


class CircuitBreaker:
    """Circuit breaker pattern to prevent request pile-up during connection failures.

    States:
    - CLOSED: Normal operation, requests allowed
    - OPEN: Too many failures, requests blocked for 60 seconds
    - HALF_OPEN: After timeout, allow one test request
    """

    def __init__(self, failure_threshold: int = 3, open_duration: float = 60.0, max_backoff: float = 3600.0):
        """Initialize circuit breaker.

        Args:
            failure_threshold: Number of failures before opening circuit
            open_duration: Base seconds to wait before entering half-open state
            max_backoff: Maximum backoff duration in seconds (default: 1 hour)
        """
        self.state = "CLOSED"
        self.failure_count = 0
        self.failure_threshold = failure_threshold
        self.open_time: float | None = None
        self.base_open_duration = open_duration
        self.max_backoff = max_backoff

    def record_success(self) -> None:
        """Record successful operation - reset to CLOSED state."""
        self.failure_count = 0
        self.state = "CLOSED"
        self.open_time = None
        _LOGGER.debug("[CIRCUIT] ✓ Success recorded, circuit CLOSED")

    def record_failure(self) -> None:
        """Record failed operation - may open circuit."""
        self.failure_count += 1
        _LOGGER.debug(
            "[CIRCUIT] ✗ Failure recorded (%d/%d)",
            self.failure_count,
            self.failure_threshold
        )

        if self.failure_count >= self.failure_threshold:
            self.state = "OPEN"
            self.open_time = time.time()

            # Calculate exponential backoff: base_duration * 2^(failures - threshold)
            # Example: 3 failures → 60s, 4 → 120s, 5 → 240s, 6 → 480s, etc.
            backoff_multiplier = 2 ** (self.failure_count - self.failure_threshold)
            open_duration = min(self.base_open_duration * backoff_multiplier, self.max_backoff)

            _LOGGER.warning(
                "[CIRCUIT] Circuit OPEN due to %d consecutive failures. "
                "Blocking requests for %.0f seconds (exponential backoff)",
                self.failure_count,
                open_duration
            )

    def can_attempt(self) -> tuple[bool, str]:
        """Check if request can proceed.

        Returns:
            Tuple of (can_proceed, reason)
        """
        if self.state == "CLOSED":
            return True, "Circuit closed"

        elif self.state == "OPEN":
            if self.open_time is None:
                # Shouldn't happen, but handle gracefully
                self.state = "CLOSED"
                return True, "Circuit reset"

            # Calculate current open duration with exponential backoff
            backoff_multiplier = 2 ** (self.failure_count - self.failure_threshold)
            open_duration = min(self.base_open_duration * backoff_multiplier, self.max_backoff)

            elapsed = time.time() - self.open_time
            if elapsed >= open_duration:
                # CRITICAL FIX: Reset failure_count when entering HALF_OPEN
                # This prevents infinite accumulation of failures
                previous_failures = self.failure_count
                self.failure_count = 0
                self.state = "HALF_OPEN"
                _LOGGER.info(
                    "[CIRCUIT] Circuit entering HALF_OPEN state after %.0f seconds "
                    "(was %d failures, now testing recovery)",
                    elapsed,
                    previous_failures
                )
                return True, "Circuit half-open (testing)"

            remaining = open_duration - elapsed
            return False, f"Circuit open ({remaining:.0f}s remaining, {self.failure_count} failures)"

        else:  # HALF_OPEN
            return True, "Circuit half-open (testing)"

    def get_state_info(self) -> str:
        """Get human-readable state information."""
        if self.state == "CLOSED":
            return f"CLOSED (failures: {self.failure_count})"
        elif self.state == "OPEN" and self.open_time:
            # Calculate current open duration with exponential backoff
            backoff_multiplier = 2 ** (self.failure_count - self.failure_threshold)
            open_duration = min(self.base_open_duration * backoff_multiplier, self.max_backoff)

            elapsed = time.time() - self.open_time
            remaining = max(0, open_duration - elapsed)
            return f"OPEN (blocking for {remaining:.0f}s, {self.failure_count} failures)"
        else:
            return f"HALF_OPEN (testing, {self.failure_count} failures)"


class EM1003Device:
    """Representation of an EM1003 BLE device."""

    def __init__(self, hass: HomeAssistant, mac_address: str, device_name: str | None = None) -> None:
        """Initialize the device."""
        self.hass = hass
        self.mac_address = mac_address
        self.device_name = device_name or mac_address  # Use MAC as fallback
        self._client: BleakClient | None = None
        self.sensor_data: dict[int, float | None] = {}
        self.buzzer_state: bool | None = None  # Buzzer state (True=on, False=off, None=unknown)
        self.battery_charging: bool | None = None  # Battery charging state (True=charging, False=discharging)
        self.wifi_state: int | None = None  # Wi-Fi status code (0-6)
        self.cloud_state: int | None = None  # Cloud reporting status code (-1, 0, 1, 101, 102)
        self.wifi_sum_number: int | None = None  # Total number of configured Wi-Fi profiles
        self.current_wifi_index: int | None = None  # Currently selected Wi-Fi profile index
        self.usb_power: bool | None = None  # USB / external power status (True=connected, False=disconnected)
        self.hcho_state: int | None = None  # Formaldehyde sensor state (0=unready/dormant, 1=measuring)
        self.battery_pct: float | None = None  # Battery percentage (0-100)
        self._is_dormant: bool = False  # True when device sensors & fan are dormant / sleeping on battery
        self._frozen_cycle_count: int = 0  # Counter for consecutive identical sensor snapshots
        self._last_env_snapshot: tuple | None = None  # (temp, humid, noise, pm25) snapshot of previous cycle
        self._manual_measure_active: bool = False  # Explicitly started measurement via HA switch
        self._last_disconnect_time: float | None = None

        # Hardware variant and firmware information
        self.sn: str | None = None
        self.place_id: str | None = None
        self._place_id_queried: bool = False
        self.ble_version: str | None = None
        self.ble_version_raw: int | None = None
        self.wifi_version: str | None = None
        self.wifi_version_raw: int | None = None
        self.firmware_version: str | None = None
        self.model: str = "EM1003"
        self._device_info_queried: bool = False
        self._info_query_attempts: int = 0

        # Initialize default model from device name if possible
        self._update_model_and_device_info()

        # Request cache for matching responses to requests
        # Key: (seq_id, sensor_id), Value: PendingRequest
        self._pending_requests: dict[tuple[int, int], PendingRequest] = {}
        self._used_seq_ids: set[int] = set()  # Track used sequence IDs

        # Circuit breaker to prevent request pile-up
        self._circuit_breaker = CircuitBreaker(
            failure_threshold=3,
            open_duration=60.0
        )

        # Connection abort tracking for adaptive backoff
        self._connection_abort_count = 0
        self._last_connection_abort_time: float | None = None

        # Track last connection failure for fast-fail behavior
        self._last_connection_failure_time: float | None = None
        self._fast_fail_window = 30.0  # Seconds to fast-fail after connection failure

        # Lock to ensure BLE operations are serialized
        self._lock = asyncio.Lock()
        self._current_source: str = "default"

    @property
    def is_measuring(self) -> bool:
        """Return True if device sensors are actively measuring fresh data.

        When external USB power is connected, the device operates continuously.
        When running on battery (USB disconnected), internal sensors power down to save
        battery unless actively measuring.
        """
        return not self._is_dormant

    def _get_proxy_source(self) -> str:
        """Get the current Bluetooth proxy/adapter source identifier for this device."""
        source = get_device_proxy_source(self.hass, self.mac_address)
        self._current_source = source
        return source

    def _device_id(self) -> str:
        """Get device identifier for logging."""
        if self.device_name and self.device_name != self.mac_address:
            return f"{self.device_name} ({self.mac_address})"
        return self.mac_address

    def _update_device_registry(self) -> None:
        """Update Home Assistant device registry with detected hardware model and firmware."""
        if not self.hass:
            return
        try:
            device_registry = dr.async_get(self.hass)
            device_entry = device_registry.async_get_device(identifiers={(DOMAIN, self.mac_address)})
            if device_entry:
                update_kwargs = {"manufacturer": "720"}
                if self.model:
                    update_kwargs["model"] = self.model
                if self.firmware_version:
                    update_kwargs["sw_version"] = self.firmware_version
                if self.sn:
                    update_kwargs["serial_number"] = self.sn
                device_registry.async_update_device(device_entry.id, **update_kwargs)
                _LOGGER.info(
                    "[DEV] Updated device registry for %s: manufacturer=720, model=%s, sw_version=%s, sn=%s",
                    self.mac_address, self.model, self.firmware_version, self.sn
                )
        except Exception as err:
            _LOGGER.debug("[DEV] Error updating device registry: %s", err)

    def _update_model_and_device_info(self) -> None:
        """Determine hardware model and formatted firmware version, and update registry."""
        model = "EM1003"
        platform = None

        place_id_str = (self.place_id or "").strip()
        place_id_lower = place_id_str.lower()
        device_name_upper = (self.device_name or "").strip().upper()
        sn_upper = (self.sn or "").strip().upper()

        # 1. Samsung SmartThings edition (EM1003S)
        if (
            place_id_lower.startswith("ss")
            or "samsung" in place_id_lower
            or "1003S" in device_name_upper
            or "1003S" in sn_upper
            or sn_upper.startswith("CG3S")
            or device_name_upper.startswith("3GCG3S")
        ):
            platform = "Samsung"
            model = "EM1003S"

        # 2. Huawei HiLink edition
        # Identified by: PlaceID starts with "hi", or SN starts with "CG301",
        # or BLE name starts with "3GCG301", or FW raw int has high byte 0xF0 (e.g. 0xF001 / 61441, 0xF000 / 61440)
        elif (
            place_id_lower.startswith("hi")
            or sn_upper.startswith("CG301")
            or device_name_upper.startswith("3GCG301")
            or (self.wifi_version_raw is not None and (self.wifi_version_raw >> 8) == 0xF0)
            or (self.ble_version_raw is not None and (self.ble_version_raw >> 8) == 0xF0)
        ):
            platform = "HiLink"
            model = "EM1003"

        # 3. JingDong (JD / 京东微联 / 京东小家) edition
        # Identified by: PlaceID starts with "jd", or FW raw int has high byte 0x13 (e.g. 0x1308 / 4872, 0x1302 / 4866)
        elif (
            place_id_lower.startswith("jd")
            or (self.wifi_version_raw is not None and (self.wifi_version_raw >> 8) == 0x13)
            or (self.ble_version_raw is not None and (self.ble_version_raw >> 8) == 0x13)
        ):
            platform = "JD"
            model = "EM1003"

        # 4. China Telecom (CT / 电信小翼管家) edition
        elif (
            place_id_lower.startswith("ct")
            or sn_upper.startswith("CG3CT")
            or device_name_upper.startswith("3GCG3CT")
        ):
            platform = "CT"
            model = "EM1003"

        # 5. Original 720 edition configured via 720 环境点评 App (LeanCloud 24 hex char ID)
        elif (
            place_id_lower.startswith("720")
            or (len(place_id_lower) >= 16 and all(c in "0123456789abcdef" for c in place_id_lower[:16]))
        ):
            platform = "720"
            model = "EM1003"

        # 6. Any other non-empty PlaceID prefix
        elif place_id_lower:
            platform = place_id_str[:6]
            model = "EM1003"

        else:
            # Default fallback when no specific edition signature has been detected yet
            platform = "720"
            model = "EM1003"

        if platform:
            self.model = f"{model} ({platform})"
        else:
            self.model = model

        # Determine firmware version display
        if self.wifi_version and self.ble_version:
            self.firmware_version = f"{self.wifi_version} (BLE: {self.ble_version})"
        elif self.wifi_version:
            self.firmware_version = self.wifi_version
        elif self.ble_version:
            self.firmware_version = f"BLE: {self.ble_version}"

        # Update device registry in Home Assistant
        self._update_device_registry()

    def _get_random_sequence_id(self) -> int:
        """Get a random unused sequence ID.

        Uses random IDs to avoid collisions when multiple requests are in flight.
        Cache is limited to 256 entries (max possible sequence IDs).
        """
        # Clean up old sequence IDs if cache is getting full
        if len(self._used_seq_ids) >= 250:
            _LOGGER.debug(
                "[SEQ] Cache nearly full (%d), clearing old sequence IDs",
                len(self._used_seq_ids)
            )
            self._used_seq_ids.clear()

        # Try to find an unused random sequence ID
        for attempt in range(100):
            seq_id = random.randint(0, 255)
            if seq_id not in self._used_seq_ids:
                self._used_seq_ids.add(seq_id)
                return seq_id

        # Fallback: sequential search
        for seq_id in range(256):
            if seq_id not in self._used_seq_ids:
                self._used_seq_ids.add(seq_id)
                _LOGGER.debug("[SEQ] Used sequential fallback, seq_id=%02x", seq_id)
                return seq_id

        # Last resort: clear and start over
        _LOGGER.warning("[SEQ] All 256 sequence IDs exhausted, clearing cache")
        self._used_seq_ids.clear()
        seq_id = random.randint(0, 255)
        self._used_seq_ids.add(seq_id)
        return seq_id

    def _cleanup_expired_requests(self, max_age: float = 10.0) -> None:
        """Clean up expired pending requests.

        Args:
            max_age: Maximum age in seconds for a pending request
        """
        now = time.time()
        expired_keys = [
            key for key, req in self._pending_requests.items()
            if now - req.timestamp > max_age
        ]

        for key in expired_keys:
            seq_id, sensor_id = key
            req = self._pending_requests.pop(key)
            self._used_seq_ids.discard(seq_id)
            if not req.future.done():
                req.future.cancel()
            _LOGGER.debug(
                "[CACHE] Cleaned up expired request: seq=%02x, sensor=%02x (age=%.1fs)",
                seq_id, sensor_id, now - req.timestamp
            )

    async def _ensure_connection_delay(self) -> None:
        """Ensure sufficient delay since last disconnect to avoid connection issues.

        Uses adaptive backoff based on recent connection abort errors:
        - Base delay: 2 seconds (increased from 1 to give Bluetooth stack more time)
        - After connection abort: adds exponential backoff
        - Resets abort count after 5 minutes of no abort errors
        """
        current_time = time.time()

        # Reset connection abort counter if it's been a while since last abort
        if self._last_connection_abort_time is not None:
            time_since_abort = current_time - self._last_connection_abort_time
            if time_since_abort > 300:  # 5 minutes
                self._connection_abort_count = 0
                self._last_connection_abort_time = None

        # Calculate base delay with adaptive backoff for connection aborts
        base_delay = 2.0  # Increased from 1.0 to 2.0 seconds

        # Add exponential backoff for repeated connection aborts
        # abort_count: 0 → 0s, 1 → 2s, 2 → 4s, 3 → 8s, 4 → 16s, max 30s
        abort_backoff = 0.0
        if self._connection_abort_count > 0:
            abort_backoff = min(2.0 ** self._connection_abort_count, 30.0)

        min_delay = base_delay + abort_backoff

        # Wait if we recently disconnected
        if self._last_disconnect_time is not None:
            time_since_disconnect = current_time - self._last_disconnect_time
            if time_since_disconnect < min_delay:
                delay = min_delay - time_since_disconnect
                if abort_backoff > 0:
                    _LOGGER.info("Waiting %.1fs before retry (connection abort backoff)", delay)
                await asyncio.sleep(delay)
        elif abort_backoff > 0:
            # No recent disconnect but we have abort history, add safety delay
            _LOGGER.info("Waiting %.1fs before retry (connection abort backoff)", abort_backoff)
            await asyncio.sleep(abort_backoff)

    async def _establish_connection(self) -> BleakClient:
        """Establish a connection to the device with proper error handling.

        Returns:
            Connected BleakClient instance

        Raises:
            BleakError: If connection fails after retries
        """
        _LOGGER.debug(
            "[DIAG] Starting connection attempt to %s",
            self.mac_address
        )

        # Wait if we recently disconnected to avoid connection abort errors
        await self._ensure_connection_delay()

        # Get device from Home Assistant's Bluetooth integration
        _LOGGER.info(
            "[DIAG] Getting device %s from Home Assistant Bluetooth integration...",
            self._device_id()
        )

        device = bluetooth.async_ble_device_from_address(
            self.hass,
            self.mac_address,
            connectable=True
        )

        if not device:
            _LOGGER.error(
                "[DIAG] ✗ Device %s not found",
                self._device_id()
            )
            raise BleakError(
                f"Device not found: {self.mac_address}. "
                "Make sure device is powered on and nearby."
            )

        device_rssi = getattr(device, 'rssi', None)
        _LOGGER.info(
            "[DIAG] ✓ Found device %s (Name: %s, RSSI: %s dBm)",
            self.mac_address,
            device.name or "Unknown",
            device_rssi if device_rssi is not None else "N/A"
        )

        if device.name and (not self.device_name or self.device_name == self.mac_address):
            self.device_name = device.name.strip()
            self._update_model_and_device_info()

        # Establish connection with timeout and retry logic
        # CRITICAL: Use max_attempts=1 to prevent slot exhaustion
        # Each failed attempt can occupy a BLE slot that doesn't get released immediately
        # Multiple retries can exhaust all available slots, preventing new connections
        _LOGGER.debug(
            "[DIAG] Attempting to establish connection to %s using bleak-retry-connector...",
            self.mac_address
        )

        connection_start_time = time.time()
        client = None
        try:
            _LOGGER.info(
                "[CONN_ROOT_CAUSE] Starting connection attempt to %s (RSSI: %s)",
                self.mac_address, getattr(device, 'rssi', 'N/A')
            )

            client = await establish_connection(
                BleakClient,
                device,
                self.mac_address,
                disconnected_callback=lambda _: None,
                max_attempts=1,  # CRITICAL: Reduced to 1 to prevent slot exhaustion
                timeout=30.0,  # 30 second timeout per attempt
            )

            connection_duration = time.time() - connection_start_time
            _LOGGER.info(
                "[CONN_ROOT_CAUSE] ✓ Connection successful to %s (took %.2fs)",
                self.mac_address, connection_duration
            )

            # Reset connection abort tracking on successful connection
            if self._connection_abort_count > 0:
                self._connection_abort_count = 0
                self._last_connection_abort_time = None

            return client

        except Exception as conn_err:
            # CRITICAL: Clean up any partially established connection
            # Failed connection attempts can leave slots occupied
            if client is not None:
                try:
                    _LOGGER.debug(
                        "[CONN_ROOT_CAUSE] Cleaning up failed connection attempt to %s",
                        self.mac_address
                    )
                    await client.disconnect()
                except Exception as cleanup_err:
                    _LOGGER.debug(
                        "[CONN_ROOT_CAUSE] Error during connection cleanup: %s",
                        cleanup_err
                    )
            connection_duration = time.time() - connection_start_time

            # Analyze the error to determine root cause
            error_message = str(conn_err).lower()
            error_type = type(conn_err).__name__

            # Categorize the error
            is_connection_abort = (
                "connection abort" in error_message or
                "software caused connection abort" in error_message
            )
            is_timeout = "timeout" in error_message or isinstance(conn_err, asyncio.TimeoutError)
            is_device_unreachable = (
                "device unreachable" in error_message or
                "no route to host" in error_message or
                "host is down" in error_message
            )
            is_auth_failed = (
                "authentication" in error_message or
                "pairing" in error_message
            )
            is_resource_busy = (
                "resource busy" in error_message or
                "device busy" in error_message
            )

            # Build diagnostic context
            device_rssi = getattr(device, 'rssi', None)
            device_name = getattr(device, 'name', 'Unknown')

            # Log detailed root cause analysis
            _LOGGER.error(
                "[CONN_ROOT_CAUSE] ✗ Connection FAILED to %s after %.2fs",
                self.mac_address, connection_duration
            )
            _LOGGER.error(
                "[CONN_ROOT_CAUSE] Error type: %s", error_type
            )
            _LOGGER.error(
                "[CONN_ROOT_CAUSE] Error message: %s", conn_err
            )
            _LOGGER.error(
                "[CONN_ROOT_CAUSE] Device info: name=%s, RSSI=%s",
                device_name, device_rssi
            )

            # Determine and log the root cause
            if is_connection_abort:
                self._connection_abort_count += 1
                self._last_connection_abort_time = time.time()
                _LOGGER.error(
                    "[CONN_ROOT_CAUSE] Root cause: CONNECTION ABORT - "
                    "Bluetooth stack aborted connection (count: %d). "
                    "Possible reasons: device too far, interference, device busy, or Bluetooth stack overload",
                    self._connection_abort_count
                )
            elif is_timeout:
                _LOGGER.error(
                    "[CONN_ROOT_CAUSE] Root cause: TIMEOUT - "
                    "Device did not respond within 30s. "
                    "Possible reasons: device out of range (RSSI: %s), device sleeping, "
                    "or device not advertising",
                    device_rssi
                )
            elif is_device_unreachable:
                _LOGGER.error(
                    "[CONN_ROOT_CAUSE] Root cause: DEVICE UNREACHABLE - "
                    "Device cannot be reached. "
                    "Possible reasons: device powered off, out of range (RSSI: %s), "
                    "or Bluetooth adapter issue",
                    device_rssi
                )
            elif is_auth_failed:
                _LOGGER.error(
                    "[CONN_ROOT_CAUSE] Root cause: AUTHENTICATION/PAIRING FAILED - "
                    "Device rejected pairing or authentication. "
                    "Possible reasons: device requires pairing, incorrect PIN, or bonding issue"
                )
            elif is_resource_busy:
                _LOGGER.error(
                    "[CONN_ROOT_CAUSE] Root cause: RESOURCE BUSY - "
                    "Bluetooth resource is busy. "
                    "Possible reasons: another process connected to device, "
                    "Bluetooth adapter busy, or previous connection not fully closed"
                )
            else:
                _LOGGER.error(
                    "[CONN_ROOT_CAUSE] Root cause: UNKNOWN ERROR - "
                    "Unexpected error during connection. "
                    "Check device status, Bluetooth adapter, and system logs",
                    exc_info=True
                )

            # Log environmental factors
            if device_rssi is not None:
                if device_rssi < -90:
                    _LOGGER.warning(
                        "[CONN_ROOT_CAUSE] ⚠ Signal strength VERY WEAK (RSSI: %s dBm). "
                        "Device is likely too far away or obstructed",
                        device_rssi
                    )
                elif device_rssi < -80:
                    _LOGGER.warning(
                        "[CONN_ROOT_CAUSE] ⚠ Signal strength WEAK (RSSI: %s dBm). "
                        "Connection may be unreliable",
                        device_rssi
                    )
                elif device_rssi < -70:
                    _LOGGER.info(
                        "[CONN_ROOT_CAUSE] Signal strength FAIR (RSSI: %s dBm)",
                        device_rssi
                    )
                else:
                    _LOGGER.info(
                        "[CONN_ROOT_CAUSE] Signal strength GOOD (RSSI: %s dBm)",
                        device_rssi
                    )

            # Log recommended actions
            _LOGGER.error(
                "[CONN_ROOT_CAUSE] Recommended actions: "
                "1) Check device is powered on and nearby, "
                "2) Check for Bluetooth interference, "
                "3) Restart Bluetooth adapter if issues persist, "
                "4) Check Home Assistant Bluetooth integration logs"
            )

            raise

    async def _ensure_connected(self) -> BleakClient:
        """Ensure we have an active connection, reusing existing if possible.

        This method prioritizes reusing existing connections:
        1. If self._client exists and is connected, use it immediately
        2. Otherwise, establish a new connection via active BLE scanning

        Returns:
            Connected BleakClient instance

        Raises:
            BleakError: If connection fails or fast-fail is active
        """
        # PRIORITY 1: Check if we already have a valid connection
        if self._client and self._client.is_connected:
            _LOGGER.info(
                "[CONN] ✓ Reusing existing active connection to %s",
                self.mac_address
            )
            return self._client

        _LOGGER.debug(
            "[CONN] No active connection to %s, need to establish new connection",
            self.mac_address
        )

        # Fast-fail if we recently failed to connect (unless circuit breaker is testing)
        if self._last_connection_failure_time is not None:
            time_since_failure = time.time() - self._last_connection_failure_time

            # Only fast-fail if we're not in HALF_OPEN state (testing phase)
            if time_since_failure < self._fast_fail_window and self._circuit_breaker.state != "HALF_OPEN":
                remaining = self._fast_fail_window - time_since_failure
                _LOGGER.debug(
                    "[CONN] Fast-fail: Recent connection failure (%.0fs ago), "
                    "skipping connection attempt for %.0fs more",
                    time_since_failure, remaining
                )
                raise BleakError(
                    f"Fast-fail: Connection failed {time_since_failure:.0f}s ago, "
                    f"will retry after {remaining:.0f}s"
                )

        # PRIORITY 2: Need to establish a new connection via active BLE scanning
        _LOGGER.info(
            "[CONN] Establishing new connection to %s via active BLE scan",
            self.mac_address
        )

        try:
            self._client = await self._establish_connection()

            # Subscribe to notifications (only need to do this once per connection)
            try:
                await self._client.start_notify(EM1003_NOTIFY_CHAR_UUID, self._notification_handler)
                _LOGGER.debug("[CONN] ✓ Connected and subscribed to %s", self.mac_address)

                # Connection successful - clear failure timestamp
                self._last_connection_failure_time = None

            except Exception as err:
                # Failed to subscribe, disconnect and re-raise
                _LOGGER.error("[CONN] Failed to subscribe to notifications: %s", err)
                # CRITICAL: Ensure connection is properly cleaned up to free slot
                if self._client is not None:
                    try:
                        await self._client.disconnect()
                        _LOGGER.debug("[CONN] Disconnected after subscription failure to free slot")
                    except Exception as disconnect_err:
                        _LOGGER.debug("[CONN] Error during cleanup disconnect: %s", disconnect_err)
                self._client = None
                # Record failure timestamp
                self._last_connection_failure_time = time.time()
                raise

            return self._client

        except Exception as err:
            # Record failure timestamp for fast-fail
            self._last_connection_failure_time = time.time()
            # CRITICAL: Ensure client is cleared so it doesn't hold a stale connection
            if self._client is not None:
                try:
                    await self._client.disconnect()
                    _LOGGER.debug("[CONN] Disconnected after connection error to free slot")
                except Exception:
                    pass
                self._client = None
            raise

    async def disconnect(self) -> None:
        """Explicitly disconnect from the device."""
        if self._client and self._client.is_connected:
            try:
                # Stop notifications first
                try:
                    await self._client.stop_notify(EM1003_NOTIFY_CHAR_UUID)
                    _LOGGER.debug("[CONN] Stopped notifications for %s", self.mac_address)
                except Exception as err:
                    _LOGGER.debug("[CONN] Could not stop notifications: %s", err)

                # Disconnect
                await self._client.disconnect()
                _LOGGER.debug("[CONN] Disconnected from %s", self.mac_address)
            except Exception as err:
                _LOGGER.debug("[CONN] Error during disconnect: %s", err)
            finally:
                self._client = None
                self._last_disconnect_time = time.time()
                source = getattr(self, "_current_source", "default")
                _PROXY_LAST_DISCONNECT[source] = time.time()
        else:
            _LOGGER.debug("[CONN] Already disconnected from %s", self.mac_address)
            self._client = None

    def _notification_handler(self, sender, data: bytearray) -> None:
        """Handle notification from device.

        Validates response matches pending request by checking (seq_id, sensor_id).
        This prevents accepting wrong responses due to timing issues.
        """
        try:
            if len(data) < 3:
                _LOGGER.warning("[RX] Notification too short: %d bytes, raw: %s", len(data), data.hex())
                return

            seq_id = data[0]
            cmd_type = data[1]
            sensor_id = data[2]
            value_bytes = data[3:]

            # Handle buzzer set response (0x05)
            if cmd_type == CMD_BUZZER_SET_RESPONSE:
                hex_parts = ' '.join([f'{b:02x}' for b in data])
                _LOGGER.debug(
                    "[RX] (蜂鸣器设置响应)[0x %s] len=%d",
                    hex_parts, len(data)
                )

                # Find matching pending request
                # For set operations, we use sensor_id 0x01
                request_key = (seq_id, 0x01)
                pending_request = self._pending_requests.get(request_key)

                if not pending_request:
                    _LOGGER.warning(
                        "[RX] ✗ Buzzer set: Unexpected response (seq=0x%02x, no matching request)",
                        seq_id
                    )
                    return

                _LOGGER.info(
                    "[RESP] ✓ Buzzer set command acknowledged"
                )

                # Complete the future
                if not pending_request.future.done():
                    pending_request.future.set_result(data)

                del self._pending_requests[request_key]
                self._used_seq_ids.discard(seq_id)
                return

            # Handle buzzer query response (0x50)
            if cmd_type == CMD_BUZZER:
                hex_parts = ' '.join([f'{b:02x}' for b in data])
                _LOGGER.debug(
                    "[RX] (蜂鸣器响应)[0x %s] len=%d, sensor_id=0x%02x, value_bytes=%s",
                    hex_parts, len(data), sensor_id, value_bytes.hex() if value_bytes else "empty"
                )

                # Find matching pending request
                request_key = (seq_id, sensor_id)
                pending_request = self._pending_requests.get(request_key)

                if not pending_request:
                    _LOGGER.warning(
                        "[RX] ✗ Buzzer: Unexpected response (seq=0x%02x, sensor_id=0x%02x, no matching request)",
                        seq_id, sensor_id
                    )
                    return

                # Parse buzzer state from response
                # Response format: [seq_id][0x50][0x00][state]
                if len(value_bytes) >= 1:
                    buzzer_value = value_bytes[0]
                    self.buzzer_state = (buzzer_value == BUZZER_ON)
                    _LOGGER.info(
                        "[RESP] ✓ Buzzer state = %s (0x%02x)",
                        "ON" if self.buzzer_state else "OFF",
                        buzzer_value
                    )
                else:
                    _LOGGER.warning(
                        "[RX] ✗ Buzzer: Response too short (len=%d, expected >= 4 bytes)",
                        len(data)
                    )

                # Complete the future
                if not pending_request.future.done():
                    pending_request.future.set_result(data)

                del self._pending_requests[request_key]
                self._used_seq_ids.discard(seq_id)
                return

            # Handle Wi-Fi and Cloud status response (0x19)
            if cmd_type == CMD_READ_WIFI_CURRENT_STATE:
                hex_parts = ' '.join([f'{b:02x}' for b in data])
                _LOGGER.debug("[RX] (Wi-Fi/云端状态响应)[0x %s] len=%d", hex_parts, len(data))
                request_key = (seq_id, CMD_READ_WIFI_CURRENT_STATE)
                pending_request = self._pending_requests.get(request_key)
                if pending_request:
                    if len(data) > 5:
                        self.wifi_sum_number = data[2]
                        self.current_wifi_index = data[3]
                        self.wifi_state = data[4]
                        # In protocol, cloud_state is a signed byte: 0xFF is -1 (unknown)
                        self.cloud_state = data[5] if data[5] < 128 else data[5] - 256
                        _LOGGER.info(
                            "[WIFI] Wi-Fi state=%d, Cloud state=%d, Total profiles=%d, Active profile=%d",
                            self.wifi_state, self.cloud_state, self.wifi_sum_number, self.current_wifi_index
                        )
                    if not pending_request.future.done():
                        pending_request.future.set_result(data)
                    del self._pending_requests[request_key]
                    self._used_seq_ids.discard(seq_id)
                return

            # Handle USB power status response (0x26)
            if cmd_type == CMD_READ_USB_STATE:
                hex_parts = ' '.join([f'{b:02x}' for b in data])
                _LOGGER.debug("[RX] (USB供电状态响应)[0x %s] len=%d", hex_parts, len(data))
                request_key = (seq_id, CMD_READ_USB_STATE)
                pending_request = self._pending_requests.get(request_key)
                if pending_request:
                    if len(data) > 2:
                        self.usb_power = bool(data[2] == 1)
                        _LOGGER.info("[USB] External USB power: %s", "Connected" if self.usb_power else "Disconnected")
                        # If USB power is disconnected, battery cannot be charging
                        if self.usb_power is False and self.battery_charging:
                            self.battery_charging = False
                    if not pending_request.future.done():
                        pending_request.future.set_result(data)
                    del self._pending_requests[request_key]
                    self._used_seq_ids.discard(seq_id)
                return

            # Handle Activate Wi-Fi response (0x2E)
            if cmd_type == CMD_ACTIVATE_WIFI:
                hex_parts = ' '.join([f'{b:02x}' for b in data])
                _LOGGER.debug("[RX] (激活Wi-Fi响应)[0x %s] len=%d", hex_parts, len(data))
                request_key = (seq_id, CMD_ACTIVATE_WIFI)
                pending_request = self._pending_requests.get(request_key)
                if pending_request:
                    if not pending_request.future.done():
                        pending_request.future.set_result(data)
                    del self._pending_requests[request_key]
                    self._used_seq_ids.discard(seq_id)
                return

            # Handle Start Measure response (0x12)
            if cmd_type == CMD_START_MEASURE:
                hex_parts = ' '.join([f'{b:02x}' for b in data])
                _LOGGER.debug("[RX] (开始测量响应)[0x %s] len=%d", hex_parts, len(data))
                request_key = (seq_id, CMD_START_MEASURE)
                pending_request = self._pending_requests.get(request_key)
                if not pending_request:
                    for k, req in list(self._pending_requests.items()):
                        if k[1] == CMD_START_MEASURE:
                            request_key = k
                            pending_request = req
                            break
                if pending_request:
                    _LOGGER.info("[RESP] ✓ Start measurement acknowledged")
                    if not pending_request.future.done():
                        pending_request.future.set_result(data)
                    self._pending_requests.pop(request_key, None)
                    self._used_seq_ids.discard(seq_id)
                return

            # Handle Stop Measure response (0x13)
            if cmd_type == CMD_STOP_MEASURE:
                hex_parts = ' '.join([f'{b:02x}' for b in data])
                _LOGGER.debug("[RX] (停止测量响应)[0x %s] len=%d", hex_parts, len(data))
                request_key = (seq_id, CMD_STOP_MEASURE)
                pending_request = self._pending_requests.get(request_key)
                if not pending_request:
                    for k, req in list(self._pending_requests.items()):
                        if k[1] == CMD_STOP_MEASURE:
                            request_key = k
                            pending_request = req
                            break
                if pending_request:
                    _LOGGER.info("[RESP] ✓ Stop measurement acknowledged")
                    if not pending_request.future.done():
                        pending_request.future.set_result(data)
                    self._pending_requests.pop(request_key, None)
                    self._used_seq_ids.discard(seq_id)
                return

            # Handle Read SN response (0x15)
            if cmd_type == CMD_READ_SN:
                hex_parts = ' '.join([f'{b:02x}' for b in data])
                _LOGGER.debug("[RX] (设备SN号响应)[0x %s] len=%d", hex_parts, len(data))
                request_key = (seq_id, CMD_READ_SN)
                pending_request = self._pending_requests.get(request_key)
                if pending_request:
                    if len(data) >= 3:
                        raw_sn = bytes([b for b in data[2:] if 32 <= b <= 126]).decode('ascii', errors='ignore').strip()
                        if raw_sn:
                            self.sn = raw_sn
                            _LOGGER.info("[SN] ✓ Device Serial Number: %s", self.sn)
                            self._update_model_and_device_info()
                    if not pending_request.future.done():
                        pending_request.future.set_result(data)
                    del self._pending_requests[request_key]
                    self._used_seq_ids.discard(seq_id)
                return

            # Handle Read PlaceID response (0x23)
            if cmd_type == CMD_READ_PLACEID:
                hex_parts = ' '.join([f'{b:02x}' for b in data])
                _LOGGER.debug("[RX] (PlaceID响应)[0x %s] len=%d", hex_parts, len(data))
                request_key = (seq_id, CMD_READ_PLACEID)
                pending_request = self._pending_requests.get(request_key)
                if pending_request:
                    if len(data) >= 3:
                        length = data[2]
                        if length > 0 and len(data) >= 3 + length:
                            part_bytes = data[3:3 + length]
                            part_str = bytes([b for b in part_bytes if 32 <= b <= 126]).decode('ascii', errors='ignore').strip()
                            if part_str:
                                if not self.place_id:
                                    self.place_id = part_str
                                else:
                                    self.place_id += part_str
                                _LOGGER.info("[PLACEID] ✓ Device PlaceID: %s", self.place_id)
                                self._update_model_and_device_info()
                    if not pending_request.future.done():
                        pending_request.future.set_result(data)
                    del self._pending_requests[request_key]
                    self._used_seq_ids.discard(seq_id)
                return

            # Handle Read BLE Version response (0x3D)
            if cmd_type == CMD_READ_BLE_VERSION:
                hex_parts = ' '.join([f'{b:02x}' for b in data])
                _LOGGER.debug("[RX] (BLE固件版本响应)[0x %s] len=%d", hex_parts, len(data))
                request_key = (seq_id, CMD_READ_BLE_VERSION)
                pending_request = self._pending_requests.get(request_key)
                if pending_request:
                    if len(data) >= 4:
                        raw_int = (data[2] | (data[3] << 8)) & 0xFFFF
                        release = (data[2] >> 4) & 15
                        beta = data[2] & 15
                        alpha = data[3] & 0xFF
                        self.ble_version = f"{release}.{beta}.{alpha}"
                        self.ble_version_raw = raw_int
                        _LOGGER.info(
                            "[BLE_VER] ✓ BLE Firmware Version: %s (raw int: %d)",
                            self.ble_version, raw_int
                        )
                        self._update_model_and_device_info()
                    if not pending_request.future.done():
                        pending_request.future.set_result(data)
                    del self._pending_requests[request_key]
                    self._used_seq_ids.discard(seq_id)
                return

            # Handle Read Wi-Fi Version response (0x41)
            if cmd_type == CMD_READ_WIFI_VERSION:
                hex_parts = ' '.join([f'{b:02x}' for b in data])
                _LOGGER.debug("[RX] (Wi-Fi固件版本响应)[0x %s] len=%d", hex_parts, len(data))
                request_key = (seq_id, CMD_READ_WIFI_VERSION)
                pending_request = self._pending_requests.get(request_key)
                if pending_request:
                    if len(data) >= 4:
                        raw_int = (data[2] | (data[3] << 8)) & 0xFFFF
                        release = (data[2] >> 4) & 15
                        beta = data[2] & 15
                        alpha = data[3] & 0xFF
                        self.wifi_version = f"{release}.{beta}.{alpha}"
                        self.wifi_version_raw = raw_int
                        _LOGGER.info(
                            "[WIFI_VER] ✓ Wi-Fi Firmware Version: %s (raw int: %d)",
                            self.wifi_version, raw_int
                        )
                        self._update_model_and_device_info()
                    if not pending_request.future.done():
                        pending_request.future.set_result(data)
                    del self._pending_requests[request_key]
                    self._used_seq_ids.discard(seq_id)
                return

            # Get sensor name for logging
            sensor_info = SENSOR_TYPES.get(sensor_id, {})
            sensor_name = sensor_info.get("name", f"0x{sensor_id:02x}")

            # Format: (设备响应)[0x seq-cmd-sensor-value...] 实体XX 传感器名
            hex_parts = ' '.join([f'{b:02x}' for b in data])
            _LOGGER.debug(
                "[RX] (设备响应)[0x %s] 实体%02x %s",
                hex_parts, sensor_id, sensor_name
            )


            # Find matching pending request using (seq_id, sensor_id) key
            request_key = (seq_id, sensor_id)
            pending_request = self._pending_requests.get(request_key)

            if not pending_request:
                _LOGGER.warning(
                    "[RX] ✗ %s: Unexpected response (seq=0x%02x, no matching request)",
                    sensor_name, seq_id
                )
                return

            # Parse value based on sensor type
            # Value is in little-endian format (e.g., 0x31 0x00 = 49)
            parsed_result: float | None = None
            if len(value_bytes) >= 2:
                raw_value = int.from_bytes(value_bytes[:2], byteorder='little')

                # Apply sensor-specific scaling and offsets
                formula_desc = ""
                calculated_value = None

                if sensor_id == 0x05:
                    # Battery Level (%) and Charging Status
                    # value_bytes[0] is battery percentage (0-100)
                    # value_bytes[1] bit 7 is charging status (1=charging, 0=not charging)
                    battery_pct = value_bytes[0]
                    raw_charging = bool((value_bytes[1] >> 7) & 1) if len(value_bytes) >= 2 else False
                    # Cross-validate charging with USB power status:
                    # If USB external power is known to be Disconnected, the battery cannot be charging.
                    # This prevents hardware/PMIC under-voltage warning flags (e.g. 0xec) from misreporting charging.
                    if self.usb_power is False:
                        is_charging = False
                    else:
                        is_charging = raw_charging
                    self.battery_charging = is_charging
                    self.battery_pct = float(battery_pct)
                    calculated_value = float(battery_pct)
                    formula_desc = f"{battery_pct}% (charging={is_charging})"
                elif sensor_id == 0x01:
                    # Temperature: (raw - 4000) / 100
                    calculated_value = (raw_value - 4000) / 100.0
                    formula_desc = f"({raw_value} - 4000) / 100"
                elif sensor_id == 0x06:
                    # Humidity: raw / 100
                    calculated_value = raw_value / 100.0
                    formula_desc = f"{raw_value} / 100"
                elif sensor_id == 0x0A:
                    # Formaldehyde: (raw - 16384) / 1000
                    calculated_value = (raw_value - 16384) / 1000.0
                    formula_desc = f"({raw_value} - 16384) / 1000"
                elif sensor_id in [0x08, 0x09, 0x11, 0x12, 0x13]:
                    # Noise, PM2.5, PM10, TVOC, eCO2: raw value directly
                    calculated_value = raw_value
                    formula_desc = f"{raw_value} (direct)"
                else:
                    # Other sensors use raw value directly (Noise, etc.)
                    calculated_value = raw_value
                    formula_desc = f"{raw_value} (direct)"

                # Validate sensor reading to filter out invalid values, sensor errors, and unready states
                is_valid = True
                filter_reason = ""

                if sensor_id == 0x05:  # Battery (%)
                    if calculated_value < 0 or calculated_value > 100:
                        is_valid = False
                        filter_reason = f"out of range ({calculated_value}%)"

                elif sensor_id == 0x01:  # Temperature (°C)
                    # Formula: (raw - 4000) / 100.0
                    # When sensor is unready/resetting, raw is 0x0000, which calculates to -40.0°C.
                    # Sensirion SHT30 hardware operating limit is -40.0°C to 125.0°C.
                    # Valid temperatures (even extreme cold like -25°C) have non-zero raw values (e.g. 1500).
                    # Only raw == 0 (uninitialized buffer) or raw == 0xFFFF (bus error) or out of physical limits are invalid.
                    if raw_value == 0 or raw_value == 0xFFFF or calculated_value < -40.0 or calculated_value > 125.0:
                        is_valid = False
                        filter_reason = f"unready/error ({calculated_value}°C, raw={raw_value})"

                elif sensor_id == 0x06:  # Humidity (%)
                    # Formula: raw / 100.0
                    # Relative humidity of 0.0% is physically impossible in ambient air.
                    # Raw == 0 indicates unready/reset sensor. Raw == 0xFFFF indicates bus error.
                    if raw_value == 0 or raw_value == 0xFFFF or calculated_value <= 0.0 or calculated_value > 100.0:
                        is_valid = False
                        filter_reason = f"unready/out of range ({calculated_value}%, raw={raw_value})"

                elif sensor_id == 0x0A:  # Formaldehyde (mg/m³)
                    # Upper 2 bits are device status (0 = unready/preheating, 1 = normal).
                    # Lower 14 bits are data (0x3FFF / 16383 is error/unready code per APK HCHOModle).
                    # Raw == 0 indicates unready/uninitialized register.
                    hcho_raw = raw_value & 0x3FFF
                    device_state = (raw_value >> 14) & 3
                    self.hcho_state = device_state
                    if raw_value == 0 or hcho_raw == 0x3FFF or device_state == 0:
                        is_valid = False
                        filter_reason = f"unready/error (raw={raw_value}, state={device_state}, hcho_raw={hcho_raw})"
                    elif calculated_value < 0:
                        is_valid = False
                        filter_reason = f"negative value ({calculated_value})"

                elif sensor_id == 0x08:  # Noise (dB)
                    if calculated_value < 0 or calculated_value > 140:
                        is_valid = False
                        filter_reason = f"out of range ({calculated_value} dB)"

                elif sensor_id in [0x09, 0x11]:  # PM2.5, PM10 (µg/m³)
                    if calculated_value < 0 or calculated_value > 1000:
                        is_valid = False
                        filter_reason = f"out of range ({calculated_value})"

                elif sensor_id == 0x12:  # TVOC (mg/m³)
                    if calculated_value < 0:
                        is_valid = False
                        filter_reason = f"negative value ({calculated_value})"

                elif sensor_id == 0x13:  # eCO2 (ppm)
                    # Baseline atmospheric CO2 concentration is ~400 ppm.
                    if calculated_value < 400 or calculated_value > 10000:
                        is_valid = False
                        filter_reason = f"out of range ({calculated_value} ppm)"

                else:
                    if calculated_value < 0:
                        is_valid = False
                        filter_reason = f"negative value ({calculated_value})"

                unit = sensor_info.get("unit", "")
                value_hex = ' '.join([f'{b:02x}' for b in value_bytes[:2]])
                _LOGGER.debug(
                    "[RX] 解析: 字节[%s] → 原始值%d → 公式[%s] → 最终值%s %s",
                    value_hex, raw_value, formula_desc, calculated_value, unit
                )

                if not is_valid:
                    _LOGGER.warning(
                        "[FILTER] ✗ %s (0x%02x): Skipping invalid reading: %s (formula: %s)",
                        sensor_name, sensor_id, filter_reason, formula_desc
                    )
                    parsed_result = None
                else:
                    # Store valid value
                    self.sensor_data[sensor_id] = calculated_value
                    parsed_result = calculated_value
                    _LOGGER.info(
                        "[RESP] ✓ %s (0x%02x) = %s %s",
                        sensor_name, sensor_id, calculated_value, unit
                    )
            else:
                _LOGGER.warning(
                    "[RX] ✗ %s: Value too short (got %d bytes, need at least 2)",
                    sensor_name, len(value_bytes)
                )

            # Complete the future with parsed result (or None if invalid/filtered)
            if not pending_request.future.done():
                pending_request.future.set_result(parsed_result)

            del self._pending_requests[request_key]
            self._used_seq_ids.discard(seq_id)
            _LOGGER.debug(
                "[CACHE] Removed completed request (seq=%02x, sensor=%02x). "
                "Pending: %d, Used seq_ids: %d",
                seq_id, sensor_id, len(self._pending_requests), len(self._used_seq_ids)
            )

        except Exception as err:
            _LOGGER.error("Error handling notification: %s", err, exc_info=True)

    async def read_sensor(self, sensor_id: int) -> float | None:
        """Read a specific sensor value.

        Args:
            sensor_id: Sensor ID to read

        Returns:
            Sensor value or None if reading fails
        """
        source = self._get_proxy_source()
        _LOGGER.debug("[LOCK] Using BLE lock for proxy '%s' (%s)", source, self.mac_address)
        async with _proxy_ble_session(source):
            async with self._lock:
                return await self._read_sensor_locked(sensor_id)

    async def _read_sensor_locked(self, sensor_id: int) -> float | None:
        """Internal implementation of read_sensor (must hold self._lock)."""
        # Check circuit breaker
        can_attempt, reason = self._circuit_breaker.can_attempt()
        if not can_attempt:
            _LOGGER.warning(
                "[CIRCUIT] Blocked read_sensor for 0x%02x: %s",
                sensor_id, reason
            )
            return None

        # Clean up expired requests
        self._cleanup_expired_requests()

        already_connected = self._client is not None and self._client.is_connected
        try:
            # Ensure connection (will reuse existing or create new)
            client = await self._ensure_connected()

            # Prepare request with random sequence ID
            seq_id = self._get_random_sequence_id()
            request = bytes([seq_id, CMD_READ_SENSOR, sensor_id])

            # Get sensor name for logging
            sensor_info = SENSOR_TYPES.get(sensor_id, {})
            sensor_name = sensor_info.get("name", f"0x{sensor_id:02x}")

            # Format: (请求传感器数据)[0x seq-cmd-sensor] 实体XX 传感器名
            hex_parts = ' '.join([f'{b:02x}' for b in request])
            _LOGGER.debug(
                "[TX] (请求传感器数据)[0x %s] 实体%02x %s",
                hex_parts, sensor_id, sensor_name
            )

            # Create pending request and add to cache
            pending_request = PendingRequest(
                seq_id=seq_id,
                sensor_id=sensor_id,
                future=asyncio.Future(),
                timestamp=time.time()
            )
            request_key = (seq_id, sensor_id)
            self._pending_requests[request_key] = pending_request

            # Send request
            await client.write_gatt_char(EM1003_WRITE_CHAR_UUID, request, response=False)

            # Wait for response with timeout
            try:
                value = await asyncio.wait_for(pending_request.future, timeout=3.0)
                if value is not None:
                    self._circuit_breaker.record_success()
                return value
            except asyncio.TimeoutError:
                _LOGGER.warning(
                    "Timeout (3s) waiting for sensor 0x%02x response (seq=%02x)",
                    sensor_id, seq_id
                )
                # Clean up pending request
                self._pending_requests.pop(request_key, None)
                self._used_seq_ids.discard(seq_id)
                self._circuit_breaker.record_failure()
                return None

        except BleakError as err:
            _LOGGER.error("Bleak error reading sensor %02x: %s", sensor_id, err)
            self._circuit_breaker.record_failure()
            # Clear client so next attempt will create new connection
            self._client = None
            return None
        except Exception as err:
            _LOGGER.error("Error reading sensor %02x: %s", sensor_id, err, exc_info=True)
            self._circuit_breaker.record_failure()
            # Clear client so next attempt will create new connection
            self._client = None
            return None
        finally:
            if not already_connected and self._client and self._client.is_connected:
                try:
                    await self.disconnect()
                except Exception as disconnect_err:
                    _LOGGER.debug("[CONN] Error disconnecting after read_sensor: %s", disconnect_err)

    async def read_battery(self) -> tuple[float | None, bool | None]:
        """Read battery level and charging status.

        Returns:
            Tuple of (battery_percentage, is_charging)
        """
        level = await self.read_sensor(0x05)
        return (level, self.battery_charging)

    async def read_all_sensors(self) -> dict[int, float | None]:
        """Read all sensors using persistent connection.

        Uses circuit breaker pattern to prevent request pile-up during failures.
        Uses request cache to match responses to requests by (seq_id, sensor_id).
        Maintains a persistent BLE connection that is reused across multiple reads.

        Returns:
            Dictionary mapping sensor IDs to their values
        """
        source = self._get_proxy_source()
        _LOGGER.debug("[LOCK] Using BLE lock for proxy '%s' (%s)", source, self.mac_address)
        async with _proxy_ble_session(source):
            async with self._lock:
                return await self._read_all_sensors_locked()

    async def _read_all_sensors_locked(self) -> dict[int, float | None]:
        """Internal implementation of read_all_sensors (must hold self._lock)."""
        _LOGGER.debug("[DIAG] read_all_sensors called for %s", self.mac_address)
        results = {}

        # Check circuit breaker before attempting connection
        can_attempt, reason = self._circuit_breaker.can_attempt()
        if not can_attempt:
            _LOGGER.warning(
                "[CIRCUIT] Blocked read_all_sensors: %s. State: %s",
                reason,
                self._circuit_breaker.get_state_info()
            )
            # Return None for all sensors when circuit is open
            return {sensor_id: None for sensor_id in SENSOR_TYPES.keys()}

        _LOGGER.debug(
            "[CIRCUIT] Attempt allowed: %s. State: %s",
            reason,
            self._circuit_breaker.get_state_info()
        )

        # Clean up any expired requests before starting
        self._cleanup_expired_requests()

        try:
            # Ensure connection (will reuse existing or create new)
            _LOGGER.debug("[CONN] Ensuring connection to %s", self.mac_address)
            client = await self._ensure_connected()

            _LOGGER.info(
                "[DIAG] Using connection to %s, starting sensor reads",
                self.mac_address
            )

            # Query USB external power status first so power mode is known prior to reading sensors
            if client.is_connected:
                try:
                    await self._read_usb_power_internal(client)
                except Exception as usb_err:
                    _LOGGER.debug("[USB] Failed to read usb power state prior to sensor reads: %s", usb_err)

            # Read all sensors using the persistent connection
            sensor_count = len(SENSOR_TYPES)
            _LOGGER.debug(
                "[DIAG] Reading %d sensors: %s",
                sensor_count,
                [f"0x{sid:02x}" for sid in SENSOR_TYPES.keys()]
            )

            ble_response_count = 0

            for idx, sensor_id in enumerate(SENSOR_TYPES.keys(), 1):
                # Check if connection is still valid before each read
                if not client.is_connected:
                    _LOGGER.warning(
                        "[REQ] [%d/%d] Connection lost, aborting remaining sensor reads",
                        idx, sensor_count
                    )
                    # Mark remaining sensors as None
                    for remaining_id in list(SENSOR_TYPES.keys())[idx-1:]:
                        results[remaining_id] = None
                    break

                try:
                    # Get random sequence ID to avoid collisions
                    seq_id = self._get_random_sequence_id()
                    request = bytes([seq_id, CMD_READ_SENSOR, sensor_id])

                    # Get sensor name for logging
                    sensor_info = SENSOR_TYPES.get(sensor_id, {})
                    sensor_name = sensor_info.get("name", f"0x{sensor_id:02x}")

                    if sensor_id in [0x11, 0x12, 0x13]:  # PM10, TVOC, eCO2
                        _LOGGER.info(
                            "[%s] Requesting sensor 0x%02x (seq=%02x)",
                            sensor_name, sensor_id, seq_id
                        )

                    # Format: (请求传感器数据)[0x seq-cmd-sensor] 实体XX 传感器名
                    hex_parts = ' '.join([f'{b:02x}' for b in request])
                    _LOGGER.debug(
                        "[TX] [%d/%d] (请求传感器数据)[0x %s] 实体%02x %s",
                        idx, sensor_count, hex_parts, sensor_id, sensor_name
                    )

                    # Create pending request and add to cache
                    pending_request = PendingRequest(
                        seq_id=seq_id,
                        sensor_id=sensor_id,
                        future=asyncio.Future(),
                        timestamp=time.time()
                    )
                    request_key = (seq_id, sensor_id)
                    self._pending_requests[request_key] = pending_request

                    # Send request
                    await client.write_gatt_char(EM1003_WRITE_CHAR_UUID, request, response=False)

                    # Wait for response with timeout
                    try:
                        parsed_val = await asyncio.wait_for(pending_request.future, timeout=3.0)
                        results[sensor_id] = parsed_val
                        ble_response_count += 1

                        if sensor_id in [0x11, 0x12, 0x13]:  # PM10, TVOC, eCO2
                            _LOGGER.info(
                                "[%s] ✓ Got value: %s",
                                sensor_name, results[sensor_id]
                            )

                        _LOGGER.debug(
                            "[REQ] [%d/%d] ✓ Sensor 0x%02x = %s",
                            idx, sensor_count, sensor_id, results[sensor_id]
                        )
                    except asyncio.TimeoutError:
                        if sensor_id in [0x11, 0x12, 0x13]:  # PM10, TVOC, eCO2
                            _LOGGER.warning(
                                "[%s] ✗ TIMEOUT (3s) - sensor 0x%02x not responding",
                                sensor_name, sensor_id
                            )
                        else:
                            _LOGGER.warning(
                                "[REQ] [%d/%d] ✗ Timeout (3s) waiting for sensor 0x%02x response (seq=%02x)",
                                idx, sensor_count, sensor_id, seq_id
                            )
                        results[sensor_id] = None
                        # Clean up pending request on timeout
                        self._pending_requests.pop(request_key, None)
                        self._used_seq_ids.discard(seq_id)

                    # Small delay between sensor reads
                    await asyncio.sleep(0.3)

                except BleakError as err:
                    if sensor_id in [0x11, 0x12, 0x13]:  # PM10, TVOC, eCO2
                        _LOGGER.error(
                            "[%s] ✗ BLE error: %s",
                            sensor_name, err
                        )
                    else:
                        _LOGGER.error(
                            "[REQ] [%d/%d] ✗ BLE error reading sensor 0x%02x: %s",
                            idx, sensor_count, sensor_id, err
                        )
                    results[sensor_id] = None
                    # Clean up on error
                    request_key = (seq_id, sensor_id)
                    self._pending_requests.pop(request_key, None)
                    self._used_seq_ids.discard(seq_id)

                    # If we get a BLE error, connection might be broken
                    # Check and abort if disconnected
                    if not client.is_connected:
                        _LOGGER.warning(
                            "[REQ] Connection lost after BLE error, aborting remaining reads"
                        )
                        # Mark remaining sensors as None
                        for remaining_id in list(SENSOR_TYPES.keys())[idx:]:
                            results[remaining_id] = None
                        break
                except Exception as err:
                    if sensor_id in [0x11, 0x12, 0x13]:  # PM10, TVOC, eCO2
                        _LOGGER.error(
                            "[%s] ✗ Error: %s",
                            sensor_name, err
                        )
                    else:
                        _LOGGER.error(
                            "[REQ] [%d/%d] ✗ Error reading sensor 0x%02x: %s",
                            idx, sensor_count, sensor_id, err
                        )
                    results[sensor_id] = None
                    # Clean up on error
                    request_key = (seq_id, sensor_id)
                    self._pending_requests.pop(request_key, None)
                    self._used_seq_ids.discard(seq_id)

            # Evaluate dormant vs active measuring state
            current_env_snapshot = (
                results.get(0x01),
                results.get(0x06),
                results.get(0x08),
                results.get(0x09),
            )

            if self.usb_power is True:
                # External USB power connected: device measures continuously
                self._is_dormant = False
                self._frozen_cycle_count = 0
                self._last_env_snapshot = current_env_snapshot
            elif self.usb_power is False:
                # Running on battery (USB disconnected)
                if self.hcho_state == 1:
                    # Fully preheated and active
                    self._is_dormant = False
                    self._frozen_cycle_count = 0
                    self._last_env_snapshot = current_env_snapshot
                elif self._manual_measure_active:
                    # User explicitly started measurement via switch
                    # Keep is_dormant = False so sensors and switch remain active immediately
                    self._is_dormant = False
                    has_valid_readings = any(v is not None for v in current_env_snapshot)
                    if has_valid_readings and self._last_env_snapshot is not None:
                        if current_env_snapshot == self._last_env_snapshot:
                            self._frozen_cycle_count += 1
                            # Only consider auto-sleep if frozen for 2+ consecutive cycles after starting
                            if self._frozen_cycle_count >= 2:
                                self._is_dormant = True
                                self._manual_measure_active = False
                                _LOGGER.debug(
                                    "[SLEEP] Device auto-slept on battery after measurement for %s (cycle_count=%d)",
                                    self.mac_address, self._frozen_cycle_count
                                )
                        else:
                            self._frozen_cycle_count = 0
                    if has_valid_readings:
                        self._last_env_snapshot = current_env_snapshot
                else:
                    # Not manual measure, hcho_state == 0: check if physical button was pressed on device
                    has_valid_readings = any(v is not None for v in current_env_snapshot)
                    if has_valid_readings and self._last_env_snapshot is not None:
                        if current_env_snapshot == self._last_env_snapshot:
                            self._frozen_cycle_count += 1
                            # On battery, identical readings across consecutive cycles indicates dormant MCU RAM cache
                            self._is_dormant = True
                            _LOGGER.debug(
                                "[SLEEP] Detected frozen sensor snapshot on battery for %s (cycle_count=%d): %s",
                                self.mac_address, self._frozen_cycle_count, current_env_snapshot
                            )
                        else:
                            # Readings changed / fluctuating (fan is active, sensors sampling)
                            self._frozen_cycle_count = 0
                            self._is_dormant = False
                    else:
                        # First reading on battery with hcho_state == 0 and not manual: default to dormant
                        self._is_dormant = True

                    if has_valid_readings:
                        self._last_env_snapshot = current_env_snapshot

            # If device is running on battery (USB disconnected) and internal sensors are dormant,
            # suppress stale/frozen environmental readings from MCU RAM.
            if not self.is_measuring:
                _LOGGER.warning(
                    "[SLEEP] Device %s is running on battery (USB disconnected) and internal sensors are dormant (hcho_state=%s). "
                    "Suppressing stale/frozen environmental readings to prevent misleading data.",
                    self.mac_address, self.hcho_state
                )
                for env_sid in [0x01, 0x06, 0x08, 0x09, 0x0A, 0x11, 0x12, 0x13]:
                    results[env_sid] = None

            # Re-verify charging state against USB power
            if self.usb_power is False:
                self.battery_charging = False
            results["battery_charging"] = self.battery_charging
            results["is_measuring"] = self.is_measuring
            results["hcho_state"] = self.hcho_state

            # Also query buzzer state while connection is open
            if client.is_connected:
                try:
                    await self._read_buzzer_state_internal(client)
                except Exception as buzzer_err:
                    _LOGGER.debug("[BUZZER] Failed to read buzzer state during sensor read: %s", buzzer_err)

            # Query Wi-Fi and Cloud status while connection is open
            if client.is_connected:
                try:
                    await self._read_wifi_cloud_state_internal(client)
                except Exception as wifi_err:
                    _LOGGER.debug("[WIFI] Failed to read wifi/cloud state during sensor read: %s", wifi_err)

            # Query device static info (SN, PlaceID, BLE/Wi-Fi versions) if not yet fetched
            if not self._device_info_queried and client.is_connected:
                try:
                    await self._read_device_static_info_internal(client)
                except Exception as info_err:
                    _LOGGER.debug("[INFO] Failed to read device static info: %s", info_err)

            results["wifi_state"] = self.wifi_state
            results["cloud_state"] = self.cloud_state
            results["wifi_sum_number"] = self.wifi_sum_number
            results["current_wifi_index"] = self.current_wifi_index
            results["usb_power"] = self.usb_power
            results["sn"] = self.sn
            results["place_id"] = self.place_id
            results["model"] = self.model
            results["firmware_version"] = self.firmware_version
            results["ble_version"] = self.ble_version
            results["wifi_version"] = self.wifi_version

            # Calculate counts
            valid_count = sum(1 for k, v in results.items() if k in SENSOR_TYPES and v is not None)

            _LOGGER.info(
                "[DIAG] Completed reading all sensors. Valid readings: %d/%d, BLE responses: %d/%d",
                valid_count,
                len(SENSOR_TYPES),
                ble_response_count,
                len(SENSOR_TYPES)
            )

            # Record success or failure to circuit breaker:
            # We evaluate BLE health by whether the device answered our BLE requests,
            # not by whether individual sensor values were filtered out.
            if ble_response_count >= len(SENSOR_TYPES) * 0.5:
                self._circuit_breaker.record_success()
            else:
                _LOGGER.warning(
                    "[CIRCUIT] Low BLE response rate (%d/%d), recording failure",
                    ble_response_count, len(SENSOR_TYPES)
                )
                self._circuit_breaker.record_failure()

            # CRITICAL: Disconnect immediately after reading to free up connection slot
            # EM1003 device responds very fast (<2s), no need to keep connection open
            # This prevents "No backend with an available connection slot" errors
            if self._client and self._client.is_connected:
                try:
                    _LOGGER.info(
                        "[CONN] Disconnecting after successful read to free connection slot"
                    )
                    await self.disconnect()
                except Exception as disconnect_err:
                    _LOGGER.debug(
                        "[CONN] Error during post-read disconnect: %s",
                        disconnect_err
                    )

            return results

        except BleakError as err:
            # Connection or BLE error - clean up client and record failure
            _LOGGER.error(
                "[DIAG] ✗ BLE error while reading sensors from %s: %s",
                self.mac_address, err
            )
            self._circuit_breaker.record_failure()
            # CRITICAL: Disconnect to free connection slot even on error
            if self._client is not None:
                try:
                    await self.disconnect()
                    _LOGGER.debug("[CONN] Disconnected after BLE error to free slot")
                except Exception as disconnect_err:
                    _LOGGER.debug("[CONN] Error during error-path disconnect: %s", disconnect_err)
                self._client = None
            raise
        except Exception as err:
            # Unexpected error - clean up and record failure
            _LOGGER.error(
                "[DIAG] ✗ Error while reading sensors from %s: %s",
                self.mac_address, err, exc_info=True
            )
            self._circuit_breaker.record_failure()
            # CRITICAL: Disconnect to free connection slot even on error
            if self._client is not None:
                try:
                    await self.disconnect()
                    _LOGGER.debug("[CONN] Disconnected after error to free slot")
                except Exception as disconnect_err:
                    _LOGGER.debug("[CONN] Error during error-path disconnect: %s", disconnect_err)
                self._client = None
            raise

    async def _read_buzzer_state_internal(self, client: BleakClient) -> bool | None:
        """Internal helper to read buzzer state using an existing connected client."""
        self._cleanup_expired_requests()
        seq_id = self._get_random_sequence_id()
        request = bytes([seq_id, CMD_BUZZER, 0x00])

        hex_parts = ' '.join([f'{b:02x}' for b in request])
        _LOGGER.debug("[TX] (查询蜂鸣器状态)[0x %s]", hex_parts)

        pending_request = PendingRequest(
            seq_id=seq_id,
            sensor_id=0x00,  # Use 0x00 as placeholder for buzzer
            future=asyncio.Future(),
            timestamp=time.time()
        )
        request_key = (seq_id, 0x00)
        self._pending_requests[request_key] = pending_request

        try:
            await client.write_gatt_char(EM1003_WRITE_CHAR_UUID, request, response=False)
            await asyncio.wait_for(pending_request.future, timeout=2.0)
            return self.buzzer_state
        except asyncio.TimeoutError:
            _LOGGER.debug("Timeout waiting for buzzer state response (seq=%02x)", seq_id)
            self._pending_requests.pop(request_key, None)
            self._used_seq_ids.discard(seq_id)
            return None
        except Exception as err:
            _LOGGER.debug("Error reading buzzer state: %s", err)
            self._pending_requests.pop(request_key, None)
            self._used_seq_ids.discard(seq_id)
            return None

    async def _read_wifi_cloud_state_internal(self, client: BleakClient) -> None:
        """Internal helper to read Wi-Fi and Cloud status using an existing connected client."""
        self._cleanup_expired_requests()
        seq_id = self._get_random_sequence_id()
        request = bytes([seq_id, CMD_READ_WIFI_CURRENT_STATE, 0x00])

        hex_parts = ' '.join([f'{b:02x}' for b in request])
        _LOGGER.debug("[TX] (查询Wi-Fi/云端状态)[0x %s]", hex_parts)

        pending_request = PendingRequest(
            seq_id=seq_id,
            sensor_id=CMD_READ_WIFI_CURRENT_STATE,
            future=asyncio.Future(),
            timestamp=time.time()
        )
        request_key = (seq_id, CMD_READ_WIFI_CURRENT_STATE)
        self._pending_requests[request_key] = pending_request

        try:
            await client.write_gatt_char(EM1003_WRITE_CHAR_UUID, request, response=False)
            await asyncio.wait_for(pending_request.future, timeout=2.0)
        except asyncio.TimeoutError:
            _LOGGER.debug("Timeout waiting for Wi-Fi/Cloud state response (seq=%02x)", seq_id)
            self._pending_requests.pop(request_key, None)
            self._used_seq_ids.discard(seq_id)
        except Exception as err:
            _LOGGER.debug("Error reading Wi-Fi/Cloud state: %s", err)
            self._pending_requests.pop(request_key, None)
            self._used_seq_ids.discard(seq_id)

    async def _read_usb_power_internal(self, client: BleakClient) -> None:
        """Internal helper to read USB external power status using an existing connected client."""
        self._cleanup_expired_requests()
        seq_id = self._get_random_sequence_id()
        request = bytes([seq_id, CMD_READ_USB_STATE, 0x00])

        hex_parts = ' '.join([f'{b:02x}' for b in request])
        _LOGGER.debug("[TX] (查询USB供电状态)[0x %s]", hex_parts)

        pending_request = PendingRequest(
            seq_id=seq_id,
            sensor_id=CMD_READ_USB_STATE,
            future=asyncio.Future(),
            timestamp=time.time()
        )
        request_key = (seq_id, CMD_READ_USB_STATE)
        self._pending_requests[request_key] = pending_request

        try:
            await client.write_gatt_char(EM1003_WRITE_CHAR_UUID, request, response=False)
            await asyncio.wait_for(pending_request.future, timeout=2.0)
        except asyncio.TimeoutError:
            _LOGGER.debug("Timeout waiting for USB power state response (seq=%02x)", seq_id)
            self._pending_requests.pop(request_key, None)
            self._used_seq_ids.discard(seq_id)
        except Exception as err:
            _LOGGER.debug("Error reading USB power state: %s", err)
            self._pending_requests.pop(request_key, None)
            self._used_seq_ids.discard(seq_id)

    async def _read_sn_internal(self, client: BleakClient) -> str | None:
        """Internal helper to read device Serial Number using connected client."""
        self._cleanup_expired_requests()
        seq_id = self._get_random_sequence_id()
        request = bytes([seq_id, CMD_READ_SN, 0x00])

        hex_parts = ' '.join([f'{b:02x}' for b in request])
        _LOGGER.debug("[TX] (查询设备SN号)[0x %s]", hex_parts)

        pending_request = PendingRequest(
            seq_id=seq_id,
            sensor_id=CMD_READ_SN,
            future=asyncio.Future(),
            timestamp=time.time()
        )
        request_key = (seq_id, CMD_READ_SN)
        self._pending_requests[request_key] = pending_request

        try:
            await client.write_gatt_char(EM1003_WRITE_CHAR_UUID, request, response=False)
            await asyncio.wait_for(pending_request.future, timeout=2.0)
            return self.sn
        except asyncio.TimeoutError:
            _LOGGER.debug("Timeout waiting for SN response (seq=%02x)", seq_id)
            self._pending_requests.pop(request_key, None)
            self._used_seq_ids.discard(seq_id)
            return None
        except Exception as err:
            _LOGGER.debug("Error reading SN: %s", err)
            self._pending_requests.pop(request_key, None)
            self._used_seq_ids.discard(seq_id)
            return None

    async def _read_place_id_internal(self, client: BleakClient, wifi_index: int = 0, part: int = 0) -> str | None:
        """Internal helper to read Place ID using connected client."""
        self._cleanup_expired_requests()
        seq_id = self._get_random_sequence_id()
        request = bytes([seq_id, CMD_READ_PLACEID, wifi_index, part])

        hex_parts = ' '.join([f'{b:02x}' for b in request])
        _LOGGER.debug("[TX] (查询PlaceID part=%d)[0x %s]", part, hex_parts)

        pending_request = PendingRequest(
            seq_id=seq_id,
            sensor_id=CMD_READ_PLACEID,
            future=asyncio.Future(),
            timestamp=time.time()
        )
        request_key = (seq_id, CMD_READ_PLACEID)
        self._pending_requests[request_key] = pending_request

        try:
            await client.write_gatt_char(EM1003_WRITE_CHAR_UUID, request, response=False)
            await asyncio.wait_for(pending_request.future, timeout=1.0)
            return self.place_id
        except asyncio.TimeoutError:
            _LOGGER.debug("Timeout waiting for PlaceID response (seq=%02x, part=%d)", seq_id, part)
            self._pending_requests.pop(request_key, None)
            self._used_seq_ids.discard(seq_id)
            return None
        except Exception as err:
            _LOGGER.debug("Error reading PlaceID: %s", err)
            self._pending_requests.pop(request_key, None)
            self._used_seq_ids.discard(seq_id)
            return None

    async def _read_ble_version_internal(self, client: BleakClient) -> str | None:
        """Internal helper to read BLE firmware version using connected client."""
        self._cleanup_expired_requests()
        seq_id = self._get_random_sequence_id()
        request = bytes([seq_id, CMD_READ_BLE_VERSION, 0x00])

        hex_parts = ' '.join([f'{b:02x}' for b in request])
        _LOGGER.debug("[TX] (查询BLE固件版本)[0x %s]", hex_parts)

        pending_request = PendingRequest(
            seq_id=seq_id,
            sensor_id=CMD_READ_BLE_VERSION,
            future=asyncio.Future(),
            timestamp=time.time()
        )
        request_key = (seq_id, CMD_READ_BLE_VERSION)
        self._pending_requests[request_key] = pending_request

        try:
            await client.write_gatt_char(EM1003_WRITE_CHAR_UUID, request, response=False)
            await asyncio.wait_for(pending_request.future, timeout=2.0)
            return self.ble_version
        except asyncio.TimeoutError:
            _LOGGER.debug("Timeout waiting for BLE version response (seq=%02x)", seq_id)
            self._pending_requests.pop(request_key, None)
            self._used_seq_ids.discard(seq_id)
            return None
        except Exception as err:
            _LOGGER.debug("Error reading BLE version: %s", err)
            self._pending_requests.pop(request_key, None)
            self._used_seq_ids.discard(seq_id)
            return None

    async def _read_wifi_version_internal(self, client: BleakClient) -> str | None:
        """Internal helper to read Wi-Fi firmware version using connected client."""
        self._cleanup_expired_requests()
        seq_id = self._get_random_sequence_id()
        request = bytes([seq_id, CMD_READ_WIFI_VERSION, 0x00])

        hex_parts = ' '.join([f'{b:02x}' for b in request])
        _LOGGER.debug("[TX] (查询Wi-Fi固件版本)[0x %s]", hex_parts)

        pending_request = PendingRequest(
            seq_id=seq_id,
            sensor_id=CMD_READ_WIFI_VERSION,
            future=asyncio.Future(),
            timestamp=time.time()
        )
        request_key = (seq_id, CMD_READ_WIFI_VERSION)
        self._pending_requests[request_key] = pending_request

        try:
            await client.write_gatt_char(EM1003_WRITE_CHAR_UUID, request, response=False)
            await asyncio.wait_for(pending_request.future, timeout=2.0)
            return self.wifi_version
        except asyncio.TimeoutError:
            _LOGGER.debug("Timeout waiting for Wi-Fi version response (seq=%02x)", seq_id)
            self._pending_requests.pop(request_key, None)
            self._used_seq_ids.discard(seq_id)
            return None
        except Exception as err:
            _LOGGER.debug("Error reading Wi-Fi version: %s", err)
            self._pending_requests.pop(request_key, None)
            self._used_seq_ids.discard(seq_id)
            return None

    async def _read_device_static_info_internal(self, client: BleakClient) -> None:
        """Read static device info (SN, PlaceID, BLE and Wi-Fi versions) once."""
        _LOGGER.debug("[INFO] Reading device static information (SN, PlaceID, Firmware versions)")

        # 1. Read SN (0x15)
        if self.sn is None and client.is_connected:
            try:
                await self._read_sn_internal(client)
                await asyncio.sleep(0.1)
            except Exception as err:
                _LOGGER.debug("[SN] Failed to read SN: %s", err)

        # 2. Read PlaceID (0x23, index 0, part 0)
        if not self._place_id_queried and client.is_connected:
            self._place_id_queried = True
            try:
                await self._read_place_id_internal(client, wifi_index=0, part=0)
                await asyncio.sleep(0.1)
                if self.place_id and len(self.place_id) >= 16 and client.is_connected:
                    await self._read_place_id_internal(client, wifi_index=0, part=1)
                    await asyncio.sleep(0.1)
            except Exception as err:
                _LOGGER.debug("[PLACEID] Failed to read PlaceID: %s", err)

        # 3. Read Wi-Fi firmware version (0x41)
        if self.wifi_version is None and client.is_connected:
            try:
                await self._read_wifi_version_internal(client)
                await asyncio.sleep(0.1)
            except Exception as err:
                _LOGGER.debug("[WIFI_VER] Failed to read Wi-Fi version: %s", err)

        # 4. Read BLE firmware version (0x3D)
        if self.ble_version is None and client.is_connected:
            try:
                await self._read_ble_version_internal(client)
                await asyncio.sleep(0.1)
            except Exception as err:
                _LOGGER.debug("[BLE_VER] Failed to read BLE version: %s", err)

        self._info_query_attempts += 1
        if (self.sn and self.wifi_version and self.ble_version and self._place_id_queried) or self._info_query_attempts >= 2:
            self._device_info_queried = True
            self._update_model_and_device_info()
            _LOGGER.info(
                "[INFO] Device static info query completed: Model=%s, SN=%s, PlaceID=%s, FW=%s (Attempts: %d)",
                self.model, self.sn, self.place_id, self.firmware_version, self._info_query_attempts
            )

    async def read_buzzer_state(self) -> bool | None:
        """Read current buzzer state.

        Returns:
            True if buzzer is on, False if off, None if reading fails
        """
        source = self._get_proxy_source()
        async with _proxy_ble_session(source):
            async with self._lock:
                # Check circuit breaker
                can_attempt, reason = self._circuit_breaker.can_attempt()
                if not can_attempt:
                    _LOGGER.warning(
                        "[CIRCUIT] Blocked read_buzzer_state: %s",
                        reason
                    )
                    return None

                already_connected = self._client is not None and self._client.is_connected
                try:
                    # Ensure connection
                    client = await self._ensure_connected()
                    res = await self._read_buzzer_state_internal(client)
                    if res is not None:
                        self._circuit_breaker.record_success()
                    else:
                        self._circuit_breaker.record_failure()
                    return res

                except BleakError as err:
                    _LOGGER.error("Bleak error reading buzzer state: %s", err)
                    self._circuit_breaker.record_failure()
                    self._client = None
                    return None
                except Exception as err:
                    _LOGGER.error("Error reading buzzer state: %s", err, exc_info=True)
                    self._circuit_breaker.record_failure()
                    self._client = None
                    return None
                finally:
                    if not already_connected and self._client and self._client.is_connected:
                        try:
                            await self.disconnect()
                        except Exception as disconnect_err:
                            _LOGGER.debug("[CONN] Error disconnecting after read_buzzer_state: %s", disconnect_err)

    async def set_buzzer_state(self, turn_on: bool) -> bool:
        """Set buzzer state.

        Args:
            turn_on: True to turn on buzzer, False to turn off

        Returns:
            True if successful, False otherwise
        """
        source = self._get_proxy_source()
        async with _proxy_ble_session(source):
            async with self._lock:
                # Check circuit breaker
                can_attempt, reason = self._circuit_breaker.can_attempt()
                if not can_attempt:
                    _LOGGER.warning(
                        "[CIRCUIT] Blocked set_buzzer_state: %s",
                        reason
                    )
                    return False

            # Clean up expired requests
            self._cleanup_expired_requests()

            already_connected = self._client is not None and self._client.is_connected
            try:
                # Ensure connection
                client = await self._ensure_connected()

                # Prepare request with random sequence ID
                # Set buzzer state: [seq_id][0x50][0x01][state]
                seq_id = self._get_random_sequence_id()
                state_byte = BUZZER_ON if turn_on else BUZZER_OFF
                request = bytes([seq_id, CMD_BUZZER, 0x01, state_byte])

                hex_parts = ' '.join([f'{b:02x}' for b in request])
                _LOGGER.debug(
                    "[TX] (设置蜂鸣器状态)[0x %s] %s",
                    hex_parts,
                    "开启" if turn_on else "关闭"
                )

                # Create pending request and add to cache
                pending_request = PendingRequest(
                    seq_id=seq_id,
                    sensor_id=0x01,  # Use 0x01 for buzzer set operation
                    future=asyncio.Future(),
                    timestamp=time.time()
                )
                request_key = (seq_id, 0x01)
                self._pending_requests[request_key] = pending_request

                # Send request
                await client.write_gatt_char(EM1003_WRITE_CHAR_UUID, request, response=False)

                # Wait for response with timeout
                try:
                    await asyncio.wait_for(pending_request.future, timeout=2.0)

                    # Set command received, now query to verify the actual state
                    # The set response may not contain reliable state info, so we query separately
                    _LOGGER.debug("[BUZZER] Set command acknowledged, querying actual state...")

                    # Small delay to allow device to process the command
                    await asyncio.sleep(0.1)

                    # Query current state
                    query_seq_id = self._get_random_sequence_id()
                    query_request = bytes([query_seq_id, CMD_BUZZER, 0x00])

                    query_hex = ' '.join([f'{b:02x}' for b in query_request])
                    _LOGGER.debug("[TX] (验证蜂鸣器状态)[0x %s]", query_hex)

                    # Create pending request for query
                    query_pending = PendingRequest(
                        seq_id=query_seq_id,
                        sensor_id=0x00,
                        future=asyncio.Future(),
                        timestamp=time.time()
                    )
                    query_key = (query_seq_id, 0x00)
                    self._pending_requests[query_key] = query_pending

                    # Send query request
                    await client.write_gatt_char(EM1003_WRITE_CHAR_UUID, query_request, response=False)

                    # Wait for query response
                    await asyncio.wait_for(query_pending.future, timeout=2.0)

                    # Now verify the state
                    if self.buzzer_state == turn_on:
                        _LOGGER.info(
                            "[BUZZER] ✓ Successfully %s buzzer (verified)",
                            "turned on" if turn_on else "turned off"
                        )
                        self._circuit_breaker.record_success()
                        return True
                    else:
                        _LOGGER.warning(
                            "[BUZZER] ✗ State mismatch after verification: expected %s, got %s",
                            turn_on, self.buzzer_state
                        )
                        self._circuit_breaker.record_failure()
                        return False

                except asyncio.TimeoutError:
                    _LOGGER.warning(
                        "Timeout waiting for buzzer response (set seq=%02x)",
                        seq_id
                    )
                    # Clean up any pending requests
                    self._pending_requests.pop((seq_id, 0x01), None)
                    self._used_seq_ids.discard(seq_id)
                    # Also clean up query request if it exists
                    if 'query_seq_id' in locals():
                        self._pending_requests.pop((query_seq_id, 0x00), None)
                        self._used_seq_ids.discard(query_seq_id)
                    self._circuit_breaker.record_failure()
                    return False

            except BleakError as err:
                _LOGGER.error("Bleak error setting buzzer state: %s", err)
                self._circuit_breaker.record_failure()
                self._client = None
                return False
            except Exception as err:
                _LOGGER.error("Error setting buzzer state: %s", err, exc_info=True)
                self._circuit_breaker.record_failure()
                self._client = None
                return False
            finally:
                if not already_connected and self._client and self._client.is_connected:
                    try:
                        await self.disconnect()
                    except Exception as disconnect_err:
                        _LOGGER.debug("[CONN] Error disconnecting after set_buzzer_state: %s", disconnect_err)

    async def activate_wifi(self, wifi_index: int = 0) -> bool:
        """Activate Wi-Fi on device.

        Args:
            wifi_index: Index of configured Wi-Fi network profile (default: 0)

        Returns:
            True if activation command was acknowledged, False otherwise
        """
        source = self._get_proxy_source()
        async with _proxy_ble_session(source):
            async with self._lock:
                # Check circuit breaker
                can_attempt, reason = self._circuit_breaker.can_attempt()
                if not can_attempt:
                    _LOGGER.warning("[CIRCUIT] Blocked activate_wifi: %s", reason)
                    return False

            self._cleanup_expired_requests()
            already_connected = self._client is not None and self._client.is_connected

            try:
                client = await self._ensure_connected()

                seq_id = self._get_random_sequence_id()
                request = bytes([seq_id, CMD_ACTIVATE_WIFI, wifi_index])

                hex_parts = ' '.join([f'{b:02x}' for b in request])
                _LOGGER.info("[TX] (激活Wi-Fi)[0x %s] profile_index=%d", hex_parts, wifi_index)

                pending_request = PendingRequest(
                    seq_id=seq_id,
                    sensor_id=CMD_ACTIVATE_WIFI,
                    future=asyncio.Future(),
                    timestamp=time.time()
                )
                request_key = (seq_id, CMD_ACTIVATE_WIFI)
                self._pending_requests[request_key] = pending_request

                await client.write_gatt_char(EM1003_WRITE_CHAR_UUID, request, response=False)
                await asyncio.wait_for(pending_request.future, timeout=3.0)
                _LOGGER.info("[WIFI] ✓ Wi-Fi activation command acknowledged")
                self._circuit_breaker.record_success()

                # Also try to read back status immediately if still connected
                try:
                    await self._read_wifi_cloud_state_internal(client)
                except Exception:
                    pass

                return True
            except asyncio.TimeoutError:
                _LOGGER.warning("Timeout waiting for Wi-Fi activation response (seq=%02x)", seq_id)
                self._pending_requests.pop((seq_id, CMD_ACTIVATE_WIFI), None)
                self._used_seq_ids.discard(seq_id)
                self._circuit_breaker.record_failure()
                return False
            except Exception as err:
                _LOGGER.error("Error activating Wi-Fi: %s", err)
                self._pending_requests.pop((seq_id, CMD_ACTIVATE_WIFI), None)
                self._used_seq_ids.discard(seq_id)
                self._circuit_breaker.record_failure()
                return False
            finally:
                if not already_connected and self._client and self._client.is_connected:
                    try:
                        await self.disconnect()
                    except Exception as disconnect_err:
                        _LOGGER.debug("[CONN] Error disconnecting after activate_wifi: %s", disconnect_err)

    async def set_measurement_state(self, turn_on: bool) -> bool:
        """Start or stop measurement on device.

        Args:
            turn_on: True to start measurement, False to stop/enter sleep

        Returns:
            True if command was sent successfully, False otherwise
        """
        # Low-battery UVLO safety check (mimicking official APK behavior):
        # Prevent starting measurement if running on battery and charge <= 10%
        if turn_on and self.usb_power is False:
            if self.battery_pct is not None and self.battery_pct <= 10:
                _LOGGER.warning(
                    "[MEASURE] Cannot start measurement for %s: battery is too low (%.0f%% <= 10%%) and USB disconnected",
                    self.mac_address, self.battery_pct
                )
                return False

        source = self._get_proxy_source()
        async with _proxy_ble_session(source):
            async with self._lock:
                can_attempt, reason = self._circuit_breaker.can_attempt()
                if not can_attempt:
                    _LOGGER.warning(
                        "[CIRCUIT] Blocked set_measurement_state for %s: %s",
                        self.mac_address, reason
                    )
                    return False

                self._cleanup_expired_requests()
                already_connected = self._client is not None and self._client.is_connected

                try:
                    client = await self._ensure_connected()

                    seq_id = self._get_random_sequence_id()
                    cmd = CMD_START_MEASURE if turn_on else CMD_STOP_MEASURE
                    arg_byte = 0x00 if turn_on else 0x05
                    request = bytes([seq_id, cmd, arg_byte])

                    hex_parts = ' '.join([f'{b:02x}' for b in request])
                    _LOGGER.debug(
                        "[TX] (%s)[0x %s]",
                        "发送开始测量命令" if turn_on else "发送停止测量命令",
                        hex_parts
                    )

                    pending_request = PendingRequest(
                        seq_id=seq_id,
                        sensor_id=cmd,
                        future=asyncio.Future(),
                        timestamp=time.time()
                    )
                    request_key = (seq_id, cmd)
                    self._pending_requests[request_key] = pending_request

                    await client.write_gatt_char(EM1003_WRITE_CHAR_UUID, request, response=False)

                    try:
                        await asyncio.wait_for(pending_request.future, timeout=3.0)
                        _LOGGER.info(
                            "[MEASURE] Measurement %s command acknowledged for %s",
                            "START" if turn_on else "STOP",
                            self.mac_address
                        )
                    except asyncio.TimeoutError:
                        _LOGGER.warning(
                            "[MEASURE] No ACK received for measurement %s command on %s (command sent without response)",
                            "START" if turn_on else "STOP",
                            self.mac_address
                        )
                    finally:
                        self._pending_requests.pop(request_key, None)
                        self._used_seq_ids.discard(seq_id)

                    # Update internal state tracking
                    if turn_on:
                        self._is_dormant = False
                        self._frozen_cycle_count = 0
                        self._manual_measure_active = True
                        self._last_env_snapshot = None
                    else:
                        self._is_dormant = True
                        self._frozen_cycle_count = 0
                        self._manual_measure_active = False
                        self._last_env_snapshot = None
                        # Clear stale environmental readings
                        for env_sid in [0x01, 0x06, 0x08, 0x09, 0x0A, 0x11, 0x12, 0x13]:
                            self.sensor_data[env_sid] = None

                    self._circuit_breaker.record_success()
                    return True

                except BleakError as err:
                    _LOGGER.error("Bleak error setting measurement state for %s: %s", self.mac_address, err)
                    self._circuit_breaker.record_failure()
                    self._client = None
                    return False
                except Exception as err:
                    _LOGGER.error(
                        "[MEASURE] Failed to %s measurement for %s: %s",
                        "start" if turn_on else "stop",
                        self.mac_address, err
                    )
                    self._circuit_breaker.record_failure()
                    self._client = None
                    return False
                finally:
                    if not already_connected and self._client and self._client.is_connected:
                        try:
                            await self.disconnect()
                        except Exception as disconnect_err:
                            _LOGGER.debug("[CONN] Error disconnecting after set_measurement_state: %s", disconnect_err)
