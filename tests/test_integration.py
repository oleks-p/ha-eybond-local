"""The integration end to end in Home Assistant, against the fake collector."""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant, State
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache_with_extra_data,
)

from custom_components.eybond_local.const import (
    CONF_ADVERTISE_IP,
    CONF_COLLECTOR_IP,
    CONF_PN,
    CONF_PORT,
    CONF_PREFIX,
    DOMAIN,
)

from .fake_collector import PN

DATA = {CONF_COLLECTOR_IP: "127.0.0.1", CONF_ADVERTISE_IP: "127.0.0.1", CONF_PORT: 0,
        CONF_PREFIX: "inverter", CONF_PN: PN}


async def wait_for_state(hass: HomeAssistant, entity_id: str, expected: str, timeout=5.0):
    async with asyncio.timeout(timeout):
        while (state := hass.states.get(entity_id)) is None or state.state != expected:
            await asyncio.sleep(0.05)
    return state


@pytest.fixture
async def entry(hass: HomeAssistant, collector):
    entry = MockConfigEntry(domain=DOMAIN, data=DATA, unique_id=PN)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    yield entry
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_the_unit_comes_up_with_english_ids(hass: HomeAssistant, entry):
    await wait_for_state(hass, "binary_sensor.inverter_collector_connected", "on")
    assert hass.states.get("sensor.inverter_battery_power").state == "-468"
    assert hass.states.get("sensor.inverter_battery_soc").state == "80"
    assert hass.states.get("sensor.inverter_battery_voltage").state == "52.0"
    assert hass.states.get("sensor.inverter_operating_mode").state == "off_grid"
    assert hass.states.get("sensor.inverter_battery_discharge_power").state == "468"
    assert hass.states.get("binary_sensor.inverter_grid_available").state == "off"
    attrs = hass.states.get("sensor.inverter_battery_power").attributes
    assert (attrs["register"], attrs["confidence"]) == (217, "confirmed")


async def test_inferred_and_secondary_fields_are_registered_but_off(hass: HomeAssistant, entry):
    await wait_for_state(hass, "binary_sensor.inverter_collector_connected", "on")
    registry = er.async_get(hass)
    for entity_id in ("sensor.inverter_bms_soc", "sensor.inverter_bridge_voltage",
                      "binary_sensor.inverter_warning"):
        row = registry.async_get(entity_id)
        assert row is not None and row.disabled_by is er.RegistryEntryDisabler.INTEGRATION, entity_id
        assert hass.states.get(entity_id) is None


async def test_the_device_carries_what_the_inverter_reported(hass: HomeAssistant, entry):
    await wait_for_state(hass, "binary_sensor.inverter_collector_connected", "on")
    for _ in range(100):
        device = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, PN), entry.entry_id)
        if device and device.model and "5000" in device.model:
            break
        await asyncio.sleep(0.05)
    assert device.model == "SMG-II protocol 11, 5000 W"
    assert device.hw_version == "0x5001"


async def test_losing_the_collector_marks_live_values_unavailable(hass, entry, collector):
    await wait_for_state(hass, "binary_sensor.inverter_collector_connected", "on")
    collector.redial_s = None
    collector.late_reads.clear()
    await collector.hang_up()
    await wait_for_state(hass, "binary_sensor.inverter_collector_connected", "off")
    assert hass.states.get("sensor.inverter_battery_power").state == "unavailable"


async def test_energy_counters_restore_and_keep_counting(hass: HomeAssistant, collector):
    mock_restore_cache_with_extra_data(hass, [(
        State("sensor.inverter_battery_discharge_energy", "12.5"),
        {"native_value": 12.5, "native_unit_of_measurement": "kWh"},
    )])
    entry = MockConfigEntry(domain=DOMAIN, data=DATA, unique_id=PN)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await wait_for_state(hass, "binary_sensor.inverter_collector_connected", "on")
    await asyncio.sleep(3.5)  # a second live read, so there is an interval to integrate
    value = float(hass.states.get("sensor.inverter_battery_discharge_energy").state)
    assert value >= 12.5
    attrs = hass.states.get("sensor.inverter_battery_discharge_energy").attributes
    assert attrs["state_class"] == "total_increasing"
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_unload_frees_the_port(hass: HomeAssistant, entry, collector):
    port = entry.runtime_data.hub.listen_port
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    await collector.stop()  # or it redials straight into the listener this test opens
    server = await asyncio.start_server(lambda _r, _w: None, "0.0.0.0", port)
    server.close()
    await server.wait_closed()


