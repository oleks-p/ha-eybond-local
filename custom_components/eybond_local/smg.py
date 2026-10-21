"""The SMG-II register map, as data. No Home Assistant imports.

**The map is chosen by the protocol number the inverter reports at register
184, never by brand or model.** Anenji alone ships two platforms, and inside
SMG-II the map is versioned by that number. A wrong map decodes a real frame
into plausible, wrong numbers that nothing downstream can catch, so an
unknown protocol number publishes nothing.

**Every field says how sure we are of it**:

* `confirmed` - checked against a live value on hardware: a reading that
  balances against others, or a reading that matches a physical state.
* `identified` - the address comes from a published map that matched the
  hardware everywhere it could be checked, but this field has not been
  exercised. Zero is undecidable: a PV input with no panels reads 0 W either
  way.
* `inferred` - an address with a plausible value and no published source. Off
  by default.

Sources: register sweeps of a live protocol-11 unit, read with the grid both
absent and present, and the vendor's published SMG RS232 Modbus map.
"""
from __future__ import annotations

from dataclasses import dataclass

CONFIRMED, IDENTIFIED, INFERRED = "confirmed", "identified", "inferred"
DIAGNOSTIC = "diagnostic"

REG_DEVICE_TYPE = 171
REG_PROTOCOL = 184
REG_SERIAL = 186
SERIAL_WORDS = 12
# An ASCII string whose first four characters are the device type in hex,
# followed by a version. Best-effort, like the serial: both have read blank in a
# first session and correctly afterwards.
REG_FIRMWARE = 626
FIRMWARE_WORDS = 8

# Keys that were renamed, old -> new. Entries migrate their registry rows so
# history and statistics follow, and a customised entity id is left alone.
RENAMED_KEYS = {
    "inverter_voltage": "bridge_voltage",
    "inverter_current": "bridge_current",
    "inverter_frequency": "bridge_frequency",
    "inverter_power": "bridge_power",
    "inverter_temperature": "bridge_temperature",
}


@dataclass(frozen=True)
class Block:
    """One read, and how often it is worth making."""

    name: str
    address: int
    count: int
    every_s: float


# 16 registers took 0.28-0.70 s, 0.44 s on average, and 36 in one read
# worked; nothing larger has been tried on hardware. So no block exceeds 34 -
# the settings are two reads, not one of 57. Live data every 3 s leaves the line
# idle most of the time; the settings hardly ever change.
BLOCKS = (
    Block("live", 201, 34, 3.0),
    Block("status", 100, 10, 15.0),
    Block("bms", 760, 16, 15.0),
    Block("settings_a", 300, 26, 300.0),
    Block("settings_b", 327, 17, 300.0),
    Block("rating", 639, 9, 300.0),
)
MAX_BLOCK_WORDS = 34


@dataclass(frozen=True)
class Field:
    key: str
    block: str                           # the group: live, status, bms, settings, rating
    address: int
    scale: float = 1.0
    signed: bool = False
    words: int = 1                       # 2 = a 32-bit value, high word first
    unit: str | None = None
    device_class: str | None = None
    state_class: str | None = "measurement"
    category: str | None = None          # "diagnostic" or None
    enabled: bool = True                 # enabled by default
    confidence: str = IDENTIFIED
    lo: float | None = None              # outside [lo, hi] is not a reading
    hi: float | None = None
    options: dict[int, str] | None = None
    precision: int | None = None


def _v(key, block, address, **kw) -> Field:
    return Field(key, block, address, scale=0.1, unit="V", device_class="voltage",
                 precision=1, **kw)


def _a(key, block, address, **kw) -> Field:
    return Field(key, block, address, scale=0.1, signed=True, unit="A", device_class="current",
                 precision=1, **kw)


def _w(key, block, address, **kw) -> Field:
    return Field(key, block, address, signed=True, unit="W", device_class="power",
                 lo=-100_000, hi=100_000, precision=0, **kw)


def _hz(key, block, address, **kw) -> Field:
    return Field(key, block, address, scale=0.01, unit="Hz", device_class="frequency",
                 lo=0, hi=70, precision=2, **kw)


def _setting(key, address, scale=1.0, unit=None, device_class=None, **kw) -> Field:
    # No state class. A setting is not a measurement, and with one the recorder
    # keeps hourly mean/min/max of it in long-term statistics, which are never
    # purged - for a value that changes when someone walks up to the panel.
    kw.pop("state_class", None)
    return Field(key, "settings", address, scale=scale, unit=unit, device_class=device_class,
                 state_class=None, category=DIAGNOSTIC,
                 precision={0.1: 1, 0.01: 2}.get(scale), **kw)


# Read 2 ("Grid Mode") when the grid returned and 3 with it absent; the rest is
# the published map.
OPERATING_MODES = {
    0: "power_on", 1: "standby", 2: "grid", 3: "off_grid",
    4: "bypass", 5: "charging", 6: "fault",
}

