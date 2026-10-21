"""The listener the collectors dial, and the announce that tells them to.

An EyeBond collector never listens. It dials its vendor cloud, and one
unauthenticated UDP datagram on 58899 - `set>server=<ip>:<port>;` - makes it
dial us instead. Everything here follows from how the collector treats that
datagram, all measured on hardware:

* **It drops the session it holds on every one.** Announcing to a connected
  collector costs it its session; sending the datagram in bursts, one burst per
  disconnect, makes the tail of a burst kill the session its head opened, and
  every drop sends another burst - 40-50 reconnects in 30 s with no recovery.
  So: one datagram, and **never to a collector that is connected**.
* **The redirect is sticky.** After our session closed, the collector redialled
  every 3 s for the three minutes it was watched, with no further announce. So
  after a Home Assistant restart it is usually already on its way, and the first
  announce waits out a grace period rather than yanking it mid-dial.
* **A reboot presumably reverts it** to the vendor host. Untested; the repeating
  announce is what covers it.

**Collectors are matched by their own product number** (`AT+DTUPN?`), not by
connection order and not only by address: order is whatever the collectors do
after a power cut, and a lease can move. The address is what the announce is
sent to, and a collector seen from a new one is logged, not refused.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .link import CollectorLink, LinkError

_LOGGER = logging.getLogger(__name__)

ANNOUNCE_PORT = 58899
DEFAULT_LISTEN_PORT = 8899


@dataclass(frozen=True)
class CollectorConfig:
    """One collector this hub serves. `pn` is None only while it is being identified."""

    ip: str
    pn: str | None = None


@dataclass
class HubConfig:
    advertise_ip: str
    listen_host: str = "0.0.0.0"
    listen_port: int = DEFAULT_LISTEN_PORT
    announce_port: int = ANNOUNCE_PORT
    # The redirect is sticky, so after a restart the collector usually redials
    # unprompted within seconds. Announcing straight away would cost it one
    # extra drop and reconnect.
    startup_grace_s: float = 10.0
    announce_interval_s: float = 15.0
    identify_timeout_s: float = 5.0
    quiet_s: float = 0.5
    tick_s: float = 1.0


LinkListener = Callable[[CollectorLink | None], None]


@dataclass
class HubStats:
    announces: dict[str, int] = field(default_factory=dict)
    refused: dict[str, str] = field(default_factory=dict)


class CollectorHub:
    def __init__(self, config: HubConfig, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.config = config
        self.stats = HubStats()
        self._clock = clock
        self._collectors: dict[str, CollectorConfig] = {}     # keyed by ip
        self._listeners: dict[str, list[LinkListener]] = {}   # keyed by ip
        self._links: dict[str, CollectorLink] = {}            # keyed by configured ip
        self._last_announce: dict[str, float] = {}
        self._server: asyncio.Server | None = None
        self._udp: socket.socket | None = None
        self._task: asyncio.Task | None = None
        self._started_at = 0.0
        self._handlers: set[asyncio.Task] = set()

    # ── membership ────────────────────────────────────────────────────────
    def add_collector(self, collector: CollectorConfig, listener: LinkListener) -> Callable[..., None]:
        """Serve `collector`. `listener` is called with its link, or None when it goes.

        An identified link already held for this address is handed over at once:
        that is how the entry a config flow creates adopts the session the flow
        opened, without the collector reconnecting.
        """
        self._collectors[collector.ip] = collector
        self._listeners.setdefault(collector.ip, []).append(listener)
        if link := self._links.get(collector.ip):
            listener(link)

        def remove(keep_link: bool = False) -> None:
            listeners = self._listeners.get(collector.ip, [])
            if listener in listeners:
                listeners.remove(listener)
            if listeners:
                return
            self._listeners.pop(collector.ip, None)
            self._collectors.pop(collector.ip, None)
            if keep_link:
                return  # left for whoever adds this collector next; closed with the hub
            if link := self._links.pop(collector.ip, None):
                asyncio.get_running_loop().create_task(link.close("no longer configured"))

        return remove

    @property
    def collector_count(self) -> int:
        return len(self._collectors)

    def has_collector(self, ip: str) -> bool:
        return ip in self._collectors

    def announce_now(self, ip: str) -> None:
        """One announce, outside the schedule. For a collector nothing has redirected
        yet - the config flow's - where waiting out the startup grace is pointless."""
        if ip not in self._links:
            self._last_announce[ip] = self._clock()
            self._announce(ip)

    def link_for(self, ip: str) -> CollectorLink | None:
        return self._links.get(ip)

    # ── lifecycle ─────────────────────────────────────────────────────────
    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._on_connection, self.config.listen_host, self.config.listen_port
        )
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp.setblocking(False)
        self._udp = udp
        self._started_at = self._clock()
        self._task = asyncio.create_task(self._tick_loop(), name="eybond_hub_announce")
        _LOGGER.info("listening for collectors on %s:%s, announcing %s",
                     self.config.listen_host, self.listen_port, self.config.advertise_ip)

    @property
    def listen_port(self) -> int:
        if self._server and self._server.sockets:
            return self._server.sockets[0].getsockname()[1]
        return self.config.listen_port

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        # Stop accepting BEFORE closing the sessions. A collector whose session
        # closes redials at once (every 3 s, measured), and a
        # connection accepted while the server is closing trips an assertion
        # inside asyncio's accept path.
        if self._server:
            self._server.close()
        for link in list(self._links.values()):
            await link.close("hub stopped")
        self._links.clear()
        for task in list(self._handlers):
            task.cancel()
        if self._server:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._server.wait_closed(), 5)
        if self._udp:
            self._udp.close()

    # ── announce ──────────────────────────────────────────────────────────
    async def _tick_loop(self) -> None:
        while True:
            self.announce_due()
            await asyncio.sleep(self.config.tick_s)

    def announce_due(self) -> list[str]:
        """Send one datagram to each collector that is missing and due. Returns who."""
        now = self._clock()
        if now - self._started_at < self.config.startup_grace_s:
            return []
        sent = []
        for ip in self._collectors:
            if ip in self._links:
                continue  # never to a connected collector: it would drop the session
            if now - self._last_announce.get(ip, -1e9) < self.config.announce_interval_s:
                continue
            self._last_announce[ip] = now
            self._announce(ip)
            sent.append(ip)
        return sent

    def _announce(self, ip: str) -> None:
        payload = f"set>server={self.config.advertise_ip}:{self.listen_port};".encode()
        try:
            self._udp.sendto(payload, (ip, self.config.announce_port))
            self.stats.announces[ip] = self.stats.announces.get(ip, 0) + 1
            _LOGGER.debug("announced %s:%s to collector %s",
                          self.config.advertise_ip, self.listen_port, ip)
        except OSError as err:
            # Never swallowed: a collector that cannot hear us looks exactly like
            # one that is switched off, and this is the only place they differ.
            _LOGGER.warning("announce to collector %s failed: %s", ip, err)

    # ── connections ───────────────────────────────────────────────────────
    async def _on_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        self._handlers.add(task)
        try:
            await self._serve(reader, writer)
        finally:
            self._handlers.discard(task)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        address = peer[0] if peer else "unknown"
        link = CollectorLink(reader, writer, address=address, quiet_s=self.config.quiet_s,
                             clock=self._clock)
        link.start()
        try:
            pn = await link.at_query("DTUPN", timeout_s=self.config.identify_timeout_s)
        except LinkError as err:
            self._refuse(address, f"did not identify itself ({err})")
            await link.close("did not identify itself")
            return
        link.pn = pn.strip()

        collector = self._match(link.pn, address)
        if collector is None:
            self._refuse(address, f"collector {link.pn} is not configured here")
            await link.close("not configured")
            return
        if collector.ip != address:
            _LOGGER.warning(
                "collector %s dialled in from %s, but is configured at %s. Announces go "
                "to the configured address: give it a DHCP reservation.",
                link.pn, address, collector.ip)

        if old := self._links.get(collector.ip):
            # It redialled before we noticed the old socket die. The new one is live.
            await old.close("superseded by a newer connection")
        self._links[collector.ip] = link
        _LOGGER.info("collector %s connected from %s", link.pn, address)
        self._notify(collector.ip, link)
        try:
            await link.wait_closed()
        finally:
            if self._links.get(collector.ip) is link:
                del self._links[collector.ip]
                self._last_announce[collector.ip] = self._clock()  # give it time to redial unprompted
                self._notify(collector.ip, None)
            _LOGGER.info("collector %s disconnected: %s", link.pn, link.close_reason)

    def _match(self, pn: str, address: str) -> CollectorConfig | None:
        for collector in self._collectors.values():
            if collector.pn == pn:
                return collector
        # A collector still being identified - the config flow - has no PN yet.
        collector = self._collectors.get(address)
        if collector is not None and collector.pn is None:
            return collector
        return None

    def _refuse(self, address: str, why: str) -> None:
        if self.stats.refused.get(address) != why:
            _LOGGER.warning("refused a connection from %s: %s", address, why)
        self.stats.refused[address] = why

    def _notify(self, ip: str, link: CollectorLink | None) -> None:
        for listener in list(self._listeners.get(ip, [])):
            try:
                listener(link)
            except Exception:  # noqa: BLE001 - one listener must not take the hub down
                _LOGGER.exception("collector listener failed")
