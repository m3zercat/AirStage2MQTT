from __future__ import annotations

import json
from pathlib import Path

import pytest

from airstage2mqtt.airstage import DeviceSnapshot
from airstage2mqtt.config import AppConfig, UnitConfig
from airstage2mqtt.discovery import DiscoveryManager, build_discovery_payload, discovery_topic


class FakePublisher:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str | bytes | None, int, bool]] = []

    async def publish(
        self,
        topic: str,
        payload: str | bytes | None = None,
        *,
        qos: int = 0,
        retain: bool = False,
    ) -> None:
        self.messages.append((topic, payload, qos, retain))


def snapshot() -> DeviceSnapshot:
    state: dict[str, object] = {
        "state": "ON",
        "mode": "cool",
        "target_temperature": 22.0,
        "current_temperature": 21.5,
        "fan_mode": "auto",
        "economy": "OFF",
        "human_detection": "ON",
        "model": "ASYG",
    }
    return DeviceSnapshot(state, frozenset(state), "ASYG")


def test_builds_device_discovery_with_capability_components(
    app_config: AppConfig, unit_config: UnitConfig
) -> None:
    payload = build_discovery_payload(app_config, unit_config, snapshot())

    assert payload["device"]["identifiers"] == ["airstage2mqtt_e8fb1c000000"]  # type: ignore[index]
    assert payload["device"]["name"] == "Living Room"  # type: ignore[index]
    components = payload["components"]  # type: ignore[assignment]
    assert set(components) >= {"climate", "state", "current_temperature", "economy"}
    assert "outdoor_temperature" not in components
    assert components["climate"]["mode_command_topic"].endswith("/set/mode")
    assert payload["availability"][0]["topic"] == (  # type: ignore[index]
        "airstage2mqtt/bridges/testbridge001/state"
    )


def test_uses_configured_friendly_name(app_config: AppConfig, unit_config: UnitConfig) -> None:
    named_unit = UnitConfig(
        name=unit_config.name,
        mac=unit_config.mac,
        ip=unit_config.ip,
        friendly_name="Downstairs Air Conditioner",
    )

    payload = build_discovery_payload(app_config, named_unit, snapshot())

    assert payload["device"]["name"] == "Downstairs Air Conditioner"  # type: ignore[index]


def test_diagnostic_discovery_is_controlled_only_by_unit_config(
    app_config: AppConfig, unit_config: UnitConfig
) -> None:
    diagnostic_snapshot = DeviceSnapshot(
        {"state": "ON", "error_code": 0, "demand": 50, "power_consumption": 123},
        frozenset({"state", "error_code", "demand", "power_consumption"}),
        "ASYG",
    )

    disabled = build_discovery_payload(app_config, unit_config, diagnostic_snapshot)
    disabled_components = disabled["components"]  # type: ignore[assignment]
    assert not {"error_code", "demand", "power_consumption"} & set(disabled_components)

    enabled_unit = UnitConfig(
        name=unit_config.name,
        mac=unit_config.mac,
        ip=unit_config.ip,
        diagnostics=frozenset({"error_code", "demand"}),
    )
    missing_values = DeviceSnapshot({"state": "ON"}, frozenset({"state"}), "ASYG")
    enabled = build_discovery_payload(app_config, enabled_unit, missing_values)
    enabled_components = enabled["components"]  # type: ignore[assignment]

    assert {"error_code", "demand"} <= set(enabled_components)
    assert "power_consumption" not in enabled_components
    assert "enabled_by_default" not in enabled_components["error_code"]


def test_bridge_initializing_is_treated_as_unavailable(
    app_config: AppConfig, unit_config: UnitConfig
) -> None:
    payload = build_discovery_payload(app_config, unit_config, snapshot())

    bridge_availability = payload["availability"][0]  # type: ignore[index]
    assert bridge_availability["value_template"] == (
        "{{ 'online' if value == 'online' else 'offline' }}"
    )


@pytest.mark.asyncio
async def test_publishes_once_and_cleans_removed_discovery(
    app_config: AppConfig, unit_config: UnitConfig, tmp_path: Path
) -> None:
    publisher = FakePublisher()
    manager = DiscoveryManager(app_config)

    await manager.publish(publisher, unit_config, snapshot())
    await manager.publish(publisher, unit_config, snapshot())
    assert len(publisher.messages) == 1
    assert publisher.messages[0][0] == discovery_topic(app_config, unit_config)
    assert publisher.messages[0][3] is True

    state_path = tmp_path / "homeassistant-discovery.json"
    stored = json.loads(state_path.read_text(encoding="utf-8"))
    stored_record = stored["topics"][discovery_topic(app_config, unit_config)]
    assert stored["schema_version"] == 2
    assert stored_record["device_id"] == unit_config.device_id
    assert json.loads(stored_record["payload"])["device"]["name"] == "Living Room"

    manager.connection_reset()
    await manager.publish(publisher, unit_config, snapshot())
    assert len(publisher.messages) == 2

    removed_config = AppConfig(
        mqtt=app_config.mqtt,
        polling=app_config.polling,
        homeassistant=app_config.homeassistant,
        bridge_key=app_config.bridge_key,
        units=(UnitConfig("other", "E8FB1C000001", "192.168.1.41"),),
        data_dir=tmp_path,
    )
    removed_manager = DiscoveryManager(removed_config)
    await removed_manager.reconcile(publisher)
    assert publisher.messages[-1][0] == discovery_topic(app_config, unit_config)
    assert publisher.messages[-1][1] == b""


@pytest.mark.asyncio
async def test_explicitly_removes_disabled_diagnostic_across_restart(
    app_config: AppConfig, unit_config: UnitConfig
) -> None:
    enabled_unit = UnitConfig(
        name=unit_config.name,
        mac=unit_config.mac,
        ip=unit_config.ip,
        diagnostics=frozenset({"error_code"}),
    )
    enabled_config = AppConfig(
        mqtt=app_config.mqtt,
        polling=app_config.polling,
        homeassistant=app_config.homeassistant,
        bridge_key=app_config.bridge_key,
        units=(enabled_unit,),
        data_dir=app_config.data_dir,
    )
    publisher = FakePublisher()
    await DiscoveryManager(enabled_config).publish(publisher, enabled_unit, snapshot())

    disabled_manager = DiscoveryManager(app_config)
    await disabled_manager.publish(publisher, unit_config, snapshot())

    removal = json.loads(publisher.messages[-2][1])
    final = json.loads(publisher.messages[-1][1])
    assert removal["components"]["error_code"] == {"platform": "sensor"}
    assert "error_code" not in final["components"]


@pytest.mark.asyncio
async def test_migrates_legacy_index_and_removes_old_default_diagnostics(
    app_config: AppConfig, unit_config: UnitConfig, tmp_path: Path
) -> None:
    topic = discovery_topic(app_config, unit_config)
    (tmp_path / "homeassistant-discovery.json").write_text(
        json.dumps({topic: unit_config.device_id}), encoding="utf-8"
    )
    publisher = FakePublisher()

    await DiscoveryManager(app_config).publish(publisher, unit_config, snapshot())

    removal = json.loads(publisher.messages[-2][1])
    assert {
        "demand": {"platform": "sensor"},
        "error_code": {"platform": "sensor"},
        "power_consumption": {"platform": "sensor"},
    }.items() <= removal["components"].items()
