from __future__ import annotations

from pathlib import Path

import pytest

from airstage2mqtt.config import ConfigurationError, load_config, normalize_mac

BASE_CONFIG = """
polling:
  interval_seconds: 15
homeassistant:
  enabled: true
units:
  - name: living_room
    mac: "e8:fb:1c:00:00:00"
    ip: "192.168.1.40"
"""


def write_config(tmp_path: Path, content: str = BASE_CONFIG) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(content, encoding="utf-8")
    return path


def test_environment_overrides_yaml_and_password_file(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path,
        """
mqtt:
  host: yaml-broker
  port: 1883
  password: yaml-password
  base_topic: house/airstage
units:
  - name: living_room
    friendly_name: Living Room Air Conditioner
    mac: E8FB1C000000
    ip: 192.168.1.40
""",
    )
    password_path = tmp_path / "mqtt-password"
    password_path.write_text("file-secret\n", encoding="utf-8")

    config = load_config(
        config_path,
        environ={
            "A2M_MQTT_HOST": "env-broker",
            "A2M_MQTT_PORT": "8883",
            "A2M_MQTT_PASSWORD": "env-secret",
            "A2M_MQTT_PASSWORD_FILE": str(password_path),
            "A2M_MQTT_TLS": "true",
            "A2M_BASE_TOPIC": "ignored/environment/topic",
            "A2M_DATA_DIR": str(tmp_path / "data"),
        },
    )

    assert config.mqtt.host == "env-broker"
    assert config.mqtt.port == 8883
    assert config.mqtt.password == "file-secret"
    assert config.mqtt.tls is True
    assert config.mqtt.base_topic == "house/airstage"
    assert config.units[0].device_id == "E8FB1C000000"
    assert config.units[0].display_name == "Living Room Air Conditioner"


def test_derives_friendly_name_from_topic_name(tmp_path: Path) -> None:
    config = load_config(
        write_config(tmp_path),
        environ={"A2M_MQTT_HOST": "broker"},
    )

    assert config.units[0].friendly_name is None
    assert config.units[0].display_name == "Living Room"


@pytest.mark.parametrize(
    ("mac", "expected"),
    [
        ("e8:fb:1c:00:00:00", "E8FB1C000000"),
        ("e8-fb-1c-00-00-00", "E8FB1C000000"),
        ("E8FB1C000000", "E8FB1C000000"),
    ],
)
def test_normalize_mac(mac: str, expected: str) -> None:
    assert normalize_mac(mac) == expected


@pytest.mark.parametrize("mac", ["", "not-a-mac", "E8FB1C00000Z"])
def test_rejects_invalid_mac(mac: str) -> None:
    with pytest.raises(ConfigurationError, match="12 hexadecimal"):
        normalize_mac(mac)


def test_rejects_duplicate_units(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        """
units:
  - name: office
    mac: E8FB1C000000
    ip: 192.168.1.40
  - name: office
    mac: E8FB1C000001
    ip: 192.168.1.41
""",
    )
    with pytest.raises(ConfigurationError, match="duplicate unit name"):
        load_config(path, environ={"A2M_MQTT_HOST": "broker"})


def test_requires_static_ipv4_and_mqtt_host(tmp_path: Path) -> None:
    path = write_config(tmp_path, BASE_CONFIG.replace("192.168.1.40", "unit.local"))
    with pytest.raises(ConfigurationError, match="MQTT host"):
        load_config(path, environ={})
    with pytest.raises(ConfigurationError, match="IPv4"):
        load_config(path, environ={"A2M_MQTT_HOST": "broker"})


def test_rejects_wildcard_base_topic(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="no wildcards"):
        load_config(
            write_config(
                tmp_path, BASE_CONFIG.replace("polling:", "mqtt:\n  base_topic: bad/+\npolling:")
            ),
            environ={"A2M_MQTT_HOST": "broker"},
        )
