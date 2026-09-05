"""Configuration loading and validation."""

from __future__ import annotations

import ipaddress
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = Path("/config/config.yaml")
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_BRIDGE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_MAC_RE = re.compile(r"^[0-9A-F]{12}$")
_RESERVED_UNIT_NAMES = {"bridge", "bridges", "manifests"}


class ConfigurationError(ValueError):
    """Raised when configuration is missing or invalid."""


@dataclass(frozen=True, slots=True)
class MqttConfig:
    host: str
    port: int = 1883
    username: str | None = None
    password: str | None = None
    tls: bool = False
    tls_ca_file: str | None = None
    tls_insecure: bool = False
    client_id: str = "airstage2mqtt"
    base_topic: str = "airstage2mqtt"
    qos: int = 1


@dataclass(frozen=True, slots=True)
class PollingConfig:
    interval_seconds: float = 10
    timeout_seconds: int = 20
    retries: int = 5
    offline_after_failures: int = 2
    reconnect_min_seconds: float = 1
    reconnect_max_seconds: float = 30


@dataclass(frozen=True, slots=True)
class HomeAssistantConfig:
    enabled: bool = False
    discovery_prefix: str = "homeassistant"


@dataclass(frozen=True, slots=True)
class UnitConfig:
    name: str
    mac: str
    ip: str
    use_https: bool = False
    turn_on_before_set_temperature: bool = False
    friendly_name: str | None = None

    @property
    def device_id(self) -> str:
        """Return the Fujitsu device ID (the MAC without separators)."""
        return self.mac

    @property
    def display_name(self) -> str:
        """Return the configured display name or derive one from the topic-safe name."""
        return self.friendly_name or self.name.replace("_", " ").replace("-", " ").title()


@dataclass(frozen=True, slots=True)
class AppConfig:
    mqtt: MqttConfig
    polling: PollingConfig
    homeassistant: HomeAssistantConfig
    bridge_key: str
    units: tuple[UnitConfig, ...]
    data_dir: Path = Path("/data")
    log_level: str = "INFO"

    @property
    def bridge_topic(self) -> str:
        return f"{self.mqtt.base_topic}/bridges/{self.bridge_key}"

    @property
    def manifest_topic(self) -> str:
        return f"{self.mqtt.base_topic}/manifests/{self.bridge_key}"


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigurationError(f"{name} must be a mapping")
    return value


