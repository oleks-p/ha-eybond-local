"""The listener and the announce: the behaviour that decides whether a collector stays connected."""
from __future__ import annotations

import asyncio

import pytest

from custom_components.eybond_local.protocol.hub import CollectorConfig, CollectorHub, HubConfig

from .fake_collector import PN, FakeCollector


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
async def fake():
    f = FakeCollector()
    await f.start()
    yield f
    await f.stop()


async def make_hub(fake, clock=None, **kw) -> CollectorHub:
    cfg = dict(advertise_ip="127.0.0.1", listen_host="127.0.0.1", listen_port=0,
               announce_port=fake.udp_port, tick_s=3600, quiet_s=0.1)
    cfg.update(kw)
    hub = CollectorHub(HubConfig(**cfg), clock=clock or Clock())
    await hub.start()
    return hub


async def wait_for(predicate, timeout=2.0):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


async def test_nothing_is_announced_during_the_startup_grace(fake):
    clock = Clock()
    hub = await make_hub(fake, clock, startup_grace_s=10)
    hub.add_collector(CollectorConfig("127.0.0.1", PN), lambda _link: None)
    assert hub.announce_due() == []
    clock.now += 10
    assert hub.announce_due() == ["127.0.0.1"]
    await hub.stop()


async def test_one_datagram_per_announce_and_the_collector_dials_in(fake):
    clock = Clock()
    hub = await make_hub(fake, clock, startup_grace_s=0)
    links = []
    hub.add_collector(CollectorConfig("127.0.0.1", PN), links.append)
    hub.announce_due()
    await wait_for(lambda: any(links))
    assert fake.announces == [f"set>server=127.0.0.1:{hub.listen_port};"]
    assert links[-1].pn == PN
    await hub.stop()


async def test_a_connected_collector_is_never_announced_to(fake):
    """It would drop the session it holds. A burst of announces on every
    disconnect turns that into a reconnect loop that never recovers."""
    clock = Clock()
    hub = await make_hub(fake, clock, startup_grace_s=0, announce_interval_s=1)
    links = []
    hub.add_collector(CollectorConfig("127.0.0.1", PN), links.append)
    hub.announce_due()
    await wait_for(lambda: hub.link_for("127.0.0.1") is not None)
    for _ in range(5):
        clock.now += 60
        assert hub.announce_due() == []
    assert len(fake.announces) == 1
    assert fake.connections == 1
    await hub.stop()


async def test_after_a_disconnect_the_collector_gets_time_to_redial_unprompted(fake):
    clock = Clock()
    hub = await make_hub(fake, clock, startup_grace_s=0, announce_interval_s=15)
    hub.add_collector(CollectorConfig("127.0.0.1", PN), lambda _link: None)
    hub.announce_due()
    await wait_for(lambda: hub.link_for("127.0.0.1") is not None)
    await fake.hang_up()
    await wait_for(lambda: hub.link_for("127.0.0.1") is None)
    clock.now += 5
    assert hub.announce_due() == [], "the redirect is sticky; it is probably already redialling"
    clock.now += 15
    assert hub.announce_due() == ["127.0.0.1"]
    await hub.stop()


async def test_an_unknown_collector_is_refused(fake):
    hub = await make_hub(fake, startup_grace_s=0)
    links = []
    hub.add_collector(CollectorConfig("127.0.0.1", "I2000SOMEONE-ELSE"), links.append)
    await fake.dial(("127.0.0.1", hub.listen_port))
    await wait_for(lambda: "127.0.0.1" in hub.stats.refused)
    assert not any(links)
    await hub.stop()


async def test_a_collector_is_matched_by_its_product_number_not_its_address(fake):
    """A lease can move; the PN cannot."""
    hub = await make_hub(fake, startup_grace_s=0)
    links = []
    hub.add_collector(CollectorConfig("192.0.2.50", PN), links.append)  # configured elsewhere
    await fake.dial(("127.0.0.1", hub.listen_port))
    await wait_for(lambda: any(links))
    assert links[-1].address == "127.0.0.1"
    await hub.stop()


async def test_a_redial_supersedes_the_old_session(fake):
    hub = await make_hub(fake, startup_grace_s=0)
    links = []
    hub.add_collector(CollectorConfig("127.0.0.1", PN), links.append)
    target = ("127.0.0.1", hub.listen_port)
    await fake.dial(target)
    await wait_for(lambda: any(links))
    first = links[-1]
    await fake.dial(target)  # a second socket, as after a missed disconnect
    await wait_for(lambda: hub.link_for("127.0.0.1") not in (None, first))
    await wait_for(lambda: first.closed)
    await hub.stop()
