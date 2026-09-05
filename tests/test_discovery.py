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


def test_uses_configured_friendly_name(app_config: AppConfig, unit_config: UnitConfig) -> None:
    named_unit = UnitConfig(
        name=unit_config.name,
        mac=unit_config.mac,
        ip=unit_config.ip,
        friendly_name="Downstairs Air Conditioner",
    )

    payload = build_discovery_payload(app_config, named_unit, snapshot())

    assert payload["device"]["name"] == "Downstairs Air Conditioner"  # type: ignore[index]


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
    assert stored == {discovery_topic(app_config, unit_config): unit_config.device_id}

    removed_config = AppConfig(
        mqtt=app_config.mqtt,
        polling=app_config.polling,
        homeassistant=app_config.homeassistant,
        units=(UnitConfig("other", "E8FB1C000001", "192.168.1.41"),),
        data_dir=tmp_path,
    )
    removed_manager = DiscoveryManager(removed_config)
    await removed_manager.reconcile(publisher)
    assert publisher.messages[-1][0] == discovery_topic(app_config, unit_config)
    assert publisher.messages[-1][1] == b""
