"""Sensors: one per register-map field, the directional power halves, and the energy counters."""
from __future__ import annotations

import contextlib

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant

from .energy import DIRECTIONAL_POWER, ENERGY_SOURCES
from .entity import EybondEntity
from .smg import DIAGNOSTIC, FIELDS, Field


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities) -> None:
    entities: list[SensorEntity] = [FieldSensor(entry, spec) for spec in FIELDS]
    entities += [DirectionalPowerSensor(entry, key) for key in DIRECTIONAL_POWER]
    entities += [EnergySensor(entry, key) for key in ENERGY_SOURCES]
    async_add_entities(entities)


class FieldSensor(EybondEntity, SensorEntity):
    """One field of the register map, exactly as decoded."""

    def __init__(self, entry, spec: Field) -> None:
        super().__init__(entry, "sensor", spec.key)
        self._spec = spec
        self._attr_native_unit_of_measurement = spec.unit
        self._attr_device_class = SensorDeviceClass(spec.device_class) if spec.device_class else None
        self._attr_state_class = SensorStateClass(spec.state_class) if spec.state_class else None
        self._attr_entity_category = EntityCategory.DIAGNOSTIC if spec.category == DIAGNOSTIC else None
        self._attr_entity_registry_enabled_default = spec.enabled
        self._attr_suggested_display_precision = spec.precision
        if spec.options is not None:
            self._attr_options = list(spec.options.values())
    @property
    def extra_state_attributes(self) -> dict:
        # Where the value comes from and how sure we are of it, on the entity
        # itself: the first question when a reading looks wrong.
        attrs = {"register": self._spec.address, "confidence": self._spec.confidence}
        if self._spec.block == "status":
            # A raw bit field is unreadable as a number; which bits are set is
            # what anyone looking at it wants. What each bit means is not known.
            code = int(self._poller.values.get(self._key, 0))
            attrs["bits"] = [b for b in range(32) if code >> b & 1]
        return attrs

    @property
    def available(self) -> bool:
        return self._poller.connected and self._key in self._poller.values and (
            self._spec.block != "live" or self._poller.available)

    @property
    def native_value(self):
        return self._poller.values.get(self._key)


class DirectionalPowerSensor(EybondEntity, SensorEntity):
    """One direction of a signed power, always positive - what the Energy dashboard's
    two-sensor power option and most power-flow cards want."""

    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_suggested_display_precision = 0

    def __init__(self, entry, key: str) -> None:
        super().__init__(entry, "sensor", key)

    @property
    def available(self) -> bool:
        return self._poller.available and self._key in self._poller.values

    @property
    def native_value(self):
        return self._poller.values.get(self._key)


class EnergySensor(EybondEntity, RestoreSensor):
    """A kWh counter integrated from power, restored across restarts.

    `total_increasing`, so a counter that ever comes back lower - a reinstall,
    or taking over the entity id of another sensor for the same quantity - is
    read by the recorder as a new meter cycle, not as negative energy.
    """

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_suggested_display_precision = 2

    def __init__(self, entry, key: str) -> None:
        super().__init__(entry, "sensor", key)
        self._counter = self._poller.energy[key]

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_sensor_data()
        if last is not None and last.native_value is not None:
            with contextlib.suppress(TypeError, ValueError):
                self._counter.restore(float(last.native_value))

    @property
    def native_value(self) -> float:
        return round(self._counter.kwh, 3)
