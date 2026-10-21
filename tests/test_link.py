"""One collector connection over a real loopback socket."""
from __future__ import annotations

import asyncio

import pytest

from custom_components.eybond_local.protocol.link import (
    CollectorLink,
    LinkClosed,
    LinkError,
    LinkTimeout,
)

from .fake_collector import FakeCollector


@pytest.fixture
async def pair():
    """A FakeCollector dialled into a CollectorLink."""
    fake = FakeCollector()
    accepted: asyncio.Future = asyncio.get_running_loop().create_future()

    async def on_conn(reader, writer):
        link = CollectorLink(reader, writer, address="127.0.0.1", quiet_s=0.2)
        link.start()
        accepted.set_result(link)
        await link.wait_closed()

    server = await asyncio.start_server(on_conn, "127.0.0.1", 0)
    await fake.dial(("127.0.0.1", server.sockets[0].getsockname()[1]))
    link = await asyncio.wait_for(accepted, 2)
    yield fake, link
    await link.close("test over")
    await fake.stop()
    server.close()


async def test_at_query_and_read(pair):
    fake, link = pair
    assert await link.at_query("DTUPN") == fake.pn
    assert await link.read(184, 1) == [11]
    words = await link.read(201, 34)
    assert words[217 - 201] == 65068


async def test_an_unimplemented_at_command_is_a_value_not_an_error(pair):
    _, link = pair
    assert await link.at_query("VER") == "R001"


async def test_a_late_answer_is_not_taken_for_the_next_request(pair):
    fake, link = pair
    fake.late_reads[184] = 0.3       # lands inside the 0.2 s quiet window after the timeout
    with pytest.raises(LinkTimeout):
        await link.read(184, 1, timeout_s=0.2)
    # The protocol number arrives late. Same shape as the next answer would be
    # (one word), so only the quiet window stops it being read as 229's.
    assert await link.read(229, 1) == [80]
    assert link.stats.timeouts == 1


async def test_bytes_that_do_not_parse_fail_one_read_and_keep_the_session(pair):
    fake, link = pair
    fake.garbage_before[201] = b"\x01\x99\x00\x00"
    with pytest.raises(LinkError):
        await link.read(201, 34, timeout_s=1)
    assert not link.closed, "one bad frame must not end the session"
    await asyncio.sleep(0.3)
    assert await link.read(184, 1) == [11]
    assert link.stats.resyncs >= 1


async def test_silence_times_out_and_the_link_survives(pair):
    fake, link = pair
    fake.silent_reads.add(201)
    with pytest.raises(LinkTimeout):
        await link.read(201, 34, timeout_s=0.2)
    assert await link.read(184, 1) == [11]


async def test_a_closed_socket_ends_the_link(pair):
    fake, link = pair
    await fake.hang_up()
    await asyncio.wait_for(link.wait_closed(), 2)
    with pytest.raises(LinkClosed):
        await link.read(184, 1)
