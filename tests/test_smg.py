"""The register map, decoded against a self-consistent set of register words."""
from __future__ import annotations

import json
import pathlib

import pytest

from custom_components.eybond_local import smg
from custom_components.eybond_local.energy import DIRECTIONAL_POWER, ENERGY_SOURCES

from .fake_collector import REGISTERS

BLOCK = {b.name: b for b in smg.BLOCKS}


def test_the_live_block_decodes_with_the_signs_the_energy_dashboard_needs():
    v = smg.decode(BLOCK["live"], REGISTERS[201])
    assert v["operating_mode"] == "off_grid"
    assert v["grid_voltage"] == 0.0
    assert v["output_voltage"] == 230.0
    assert v["output_power"] == 420
    assert v["battery_voltage"] == 52.0
    # Discharging is negative, and current x voltage balances power.
    assert v["battery_current"] == -9.0
    assert v["battery_power"] == -468
    assert v["battery_voltage"] * v["battery_current"] == pytest.approx(v["battery_power"])
    assert v["battery_soc"] == 80
    assert v["bridge_temperature"] == 33
    assert v["pv_voltage"] == 0.0


def test_the_settings_decode_as_a_coherent_charge_profile():
    words = REGISTERS[300]
    v = smg.decode(BLOCK["settings_a"], words[:26]) | smg.decode(BLOCK["settings_b"], words[27:44])
    assert (v["bulk_charge_voltage"], v["float_charge_voltage"]) == (56.0, 54.0)
    assert v["float_charge_voltage"] < v["bulk_charge_voltage"] < v["battery_overvoltage"]
    assert (v["low_voltage_cutoff_grid"], v["low_voltage_cutoff_off_grid"]) == (47.0, 44.0)
    assert (v["max_charge_current"], v["max_mains_charge_current"]) == (60.0, 30.0)
    assert (v["soc_low_cutoff"], v["soc_back_to_grid"], v["soc_back_to_battery"]) == (10, 15, 40)


def test_bms_fields_match_the_inverters_own_battery_readings():
    bms = smg.decode(BLOCK["bms"], REGISTERS[760])
    live = smg.decode(BLOCK["live"], REGISTERS[201])
    assert bms["bms_voltage"] == live["battery_voltage"]
    assert bms["bms_soc"] == live["battery_soc"]
    assert bms["bms_discharge_current_limit"] == 150.0


def test_32_bit_codes_are_high_word_first():
    v = smg.decode(BLOCK["status"], [0, 0, 0, 0, 0, 7, 0, 0, 0x0001, 0x0041])
    assert v["fault_code"] == 0
    assert v["warning_code"] == 0x10041


def test_an_implausible_value_is_left_out_not_clamped():
    words = list(REGISTERS[201])
    words[203 - 201] = 0xFFFF  # 655.35 Hz
    v = smg.decode(BLOCK["live"], words)
    assert "grid_frequency" not in v
    assert v["output_frequency"] == 50.0


def test_an_unknown_mode_is_left_out():
    words = list(REGISTERS[201])
    words[0] = 9
    assert "operating_mode" not in smg.decode(BLOCK["live"], words)


def test_a_short_block_decodes_only_what_it_covers():
    v = smg.decode(BLOCK["live"], REGISTERS[201][:5])
    assert set(v) == {"operating_mode", "grid_voltage", "grid_frequency", "grid_power",
                      "bridge_voltage"}


@pytest.mark.parametrize("protocol", [11])
def test_measured_protocols_are_accepted(protocol):
    smg.check_protocol(protocol)


@pytest.mark.parametrize("protocol", [0, 1, 3, 6, 12])
def test_an_unmeasured_protocol_is_refused(protocol):
    with pytest.raises(smg.UnsupportedProtocol):
        smg.check_protocol(protocol)


def test_every_field_is_read_by_exactly_one_block_and_keys_are_unique():
    keys = [f.key for f in smg.FIELDS]
    assert len(keys) == len(set(keys))
    for f in smg.FIELDS:
        readers = [name for name, fields in smg.FIELDS_BY_BLOCK.items() if f in fields]
        assert len(readers) == 1, (f.key, readers)
        assert readers[0].startswith(f.block), (f.key, readers)
        assert f.confidence in (smg.CONFIRMED, smg.IDENTIFIED, smg.INFERRED), f.key


def test_no_read_is_larger_than_has_been_measured():
    assert all(b.count <= smg.MAX_BLOCK_WORDS for b in smg.BLOCKS)


def test_inferred_fields_are_off_by_default():
    """A value whose meaning is guessed must be asked for, not handed out."""
    assert all(not f.enabled for f in smg.FIELDS if f.confidence == smg.INFERRED)


def test_every_entity_has_a_name_in_both_languages():
    base = pathlib.Path(__file__).parents[1] / "custom_components" / "eybond_local"
    sensors = {f.key for f in smg.FIELDS} | set(DIRECTIONAL_POWER) | set(ENERGY_SOURCES)
    for lang in ("en", "uk"):
        names = json.loads((base / "translations" / f"{lang}.json").read_text())["entity"]
        assert sensors <= set(names["sensor"]), (lang, sensors - set(names["sensor"]))
        states = set(names["sensor"]["operating_mode"]["state"])
        assert states == set(smg.OPERATING_MODES.values()), lang
    assert json.loads((base / "strings.json").read_text()) == \
        json.loads((base / "translations" / "en.json").read_text())


def test_settings_carry_no_state_class():
    """A setting is not a measurement. With a state class the recorder keeps
    hourly statistics of it in a table that is never purged."""
    settings = [f for f in smg.FIELDS if f.block == "settings"]
    assert settings
    assert all(f.state_class is None for f in settings), [f.key for f in settings if f.state_class]


def test_no_key_repeats_the_default_prefix():
    """Entity ids are `<prefix>_<key>` and the default prefix is `inverter`:
    a key starting `inverter_` reads `sensor.inverter_inverter_...`."""
    from custom_components.eybond_local.const import DEFAULT_PREFIX
    assert not [f.key for f in smg.FIELDS if f.key.startswith(f"{DEFAULT_PREFIX}_")]


def test_every_rename_lands_on_a_field_that_exists():
    keys = {f.key for f in smg.FIELDS}
    for old, new in smg.RENAMED_KEYS.items():
        assert new in keys and old not in keys, (old, new)


def test_no_translation_contains_anything_hassfest_reads_as_html():
    """hassfest refuses `<` and `>` in strings - `<prefix>_<key>` failed 0.2.0."""
    base = pathlib.Path(__file__).parents[1] / "custom_components" / "eybond_local"

    def strings(node):
        if isinstance(node, dict):
            for value in node.values():
                yield from strings(value)
        elif isinstance(node, str):
            yield node

    for path in [base / "strings.json", *sorted((base / "translations").glob("*.json"))]:
        bad = [s for s in strings(json.loads(path.read_text())) if "<" in s or ">" in s]
        assert not bad, (path.name, bad)
