"""EyeBond Local: SMG-II inverters through their own EyeBond Wi-Fi collector.

One config entry per collector. Entries on the same port share one listener -
a collector dials one address and port, and two listeners on one port is
`address already in use` - so the hub is reference-counted here and torn down
with its last entry.

Read-only. Writes will come only as an allow-list of settings the inverter
keeps for itself, each range-checked and read back.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send

from . import const
from .const import (
    CONF_ADVERTISE_IP,
    CONF_COLLECTOR_IP,
    CONF_PN,
    CONF_PORT,
    CONF_PREFIX,
    DEFAULT_PREFIX,
    DOMAIN,
    PLATFORMS,
    signal_update,
)
from .poller import InverterPoller
from .protocol.hub import CollectorConfig, CollectorHub, HubConfig
from .smg import RENAMED_KEYS

_LOGGER = logging.getLogger(__name__)


@dataclass
class Runtime:
    hub: CollectorHub
    poller: InverterPoller
    detach: object


type EybondConfigEntry = ConfigEntry[Runtime]


@dataclass
class _HubSlot:
    hub: CollectorHub
    users: int
    linger: asyncio.TimerHandle | None = None


async def acquire_hub(hass: HomeAssistant, port: int, advertise_ip: str) -> CollectorHub:
    """The running hub on `port`, or a new one. Raises OSError if the port is taken."""
    slots: dict[int, _HubSlot] = hass.data.setdefault(DOMAIN, {})
    if slot := slots.get(port):
        if slot.linger is not None:
            slot.linger.cancel()
            slot.linger = None
        if slot.hub.config.advertise_ip != advertise_ip:
            _LOGGER.warning(
                "entries on port %s disagree on the address to announce (%s vs %s); "
                "using %s. They share one listener, so they must share one address.",
                port, slot.hub.config.advertise_ip, advertise_ip, slot.hub.config.advertise_ip)
        slot.users += 1
        return slot.hub
    hub = CollectorHub(HubConfig(advertise_ip=advertise_ip, listen_port=port,
                                 announce_port=const.ANNOUNCE_PORT,
                                 startup_grace_s=const.HUB_STARTUP_GRACE_S,
                                 announce_interval_s=const.HUB_ANNOUNCE_INTERVAL_S))
    await hub.start()
    slots[port] = _HubSlot(hub, 1)
    return hub


async def release_hub(hass: HomeAssistant, port: int, linger_s: float = 0.0) -> None:
    """Drop one user of the hub on `port`, and stop it with the last.

    `linger_s` keeps an unused hub listening a little longer. The config flow
    uses it: the entry it creates is set up moments later on the same port,
    and closing the listener in between would make the collector - which
    redials at once - reconnect twice, once into a socket that is closing.
    """
    slots: dict[int, _HubSlot] = hass.data.get(DOMAIN, {})
    slot = slots.get(port)
    if slot is None:
        return
    slot.users -= 1
    if slot.users > 0:
        return
    if linger_s > 0:
        def expire() -> None:
            if slots.get(port) is slot and slot.users <= 0:
                del slots[port]
                hass.async_create_task(slot.hub.stop())

        slot.linger = hass.loop.call_later(linger_s, expire)
        return
    del slots[port]
    await slot.hub.stop()


async def async_setup_entry(hass: HomeAssistant, entry: EybondConfigEntry) -> bool:
    port = entry.data[CONF_PORT]
    try:
        hub = await acquire_hub(hass, port, entry.data[CONF_ADVERTISE_IP])
    except OSError as err:
        # Usually another integration holding the collector port: other local
        # EyeBond integrations listen on the same 8899. Only one can serve a
        # collector.
        raise ConfigEntryNotReady(f"cannot listen on port {port}: {err}") from err

    def changed() -> None:
        _update_device(hass, entry, poller)
        async_dispatcher_send(hass, signal_update(entry.entry_id))

    _migrate_renamed_keys(hass, entry)
    poller = InverterPoller(on_update=changed)
    detach = hub.add_collector(
        CollectorConfig(ip=entry.data[CONF_COLLECTOR_IP], pn=entry.data[CONF_PN]),
        poller.set_link,
    )
    entry.runtime_data = Runtime(hub=hub, poller=poller, detach=detach)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: EybondConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        runtime = entry.runtime_data
        runtime.detach()
        await runtime.poller.stop()
        await release_hub(hass, entry.data[CONF_PORT])
    return ok


def _migrate_renamed_keys(hass: HomeAssistant, entry: EybondConfigEntry) -> None:
    """Move registry rows of renamed keys to their new unique id, before entities are added.

    Otherwise a rename orphans the old entity and creates a new one beside it,
    and its history stays with the orphan. The entity id moves too, but only if
    it is still the one this integration gave it: a user's own rename stands.
    The recorder follows an entity id rename, so history and statistics move
    with it.
    """
    registry = er.async_get(hass)
    pn = entry.data[CONF_PN]
    prefix = entry.data.get(CONF_PREFIX) or DEFAULT_PREFIX
    for row in er.async_entries_for_config_entry(registry, entry.entry_id):
        for old, new in RENAMED_KEYS.items():
            if row.unique_id != f"{pn}_{old}":
                continue
            changes: dict = {"new_unique_id": f"{pn}_{new}"}
            target = f"{row.domain}.{prefix}_{new}"
            if row.entity_id == f"{row.domain}.{prefix}_{old}" and registry.async_get(target) is None:
                changes["new_entity_id"] = target
            _LOGGER.info("renaming %s to the key %s", row.entity_id, new)
            registry.async_update_entity(row.entity_id, **changes)


def _update_device(hass: HomeAssistant, entry: EybondConfigEntry, poller: InverterPoller) -> None:
    """Fill in what only the inverter can say about itself, once it has said it."""
    identity = poller.identity
    if identity is None:
        return
    registry = dr.async_get(hass)
    device = registry.async_get_device_by_identifier((DOMAIN, entry.data[CONF_PN]), entry.entry_id)
    if device is None:
        return
    rated = poller.values.get("rated_power")
    model = f"SMG-II protocol {identity.protocol}" + (f", {rated} W" if rated else "")
    changes = {
        "model": model,
        "hw_version": f"0x{identity.device_type:04x}",
        "sw_version": identity.firmware or None,
        "serial_number": identity.serial or None,
    }
    if any(getattr(device, k) != v for k, v in changes.items()):
        registry.async_update_device(device.id, **changes)