def _boolean(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    raise ConfigurationError(f"{name} must be true or false")


def _integer(value: Any, name: str, minimum: int, maximum: int | None = None) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if parsed < minimum or (maximum is not None and parsed > maximum):
        suffix = f" and no greater than {maximum}" if maximum is not None else ""
        raise ConfigurationError(f"{name} must be at least {minimum}{suffix}")
    return parsed


def _number(value: Any, name: str, minimum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"{name} must be a number") from exc
    if parsed < minimum:
        raise ConfigurationError(f"{name} must be at least {minimum}")
    return parsed


def normalize_mac(value: Any) -> str:
    """Normalize a MAC address into the device ID accepted by AirStage."""
    normalized = re.sub(r"[:-]", "", str(value)).upper()
    if not _MAC_RE.fullmatch(normalized):
        raise ConfigurationError("unit mac must contain exactly 12 hexadecimal digits")
    return normalized


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    return result or None


def _read_secret(path: str) -> str:
    try:
        value = Path(path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ConfigurationError(f"cannot read MQTT password file {path}: {exc}") from exc
    if not value:
        raise ConfigurationError(f"MQTT password file {path} is empty")
    return value


def _env_value(environ: Mapping[str, str], key: str, fallback: Any) -> Any:
    value = environ.get(key)
    return fallback if value is None else value


def load_config(
    path: str | Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> AppConfig:
    """Load YAML configuration and apply environment overrides."""
    env = os.environ if environ is None else environ
    selected_value: str | Path = (
        path if path is not None else env.get("A2M_CONFIG", str(DEFAULT_CONFIG_PATH))
    )
    selected_path = Path(selected_value)
    try:
        raw = yaml.safe_load(selected_path.read_text(encoding="utf-8")) or {}
    except OSError as exc:
        raise ConfigurationError(f"cannot read configuration {selected_path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"invalid YAML in {selected_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigurationError("configuration root must be a mapping")

    bridge_raw = _mapping(raw.get("bridge"), "bridge")
    bridge_key = str(bridge_raw.get("key", "")).strip()
    if not _BRIDGE_KEY_RE.fullmatch(bridge_key) or bridge_key.startswith("CHANGE_ME"):
        raise ConfigurationError(
            "bridge.key is required and must be a stable 8-64 character value containing only "
            "letters, digits, underscores, or hyphens"
        )

    mqtt_raw = _mapping(raw.get("mqtt"), "mqtt")
    host = str(_env_value(env, "A2M_MQTT_HOST", mqtt_raw.get("host", ""))).strip()
    if not host:
        raise ConfigurationError("MQTT host is required (mqtt.host or A2M_MQTT_HOST)")
    port = _integer(
        _env_value(env, "A2M_MQTT_PORT", mqtt_raw.get("port", 1883)),
        "MQTT port",
        1,
        65535,
    )
    username = _optional_string(_env_value(env, "A2M_MQTT_USERNAME", mqtt_raw.get("username")))
    password = _optional_string(_env_value(env, "A2M_MQTT_PASSWORD", mqtt_raw.get("password")))
    password_file = _optional_string(
        _env_value(env, "A2M_MQTT_PASSWORD_FILE", mqtt_raw.get("password_file"))
    )
    if password_file:
        password = _read_secret(password_file)

    tls = _boolean(_env_value(env, "A2M_MQTT_TLS", mqtt_raw.get("tls", False)), "MQTT TLS")
    tls_insecure = _boolean(
        _env_value(
            env,
            "A2M_MQTT_TLS_INSECURE",
            mqtt_raw.get("tls_insecure", False),
        ),
        "MQTT TLS insecure",
    )
    tls_ca_file = _optional_string(
        _env_value(env, "A2M_MQTT_TLS_CA_FILE", mqtt_raw.get("tls_ca_file"))
    )
    base_topic = str(mqtt_raw.get("base_topic", "airstage2mqtt")).strip(" /")
    if not base_topic or "+" in base_topic or "#" in base_topic:
        raise ConfigurationError("MQTT base topic must be non-empty and contain no wildcards")
    qos = _integer(mqtt_raw.get("qos", 1), "MQTT QoS", 0, 2)
    mqtt = MqttConfig(
        host=host,
        port=port,
        username=username,
        password=password,
        tls=tls,
        tls_ca_file=tls_ca_file,
        tls_insecure=tls_insecure,
        client_id=str(mqtt_raw.get("client_id", f"airstage2mqtt-{bridge_key}")),
        base_topic=base_topic,
        qos=qos,
    )

    polling_raw = _mapping(raw.get("polling"), "polling")
    polling = PollingConfig(
        interval_seconds=_number(polling_raw.get("interval_seconds", 10), "polling interval", 1),
        timeout_seconds=_integer(polling_raw.get("timeout_seconds", 20), "polling timeout", 1),
        retries=_integer(polling_raw.get("retries", 5), "polling retries", 1),
        offline_after_failures=_integer(
            polling_raw.get("offline_after_failures", 2), "offline failure count", 1
        ),
        reconnect_min_seconds=_number(
            polling_raw.get("reconnect_min_seconds", 1), "minimum reconnect delay", 0.1
        ),
        reconnect_max_seconds=_number(
            polling_raw.get("reconnect_max_seconds", 30), "maximum reconnect delay", 0.1
        ),
    )
    if polling.reconnect_max_seconds < polling.reconnect_min_seconds:
        raise ConfigurationError("maximum reconnect delay must not be below the minimum")

    homeassistant_raw = _mapping(raw.get("homeassistant"), "homeassistant")
    discovery_prefix = str(homeassistant_raw.get("discovery_prefix", "homeassistant")).strip(" /")
    if not discovery_prefix or "+" in discovery_prefix or "#" in discovery_prefix:
        raise ConfigurationError("Home Assistant discovery prefix is invalid")
    homeassistant = HomeAssistantConfig(
        enabled=_boolean(homeassistant_raw.get("enabled", False), "homeassistant.enabled"),
        discovery_prefix=discovery_prefix,
    )

    units_raw = raw.get("units")
    if not isinstance(units_raw, list) or not units_raw:
        raise ConfigurationError("at least one unit must be configured")
    units: list[UnitConfig] = []
    names: set[str] = set()
    macs: set[str] = set()
    ips: set[str] = set()
    for index, item in enumerate(units_raw):
        unit_raw = _mapping(item, f"units[{index}]")
        name = str(unit_raw.get("name", "")).strip()
        if not _NAME_RE.fullmatch(name):
            raise ConfigurationError(
                f"units[{index}].name must contain only letters, digits, underscores, or hyphens"
            )
        if name in _RESERVED_UNIT_NAMES:
            raise ConfigurationError(f"units[{index}].name uses reserved topic name: {name}")
        mac = normalize_mac(unit_raw.get("mac", ""))
        try:
            ip = str(ipaddress.IPv4Address(str(unit_raw.get("ip", ""))))
        except ipaddress.AddressValueError as exc:
            raise ConfigurationError(f"units[{index}].ip must be an IPv4 address") from exc
        if name in names:
            raise ConfigurationError(f"duplicate unit name: {name}")
        if mac in macs:
            raise ConfigurationError(f"duplicate unit MAC: {mac}")
        if ip in ips:
            raise ConfigurationError(f"duplicate unit IP: {ip}")
        names.add(name)
        macs.add(mac)
        ips.add(ip)
        units.append(
            UnitConfig(
                name=name,
                mac=mac,
                ip=ip,
                use_https=_boolean(unit_raw.get("use_https", False), f"{name}.use_https"),
                turn_on_before_set_temperature=_boolean(
                    unit_raw.get("turn_on_before_set_temperature", False),
                    f"{name}.turn_on_before_set_temperature",
                ),
                friendly_name=_optional_string(unit_raw.get("friendly_name")),
            )
        )

    log_level = str(_env_value(env, "A2M_LOG_LEVEL", raw.get("log_level", "INFO"))).upper()
    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ConfigurationError("log level must be DEBUG, INFO, WARNING, ERROR, or CRITICAL")
    data_dir = Path(str(_env_value(env, "A2M_DATA_DIR", raw.get("data_dir", "/data"))))
    return AppConfig(
        mqtt=mqtt,
        polling=polling,
        homeassistant=homeassistant,
        bridge_key=bridge_key,
        units=tuple(units),
        data_dir=data_dir,
        log_level=log_level,
    )
