"""One inverter's read loop over its collector link. No Home Assistant imports.

Identify, refuse an unmeasured protocol, then read each block on its own
cadence for as long as the link lives. A failed read is retried on the next
pass and never ends the loop: the collector already sits on the other end of
a sticky redirect, and an inverter can go quiet for ~20 s at times without
anything being wrong with the link.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .energy import DIRECTIONAL_POWER, ENERGY_SOURCES, EnergyCounter
from .protocol.link import CollectorLink, LinkClosed, LinkError
from .smg import (
    BLOCKS,
    FIELDS_BY_BLOCK,
    FIRMWARE_WORDS,
    REG_DEVICE_TYPE,
    REG_FIRMWARE,
    REG_PROTOCOL,
    REG_SERIAL,
    SERIAL_WORDS,
    Block,
    UnsupportedProtocol,
    check_protocol,
    decode,
    words_to_ascii,
)

_LOGGER = logging.getLogger(__name__)

# A failed block is retried sooner than its cadence, but not in a tight loop.
RETRY_S = 5.0


@dataclass
class Identity:
    pn: str
    protocol: int
    device_type: int
    # Has been seen to read blank in a first session and correctly afterwards,
    # so it is shown when present and never relied on: identity is the PN.
    serial: str = ""
    firmware: str = ""


@dataclass
class PollStats:
    reads: dict[str, int] = field(default_factory=dict)
    failures: dict[str, int] = field(default_factory=dict)
    last_error: str | None = None


class InverterPoller:
    def __init__(
        self,
        *,
        on_update: Callable[[], None],
        clock: Callable[[], float] = time.monotonic,
        blocks: tuple[Block, ...] = BLOCKS,
        live_stale_s: float = 30.0,
        timeout_s: float = 4.0,
    ) -> None:
        self.values: dict[str, float | int | str] = {}
        self.identity: Identity | None = None
        self.error: str | None = None
        self.energy = {key: EnergyCounter() for key in ENERGY_SOURCES}
        self.stats = PollStats()
        self.last_live: float | None = None
        self._on_update = on_update
        self._clock = clock
        self._blocks = blocks
        self._live_stale_s = live_stale_s
        self._timeout_s = timeout_s
        self._link: CollectorLink | None = None
        self._task: asyncio.Task | None = None

    # ── state ─────────────────────────────────────────────────────────────
    @property
    def connected(self) -> bool:
        return self._link is not None and not self._link.closed

    @property
    def link(self) -> CollectorLink | None:
        return self._link

    @property
    def available(self) -> bool:
        """Live data is current. Settings and BMS values may be older, by design."""
        return (
            self.connected
            and self.last_live is not None
            and self._clock() - self.last_live <= self._live_stale_s
        )

    # ── link changes, from the hub ────────────────────────────────────────
    def set_link(self, link: CollectorLink | None) -> None:
        if self._task:
            self._task.cancel()
            self._task = None
        self._link = link
        for counter in self.energy.values():
            counter.interrupt()
        if link is not None:
            self._task = asyncio.get_running_loop().create_task(
                self._run(link), name=f"eybond_poll_{link.address}")
        self._on_update()

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    # ── the loop ──────────────────────────────────────────────────────────
    async def _run(self, link: CollectorLink) -> None:
        try:
            self.identity = await self._identify(link)
            check_protocol(self.identity.protocol)
        except UnsupportedProtocol as err:
            self.error = str(err)
            _LOGGER.error("collector %s: %s", link.pn, err)
            self._on_update()
            return
        except LinkError as err:
            # Identification failed on a live link. Leave it be: the hub keeps the
            # link, and the next connection identifies afresh.
            self.stats.last_error = f"identify: {err}"
            _LOGGER.warning("collector %s: could not identify the inverter: %s", link.pn, err)
            return
        self.error = None
        self._on_update()

        due = {block.name: 0.0 for block in self._blocks}
        while not link.closed:
            for block in self._blocks:
                if self._clock() < due[block.name]:
                    continue
                try:
                    words = await link.read(block.address, block.count, self._timeout_s)
                except LinkClosed:
                    return
                except LinkError as err:
                    self.stats.failures[block.name] = self.stats.failures.get(block.name, 0) + 1
                    self.stats.last_error = f"{block.name}: {err}"
                    due[block.name] = self._clock() + min(block.every_s, RETRY_S)
                    _LOGGER.debug("collector %s: %s read failed: %s", link.pn, block.name, err)
                    continue
                self.stats.reads[block.name] = self.stats.reads.get(block.name, 0) + 1
                self._apply(block, words)
                due[block.name] = self._clock() + block.every_s
                self._on_update()
            await asyncio.sleep(max(0.05, min(due.values()) - self._clock()))

    async def _identify(self, link: CollectorLink) -> Identity:
        protocol = (await link.read(REG_PROTOCOL, 1, self._timeout_s))[0]
        device_type = (await link.read(REG_DEVICE_TYPE, 1, self._timeout_s))[0]
        serial = firmware = ""
        with contextlib.suppress(LinkError):
            serial = words_to_ascii(await link.read(REG_SERIAL, SERIAL_WORDS, self._timeout_s))
        with contextlib.suppress(LinkError):
            firmware = words_to_ascii(await link.read(REG_FIRMWARE, FIRMWARE_WORDS, self._timeout_s))
        identity = Identity(pn=link.pn or "", protocol=protocol, device_type=device_type,
                            serial=serial, firmware=firmware)
        _LOGGER.info("collector %s: SMG-II protocol %s, device type 0x%04x%s",
                     identity.pn, protocol, device_type, f", serial {serial}" if serial else "")
        return identity

    def _apply(self, block: Block, words: list[int]) -> None:
        decoded = decode(block, words)
        # A field that left its plausible band is dropped, not kept at its last
        # value: a stale reading presented as current is worse than none.
        for spec in FIELDS_BY_BLOCK[block.name]:
            if spec.key not in decoded:
                self.values.pop(spec.key, None)
        self.values.update(decoded)
        if block.name != "live":
            return
        now = self._clock()
        self.last_live = now
        for key, source in DIRECTIONAL_POWER.items():
            value = source(decoded)
            if value is None:
                self.values.pop(key, None)
            else:
                self.values[key] = round(value)
        for key, source in ENERGY_SOURCES.items():
            self.energy[key].add(source(decoded), now)
