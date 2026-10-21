"""What every entity shares: its device, its id, and pushed updates.

Entity ids are `<prefix>_<key>`, English and stable, whatever language Home
Assistant runs in; the display name comes from the translations. Ids that
are stable across installs also let a hand-made template or integration sensor
for the same quantity be replaced in place, its history continuing.
"""
from __future__ import annotations

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity

from .const import CONF_PN, CONF_PREFIX, DEFAULT_PREFIX, DOMAIN, signal_update


class EybondEntity(Entity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry, platform: str, key: str) -> None:
        pn = entry.data[CONF_PN]
        prefix = entry.data.get(CONF_PREFIX) or DEFAULT_PREFIX
        self._entry = entry
        self._poller = entry.runtime_data.poller
        self._key = key
        self._attr_unique_id = f"{pn}_{key}"
        self._attr_translation_key = key
        self.entity_id = f"{platform}.{prefix}_{key}"
        # Only what never changes. Model, hardware version and serial come from
        # the inverter once it has identified itself (`_update_device`), and an
        # entity passing them here would overwrite them on every restart.
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, pn)},
            name=prefix.replace("_", " ").capitalize(),
            manufacturer="SMG-II",
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(
            self.hass, signal_update(self._entry.entry_id), self._handle_update))

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()
