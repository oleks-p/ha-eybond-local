"""Energy counters integrated from power. No Home Assistant imports.

The inverter keeps no kWh register - none anywhere in 0-1207 of a protocol-11
unit - so the Energy dashboard's counters have to be integrated from power.
Doing it here rather than in a Home Assistant `integration` sensor uses the
poll's own clock and every sample, not whichever ones the recorder happened to
keep, and the counter survives a restart through its entity's restored state.

Left Riemann sum: a reading holds until the next one, which is what an
inverter that reports a value until it changes means. A gap longer than
`max_gap_s` is not integrated at all - nobody knows what flowed while the
link was down, and assuming the last value held is how a counter over-reports
an outage.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

MAX_GAP_S = 30.0


@dataclass
class EnergyCounter:
    kwh: float = 0.0
    max_gap_s: float = MAX_GAP_S
    _last_t: float | None = None
    _last_w: float | None = None

    def add(self, watts: float | None, t: float) -> None:
        """Account for the interval since the previous sample, then hold `watts`."""
        if self._last_t is not None and self._last_w is not None:
            dt = t - self._last_t
            if 0 < dt <= self.max_gap_s:
                self.kwh += self._last_w * dt / 3_600_000
        self._last_t = t
        self._last_w = None if watts is None else max(0.0, float(watts))

    def interrupt(self) -> None:
        """The source went away. The next sample starts a new interval."""
        self._last_t = None
        self._last_w = None

    def restore(self, kwh: float) -> None:
        self.kwh = max(self.kwh, float(kwh))


def _positive(key: str) -> Callable[[dict], float | None]:
    return lambda v: None if key not in v else max(0.0, float(v[key]))


def _negative(key: str) -> Callable[[dict], float | None]:
    return lambda v: None if key not in v else max(0.0, -float(v[key]))


# What each counter integrates, from a decoded live block. The signs are the
# measured ones in smg.py: battery power is negative while discharging, grid
# power positive while importing.
DIRECTIONAL_POWER: dict[str, Callable[[dict], float | None]] = {
    "battery_charge_power": _positive("battery_power"),
    "battery_discharge_power": _negative("battery_power"),
    "grid_import_power": _positive("grid_power"),
}

ENERGY_SOURCES: dict[str, Callable[[dict], float | None]] = {
    "battery_charge_energy": _positive("battery_power"),
    "battery_discharge_energy": _negative("battery_power"),
    "grid_import_energy": _positive("grid_power"),
    "solar_energy": _positive("pv_power"),
    "output_energy": _positive("output_power"),
}
