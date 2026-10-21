"""Diagnostics: what a support request needs, in one download."""
from __future__ import annotations

from dataclasses import asdict

from homeassistant.core import HomeAssistant


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry) -> dict:
    runtime = entry.runtime_data
    poller, hub = runtime.poller, runtime.hub
    link = poller.link
    return {
        "entry": dict(entry.data),
        "identity": asdict(poller.identity) if poller.identity else None,
        "error": poller.error,
        "available": poller.available,
        "link": None if link is None else {
            "address": link.address,
            "pn": link.pn,
            "closed": link.closed,
            "close_reason": link.close_reason,
            "stats": asdict(link.stats),
        },
        "hub": {"listen_port": hub.listen_port, "advertise_ip": hub.config.advertise_ip,
                "stats": asdict(hub.stats)},
        "poll": asdict(poller.stats),
        "energy_kwh": {k: round(c.kwh, 4) for k, c in poller.energy.items()},
        "values": dict(poller.values),
    }
