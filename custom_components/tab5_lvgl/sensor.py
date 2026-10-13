"""Sensor entities for Tab5 runtime telemetry."""

from __future__ import annotations

import math
from datetime import timedelta

from homeassistant.components import mqtt
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
    UnitOfElectricPotential,
    UnitOfInformation,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .capabilities import merged_capabilities_data, supports
from .const import (
    CONF_HA_PREFIX,
    DEFAULT_PREFIX,
    TOPIC_SENSOR_BATTERY_VOLTAGE,
    TOPIC_SENSOR_CHIP_TEMPERATURE,
    TOPIC_SENSOR_FREE_INTERNAL,
    TOPIC_SENSOR_FREE_PSRAM,
    TOPIC_SENSOR_SOC,
    TOPIC_SENSOR_WIFI_RSSI,
    TOPIC_STAT_FIRMWARE,
    TOPIC_STAT_IP,
    TOPIC_STAT_MAC,
    TOPIC_STAT_RESET_REASON,
    TOPIC_TELE_UPTIME,
)
from .device_helpers import (
    entry_base_topic,
    entry_device_id,
    entry_device_info,
    normalise_topic,
    sensor_topic,
    state_topic,
    telemetry_topic,
)
from .local_io import (
    LOCAL_IO_TEMPERATURE,
    entry_local_io,
    local_io_announced_entity_id,
    local_io_state_topic,
    local_io_unique_id,
    parse_on_off_payload,
    parse_temperature_payload,
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities
) -> None:
    base_topic = entry_base_topic(entry)
    merged = merged_capabilities_data(entry)
    ha_prefix = normalise_topic(merged.get(CONF_HA_PREFIX), DEFAULT_PREFIX)
    entities = []
    if supports(merged, "battery_soc"):
        entities.append(Tab5BatterySensor(entry, base_topic))
    if supports(merged, "battery_voltage"):
        entities.append(HomeTilesBatteryVoltageSensor(entry, base_topic))
    if supports(merged, "runtime_diagnostics"):
        entities.extend((
            HomeTilesWifiSignalSensor(entry, base_topic),
            HomeTilesLastRestartSensor(entry, base_topic),
            HomeTilesResetReasonSensor(entry, base_topic),
            HomeTilesFreeMemorySensor(entry, base_topic, "free_internal_memory",
                                      TOPIC_SENSOR_FREE_INTERNAL),
            HomeTilesFreeMemorySensor(entry, base_topic, "free_psram",
                                      TOPIC_SENSOR_FREE_PSRAM),
            HomeTilesChipTemperatureSensor(entry, base_topic),
            HomeTilesTextDiagnosticSensor(entry, base_topic, "ip_address",
                                          TOPIC_STAT_IP, "mdi:ip-network"),
            HomeTilesTextDiagnosticSensor(entry, base_topic, "firmware_version",
                                          TOPIC_STAT_FIRMWARE, "mdi:chip"),
            HomeTilesTextDiagnosticSensor(entry, base_topic, "mac_address",
                                          TOPIC_STAT_MAC, "mdi:network-outline"),
        ))
    if supports(merged, "legacy_external_temperature"):
        # Legacy firmware exposed one fixed external sensor on the HA prefix.
        # New firmware announces every channel through local_io, including an
        # explicit empty list, so creating both would leave a duplicate entity.
        entities.append(Tab5ExternalTemperatureSensor(entry, ha_prefix))
    entities.extend(
        HomeTilesLocalTemperatureSensor(entry, base_topic, descriptor)
        for descriptor in entry_local_io(entry)
        if descriptor["type"] == LOCAL_IO_TEMPERATURE
    )
    async_add_entities(entities)


