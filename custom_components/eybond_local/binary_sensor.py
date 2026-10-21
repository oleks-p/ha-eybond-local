"""Binary sensors: the collector link, grid presence, and the inverter's fault and warning flags."""
from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant

from .entity import EybondEntity

# The grid reads 0.0 V when absent. Anything below a brown-out is treated as
# absent rather than as a grid.
GRID_PRESENT_V = 100.0


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities) -> None:
    async_add_entities([
        CollectorConnected(entry),
        GridAvailable(entry),
        CodeFlag(entry, "fault", "fault_code"),
        CodeFlag(entry, "warning", "warning_code"),
    ])


class CollectorConnected(EybondEntity, BinarySensorEntity):
    """Whether the collector holds a session with us and the inverter answers.

    Never unavailable itself: this is the entity an alert automation watches.
    """

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry) -> None:
        super().__init__(entry, "binary_sensor", "collector_connected")

    @property
    def is_on(self) -> bool:
        return self._poller.available

    @property
    def extra_state_attributes(self) -> dict:
        link = self._poller.link
        attrs: dict = {"error": self._poller.error}
        if link is not None:
            attrs.update(address=link.address, timeouts=link.stats.timeouts,
                         resyncs=link.stats.resyncs)
        return attrs


class GridAvailable(EybondEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.POWER

    def __init__(self, entry) -> None:
        super().__init__(entry, "binary_sensor", "grid_available")

    @property
    def available(self) -> bool:
        return self._poller.available and "grid_voltage" in self._poller.values

    @property
    def is_on(self) -> bool:
        return float(self._poller.values.get("grid_voltage", 0)) >= GRID_PRESENT_V


class CodeFlag(EybondEntity, BinarySensorEntity):
    """On while the inverter reports any fault (or warning) bit. The bits are listed
    raw: which one means what is not established for this family."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, entry, key: str, code_key: str) -> None:
        super().__init__(entry, "binary_sensor", key)
        self._code_key = code_key
        # A unit with no PV attached reports warning bits as its normal state,
        # so a warning flag on by default would sit permanently on and teach
        # everyone to ignore it.
        self._attr_entity_registry_enabled_default = key != "warning"

    @property
    def available(self) -> bool:
        return self._poller.connected and self._code_key in self._poller.values

    @property
    def is_on(self) -> bool:
        return int(self._poller.values.get(self._code_key, 0)) != 0

    @property
    def extra_state_attributes(self) -> dict:
        code = int(self._poller.values.get(self._code_key, 0))
        return {"code": code, "bits": [b for b in range(32) if code >> b & 1]}
