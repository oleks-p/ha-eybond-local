# EyeBond Local

A Home Assistant integration for SMG-II hybrid inverters - Anenji, EASUN, PowMr
and the other clones - read locally through the EyeBond Wi-Fi collector they
ship with. No cloud, no dongle swap, no router changes.

## Why it exists

The collector never listens: it dials its vendor cloud. One unauthenticated UDP
datagram, `set>server=<ip>:<port>;` on port 58899, makes it dial Home Assistant
instead, and from then on it carries AT commands and bare Modbus RTU over that
one connection. Getting that right takes care in four places, and each is a
failure seen on real hardware:

| Pitfall | What this integration does |
|---|---|
| The collector **drops the session it holds on every redirect datagram**. Sending them in bursts, one burst per disconnect, locks it in a loop - 40-50 reconnects in 30 s, every entity unavailable, no recovery. | **One datagram, and never to a connected collector.** |
| One frame that does not parse is easy to treat as a dead session. | Drops the frame, waits for the line to go quiet, carries on. |
| The collector speaks two protocols on the same port, and probing the wrong one first can stall discovery. | Speaks the AT protocol the collector answers natively. |
| Half the register map is easy to miss. | Reads live data, the lithium BMS block, the settings, fault and warning codes, and the rating. |

## What you get

One device per collector. Entity ids are `<prefix>_<key>` - `sensor.inverter_battery_power`
by default - in English whatever language Home Assistant runs in; names are
translated (English, Ukrainian).

| Group | Entities |
|---|---|
| Live, every 3 s | operating mode; grid V, Hz, W; output V, A, Hz, W, VA; load %; battery V, A, W, SOC; PV V, A, W; charging power and currents; two temperatures |
| Directional power | battery charging, battery discharging, grid import - always positive, for the Energy dashboard's two-sensor power option and power-flow cards |
| Energy, kWh | battery charged, battery discharged, grid imported, solar produced, output to the house - see [Energy](#energy) |
| Status, every 15 s | fault code, warning code - each with a `bits` attribute listing the bits set - and a problem flag for each |
| BMS, every 15 s | charge and discharge voltage and current limits, BMS voltage and SOC - **off by default**, see [Confidence](#confidence) |
| Settings, every 5 min | output and charger priority, battery type, bulk and float voltage, cutoffs, charge currents, the three SOC switch points - no state class, so no long-term statistics |
| Bridge, off by default | the inverter bridge's own voltage, current, frequency and power, ahead of the bypass relay; `bridge_temperature` is on |
| Link | `binary_sensor.<prefix>_collector_connected` - never unavailable itself, so it is the one to alert on |

**Read-only.** Nothing is written to the inverter. Writes will come only as an
allow-list of settings the inverter keeps for itself, each range-checked and
read back.

## Signs

Measured on hardware, not assumed:

* **Battery power is negative while discharging.** Current x voltage matches it.
* **Grid power is positive while importing.** It balances battery charging, plus
  load, plus the inverter's own losses.

So in the Energy dashboard: battery power **inverted**, grid power standard - or
use the directional sensors and the two-sensor option.

## Energy

The inverter keeps no kWh register - none anywhere in registers 0-1207 - so
the counters are integrated from power, here, on the poll's own clock. A
reading holds until the next one (left Riemann sum). A gap longer than 30 s is
not integrated at all: nobody knows what flowed while the link was down. The
counters restore across restarts and are `total_increasing`, so one that ever
comes back lower is read as a new meter cycle, not as negative energy.

## Install

1. HACS -> Integrations -> Custom repositories -> this repository, category Integration.
2. Download **EyeBond Local** and restart Home Assistant.
3. Give the collector a **DHCP reservation**. Announces go to its address.
4. Settings -> Devices & services -> Add -> **EyeBond Local**: the collector's
   address, Home Assistant's own address on that network, port 8899.

The entry is created only once the collector has dialled in and the inverter
has reported a protocol number this integration was measured on.

**Only one integration can serve a collector.** If another integration already
reads it locally, disable that first: it will be listening on the same port
8899, and the form says so if it is still there.

Home Assistant must be reachable from the collector on that port: host
networking, Home Assistant OS, or Core. A bridge-mode container needs the port
published.

## Several inverters

One entry per collector, each with **its own prefix** - for a three-phase bank,
`inverter_l1`, `inverter_l2`, `inverter_l3`. Entity ids are built from the prefix,
not from the device name, and the form refuses a prefix another entry already
uses: two entries on one would give one unit `_2` ids, in whichever order they
happened to load. Entries on the same port share one listener.

## Checking that entities are being written

Not by `last_reported` in `/api/states`. An unchanged write updates it on the live
state object, but the REST and websocket state dumps serve a cached copy, so a
value that has not changed looks frozen there however often it is written. A
template reads the live object:

```
{{ states.sensor.inverter_battery_soc.last_reported }}
```

Every entity of an entry is written on every poll, changed or not.

## Behaviour the collector forces

| Observation | What the integration does |
|---|---|
| It drops the session it holds on every announce | Announces only while that collector is disconnected |
| The redirect is sticky: after a disconnect it redials every 3 s on its own | Waits 10 s after start, and 15 s after a disconnect, before announcing |
| No transaction id; answers strictly in order | One request at a time. An answer must match the request's kind, function and length |
| A timed-out request may still be answered | A quiet window after every timeout; late bytes are dropped, never read as the next answer |
| Bare RTU has no delimiter | Bytes that do not parse drop the buffer, not the session |
| Register 186 (serial) can read blank in a first session and correctly afterwards | Collectors are matched by their own product number, `AT+DTUPN?`, which the collector always answers |
| The map is versioned by register 184 | An unmeasured protocol number publishes nothing |
| The inverter goes quiet for ~20 s at times | A failed read is retried on the next pass; the session is kept |

## Confidence

Every register-map entity carries `register` and `confidence` attributes:

* **confirmed** - checked against hardware: a reading that balances, or matches a physical state.
* **identified** - from the published map, matching the hardware wherever it
  could be checked, not yet exercised. PV fields stay here until a unit with
  panels confirms them: zero is undecidable.
* **inferred** - a plausible value with no published source. The BMS block is
  here, and is **off by default**: its voltage and SOC match the inverter's own
  readings exactly; its limits only look right.

## Design

```
custom_components/eybond_local/
  protocol/       No Home Assistant imports.
    frames.py     AT lines and bare Modbus RTU: CRC, requests, replies, splitting the stream
    link.py       one connection: one transaction at a time, quiet windows, resync
    hub.py        the listener, the announce, collectors matched by product number
  smg.py          the register map as data, with confidence and plausible bands
  energy.py       kWh counters integrated from power
  poller.py       identify, refuse an unmeasured protocol, read each block on its cadence
  __init__.py     entries; one reference-counted hub per port
  config_flow.py  announce, wait for the collector, check the protocol, then create
  sensor.py binary_sensor.py diagnostics.py
```

Tests run against a fake collector that answers the announce, dials in and
serves a self-consistent set of registers - and that can answer late, send
junk, go silent, or redial like the real one.

```
pip install -r requirements_test.txt
pytest -q
```
