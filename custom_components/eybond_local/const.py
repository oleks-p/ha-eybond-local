"""Constants. Ports are module attributes, read at call time, so tests can move them."""
from __future__ import annotations

DOMAIN = "eybond_local"

CONF_COLLECTOR_IP = "collector_ip"
CONF_ADVERTISE_IP = "advertise_ip"
CONF_PORT = "port"
CONF_PN = "pn"
CONF_PREFIX = "prefix"

DEFAULT_PORT = 8899
DEFAULT_PREFIX = "inverter"

# The collector's fixed command port. Not configurable on the device.
ANNOUNCE_PORT = 58899

# The redirect is sticky: after a restart the collector usually redials on its
# own within seconds, and an announce sent meanwhile only costs it a reconnect.
HUB_STARTUP_GRACE_S = 10.0
HUB_ANNOUNCE_INTERVAL_S = 15.0

# How long the config flow waits for a collector to dial in after the announce.
# Measured: 0.56 s. The margin is for a collector that is mid-redial elsewhere.
PROBE_TIMEOUT_S = 30.0
# How long the config flow's listener outlives it, for the entry set up next.
PROBE_LINGER_S = 10.0

PLATFORMS = ["binary_sensor", "sensor"]


def signal_update(entry_id: str) -> str:
    return f"{DOMAIN}_{entry_id}_update"
