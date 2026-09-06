from __future__ import annotations

from typing import Any

import pytest
from pyairstage.airstageAC import AirstageAC

from airstage2mqtt.airstage import CommandError, DeviceSnapshot, PyairstageLocalUnit
from airstage2mqtt.config import UnitConfig

DEVICE_ID = "E8FB1C000000"


def parameter(name: str, value: object) -> dict[str, object]:
    return {"name": name, "value": str(value), "modifiedAt": ""}


def test_normalizes_pyairstage_snapshot() -> None:
    device: dict[str, Any] = {
        "deviceId": DEVICE_ID,
        "deviceName": "Living room",
        "model": "ASYG",
        "parameters": [
            parameter("iu_onoff", 1),
            parameter("iu_op_mode", 1),
            parameter("iu_fan_spd", 8),
            parameter("iu_set_tmp", 220),
            parameter("iu_indoor_tmp", 7150),
            parameter("iu_outdoor_tmp", 6250),
            parameter("iu_af_inc_vrt", 4),
            parameter("iu_af_dir_vrt", 2),
            parameter("iu_af_swg_vrt", 0),
            parameter("iu_economy", 1),
            parameter("iu_powerful", 0),
            parameter("ou_low_noise", 65535),
            parameter("iu_fan_ctrl", 1),
            parameter("iu_min_heat", 0),
            parameter("iu_wifi_led", 1),
            parameter("iu_hmn_det", 0),
            parameter("iu_hmn_det_auto_save", 1),
            parameter("iu_pow_cons", 123),
            parameter("iu_err_code", 0),
            parameter("iu_demand", 50),
            parameter("iu_fltr_sign_reset", 0),
        ],
    }
    ac = AirstageAC(DEVICE_ID, object()).refresh_parameters(data=device)  # type: ignore[arg-type]
    adapter = object.__new__(PyairstageLocalUnit)
    adapter.config = UnitConfig(
        "living_room",
        DEVICE_ID,
        "192.168.1.40",
        diagnostics=frozenset({"power_consumption", "error_code", "demand"}),
    )

    snapshot = adapter._snapshot(ac, device)

    assert snapshot.model == "ASYG"
    assert snapshot.state["state"] == "ON"
    assert snapshot.state["mode"] == "cool"
    assert snapshot.state["fan_mode"] == "medium"
    assert snapshot.state["target_temperature"] == 22.0
    assert snapshot.state["current_temperature"] == 21.5
    assert snapshot.state["outdoor_temperature"] == 12.5
    assert snapshot.state["swing_mode"] == "high"
    assert snapshot.state["economy"] == "ON"
    assert snapshot.state["power_consumption"] == 123
    assert "outdoor_low_noise" not in snapshot.state


def test_omits_diagnostics_unless_enabled_and_ignores_blank_values() -> None:
    device: dict[str, Any] = {
        "deviceId": DEVICE_ID,
        "model": "ASYG",
        "parameters": [
            parameter("iu_onoff", 1),
            parameter("iu_op_mode", 1),
            parameter("iu_fan_spd", 8),
            parameter("iu_err_code", 0),
            parameter("iu_demand", 65535),
            parameter("iu_pow_cons", ""),
        ],
    }
    ac = AirstageAC(DEVICE_ID, object()).refresh_parameters(data=device)  # type: ignore[arg-type]

    disabled = object.__new__(PyairstageLocalUnit)
    disabled.config = UnitConfig("living_room", DEVICE_ID, "192.168.1.40")
    disabled_snapshot = disabled._snapshot(ac, device)
    assert not {"error_code", "demand", "power_consumption"} & set(disabled_snapshot.state)

    enabled = object.__new__(PyairstageLocalUnit)
    enabled.config = UnitConfig(
        "living_room",
        DEVICE_ID,
        "192.168.1.40",
        diagnostics=frozenset({"error_code", "demand", "power_consumption"}),
    )
    enabled_snapshot = enabled._snapshot(ac, device)

    assert enabled_snapshot.state["error_code"] == 0
    assert "demand" not in enabled_snapshot.state
    assert "power_consumption" not in enabled_snapshot.state
    assert {"error_code", "demand", "power_consumption"} <= enabled_snapshot.capabilities


def test_capabilities_do_not_shrink_when_a_later_response_omits_a_field() -> None:
    common = [
        parameter("iu_onoff", 1),
        parameter("iu_op_mode", 1),
        parameter("iu_fan_spd", 8),
    ]
    complete: dict[str, Any] = {
        "deviceId": DEVICE_ID,
        "model": "ASYG",
        "parameters": [*common, parameter("iu_economy", 1)],
    }
    partial: dict[str, Any] = {
        "deviceId": DEVICE_ID,
        "model": "ASYG",
        "parameters": common,
    }
    adapter = object.__new__(PyairstageLocalUnit)
    adapter.config = UnitConfig("living_room", DEVICE_ID, "192.168.1.40")

    complete_ac = AirstageAC(DEVICE_ID, object()).refresh_parameters(data=complete)  # type: ignore[arg-type]
    first = adapter._snapshot(complete_ac, complete)
    partial_ac = AirstageAC(DEVICE_ID, object()).refresh_parameters(data=partial)  # type: ignore[arg-type]
    second = adapter._snapshot(partial_ac, partial)

    assert "economy" in first.capabilities
    assert "economy" not in second.state
    assert "economy" in second.capabilities


