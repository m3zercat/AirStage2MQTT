from __future__ import annotations

import json

import pytest

from airstage2mqtt.config import AppConfig
from airstage2mqtt.discovery import discovery_topic
from airstage2mqtt.manifest import ManifestManager, build_manifest, parse_manifest


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


def test_builds_key_scoped_manifest(app_config: AppConfig) -> None:
    document = build_manifest(app_config)

    assert app_config.manifest_topic == "airstage2mqtt/manifests/testbridge001"
    assert app_config.bridge_topic == "airstage2mqtt/bridges/testbridge001"
    assert document["bridge_key"] == "testbridge001"
    assert document["operational_topics"] == [
        "airstage2mqtt/bridges/testbridge001/info",
        "airstage2mqtt/bridges/testbridge001/state",
        "airstage2mqtt/living_room",
        "airstage2mqtt/living_room/availability",
    ]
    assert document["discovery_topics"] == [discovery_topic(app_config, app_config.units[0])]


@pytest.mark.asyncio
async def test_cleans_only_obsolete_topics_claimed_by_manifest(app_config: AppConfig) -> None:
    current = build_manifest(app_config)
    current_operational = list(current["operational_topics"])
    current_discovery = list(current["discovery_topics"])
    old_discovery = "oldprefix/device/airstage2mqtt_e8fb1c000001/config"
    previous = json.dumps(
        {
            "schema_version": 1,
            "bridge_key": app_config.bridge_key,
            "operational_topics": [
                *current_operational,
                "airstage2mqtt/old_unit",
                "airstage2mqtt/old_unit/availability",
                "airstage2mqtt/manifests/someone_else",
                "unrelated/topic",
            ],
            "discovery_topics": [
                *current_discovery,
                old_discovery,
                "homeassistant/device/someone_else/config",
            ],
        }
    )
    publisher = FakePublisher()

    await ManifestManager(app_config).reconcile(publisher, previous)

    assert {message[0] for message in publisher.messages} == {
        "airstage2mqtt/old_unit",
        "airstage2mqtt/old_unit/availability",
        old_discovery,
    }
    assert all(message[1] == b"" and message[3] is True for message in publisher.messages)


def test_rejects_manifest_owned_by_another_key(app_config: AppConfig) -> None:
    payload = json.dumps(
        {
            "schema_version": 1,
            "bridge_key": "anotherbridge",
            "operational_topics": ["airstage2mqtt/living_room"],
            "discovery_topics": [],
        }
    )

    with pytest.raises(ValueError, match="owner"):
        parse_manifest(app_config, payload)
