"""Home Assistant's test harness, real loopback sockets, and a fake collector."""
from __future__ import annotations

import pytest

from custom_components.eybond_local import const

from .fake_collector import FakeCollector


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture(autouse=True)
def loopback(socket_enabled):
    """The hub is a real TCP listener and the announce a real datagram.

    The harness disables sockets for every test; these talk to 127.0.0.1 only.
    """
    yield


@pytest.fixture
async def collector(monkeypatch):
    """A fake collector listening for announces on a free port, redialling like a real one."""
    fake = FakeCollector(redial_s=0.2)
    await fake.start()
    monkeypatch.setattr(const, "ANNOUNCE_PORT", fake.udp_port)
    monkeypatch.setattr(const, "PROBE_TIMEOUT_S", 5.0)
    monkeypatch.setattr(const, "HUB_STARTUP_GRACE_S", 0.0)
    monkeypatch.setattr(const, "HUB_ANNOUNCE_INTERVAL_S", 0.5)
    yield fake
    await fake.stop()