class FakeAc:
    def __init__(self) -> None:
        self.on = False
        self.calls: list[tuple[str, object | None]] = []

    def get_device_on_off_state(self) -> str:
        return "ON" if self.on else "OFF"

    async def turn_on(self) -> None:
        self.on = True
        self.calls.append(("turn_on", None))

    async def turn_off(self) -> None:
        self.on = False
        self.calls.append(("turn_off", None))

    async def set_operation_mode(self, value: object) -> None:
        self.calls.append(("mode", value))

    async def set_target_temperature(self, value: object) -> None:
        self.calls.append(("temperature", value))

    async def set_fan_speed(self, value: object) -> None:
        self.calls.append(("fan", value))

    async def set_vertical_swing(self, value: object) -> None:
        self.calls.append(("swing", value))

    async def set_vertical_direction(self, value: object) -> None:
        self.calls.append(("direction", value))

    def __getattr__(self, name: str) -> object:
        if name.startswith("set_"):

            async def setter(value: object) -> None:
                self.calls.append((name, value))

            return setter
        raise AttributeError(name)


@pytest.mark.asyncio
async def test_applies_multi_property_command_in_safe_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = UnitConfig(
        name="living_room",
        mac=DEVICE_ID,
        ip="192.168.1.40",
        turn_on_before_set_temperature=True,
    )
    adapter = object.__new__(PyairstageLocalUnit)
    adapter.config = config
    ac = FakeAc()

    async def current_ac() -> FakeAc:
        return ac

    monkeypatch.setattr(adapter, "_current_ac", current_ac)
    snapshot = DeviceSnapshot(
        state={"state": "OFF", "mode": "cool", "target_temperature": 20, "economy": "OFF"},
        capabilities=frozenset({"state", "mode", "target_temperature", "economy"}),
        model="test",
    )

    accepted = adapter.accept(
        {"state": "ON", "mode": "heat", "target_temperature": 21.5, "economy": "ON"},
        snapshot,
    )
    await adapter.apply(accepted)

    assert [name for name, _ in ac.calls] == [
        "turn_on",
        "mode",
        "temperature",
        "set_economy_mode",
    ]
    assert accepted.snapshot.state == {
        "state": "ON",
        "mode": "heat",
        "target_temperature": 21.5,
        "economy": "ON",
    }


@pytest.mark.asyncio
async def test_rejects_read_only_and_unsupported_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = object.__new__(PyairstageLocalUnit)
    adapter.config = UnitConfig("living_room", DEVICE_ID, "192.168.1.40")
    snapshot = DeviceSnapshot(
        state={"state": "ON", "mode": "cool"},
        capabilities=frozenset({"state", "mode"}),
        model="test",
    )
    with pytest.raises(CommandError, match="read-only"):
        adapter.accept({"current_temperature": 99}, snapshot)
    with pytest.raises(CommandError, match="unsupported"):
        adapter.accept({"economy": "ON"}, snapshot)


def test_models_canonical_command_values_and_power_side_effects() -> None:
    adapter = object.__new__(PyairstageLocalUnit)
    adapter.config = UnitConfig("living_room", DEVICE_ID, "192.168.1.40")
    snapshot = DeviceSnapshot(
        state={
            "state": "ON",
            "mode": "cool",
            "fan_mode": "medium",
            "target_temperature": 20.0,
            "economy": "OFF",
            "current_temperature": 21.0,
        },
        capabilities=frozenset({"state", "mode", "fan_mode", "target_temperature", "economy"}),
        model="test",
    )

    accepted = adapter.accept(
        {
            "state": "toggle",
            "mode": "fan",
            "fan_mode": "High",
            "economy": True,
        },
        snapshot,
    )

    assert accepted.values == {
        "state": "OFF",
        "mode": "fan_only",
        "fan_mode": "high",
        "economy": "ON",
    }
    assert accepted.snapshot.state == {
        "state": "OFF",
        "mode": "off",
        "fan_mode": "high",
        "target_temperature": 20.0,
        "economy": "ON",
        "current_temperature": 21.0,
    }
    assert accepted.snapshot.capabilities == snapshot.capabilities
    assert accepted.snapshot.model == snapshot.model


def test_validates_temperature_before_publishing_optimistic_state() -> None:
    adapter = object.__new__(PyairstageLocalUnit)
    adapter.config = UnitConfig("living_room", DEVICE_ID, "192.168.1.40")
    snapshot = DeviceSnapshot(
        state={"state": "ON", "mode": "cool", "target_temperature": 20.0},
        capabilities=frozenset({"state", "mode", "target_temperature"}),
        model="test",
    )

    with pytest.raises(CommandError, match="between 18.0 and 30.0"):
        adapter.accept({"mode": "off", "target_temperature": 16}, snapshot)
    with pytest.raises(CommandError, match="fan_only"):
        adapter.accept({"mode": "fan_only", "target_temperature": 20}, snapshot)

    accepted = adapter.accept({"mode": "heat", "target_temperature": 17.9}, snapshot)
    assert accepted.values["target_temperature"] == 18.0
    assert accepted.snapshot.state["target_temperature"] == 18.0
