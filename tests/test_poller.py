"""The read loop over a real link to the fake collector."""
from __future__ import annotations

import asyncio

import pytest

from custom_components.eybond_local.poller import InverterPoller
from custom_components.eybond_local.protocol.hub import CollectorConfig, CollectorHub, HubConfig
from custom_components.eybond_local.smg import Block

from .fake_collector import PN, FakeCollector

FAST = (Block("live", 201, 34, 0.05), Block("status", 100, 10, 0.2),
        Block("bms", 760, 16, 0.2), Block("settings_a", 300, 26, 10), Block("settings_b", 327, 17, 10),
        Block("rating", 639, 9, 10))


@pytest.fixture
async def setup():
    fake = FakeCollector()
    await fake.start()
    hub = CollectorHub(HubConfig(advertise_ip="127.0.0.1", listen_host="127.0.0.1",
                                 listen_port=0, announce_port=fake.udp_port,
                                 startup_grace_s=0, announce_interval_s=0.3, tick_s=0.05,
                                 quiet_s=0.1))
    await hub.start()
    updates = []
    poller = InverterPoller(on_update=lambda: updates.append(1), blocks=FAST, timeout_s=0.5)
    hub.add_collector(CollectorConfig("127.0.0.1", PN), poller.set_link)
    yield fake, hub, poller
    await poller.stop()
    await hub.stop()
    await fake.stop()


async def wait_for(predicate, timeout=3.0):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.02)


async def test_it_identifies_then_reads_every_block(setup):
    fake, hub, poller = setup
    await wait_for(lambda: "rated_power" in poller.values and "bms_soc" in poller.values)
    assert poller.identity.protocol == 11
    assert poller.identity.device_type == 0x5001
    assert poller.identity.serial == "", "a blank serial is not an error; identity is the PN"
    assert poller.values["battery_power"] == -468
    assert poller.values["battery_discharge_power"] == 468
    assert poller.values["battery_charge_power"] == 0
    assert poller.values["rated_power"] == 5000
    assert poller.available


async def test_energy_accumulates_from_live_reads(setup):
    fake, hub, poller = setup
    await wait_for(lambda: poller.energy["battery_discharge_energy"].kwh > 0)
    assert poller.energy["battery_charge_energy"].kwh == 0
    assert poller.energy["solar_energy"].kwh == 0


async def test_an_unmeasured_protocol_publishes_nothing(setup):
    fake, hub, poller = setup
    await poller.stop()
    fake.registers[184] = 6
    poller.values.clear()
    poller.set_link(hub.link_for("127.0.0.1"))
    await wait_for(lambda: poller.error is not None)
    await asyncio.sleep(0.2)
    assert "protocol 6" in poller.error
    assert poller.values == {}


async def test_a_silent_block_is_retried_and_the_rest_keep_flowing(setup):
    fake, hub, poller = setup
    await wait_for(lambda: poller.available)
    fake.silent_reads.add(760)
    await asyncio.sleep(1.0)
    assert poller.stats.failures.get("bms", 0) >= 1
    assert poller.available, "one block failing does not take the live data down"


async def test_losing_the_collector_makes_it_unavailable_and_it_comes_back(setup):
    fake, hub, poller = setup
    await wait_for(lambda: poller.available)
    fake.redial_s = None
    await fake.hang_up()
    await wait_for(lambda: not poller.connected)
    assert not poller.available
    # The hub announces again; the collector dials back in.
    await wait_for(lambda: poller.available, timeout=5)


async def test_the_firmware_string_is_read_when_the_inverter_has_one(setup):
    fake, hub, poller = setup
    await wait_for(lambda: poller.identity is not None)
    assert poller.identity.firmware == "", "absent until the registers hold one"
    text = b"5001_TEST0001v1\x00"
    for i in range(8):
        fake.registers[626 + i] = int.from_bytes(text[2 * i:2 * i + 2], "big")
    await poller.stop()
    poller.set_link(hub.link_for("127.0.0.1"))
    await wait_for(lambda: poller.identity and poller.identity.firmware)
    assert poller.identity.firmware == "5001_TEST0001v1"
