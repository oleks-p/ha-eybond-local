"""Framing on the collector's socket: AT lines and bare Modbus RTU."""
from __future__ import annotations

import pytest

from custom_components.eybond_local.protocol import frames
from custom_components.eybond_local.protocol.frames import (
    DeviceException,
    FrameError,
    Kind,
    at_query,
    crc16,
    parse_at_reply,
    parse_read_reply,
    read_request,
    split,
)

from .fake_collector import modbus_reply


def test_crc_matches_the_standard_vector():
    # The Modbus specification's own example: read 10 registers from 0 on slave 1.
    assert read_request(1, 0, 10) == bytes.fromhex("01030000000ac5cd")
    assert crc16(b"") == 0xFFFF


def test_a_read_reply_gives_raw_unsigned_words():
    assert parse_read_reply(modbus_reply(1, [11, 0xFFCF])) == [11, 0xFFCF]


def test_an_exception_reply_is_the_devices_refusal():
    body = bytes([1, 0x83, 0x02])
    with pytest.raises(DeviceException) as err:
        parse_read_reply(body + crc16(body).to_bytes(2, "little"))
    assert err.value.code == 2


@pytest.mark.parametrize("frame", [
    bytes.fromhex("0103020001") + b"\x00\x00",          # bad CRC
    b"\x01\x03",                                          # truncated
])
def test_a_broken_reply_is_refused(frame):
    with pytest.raises(FrameError):
        parse_read_reply(frame)


def test_at_and_modbus_frames_split_out_of_one_stream():
    stream = b"AT+DTUPN:I2000X\r\n" + modbus_reply(1, [1, 2, 3]) + b"AT+HTBT:\r\n"
    found, rest = split(stream)
    assert [f.kind for f in found] == [Kind.AT, Kind.MODBUS, Kind.AT]
    assert rest == b""
    assert parse_at_reply(found[2].raw) == ("HTBT", ""), "an empty value is a value"


@pytest.mark.parametrize("cut", range(1, 11))
def test_a_frame_cut_anywhere_waits_for_the_rest(cut):
    stream = modbus_reply(1, [7, 8, 9])
    assert len(stream) == 11
    found, rest = split(stream[:cut])
    assert found == [] and rest == stream[:cut]
    found, rest = split(rest + stream[cut:])
    assert len(found) == 1 and rest == b""


def test_a_partial_at_prefix_waits_rather_than_failing():
    assert split(b"AT") == ([], b"AT")


def test_a_bad_crc_in_the_stream_is_an_error_not_a_skip():
    """With no transaction id, re-guessing a boundary would misread everything after it."""
    good = modbus_reply(1, [1])
    with pytest.raises(FrameError):
        split(good[:-1] + bytes([good[-1] ^ 0xFF]))


def test_an_unknown_function_code_cannot_start_a_reply():
    with pytest.raises(FrameError):
        split(bytes([1, 0x42, 0, 0, 0]))


def test_an_at_value_keeps_its_own_colons():
    assert parse_at_reply(b"AT+CLDSRVHOST1:host.example,18899,TCP:x\r\n") == (
        "CLDSRVHOST1", "host.example,18899,TCP:x")


@pytest.mark.parametrize("name", ["", "A?B", "A=B", "A:B", "A\r\n", "Ä"])
def test_a_command_name_that_would_read_back_as_another_is_refused(name):
    with pytest.raises(ValueError):
        at_query(name)


@pytest.mark.parametrize(("slave", "address", "count"), [(248, 0, 1), (1, 70000, 1), (1, 0, 0),
                                                        (1, 0, frames.MAX_READ_WORDS + 1)])
def test_read_bounds_are_checked_not_masked(slave, address, count):
    with pytest.raises(ValueError):
        read_request(slave, address, count)