async def test_the_flow_creates_an_entry_once_the_collector_has_answered(hass, collector):
    with patch("homeassistant.components.network.async_get_source_ip", return_value="127.0.0.1"):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {
        CONF_COLLECTOR_IP: "127.0.0.1", CONF_ADVERTISE_IP: "127.0.0.1", CONF_PORT: 0,
        CONF_PREFIX: "inverter"})
    assert result["type"] is FlowResultType.CREATE_ENTRY, result.get("errors")
    assert result["data"][CONF_PN] == PN
    assert result["result"].unique_id == PN
    await wait_for_state(hass, "binary_sensor.inverter_collector_connected", "on")
    # The entry adopted the session the flow opened. Closing it in between made
    # the collector reconnect twice, the second time into a closing socket.
    assert collector.connections == 1
    await hass.config_entries.async_unload(result["result"].entry_id)
    await hass.async_block_till_done()


async def test_the_flow_says_when_the_port_is_taken(hass, collector):
    """Another integration holding the collector port is the usual cause; the user has to be told."""
    squatter = await asyncio.start_server(lambda _r, _w: None, "0.0.0.0", 0)
    port = squatter.sockets[0].getsockname()[1]
    try:
        with patch("homeassistant.components.network.async_get_source_ip", return_value="127.0.0.1"):
            result = await hass.config_entries.flow.async_init(
                DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {
            CONF_COLLECTOR_IP: "127.0.0.1", CONF_ADVERTISE_IP: "127.0.0.1", CONF_PORT: port,
            CONF_PREFIX: "inverter"})
        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {"base": "port_in_use"}
    finally:
        squatter.close()
        await squatter.wait_closed()


async def test_the_flow_says_when_no_collector_answers(hass, collector, monkeypatch):
    monkeypatch.setattr("custom_components.eybond_local.const.PROBE_TIMEOUT_S", 0.5)
    collector.announces.clear()
    with patch("homeassistant.components.network.async_get_source_ip", return_value="127.0.0.1"):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {
        CONF_COLLECTOR_IP: "127.0.0.2", CONF_ADVERTISE_IP: "127.0.0.1", CONF_PORT: 0,
        CONF_PREFIX: "inverter"})
    assert result["errors"] == {"base": "no_collector"}


async def test_renamed_keys_move_their_registry_rows_and_keep_custom_ids(hass, collector):
    """A rename must not orphan the old entity and start a new one beside it."""
    entry = MockConfigEntry(domain=DOMAIN, data=DATA, unique_id=PN)
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    old = registry.async_get_or_create("sensor", DOMAIN, f"{PN}_inverter_temperature",
                                       config_entry=entry,
                                       suggested_object_id="inverter_inverter_temperature")
    custom = registry.async_get_or_create("sensor", DOMAIN, f"{PN}_inverter_voltage",
                                          config_entry=entry, suggested_object_id="my_bridge_volts")
    assert old.entity_id == "sensor.inverter_inverter_temperature"
    assert custom.entity_id == "sensor.my_bridge_volts"
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    moved = registry.async_get_entity_id("sensor", DOMAIN, f"{PN}_bridge_temperature")
    assert moved == "sensor.inverter_bridge_temperature"
    assert registry.async_get(moved).id == old.id, "the same row, so its history follows"
    assert registry.async_get_entity_id("sensor", DOMAIN, f"{PN}_inverter_temperature") is None
    kept = registry.async_get_entity_id("sensor", DOMAIN, f"{PN}_bridge_voltage")
    assert kept == "sensor.my_bridge_volts", "a user's own entity id is left alone"
    await wait_for_state(hass, "sensor.inverter_bridge_temperature", "33")
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_code_sensors_list_their_bits(hass: HomeAssistant, entry):
    await wait_for_state(hass, "binary_sensor.inverter_collector_connected", "on")
    registry = er.async_get(hass)
    registry.async_update_entity("sensor.inverter_warning_code", disabled_by=None)
    state = await wait_for_state(hass, "sensor.inverter_warning_code", "5")
    assert state.attributes["bits"] == [0, 2]


@pytest.mark.parametrize(("prefix", "error"), [("inverter", "prefix_in_use"), ("Inverter-2", "invalid_prefix")])
async def test_the_flow_refuses_a_prefix_that_would_clash(hass, collector, prefix, error):
    """Three units are three entries; a shared prefix would give one of them `_2` ids."""
    MockConfigEntry(domain=DOMAIN, data=DATA, unique_id="I2000OTHER").add_to_hass(hass)
    with patch("homeassistant.components.network.async_get_source_ip", return_value="127.0.0.1"):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {
        CONF_COLLECTOR_IP: "127.0.0.1", CONF_ADVERTISE_IP: "127.0.0.1", CONF_PORT: 0,
        CONF_PREFIX: prefix})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_PREFIX: error}
    assert collector.announces == [], "refused before anything was sent to the collector"
