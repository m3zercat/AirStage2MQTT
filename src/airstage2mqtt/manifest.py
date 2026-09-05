"""Retained MQTT ownership manifest and stale-topic reconciliation."""

from __future__ import annotations

import json
import logging
import re
from typing import Protocol

from . import __version__
from .config import AppConfig
from .discovery import discovery_topic

_LOGGER = logging.getLogger(__name__)
_DISCOVERY_TOPIC_RE = re.compile(r"^.+/device/airstage2mqtt_[0-9a-f]{12}/config$", re.IGNORECASE)


class MqttPublisher(Protocol):
    async def publish(
        self, topic: str, payload: str | bytes | None = None, *, qos: int = 0, retain: bool = False
    ) -> object: ...


def desired_operational_topics(config: AppConfig) -> set[str]:
    """Return retained operational topics owned by this bridge instance."""
    topics = {
        f"{config.bridge_topic}/state",
        f"{config.bridge_topic}/info",
    }
    for unit in config.units:
        root = f"{config.mqtt.base_topic}/{unit.name}"
        topics.update({root, f"{root}/availability"})
    return topics


def desired_discovery_topics(config: AppConfig) -> set[str]:
    if not config.homeassistant.enabled:
        return set()
    return {discovery_topic(config, unit) for unit in config.units}


def build_manifest(config: AppConfig) -> dict[str, object]:
    """Build the stable retained ownership document for this instance."""
    return {
        "schema_version": 1,
        "bridge_key": config.bridge_key,
        "base_topic": config.mqtt.base_topic,
        "version": __version__,
        "operational_topics": sorted(desired_operational_topics(config)),
        "discovery_topics": sorted(desired_discovery_topics(config)),
    }


def _is_owned_operational_topic(config: AppConfig, topic: str) -> bool:
    prefix = f"{config.mqtt.base_topic}/"
    if not topic.startswith(prefix):
        return False
    parts = topic[len(prefix) :].split("/")
    if len(parts) == 1:
        return bool(parts[0]) and parts[0] not in {"bridge", "bridges", "manifests"}
    if len(parts) == 2:
        return parts[1] == "availability" and bool(parts[0])
    return parts == ["bridges", config.bridge_key, "state"] or parts == [
        "bridges",
        config.bridge_key,
        "info",
    ]


def parse_manifest(config: AppConfig, payload: bytes | str | None) -> tuple[set[str], set[str]]:
    """Parse and constrain a prior manifest so it cannot clear unrelated topics."""
    if payload is None or payload == b"" or payload == "":
        return set(), set()
    try:
        document = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
        raise ValueError("the retained bridge manifest is not valid JSON") from exc
    if not isinstance(document, dict):
        raise ValueError("the retained bridge manifest must be a JSON object")
    if document.get("schema_version") != 1 or document.get("bridge_key") != config.bridge_key:
        raise ValueError("the retained bridge manifest has an incompatible owner or schema")

    operational_raw = document.get("operational_topics", [])
    discovery_raw = document.get("discovery_topics", [])
    if not isinstance(operational_raw, list) or not isinstance(discovery_raw, list):
        raise ValueError("the retained bridge manifest topic fields must be lists")

    operational = {
        topic
        for value in operational_raw
        if isinstance(value, str)
        and (topic := value.strip())
        and _is_owned_operational_topic(config, topic)
    }
    discovery = {
        topic
        for value in discovery_raw
        if isinstance(value, str)
        and (topic := value.strip())
        and _DISCOVERY_TOPIC_RE.fullmatch(topic)
    }
    return operational, discovery


class ManifestManager:
    """Clean stale retained topics and publish the current ownership manifest."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config

    async def reconcile(self, client: MqttPublisher, previous_payload: bytes | str | None) -> None:
        try:
            previous_operational, previous_discovery = parse_manifest(self.config, previous_payload)
        except ValueError as exc:
            _LOGGER.warning(
                "Ignoring unusable retained manifest %s: %s", self.config.manifest_topic, exc
            )
            return

        obsolete = (previous_operational - desired_operational_topics(self.config)) | (
            previous_discovery - desired_discovery_topics(self.config)
        )
        for topic in sorted(obsolete):
            _LOGGER.info(
                "Clearing stale retained topic owned by %s: %s", self.config.bridge_key, topic
            )
            await client.publish(topic, b"", qos=self.config.mqtt.qos, retain=True)

    async def publish(self, client: MqttPublisher) -> None:
        payload = json.dumps(build_manifest(self.config), separators=(",", ":"), sort_keys=True)
        await client.publish(
            self.config.manifest_topic,
            payload,
            qos=self.config.mqtt.qos,
            retain=True,
        )