class HomeTilesLocalTemperatureSensor(SensorEntity):
    """Temperature channel physically attached to a HomeTiles panel."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:thermometer"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_should_poll = False

    def __init__(
        self, entry: ConfigEntry, base_topic: str, descriptor: dict
    ) -> None:
        self._device_info = entry_device_info(entry)
        self._attr_unique_id = local_io_unique_id(entry_device_id(entry), descriptor)
        if announced_entity_id := local_io_announced_entity_id(descriptor):
            # See HomeTilesLocalRelay: this is a registry suggestion, not a
            # forced runtime ID, so Home Assistant still resolves collisions.
            self.entity_id = announced_entity_id
        self._attr_name = descriptor["name"]
        self._attr_native_unit_of_measurement = descriptor["unit"]
        self._precision = descriptor["precision"]
        self._attr_suggested_display_precision = self._precision
        self._topic_state = local_io_state_topic(base_topic, descriptor["id"])
        self._topic_available = state_topic(base_topic, "connected")
        self._unsub_state = None
        self._unsub_available = None
        self._panel_available: bool | None = None
        self._sensor_available = False
        self._attr_available = False

    def _refresh_available(self) -> None:
        self._attr_available = (
            self._panel_available is not False and self._sensor_available
        )

    @property
    def device_info(self):
        return self._device_info

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        async def _handle_state(msg: mqtt.ReceiveMessage) -> None:
            value = parse_temperature_payload(msg.payload)
            if value is None:
                if msg.payload.strip().lower() in {
                    "unknown", "unavailable", "nan", "inf", "-inf"
                }:
                    self._sensor_available = False
                    self._refresh_available()
                    self._attr_native_value = None
                    self.async_write_ha_state()
                return
            self._attr_native_value = round(value, self._precision)
            self._sensor_available = True
            self._refresh_available()
            self.async_write_ha_state()

        async def _handle_available(msg: mqtt.ReceiveMessage) -> None:
            available = parse_on_off_payload(msg.payload)
            if available is None:
                return
            self._panel_available = available
            self._refresh_available()
            self.async_write_ha_state()

        self._unsub_state = await mqtt.async_subscribe(
            self.hass, self._topic_state, _handle_state
        )
        self._unsub_available = await mqtt.async_subscribe(
            self.hass, self._topic_available, _handle_available
        )

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub_state:
            self._unsub_state()
            self._unsub_state = None
        if self._unsub_available:
            self._unsub_available()
            self._unsub_available = None
        await super().async_will_remove_from_hass()


class Tab5BatterySensor(SensorEntity):
    """Battery state-of-charge in percent."""

    _attr_has_entity_name = True
    _attr_translation_key = "battery_soc"
    _attr_icon = "mdi:battery"
    _attr_native_unit_of_measurement = "%"
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0

    def __init__(self, entry: ConfigEntry, base_topic: str) -> None:
        self._entry = entry
        self._device_info = entry_device_info(entry)
        self._attr_unique_id = f"{entry_device_id(entry)}_battery_soc"
        self._topic_state = sensor_topic(base_topic, TOPIC_SENSOR_SOC)
        self._unsub_state = None

    @property
    def device_info(self):
        return self._device_info

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        async def _handle_state(msg: mqtt.ReceiveMessage) -> None:
            raw = msg.payload.strip()
            if not raw:
                return
            if raw.endswith("%"):
                raw = raw[:-1].strip()
            try:
                value = float(raw)
            except (TypeError, ValueError):
                return
            if math.isnan(value) or math.isinf(value):
                return
            value_int = int(round(value))
            if value_int < 0:
                value_int = 0
            if value_int > 100:
                value_int = 100
            self._attr_native_value = value_int
            self.async_write_ha_state()

        self._unsub_state = await mqtt.async_subscribe(
            self.hass, self._topic_state, _handle_state
        )

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub_state:
            self._unsub_state()
            self._unsub_state = None
        await super().async_will_remove_from_hass()


class Tab5ExternalTemperatureSensor(SensorEntity):
    """External DS18x20 temperature from Tab5."""

    _attr_has_entity_name = True
    _attr_translation_key = "external_temperature"
    _attr_icon = "mdi:thermometer"
    _attr_native_unit_of_measurement = "C"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1

    def __init__(self, entry: ConfigEntry, ha_prefix: str) -> None:
        self._entry = entry
        self._device_info = entry_device_info(entry)
        self._attr_unique_id = f"{entry_device_id(entry)}_external_temperature"
        self._topic_state = f"{ha_prefix}/sensor/tab5_external_temperature/state"
        self._unsub_state = None
        self._attr_available = False

    @property
    def device_info(self):
        return self._device_info

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        async def _handle_state(msg: mqtt.ReceiveMessage) -> None:
            raw = msg.payload.strip()
            if not raw:
                return
            lowered = raw.lower()
            if lowered in {"unknown", "unavailable", "nan", "inf", "-inf"}:
                self._attr_available = False
                self._attr_native_value = None
                self.async_write_ha_state()
                return
            raw = raw.replace(",", ".")
            try:
                value = float(raw)
            except (TypeError, ValueError):
                return
            if math.isnan(value) or math.isinf(value):
                self._attr_available = False
                self._attr_native_value = None
            else:
                self._attr_available = True
                self._attr_native_value = round(value, 1)
            self.async_write_ha_state()

        self._unsub_state = await mqtt.async_subscribe(
            self.hass, self._topic_state, _handle_state
        )

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub_state:
            self._unsub_state()
            self._unsub_state = None
        await super().async_will_remove_from_hass()


# Stable keys the panel publishes on {base}/stat/reset_reason.
RESET_REASONS = [
    "power_on", "external", "software", "panic", "interrupt_watchdog",
    "task_watchdog", "other_watchdog", "deep_sleep", "brownout", "sdio", "usb",
    "jtag", "efuse", "power_glitch", "cpu_lockup", "unknown",
]
UNAVAILABLE_PAYLOADS = {"unknown", "unavailable", "nan", "inf", "-inf"}


def parse_finite_float(payload: str) -> float | None:
    """A finite number, or None for empty, non-numeric or NaN/inf payloads."""
    raw = payload.strip().replace(",", ".")
    if not raw:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if math.isnan(value) or math.isinf(value):
        return None
    return value


class HomeTilesPanelDiagnosticSensor(SensorEntity):
    """One retained panel telemetry topic, unavailable while the panel is offline."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: ConfigEntry, base_topic: str, key: str, topic: str) -> None:
        self._device_info = entry_device_info(entry)
        self._attr_unique_id = f"{entry_device_id(entry)}_{key}"
        self._attr_translation_key = key
        self._topic_state = topic
        self._topic_available = state_topic(base_topic, "connected")
        self._unsub = []
        self._panel_available: bool | None = None
        self._value_available = False
        self._attr_available = False

    @property
    def device_info(self):
        return self._device_info

    def _parse(self, payload: str):
        """Return the new native value, or None when the value is unavailable."""
        raise NotImplementedError

    def _refresh_available(self) -> None:
        self._attr_available = self._panel_available is not False and self._value_available

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        async def _handle_state(msg: mqtt.ReceiveMessage) -> None:
            payload = msg.payload.strip()
            if not payload:
                # An empty retained payload only clears the topic.
                return
            value = None if payload.lower() in UNAVAILABLE_PAYLOADS else self._parse(payload)
            self._value_available = value is not None
            if value is not None:
                self._attr_native_value = value
            self._refresh_available()
            self.async_write_ha_state()

        async def _handle_available(msg: mqtt.ReceiveMessage) -> None:
            available = parse_on_off_payload(msg.payload)
            if available is None:
                return
            self._panel_available = available
            self._refresh_available()
            self.async_write_ha_state()

        self._unsub = [
            await mqtt.async_subscribe(self.hass, self._topic_state, _handle_state),
            await mqtt.async_subscribe(self.hass, self._topic_available, _handle_available),
        ]

    async def async_will_remove_from_hass(self) -> None:
        for unsub in self._unsub:
            unsub()
        self._unsub = []
        await super().async_will_remove_from_hass()


