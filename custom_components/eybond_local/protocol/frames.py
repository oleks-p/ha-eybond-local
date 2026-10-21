"""What travels on the collector's socket: AT text lines and bare Modbus RTU.

Pure functions. No sockets, no clock, no Home Assistant imports.

One TCP connection carries two kinds of exchange, interleaved:

* **AT commands**, as text. `AT+NAME?` asks; the collector answers
  `AT+NAME:value`. Every line ends in CR LF.
* **Modbus RTU**, bare: slave, function, payload, CRC-16, and nothing around
  it - no length prefix, no delimiter. On the serial line behind the
  collector, silence between frames marks where one ends. TCP keeps no
  silence, so a frame's length has to be read from its own first bytes.

Only what the collector sends back is parsed here, because this integration
only reads. A reply that cannot be framed is an error, never skipped over: with
no transaction id on this line, guessing where the next frame starts would
re-read everything after it as something else.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import Enum

AT_PREFIX = b"AT+"
LINE_END = b"\r\n"

READ_HOLDING = 0x03
READ_INPUT = 0x04
WRITE_SINGLE = 0x06
WRITE_MULTIPLE = 0x10
ERROR_FLAG = 0x80          # set in the function byte of an exception reply

MAX_READ_WORDS = 125       # the byte count is one byte: 125 words = 250 bytes


class FrameError(Exception):
    """Bytes that are not a well-formed frame."""


class DeviceException(FrameError):
    """A well-formed Modbus exception reply: the device declined the request."""

    def __init__(self, code: int) -> None:
        super().__init__(f"device exception 0x{code:02x}")
        self.code = code


class Kind(Enum):
    AT = "at"
    MODBUS = "modbus"


@dataclass(frozen=True)
class Frame:
    kind: Kind
    raw: bytes


# ── CRC-16/MODBUS ─────────────────────────────────────────────────────────
# Reflected polynomial 0xA001, initial value 0xFFFF, sent low byte first.

def _crc_table() -> tuple[int, ...]:
    table = []
    for byte in range(256):
        value = byte
        for _ in range(8):
            value = (value >> 1) ^ 0xA001 if value & 1 else value >> 1
        table.append(value)
    return tuple(table)


_CRC_TABLE = _crc_table()


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc = (crc >> 8) ^ _CRC_TABLE[(crc ^ byte) & 0xFF]
    return crc


def _with_crc(body: bytes) -> bytes:
    return body + struct.pack("<H", crc16(body))


def _crc_ok(frame: bytes) -> bool:
    return len(frame) >= 4 and crc16(frame[:-2]) == struct.unpack("<H", frame[-2:])[0]


# ── Modbus requests and replies ───────────────────────────────────────────

def read_request(slave: int, address: int, count: int, function: int = READ_HOLDING) -> bytes:
    """A function 3 (or 4) read. Every bound is checked, never masked: a
    truncated address would read a different register without complaint."""
    if not 0 <= slave <= 247:
        raise ValueError(f"slave {slave} is outside 0-247")
    if not 0 <= address <= 0xFFFF:
        raise ValueError(f"address {address} is outside 0-65535")
    if not 1 <= count <= MAX_READ_WORDS:
        raise ValueError(f"count {count} is outside 1-{MAX_READ_WORDS}")
    if function not in (READ_HOLDING, READ_INPUT):
        raise ValueError(f"function 0x{function:02x} is not a read")
    return _with_crc(struct.pack(">BBHH", slave, function, address, count))


def parse_read_reply(frame: bytes) -> list[int]:
    """The words of a read reply, raw and unsigned.

    Sign and scale belong to a register, not to a frame - one reply can carry an
    unsigned voltage next to a signed power - so they are applied by the map.
    """
    if len(frame) < 5 or not _crc_ok(frame):
        raise FrameError(f"not a valid reply: {frame.hex()}")
    function = frame[1]
    if function & ERROR_FLAG:
        raise DeviceException(frame[2])
    if function not in (READ_HOLDING, READ_INPUT):
        raise FrameError(f"function 0x{function:02x} is not a read reply")
    size = frame[2]
    if size % 2 or len(frame) != 5 + size:
        raise FrameError(f"byte count {size} does not fit a {len(frame)}-byte frame")
    return list(struct.unpack(f">{size // 2}H", frame[3:3 + size]))


# ── AT commands ───────────────────────────────────────────────────────────

def at_query(command: str) -> bytes:
    """`AT+{command}?` - refused if the name could be read back as another command."""
    if not command or not command.isascii() or any(c in command for c in "?=:\r\n"):
        raise ValueError(f"not a usable AT command name: {command!r}")
    return AT_PREFIX + command.encode("ascii") + b"?" + LINE_END


def parse_at_reply(line: bytes) -> tuple[str, str]:
    """`(name, value)` of an `AT+NAME:value` line.

    Split at the first colon only: a server address comes back as
    `host,port,TCP`, and values may hold colons of their own. An empty value is
    a value - the heartbeat answers with nothing after the colon.
    """
    if not (line.startswith(AT_PREFIX) and line.endswith(LINE_END)):
        raise FrameError(f"not an AT line: {line!r}")
    try:
        text = line[len(AT_PREFIX):-len(LINE_END)].decode("ascii")
    except UnicodeDecodeError as err:
        raise FrameError(f"AT line is not ASCII: {line!r}") from err
    name, colon, value = text.partition(":")
    if not colon or not name:
        raise FrameError(f"AT line has no name:value: {line!r}")
    return name, value


# ── splitting the stream ──────────────────────────────────────────────────

def _modbus_length(head: bytes) -> int | None:
    """How long the Modbus reply starting `head` is, or None until that is knowable."""
    if len(head) < 2:
        return None
    function = head[1]
    if function & ERROR_FLAG:
        return 5                                   # slave, function, code, CRC
    if function in (READ_HOLDING, READ_INPUT):
        return None if len(head) < 3 else 5 + head[2]
    if function in (WRITE_SINGLE, WRITE_MULTIPLE):
        return 8                                   # both echo six bytes, plus CRC
    raise FrameError(f"function 0x{function:02x} cannot start a reply: {head[:16].hex()}")


def split(buffer: bytes) -> tuple[list[Frame], bytes]:
    """Every complete frame at the front of `buffer`, and the incomplete rest.

    Feed the rest back in front of the next bytes read. Raises `FrameError` when
    the buffer cannot be framed, including a Modbus frame that fails its CRC:
    the caller has to drop the buffer, because no later boundary can be trusted.
    """
    frames: list[Frame] = []
    pos = 0
    while pos < len(buffer):
        rest = buffer[pos:]
        if rest.startswith(AT_PREFIX):
            end = rest.find(LINE_END)
            if end < 0:
                break                              # the rest of the line is still coming
            frames.append(Frame(Kind.AT, rest[:end + len(LINE_END)]))
            pos += end + len(LINE_END)
            continue
        if len(rest) < len(AT_PREFIX) and AT_PREFIX.startswith(rest):
            break                                  # may yet become "AT+"
        length = _modbus_length(rest)
        if length is None or len(rest) < length:
            break
        frame = rest[:length]
        if not _crc_ok(frame):
            raise FrameError(f"CRC mismatch on {frame.hex()}")
        frames.append(Frame(Kind.MODBUS, frame))
        pos += length
    return frames, buffer[pos:]
