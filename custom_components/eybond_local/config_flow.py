"""Add a collector: announce to it, wait for it to dial in, and ask what it is.

The entry is only created once the collector has actually connected and the
inverter behind it has reported a protocol number this integration was
measured on. A form that accepted an address on trust could leave an entry
that never works, retrying for ever with nothing to say why.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from typing import Any

import voluptuous as vol
from homeassistant.components import network
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from . import acquire_hub, const, release_hub
from .const import (
    CONF_ADVERTISE_IP,
    CONF_COLLECTOR_IP,
    CONF_PN,
    CONF_PORT,
    CONF_PREFIX,
    DEFAULT_PORT,
    DEFAULT_PREFIX,
    DOMAIN,
)
from .protocol.hub import CollectorConfig
from .protocol.link import LinkError
from .smg import REG_PROTOCOL, UnsupportedProtocol, check_protocol

_LOGGER = logging.getLogger(__name__)


class ProbeFailed(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


async def probe(hass, collector_ip: str, advertise_ip: str, port: int) -> tuple[str, int]:
    """Announce to the collector and wait for it. Returns `(pn, protocol)`."""
    try:
        hub = await acquire_hub(hass, port, advertise_ip)
    except OSError as err:
        raise ProbeFailed("port_in_use") from err
    if hub.has_collector(collector_ip):
        # Another entry serves this address on this hub. Adding it again would
        # replace that entry's collector with an unidentified one.
        await release_hub(hass, port)
        raise ProbeFailed("already_configured")
    arrived: asyncio.Future = asyncio.get_running_loop().create_future()

    def on_link(link) -> None:
        if link is not None and not arrived.done():
            arrived.set_result(link)

    detach = hub.add_collector(CollectorConfig(ip=collector_ip), on_link)
    succeeded = False
    try:
        # Nothing is redirecting this collector yet, so do not wait out the
        # hub's startup grace: announce now.
        hub.announce_now(collector_ip)
        try:
            link = await asyncio.wait_for(arrived, const.PROBE_TIMEOUT_S)
        except TimeoutError as err:
            raise ProbeFailed("no_collector") from err
        try:
            protocol = (await link.read(REG_PROTOCOL, 1))[0]
        except LinkError as err:
            raise ProbeFailed("no_inverter") from err
        try:
            check_protocol(protocol)
        except UnsupportedProtocol as err:
            raise ProbeFailed("unsupported_protocol") from err
        succeeded = True
        return link.pn, protocol
    finally:
        # On success, the live session is left for the entry to adopt.
        detach(keep_link=succeeded)
        with contextlib.suppress(Exception):
            # On success the entry is set up next, on this port: keep listening
            # for it. On failure there is nothing to wait for.
            await release_hub(hass, port, const.PROBE_LINGER_S if succeeded else 0.0)


class EybondConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = self._check_prefix(user_input[CONF_PREFIX])
        if user_input is not None and not errors:
            try:
                pn, protocol = await probe(
                    self.hass, user_input[CONF_COLLECTOR_IP], user_input[CONF_ADVERTISE_IP],
                    user_input[CONF_PORT])
            except ProbeFailed as err:
                errors["base"] = err.reason
            else:
                await self.async_set_unique_id(pn)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"SMG-II {pn}",
                    data={**user_input, CONF_PN: pn},
                    description_placeholders={"protocol": str(protocol)},
                )

        advertise = await network.async_get_source_ip(self.hass) if user_input is None \
            else user_input[CONF_ADVERTISE_IP]
        defaults = user_input or {}
        schema = vol.Schema({
            vol.Required(CONF_COLLECTOR_IP, default=defaults.get(CONF_COLLECTOR_IP, "")): str,
            vol.Required(CONF_ADVERTISE_IP, default=advertise): str,
            vol.Required(CONF_PORT, default=defaults.get(CONF_PORT, DEFAULT_PORT)): int,
            vol.Required(CONF_PREFIX, default=defaults.get(CONF_PREFIX, DEFAULT_PREFIX)): str,
        })
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    def _check_prefix(self, prefix: str) -> dict[str, str]:
        """Entity ids are `<prefix>_<key>`, so the prefix is what keeps units apart.

        Two entries on one prefix would give the second unit `_2` ids - silently,
        and in whichever order the entries happened to load. A three-phase bank is
        three entries, so this is the normal case, not an edge.
        """
        if not re.fullmatch(r"[a-z][a-z0-9_]*", prefix):
            return {CONF_PREFIX: "invalid_prefix"}
        taken = {(e.data.get(CONF_PREFIX) or DEFAULT_PREFIX) for e in self._async_current_entries()}
        if prefix in taken:
            return {CONF_PREFIX: "prefix_in_use"}
        return {}