class HomeTilesBatteryVoltageSensor(HomeTilesPanelDiagnosticSensor):
    """Filtered cell voltage of the panel's own battery."""

    _attr_icon = "mdi:battery"
    _attr_device_class = SensorDeviceClass.VOLTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfElectricPotential.VOLT
    _attr_suggested_display_precision = 2

    def __init__(self, entry: ConfigEntry, base_topic: str) -> None:
        super().__init__(entry, base_topic, "battery_voltage",
                         sensor_topic(base_topic, TOPIC_SENSOR_BATTERY_VOLTAGE))

    def _parse(self, payload: str):
        value = parse_finite_float(payload)
        return round(value, 2) if value is not None and 0 < value < 10 else None


class HomeTilesWifiSignalSensor(HomeTilesPanelDiagnosticSensor):
    """Wi-Fi RSSI; unavailable while the panel uses Ethernet."""

    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = SIGNAL_STRENGTH_DECIBELS_MILLIWATT

    def __init__(self, entry: ConfigEntry, base_topic: str) -> None:
        super().__init__(entry, base_topic, "wifi_rssi",
                         sensor_topic(base_topic, TOPIC_SENSOR_WIFI_RSSI))

    def _parse(self, payload: str):
        value = parse_finite_float(payload)
        return int(round(value)) if value is not None and -120 <= value < 0 else None