FIELDS: tuple[Field, ...] = (
    # ── live ──────────────────────────────────────────────────────────────
    Field("operating_mode", "live", 201, unit=None, device_class="enum", state_class=None,
          options=OPERATING_MODES, confidence=CONFIRMED),
    _v("grid_voltage", "live", 202, lo=0, hi=300, confidence=CONFIRMED),
    _hz("grid_frequency", "live", 203, confidence=CONFIRMED),
    # Positive while importing: measured, it balanced battery charging plus load
    # plus the inverter's own losses.
    _w("grid_power", "live", 204, confidence=CONFIRMED),
    # 205-208 are the inverter bridge's own output, ahead of the bypass relay;
    # 210-214 are what reaches the loads. "bridge", not "inverter": the device
    # is the inverter, and `sensor.inverter_inverter_voltage` says it twice.
    _v("bridge_voltage", "live", 205, lo=0, hi=300, enabled=False, confidence=CONFIRMED),
    _a("bridge_current", "live", 206, lo=-1000, hi=1000, enabled=False),
    _hz("bridge_frequency", "live", 207, enabled=False, confidence=CONFIRMED),
    _w("bridge_power", "live", 208, enabled=False),
    _w("ac_charging_power", "live", 209),
    _v("output_voltage", "live", 210, lo=0, hi=300, confidence=CONFIRMED),
    _a("output_current", "live", 211, lo=-1000, hi=1000, confidence=CONFIRMED),
    _hz("output_frequency", "live", 212, confidence=CONFIRMED),
    _w("output_power", "live", 213, confidence=CONFIRMED),
    Field("output_apparent_power", "live", 214, unit="VA", device_class="apparent_power",
          lo=0, hi=150_000, precision=0, confidence=CONFIRMED),
    _v("battery_voltage", "live", 215, lo=0, hi=1000, confidence=CONFIRMED),
    _a("battery_current", "live", 216, lo=-1000, hi=1000, confidence=CONFIRMED),
    # NEGATIVE while discharging: measured off-grid, and current x voltage
    # matches it.
    _w("battery_power", "live", 217, confidence=CONFIRMED),
    _v("pv_voltage", "live", 219, lo=0, hi=1500),
    _a("pv_current", "live", 220, lo=-1000, hi=1000),
    _w("pv_power", "live", 223),
    _w("pv_charging_power", "live", 224),
    # Of apparent power, not active: it tracks VA over rated power, not W.
    Field("load_percentage", "live", 225, unit="%", lo=0, hi=300, precision=0,
          confidence=CONFIRMED),
    # The DC/DC stage's heatsink (226) and the inverter bridge's (227).
    Field("dc_temperature", "live", 226, signed=True, unit="°C", device_class="temperature",
          lo=-60, hi=250, precision=0, confidence=CONFIRMED),
    Field("bridge_temperature", "live", 227, signed=True, unit="°C",
          device_class="temperature", lo=-60, hi=250, precision=0, confidence=CONFIRMED),
    Field("battery_soc", "live", 229, unit="%", device_class="battery", lo=0, hi=100,
          precision=0, confidence=CONFIRMED),
    _a("battery_average_current", "live", 232, lo=-1000, hi=1000, enabled=False,
       confidence=CONFIRMED),
    _a("ac_charging_current", "live", 233, lo=-1000, hi=1000),
    _a("pv_charging_current", "live", 234, lo=-1000, hi=1000),
    # ── status ────────────────────────────────────────────────────────────
    Field("fault_code", "status", 100, words=2, state_class=None, category=DIAGNOSTIC,
          confidence=IDENTIFIED),
    # Bit 0 has been seen set with the grid out and clear with it back, and
    # bit 6 set with no PV attached. Two observations are not a map: the code
    # is published raw, with its bits.
    Field("warning_code", "status", 108, words=2, state_class=None, category=DIAGNOSTIC,
          confidence=IDENTIFIED),
    # ── Lithium BMS values, relayed by the inverter. No published source. ──
    # 770 and 773 matched the inverter's own battery voltage and SOC exactly;
    # the limits are plausible for the battery bank and nothing more. All off
    # by default.
    _v("bms_charge_voltage_limit", "bms", 761, lo=0, hi=1000, enabled=False,
       category=DIAGNOSTIC, confidence=INFERRED),
    _v("bms_discharge_voltage_limit", "bms", 762, lo=0, hi=1000, enabled=False,
       category=DIAGNOSTIC, confidence=INFERRED),
    Field("bms_charge_current_limit", "bms", 763, scale=0.1, unit="A", device_class="current",
          lo=0, hi=2000, precision=1, enabled=False, category=DIAGNOSTIC, confidence=INFERRED),
    Field("bms_discharge_current_limit", "bms", 764, scale=0.1, unit="A",
          device_class="current", lo=0, hi=2000, precision=1, enabled=False,
          category=DIAGNOSTIC, confidence=INFERRED),
    _v("bms_voltage", "bms", 770, lo=0, hi=1000, enabled=False, category=DIAGNOSTIC,
       confidence=INFERRED),
    Field("bms_soc", "bms", 773, unit="%", device_class="battery", lo=0, hi=100, precision=0,
          enabled=False, category=DIAGNOSTIC, confidence=INFERRED),
    # ── settings: read only here. Names and scales from the published map; on
    # a live unit they decode as a coherent charge profile - float below bulk
    # below over-voltage, the off-grid cutoff below the on-grid one. ────────
    _setting("output_mode", 300),
    _setting("output_priority", 301),
    _setting("input_voltage_range", 302, enabled=False),
    _setting("buzzer_mode", 303, enabled=False),
    _setting("output_voltage_setting", 320, 0.1, "V", "voltage", enabled=False),
    _setting("output_frequency_setting", 321, 0.01, "Hz", "frequency", enabled=False),
    _setting("battery_type", 322),
    _setting("battery_overvoltage", 323, 0.1, "V", "voltage", enabled=False),
    _setting("bulk_charge_voltage", 324, 0.1, "V", "voltage"),
    _setting("float_charge_voltage", 325, 0.1, "V", "voltage"),
    _setting("low_voltage_cutoff_grid", 327, 0.1, "V", "voltage"),
    _setting("low_voltage_cutoff_off_grid", 329, 0.1, "V", "voltage"),
    # Confirmed against the inverter's own panel: the values set there, read
    # here exactly, before and after a change.
    _setting("max_charge_current", 332, 0.1, "A", "current", confidence=CONFIRMED),
    _setting("max_mains_charge_current", 333, 0.1, "A", "current", confidence=CONFIRMED),
    _setting("equalization_voltage", 334, 0.1, "V", "voltage", enabled=False),
    _setting("soc_back_to_grid", 341, 1, "%"),
    _setting("soc_back_to_battery", 342, 1, "%"),
    _setting("soc_low_cutoff", 343, 1, "%"),
    # ── rating ────────────────────────────────────────────────────────────
    Field("rated_power", "rating", 643, unit="W", device_class="power", state_class=None,
          category=DIAGNOSTIC, lo=100, hi=100_000, confidence=CONFIRMED),
)

