"""Home Assistant MQTT device discovery generation and retained-topic cleanup."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Protocol

from . import __version__
from .airstage import DeviceSnapshot
from .config import DIAGNOSTIC_FIELDS, AppConfig, UnitConfig

_LOGGER = logging.getLogger(__name__)


class MqttPublisher(Protocol):
    async def publish(
        self, topic: str, payload: str | bytes | None = None, *, qos: int = 0, retain: bool = False
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class _DiscoveryRecord:
    device_id: str
    payload: str | None = None


def discovery_topic(config: AppConfig, unit: UnitConfig) -> str:
    return (
        f"{config.homeassistant.discovery_prefix}/device/"
        f"airstage2mqtt_{unit.device_id.lower()}/config"
    )


def _availability(config: AppConfig, unit: UnitConfig) -> list[dict[str, object]]:
    base = config.mqtt.base_topic
    return [
        {
            "topic": f"{config.bridge_topic}/state",
            "payload_available": "online",
            "payload_not_available": "offline",
            "value_template": "{{ 'online' if value == 'online' else 'offline' }}",
        },
        {
            "topic": f"{base}/{unit.name}/availability",
            "payload_available": "online",
            "payload_not_available": "offline",
        },
    ]


def build_discovery_payload(
    config: AppConfig, unit: UnitConfig, snapshot: DeviceSnapshot
) -> dict[str, object]:
    """Build one Home Assistant device-discovery payload for a unit."""
    topic = f"{config.mqtt.base_topic}/{unit.name}"
    identifier = f"airstage2mqtt_{unit.device_id.lower()}"
    formatted_mac = ":".join(
        unit.device_id[index : index + 2] for index in range(0, len(unit.device_id), 2)
    )
    components: dict[str, dict[str, object]] = {}

    climate: dict[str, object] = {
        "platform": "climate",
        "unique_id": f"{identifier}_climate",
        "name": None,
        "modes": ["off", "auto", "cool", "dry", "fan_only", "heat"],
        "mode_state_topic": topic,
        "mode_state_template": "{{ value_json.mode }}",
        "mode_command_topic": f"{topic}/set/mode",
        "temperature_state_topic": topic,
        "temperature_state_template": "{{ value_json.target_temperature }}",
        "temperature_command_topic": f"{topic}/set/target_temperature",
        "current_temperature_topic": topic,
        "current_temperature_template": "{{ value_json.current_temperature }}",
        "min_temp": 16,
        "max_temp": 30,
        "temp_step": 0.5,
        "temperature_unit": "C",
    }
    if "fan_mode" in snapshot.capabilities:
        climate.update(
            {
                "fan_modes": ["auto", "quiet", "low", "medium", "high"],
                "fan_mode_state_topic": topic,
                "fan_mode_state_template": "{{ value_json.fan_mode }}",
                "fan_mode_command_topic": f"{topic}/set/fan_mode",
            }
        )
    if "swing_mode" in snapshot.capabilities:
        swing_modes = [
            "vertical_swing",
            "highest",
            "higher",
            "high",
            "center_high",
            "center_low",
            "low",
            "lower",
            "lowest",
        ]
        climate.update(
            {
                "swing_modes": swing_modes,
                "swing_mode_state_topic": topic,
                "swing_mode_state_template": "{{ value_json.swing_mode }}",
                "swing_mode_command_topic": f"{topic}/set/swing_mode",
            }
        )
    components["climate"] = climate

    sensors = {
        "current_temperature": ("Indoor temperature", "temperature", "°C"),
        "outdoor_temperature": ("Outdoor temperature", "temperature", "°C"),
        "filter_sign_reset": ("Filter sign", None, None),
    }
    for field, (name, device_class, unit_of_measurement) in sensors.items():
        if field not in snapshot.capabilities:
            continue
        component: dict[str, object] = {
            "platform": "sensor",
            "unique_id": f"{identifier}_{field}",
            "name": name,
            "state_topic": topic,
            "value_template": f"{{{{ value_json.{field} }}}}",
        }
        if device_class:
            component["device_class"] = device_class
        if unit_of_measurement:
            component["unit_of_measurement"] = unit_of_measurement
            component["state_class"] = "measurement"
        if field == "filter_sign_reset":
            component["entity_category"] = "diagnostic"
            component["enabled_by_default"] = False
        components[field] = component

    diagnostic_names = {
        "power_consumption": "Power consumption",
        "demand": "Demand",
        "error_code": "Error code",
    }
    for field in sorted(unit.diagnostics):
        components[field] = {
            "platform": "sensor",
            "unique_id": f"{identifier}_{field}",
            "name": diagnostic_names[field],
            "state_topic": topic,
            "value_template": f"{{{{ value_json.{field} }}}}",
            "entity_category": "diagnostic",
        }

    switches = {
        "state": "Power",
        "economy": "Economy",
        "powerful": "Powerful",
        "outdoor_low_noise": "Outdoor low noise",
        "energy_save_fan": "Energy-saving fan",
        "minimum_heat": "Minimum heat",
        "indoor_led": "Indoor LED",
        "human_detection_auto_save": "Human detection auto-save",
    }
    for field, name in switches.items():
        if field not in snapshot.capabilities:
            continue
        components[field] = {
            "platform": "switch",
            "unique_id": f"{identifier}_{field}",
            "name": name,
            "state_topic": topic,
            "value_template": f"{{{{ value_json.{field} }}}}",
            "command_topic": f"{topic}/set/{field}",
            "payload_on": "ON",
            "payload_off": "OFF",
            "state_on": "ON",
            "state_off": "OFF",
        }

    if "human_detection" in snapshot.capabilities:
        components["human_detection"] = {
            "platform": "binary_sensor",
            "unique_id": f"{identifier}_human_detection",
            "name": "Human detection",
            "device_class": "occupancy",
            "state_topic": topic,
            "value_template": "{{ value_json.human_detection }}",
            "payload_on": "ON",
            "payload_off": "OFF",
        }

    return {
        "device": {
            "identifiers": [identifier],
            "connections": [["mac", formatted_mac]],
            "manufacturer": "Fujitsu",
            "model": snapshot.model,
            "name": unit.display_name,
        },
        "origin": {
            "name": "AirStage2MQTT",
            "sw_version": __version__,
            "support_url": "https://github.com/m3zercat/AirStage2MQTT",
        },
        "availability": _availability(config, unit),
        "availability_mode": "all",
        "qos": config.mqtt.qos,
        "components": components,
    }


class DiscoveryManager:
    """Publish discovery and remember retained topics for cleanup across restarts."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self._path = config.data_dir / "homeassistant-discovery.json"
        self._records, self._legacy_topics = self._load()
        self._payloads: dict[str, str] = {}
        self._lock = asyncio.Lock()

    def _load(self) -> tuple[dict[str, _DiscoveryRecord], set[str]]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                topics = data.get("topics")
                if data.get("schema_version") == 2 and isinstance(topics, dict):
                    records: dict[str, _DiscoveryRecord] = {}
                    for topic, value in topics.items():
                        if not isinstance(value, dict) or "device_id" not in value:
                            continue
                        payload = value.get("payload")
                        records[str(topic)] = _DiscoveryRecord(
                            device_id=str(value["device_id"]),
                            payload=str(payload) if payload is not None else None,
                        )
                    return records, set()
                legacy = {
                    str(topic): _DiscoveryRecord(device_id=str(device_id))
                    for topic, device_id in data.items()
                    if isinstance(device_id, str)
                }
                return legacy, set(legacy)
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError):
            _LOGGER.warning("Could not read discovery state %s", self._path, exc_info=True)
        return {}, set()

    def _save(self) -> None:
        try:
            self.config.data_dir.mkdir(parents=True, exist_ok=True)
            temporary = self._path.with_suffix(".tmp")
            document = {
                "schema_version": 2,
                "topics": {
                    topic: {"device_id": record.device_id, "payload": record.payload}
                    for topic, record in self._records.items()
                },
            }
            temporary.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
            temporary.replace(self._path)
        except OSError:
            _LOGGER.warning("Could not persist discovery state %s", self._path, exc_info=True)

    def connection_reset(self) -> None:
        """Force discovery republishing after every MQTT reconnection."""
        self._payloads.clear()

    async def reconcile(self, client: MqttPublisher) -> None:
        """Remove retained records for removed devices or disabled discovery."""
        async with self._lock:
            desired_topics = (
                {discovery_topic(self.config, unit) for unit in self.config.units}
                if self.config.homeassistant.enabled
                else set()
            )
            obsolete = [topic for topic in self._records if topic not in desired_topics]
            for topic in obsolete:
                await client.publish(topic, b"", qos=self.config.mqtt.qos, retain=True)
                self._records.pop(topic, None)
                self._payloads.pop(topic, None)
                self._legacy_topics.discard(topic)
            if obsolete:
                self._save()

    @staticmethod
    def _prepare_payloads(
        previous_payload: str | None,
        current_payload: str,
        unit: UnitConfig,
        *,
        legacy: bool,
    ) -> tuple[str | None, str]:
        """Preserve transient capabilities and explicitly remove disabled diagnostics."""
        current = json.loads(current_payload)
        components = current.get("components", {})
        if not isinstance(components, dict):
            return None, current_payload

        removed_diagnostics: set[str] = set()
        if previous_payload is not None:
            try:
                previous = json.loads(previous_payload)
                previous_components = previous.get("components", {})
            except (json.JSONDecodeError, TypeError, AttributeError):
                previous_components = {}
            if isinstance(previous_components, dict):
                for component_id, component in previous_components.items():
                    if component_id in components:
                        continue
                    if component_id in DIAGNOSTIC_FIELDS and component_id not in unit.diagnostics:
                        removed_diagnostics.add(component_id)
                    elif isinstance(component, dict):
                        components[component_id] = component
        elif legacy:
            removed_diagnostics.update(DIAGNOSTIC_FIELDS - unit.diagnostics)

        final_payload = json.dumps(current, separators=(",", ":"), sort_keys=True)
        if not removed_diagnostics:
            return None, final_payload

        removal = json.loads(final_payload)
        removal_components = removal["components"]
        for component_id in sorted(removed_diagnostics):
            removal_components[component_id] = {"platform": "sensor"}
        removal_payload = json.dumps(removal, separators=(",", ":"), sort_keys=True)
        return removal_payload, final_payload

    async def publish(
        self,
        client: MqttPublisher,
        unit: UnitConfig,
        snapshot: DeviceSnapshot,
        *,
        force: bool = False,
    ) -> None:
        if not self.config.homeassistant.enabled:
            return
        topic = discovery_topic(self.config, unit)
        payload = json.dumps(
            build_discovery_payload(self.config, unit, snapshot),
            separators=(",", ":"),
            sort_keys=True,
        )
        async with self._lock:
            record = self._records.get(topic)
            previous_payload = self._payloads.get(topic)
            if previous_payload is None and record is not None:
                previous_payload = record.payload
            removal_payload, payload = self._prepare_payloads(
                previous_payload,
                payload,
                unit,
                legacy=topic in self._legacy_topics,
            )
            if force or self._payloads.get(topic) != payload:
                if removal_payload is not None:
                    await client.publish(
                        topic, removal_payload, qos=self.config.mqtt.qos, retain=True
                    )
                await client.publish(topic, payload, qos=self.config.mqtt.qos, retain=True)
                self._payloads[topic] = payload
            updated_record = _DiscoveryRecord(unit.device_id, payload)
            needs_save = record != updated_record or topic in self._legacy_topics
            self._records[topic] = updated_record
            self._legacy_topics.discard(topic)
            if needs_save:
                self._save()