class HomeTilesLastRestartSensor(HomeTilesPanelDiagnosticSensor):
    """Boot time derived from the panel's uptime counter.

    A timestamp instead of a growing seconds counter keeps the recorder quiet:
    the state only changes when the panel restarts.
    """

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:restart"
    # Uptime arrives once a minute; ignore drift below this between reports.
    _TOLERANCE = timedelta(minutes=2)

    def __init__(self, entry: ConfigEntry, base_topic: str) -> None:
        super().__init__(entry, base_topic, "last_restart",
                         telemetry_topic(base_topic, TOPIC_TELE_UPTIME))

    def _parse(self, payload: str):
        value = parse_finite_float(payload)
        if value is None or value < 0:
            return None
        boot = (dt_util.utcnow() - timedelta(seconds=int(value))).replace(microsecond=0)
        current = getattr(self, "_attr_native_value", None)
        if current is not None and abs(boot - current) < self._TOLERANCE:
            return current
        return boot


class HomeTilesResetReasonSensor(HomeTilesPanelDiagnosticSensor):
    """Why the panel last restarted (power-on, software, watchdog, brownout...)."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = RESET_REASONS
    _attr_icon = "mdi:restart-alert"

    def __init__(self, entry: ConfigEntry, base_topic: str) -> None:
        super().__init__(entry, base_topic, "reset_reason",
                         state_topic(base_topic, TOPIC_STAT_RESET_REASON))

    def _parse(self, payload: str):
        key = payload.strip().lower()
        return key if key in RESET_REASONS else "unknown"


class HomeTilesFreeMemorySensor(HomeTilesPanelDiagnosticSensor):
    """Free heap of one memory region in KiB."""

    _attr_device_class = SensorDeviceClass.DATA_SIZE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfInformation.KIBIBYTES
    _attr_icon = "mdi:memory"

    def __init__(self, entry: ConfigEntry, base_topic: str, key: str, leaf: str) -> None:
        super().__init__(entry, base_topic, key, sensor_topic(base_topic, leaf))

    def _parse(self, payload: str):
        value = parse_finite_float(payload)
        return int(value) if value is not None and value >= 0 else None


class HomeTilesChipTemperatureSensor(HomeTilesPanelDiagnosticSensor):
    """The SoC's internal temperature sensor."""

    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_suggested_display_precision = 1

    def __init__(self, entry: ConfigEntry, base_topic: str) -> None:
        super().__init__(entry, base_topic, "chip_temperature",
                         sensor_topic(base_topic, TOPIC_SENSOR_CHIP_TEMPERATURE))

    def _parse(self, payload: str):
        value = parse_finite_float(payload)
        return round(value, 1) if value is not None and -40 <= value <= 150 else None


class HomeTilesTextDiagnosticSensor(HomeTilesPanelDiagnosticSensor):
    """A retained text value such as the panel IP or firmware version."""

    _MAX_LENGTH = 64

    def __init__(self, entry: ConfigEntry, base_topic: str, key: str, leaf: str,
                 icon: str) -> None:
        super().__init__(entry, base_topic, key, state_topic(base_topic, leaf))
        self._attr_icon = icon

    def _parse(self, payload: str):
        value = payload.strip()
        return value[: self._MAX_LENGTH] if value else None