def _covers(block: Block, spec: Field) -> bool:
    return block.address <= spec.address <= block.address + block.count - spec.words


# A field belongs to whichever read covers its address, so a group can be
# split across reads without touching the fields.
FIELDS_BY_BLOCK: dict[str, tuple[Field, ...]] = {
    b.name: tuple(f for f in FIELDS if _covers(b, f)) for b in BLOCKS
}

# Only protocol numbers measured on hardware. Others in the family use other
# layouts - the published map for protocols 3-6 is a different one - and a
# protocol that merely might share this layout is not decoded on a guess.
SUPPORTED_PROTOCOLS = frozenset({11})


class UnsupportedProtocol(Exception):
    """The inverter reports a protocol number this map was never measured on."""


def check_protocol(protocol: int) -> None:
    if protocol not in SUPPORTED_PROTOCOLS:
        raise UnsupportedProtocol(
            f"the inverter reports SMG-II protocol {protocol}; this integration has been "
            f"measured on {sorted(SUPPORTED_PROTOCOLS)} only, and decoding another map on a "
            "guess yields plausible wrong numbers. Capture a register sweep before adding it."
        )


def _to_signed(word: int, bits: int) -> int:
    return word - (1 << bits) if word & (1 << (bits - 1)) else word


def decode(block: Block, words: list[int]) -> dict[str, float | int | str]:
    """Decode one block. A value outside its plausible band is left out, not clamped.

    A partial block decodes what it covers. Inventing a value for what it does
    not cover would publish a reading that looks complete.
    """
    values: dict[str, float | int | str] = {}
    end = block.address + len(words)
    for spec in FIELDS_BY_BLOCK[block.name]:
        if not block.address <= spec.address <= end - spec.words:
            continue
        i = spec.address - block.address
        if spec.words == 2:
            raw = (words[i] << 16) | words[i + 1]
            raw = _to_signed(raw, 32) if spec.signed else raw
        else:
            raw = _to_signed(words[i], 16) if spec.signed else words[i]
        if spec.options is not None:
            if raw in spec.options:
                values[spec.key] = spec.options[raw]
            continue
        value = raw * spec.scale
        if spec.lo is not None and value < spec.lo:
            continue
        if spec.hi is not None and value > spec.hi:
            continue
        if spec.precision is not None:
            value = round(value, spec.precision)
            if spec.precision == 0:
                value = int(value)
        elif spec.scale == 1:
            value = int(value)
        values[spec.key] = value
    return values


def words_to_ascii(words: list[int]) -> str:
    """Two characters per register, big-endian, NUL- and space-trimmed."""
    raw = b"".join(w.to_bytes(2, "big") for w in words)
    return raw.split(b"\x00", 1)[0].decode("ascii", errors="replace").strip()
