"""Energy integrated from power: a left Riemann sum that does not invent outages."""
from __future__ import annotations

import pytest

from custom_components.eybond_local.energy import DIRECTIONAL_POWER, ENERGY_SOURCES, EnergyCounter


def test_a_steady_kilowatt_for_an_hour_is_one_kilowatt_hour():
    c = EnergyCounter()
    for i in range(1201):
        c.add(1000, i * 3.0)
    assert c.kwh == pytest.approx(1.0)


def test_each_reading_holds_until_the_next():
    c = EnergyCounter()
    c.add(3600, 0)
    c.add(0, 10)      # 3600 W held for 10 s = 0.01 kWh
    c.add(0, 20)
    assert c.kwh == pytest.approx(0.01)


def test_a_gap_longer_than_the_limit_is_not_integrated():
    c = EnergyCounter(max_gap_s=30)
    c.add(1000, 0)
    c.add(1000, 600)   # ten minutes with no readings: unknown, not 1 kW
    c.add(1000, 603)
    assert c.kwh == pytest.approx(1000 * 3 / 3_600_000)


def test_an_interruption_starts_a_new_interval():
    c = EnergyCounter()
    c.add(1000, 0)
    c.interrupt()
    c.add(1000, 3)
    assert c.kwh == 0


def test_negative_power_never_counts_backwards():
    c = EnergyCounter()
    c.add(-500, 0)
    c.add(-500, 3)
    assert c.kwh == 0


def test_restore_never_lowers_the_counter():
    c = EnergyCounter(kwh=2.0)
    c.restore(1.5)
    assert c.kwh == 2.0
    c.restore(5.0)
    assert c.kwh == 5.0


def test_battery_power_is_split_by_its_measured_sign():
    discharging = {"battery_power": -468, "grid_power": 1200, "pv_power": 0, "output_power": 420}
    assert DIRECTIONAL_POWER["battery_discharge_power"](discharging) == 468
    assert DIRECTIONAL_POWER["battery_charge_power"](discharging) == 0
    assert DIRECTIONAL_POWER["grid_import_power"](discharging) == 1200
    assert ENERGY_SOURCES["solar_energy"]({}) is None, "absent is not zero"
