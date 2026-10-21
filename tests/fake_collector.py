"""A collector, from the collector's side: answers the announce, dials in, serves registers.

Its registers describe a made-up protocol-11 unit with a self-consistent state -
off-grid, a 52.0 V battery discharging 9.0 A (-468 W), 80 % SOC, a coherent 48 V
charge profile, BMS values that match the battery - so decoding is checked
against numbers that agree with each other, not just with themselves.
"""
from __future__ import annotations

import asyncio
import contextlib
import struct

from custom_components.eybond_local.protocol.frames import crc16

PN = "I20000000000000042"


REGISTERS: dict[int, list[int]] = {
    100: [0, 0, 0, 0, 0, 0, 0, 0, 0, 0x0005],     # warning code: bits 0 and 2
    171: [0x5001],
    184: [11],
    186: [0] * 12,
    201: [3, 0, 0, 0, 2300, 25, 5000, 520, 0, 2300, 20, 5000, 420, 460, 520, 65446, 65068, 0, 0, 0, 0, 0, 0, 0, 9, 30, 33, 0, 80, 0, 0, 65446, 0, 0],
    300: [0, 2, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 2300, 5000, 2, 600, 560, 540, 0, 470, 0, 440, 0, 0, 600, 300, 580, 0, 0, 0, 0, 0, 0, 15, 40, 10, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    639: [0, 0, 0x5001, 1, 5000, 0, 0, 0, 0],
    760: [0, 532, 470, 1000, 1500, 0, 0, 0, 0, 0, 520, 0, 0, 80, 0, 0],
}


def default_registers() -> dict[int, int]:
    regs: dict[int, int] = {}
    for start, words in REGISTERS.items():
        for i, word in enumerate(words):
            regs[start + i] = word
    return regs


def modbus_reply(slave: int, words: list[int]) -> bytes:
    body = bytes([slave, 3, 2 * len(words)]) + b"".join(struct.pack(">H", w) for w in words)
    return body + struct.pack("<H", crc16(body))


class FakeCollector:
    def __init__(self, pn: str = PN, registers: dict[int, int] | None = None, slave: int = 1,
                 redial_s: float | None = None) -> None:
        self.pn = pn
        self.registers = default_registers() if registers is None else registers
        self.slave = slave
        self.redial_s = redial_s          # None: never redial on its own
        self.announces: list[str] = []
        self.connections = 0
        self.reads: list[tuple[int, int]] = []
        # Misbehaviour, consumed as it is used.
        self.silent_reads: set[int] = set()       # addresses that get no answer at all
        self.late_reads: dict[int, float] = {}    # address -> seconds before answering
        self.garbage_before: dict[int, bytes] = {}
        self.udp_port = 0
        self._udp: asyncio.DatagramTransport | None = None
        self._tasks: set[asyncio.Task] = set()
        self._writer: asyncio.StreamWriter | None = None
        self._target: tuple[str, int] | None = None
        self._stopped = False

    # ── lifecycle ─────────────────────────────────────────────────────────
    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        fake = self

        class Announce(asyncio.DatagramProtocol):
            def datagram_received(self, data, addr):
                fake._on_announce(data.decode("ascii", "replace"))

        self._udp, _ = await loop.create_datagram_endpoint(Announce, local_addr=("127.0.0.1", 0))
        self.udp_port = self._udp.get_extra_info("sockname")[1]

    async def stop(self) -> None:
        self._stopped = True
        if self._udp:
            self._udp.close()
        await self.hang_up()
        for task in list(self._tasks):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def hang_up(self) -> None:
        if self._writer:
            self._writer.close()
            with contextlib.suppress(Exception):
                await self._writer.wait_closed()
            self._writer = None

    @property
    def connected(self) -> bool:
        return self._writer is not None

    # ── the announce ──────────────────────────────────────────────────────
    def _on_announce(self, text: str) -> None:
        self.announces.append(text)
        if not (text.startswith("set>server=") and text.endswith(";")):
            return
        host, _, port = text[len("set>server="):-1].rpartition(":")
        self._target = (host, int(port))
        # A real collector drops the session it holds and dials the new server.
        self._spawn(self._redirect())

    async def _redirect(self) -> None:
        await self.hang_up()
        await self.dial()

    def _spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def dial(self, target: tuple[str, int] | None = None) -> None:
        target = target or self._target
        try:
            reader, writer = await asyncio.open_connection(*target)
        except OSError:
            await self._redial_later(target)
            return
        self._target = target
        self.connections += 1
        self._writer = writer
        self._spawn(self._serve(reader, writer))

    # ── serving ───────────────────────────────────────────────────────────
    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        buf = b""
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                buf += data
                while buf:
                    if buf.startswith(b"AT+"):
                        end = buf.find(b"\r\n")
                        if end < 0:
                            break
                        line, buf = buf[:end], buf[end + 2:]
                        await self._answer_at(writer, line[3:].decode())
                    elif len(buf) >= 8:
                        frame, buf = buf[:8], buf[8:]
                        await self._answer_modbus(writer, frame)
                    else:
                        break
        except (ConnectionError, OSError):
            pass
        finally:
            # Hung up on by the server, as opposed to dropped by us on a redirect
            # or a test's hang_up. Only that makes a real collector redial.
            dropped_by_server = self._writer is writer
            if dropped_by_server:
                self._writer = None
            writer.close()
            if dropped_by_server:
                # The sticky redirect: it comes back on its own.
                await self._redial_later(None)

    async def _redial_later(self, target: tuple[str, int] | None) -> None:
        if self.redial_s is None or self._stopped:
            return
        await asyncio.sleep(self.redial_s)
        # Checked again: a test may have told it to stop while it waited.
        if self.redial_s is not None and not self._stopped:
            self._spawn(self.dial(target))

    async def _answer_at(self, writer, query: str) -> None:
        command = query.rstrip("?")
        value = self.pn if command == "DTUPN" else "R001"
        writer.write(f"AT+{command}:{value}\r\n".encode())
        await writer.drain()

    async def _answer_modbus(self, writer, frame: bytes) -> None:
        slave, function, address, count = struct.unpack(">BBHH", frame[:6])
        if slave != self.slave or function != 3:
            return  # the real device answers an unknown request with silence
        self.reads.append((address, count))
        if address in self.silent_reads:
            self.silent_reads.discard(address)
            return
        reply = modbus_reply(slave, [self.registers.get(address + i, 0) for i in range(count)])
        if (garbage := self.garbage_before.pop(address, None)) is not None:
            writer.write(garbage)
        if (delay := self.late_reads.pop(address, None)) is not None:
            await asyncio.sleep(delay)
        writer.write(reply)
        await writer.drain()
