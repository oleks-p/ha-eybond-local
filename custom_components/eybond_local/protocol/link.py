"""One collector connection: framing, one transaction at a time, and resync.

The collector carries AT-text lines and bare Modbus RTU on one socket, with no
transaction id, and answers strictly in order. So a frame is the answer to a
request only if that request is outstanding AND the frame has the shape the
request expects - right kind, right function, right byte count. Anything else
is not guessed at.

What this does about the two ways that goes wrong:

* **A late answer.** A request that timed out may still be answered. Taken as
  the answer to the next request, it would decode into plausible, wrong
  numbers. So a timeout opens a quiet window in which every byte is dropped,
  and the next request waits for the window to close.
* **Bytes that do not parse.** Bare RTU has no delimiter, so a desynchronised
  stream cannot be re-framed by searching for one. The buffer is dropped and
  the same quiet window applies. The session is kept: a lost session costs a
  reconnect and a re-identify, and one bad frame is not worth that.

Only a closed socket ends the link.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from .frames import (
    ERROR_FLAG,
    READ_HOLDING,
    Frame,
    FrameError,
    Kind,
    at_query,
    parse_at_reply,
    parse_read_reply,
    read_request,
    split,
)

_LOGGER = logging.getLogger(__name__)

DEFAULT_QUIET_S = 0.5
DEFAULT_TIMEOUT_S = 4.0


class LinkError(Exception):
    """A request got no usable answer. The link may still be fine."""


class LinkTimeout(LinkError):
    """Nothing answered in time."""


class LinkClosed(LinkError):
    """The socket is gone. Nothing more will be answered on this link."""


@dataclass
class LinkStats:
    """Counters for diagnostics. Each one is a question worth asking in the field."""

    transactions: int = 0
    timeouts: int = 0
    mismatches: int = 0     # a frame arrived that does not answer the outstanding request
    stray_frames: int = 0   # a frame arrived when nothing was outstanding
    resyncs: int = 0        # the buffer was dropped because it did not parse
    discarded_bytes: int = 0


class CollectorLink:
    """An accepted collector connection."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        address: str,
        slave: int = 1,
        quiet_s: float = DEFAULT_QUIET_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.address = address
        self.slave = slave
        self.pn: str | None = None
        self.stats = LinkStats()
        self.connected_at = clock()
        self.last_activity = self.connected_at

        self._reader = reader
        self._writer = writer
        self._quiet_s = quiet_s
        self._clock = clock
        self._buf = b""
        self._discard_until = 0.0
        self._lock = asyncio.Lock()
        self._pending: asyncio.Future[Frame] | None = None
        self._expect: Callable[[Frame], bool] | None = None
        self._closed = asyncio.Event()
        self._close_reason = ""
        self._pump_task: asyncio.Task | None = None

    # ── lifecycle ─────────────────────────────────────────────────────────
    def start(self) -> None:
        self._pump_task = asyncio.create_task(self._pump(), name=f"eybond_link_{self.address}")

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    @property
    def close_reason(self) -> str:
        return self._close_reason

    async def wait_closed(self) -> None:
        await self._closed.wait()

    async def close(self, reason: str) -> None:
        self._finish(reason)
        if self._pump_task and self._pump_task is not asyncio.current_task():
            self._pump_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._pump_task

    def _finish(self, reason: str) -> None:
        if self._closed.is_set():
            return
        self._close_reason = reason
        self._closed.set()
        self._fail(LinkClosed(reason))
        with contextlib.suppress(Exception):
            self._writer.close()
        _LOGGER.debug("collector %s link closed: %s", self.address, reason)

    # ── inbound ───────────────────────────────────────────────────────────
    async def _pump(self) -> None:
        try:
            while True:
                data = await self._reader.read(4096)
                if not data:
                    break
                self._on_data(data)
        except (ConnectionError, OSError):
            pass
        finally:
            self._finish("the collector closed the connection")

    def _on_data(self, data: bytes) -> None:
        now = self._clock()
        self.last_activity = now
        if now < self._discard_until:
            # Still inside a quiet window: whatever this is, it is not an answer
            # to anything we will ask next. Each chunk extends the window, so it
            # only closes once the line has actually gone quiet.
            self._discard_until = now + self._quiet_s
            self.stats.discarded_bytes += len(data)
            return
        self._buf += data
        try:
            frames, self._buf = split(self._buf)
        except FrameError as err:
            self._resync(f"bytes that do not parse: {err}")
            return
        for frame in frames:
            self._dispatch(frame)

    def _dispatch(self, frame: Frame) -> None:
        pending = self._pending
        if pending is None or pending.done():
            self.stats.stray_frames += 1
            _LOGGER.debug("collector %s: frame with nothing outstanding: %s",
                          self.address, frame.raw.hex())
            return
        if self._expect is not None and self._expect(frame):
            pending.set_result(frame)
            return
        self.stats.mismatches += 1
        self._resync(f"a frame that does not answer the request: {frame.raw.hex()}")

    def _resync(self, why: str) -> None:
        self.stats.resyncs += 1
        self.stats.discarded_bytes += len(self._buf)
        self._buf = b""
        self._discard_until = self._clock() + self._quiet_s
        _LOGGER.debug("collector %s: resync after %s", self.address, why)
        self._fail(LinkError(f"resynchronised after {why}"))

    def _fail(self, err: Exception) -> None:
        if self._pending is not None and not self._pending.done():
            self._pending.set_exception(err)

    # ── transactions ──────────────────────────────────────────────────────
    async def _transact(
        self, payload: bytes, expect: Callable[[Frame], bool], timeout_s: float
    ) -> Frame:
        async with self._lock:
            if self.closed:
                raise LinkClosed(self._close_reason)
            while (wait := self._discard_until - self._clock()) > 0:
                await asyncio.sleep(wait)
            loop = asyncio.get_running_loop()
            self._pending = loop.create_future()
            self._expect = expect
            self.stats.transactions += 1
            try:
                self._writer.write(payload)
                await self._writer.drain()
                return await asyncio.wait_for(self._pending, timeout_s)
            except TimeoutError as err:
                self.stats.timeouts += 1
                # A late answer must not be taken for the next request's.
                self._discard_until = self._clock() + self._quiet_s
                raise LinkTimeout(f"no answer within {timeout_s:g} s") from err
            except (ConnectionError, OSError) as err:
                self._finish(f"write failed: {err}")
                raise LinkClosed(str(err)) from err
            finally:
                self._pending = None
                self._expect = None

    async def at_query(self, command: str, timeout_s: float = DEFAULT_TIMEOUT_S) -> str:
        """`AT+{command}?` and the value of the `AT+{command}:` reply."""

        def is_reply(frame: Frame) -> bool:
            if frame.kind is not Kind.AT:
                return False
            try:
                return parse_at_reply(frame.raw)[0] == command
            except FrameError:
                return False

        frame = await self._transact(at_query(command), is_reply, timeout_s)
        return parse_at_reply(frame.raw)[1]

    async def read(self, address: int, count: int, timeout_s: float = DEFAULT_TIMEOUT_S) -> list[int]:
        """FC03 read. Raw unsigned words; scale and sign belong to the register map."""

        def is_answer(frame: Frame) -> bool:
            raw = frame.raw
            if frame.kind is not Kind.MODBUS or raw[0] != self.slave:
                return False
            if raw[1] == READ_HOLDING | ERROR_FLAG:
                return True
            return raw[1] == READ_HOLDING and raw[2] == 2 * count

        frame = await self._transact(read_request(self.slave, address, count), is_answer,
                                     timeout_s)
        try:
            return parse_read_reply(frame.raw)
        except FrameError as err:
            raise LinkError(f"read {address}+{count}: {err}") from err
