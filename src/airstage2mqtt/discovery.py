"""Home Assistant MQTT device discovery generation and retained-topic cleanup."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Protocol

from . import __version__
from .airstage import DeviceSnapshot
from .config import AppConfig, UnitConfig

_LOGGER = logging.getLogger(__name__)


class MqttPublisher(Protocol):
    async def publish(
        self, topic: str, payload: str | bytes | None = None, *, qos: int = 0, retain: bool = False
    ) -> object: ...


def discovery_topic(config: AppConfig, unit: UnitConfig) -> str:
    return (
        f"{config.homeassistant.discovery_prefix}/device/"
        f"airstage2mqtt_{unit.device_id.lower()}/config"
    )


def _availability(config: AppConfig, unit: UnitConfig) -> list[dict[str, object]]:
    base = config.mqtt.base_topic
    return [
        {
            "topic": f"{base}/bridge/state",
            "payload_available": "online",
            "payload_not_available": "offline",
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
        "power_consumption": ("Power consumption", None, None),
        "demand": ("Demand", None, None),
        "error_code": ("Error code", None, None),
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
        if field in {"power_consumption", "demand", "error_code", "filter_sign_reset"}:
            component["entity_category"] = "diagnostic"
            component["enabled_by_default"] = False
        components[field] = component

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
            "name": unit.name.replace("_", " ").replace("-", " ").title(),
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
        self._topics: dict[str, str] = self._load()
        self._payloads: dict[str, str] = {}
        self._lock = asyncio.Lock()

    def _load(self) -> dict[str, str]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {str(key): str(value) for key, value in data.items()}
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError):
            _LOGGER.warning("Could not read discovery state %s", self._path, exc_info=True)
        return {}

    def _save(self) -> None:
        try:
            self.config.data_dir.mkdir(parents=True, exist_ok=True)
            temporary = self._path.with_suffix(".tmp")
            temporary.write_text(json.dumps(self._topics, sort_keys=True), encoding="utf-8")
            temporary.replace(self._path)
        except OSError:
            _LOGGER.warning("Could not persist discovery state %s", self._path, exc_info=True)

    async def reconcile(self, client: MqttPublisher) -> None:
        """Remove retained records for removed devices or disabled discovery."""
        async with self._lock:
            desired_topics = (
                {discovery_topic(self.config, unit) for unit in self.config.units}
                if self.config.homeassistant.enabled
                else set()
            )
            obsolete = [topic for topic in self._topics if topic not in desired_topics]
            for topic in obsolete:
                await client.publish(topic, b"", qos=self.config.mqtt.qos, retain=True)
                self._topics.pop(topic, None)
                self._payloads.pop(topic, None)
            self._save()

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
            if force or self._payloads.get(topic) != payload:
                await client.publish(topic, payload, qos=self.config.mqtt.qos, retain=True)
                self._payloads[topic] = payload
            self._topics[topic] = unit.device_id
            self._save()
