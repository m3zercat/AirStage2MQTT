from __future__ import annotations

from pathlib import Path

import pytest

from airstage2mqtt.config import ConfigurationError, load_config, normalize_mac

BASE_CONFIG = """
bridge:
  key: testbridge001
polling:
  interval_seconds: 15
  command_refresh_delay_seconds: 1.5
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
bridge:
  key: testbridge001
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
    assert config.polling.command_refresh_delay_seconds == 1.5


def test_diagnostics_are_disabled_by_default(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path), environ={"A2M_MQTT_HOST": "broker"})

    assert config.units[0].diagnostics == frozenset()


def test_loads_enabled_diagnostics_per_unit(tmp_path: Path) -> None:
    content = BASE_CONFIG.replace(
        '    ip: "192.168.1.40"',
        '    ip: "192.168.1.40"\n'
        "    diagnostics:\n"
        "      enabled:\n"
        "        - error_code\n"
        "        - demand\n"
        "        - human_detection",
    )

    config = load_config(write_config(tmp_path, content), environ={"A2M_MQTT_HOST": "broker"})

    assert config.units[0].diagnostics == frozenset({"error_code", "demand", "human_detection"})


@pytest.mark.parametrize(
    ("diagnostics_yaml", "message"),
    [
        ("      enabled: error_code", "must be a list"),
        ("      enabled:\n        - made_up", "unknown diagnostic"),
    ],
)
def test_rejects_invalid_diagnostics(tmp_path: Path, diagnostics_yaml: str, message: str) -> None:
    content = BASE_CONFIG.replace(
        '    ip: "192.168.1.40"',
        f'    ip: "192.168.1.40"\n    diagnostics:\n{diagnostics_yaml}',
    )

    with pytest.raises(ConfigurationError, match=message):
        load_config(write_config(tmp_path, content), environ={"A2M_MQTT_HOST": "broker"})


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
bridge:
  key: testbridge001
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


def test_rejects_reserved_unit_topic_names(tmp_path: Path) -> None:
    content = BASE_CONFIG.replace("name: living_room", "name: manifests")
    with pytest.raises(ConfigurationError, match="reserved topic name"):
        load_config(write_config(tmp_path, content), environ={"A2M_MQTT_HOST": "broker"})


@pytest.mark.parametrize("key", ["", "short", "contains spaces", "CHANGE_ME_RANDOM_KEY"])
def test_requires_stable_bridge_key(tmp_path: Path, key: str) -> None:
    content = BASE_CONFIG.replace("testbridge001", key)
    with pytest.raises(ConfigurationError, match="bridge.key"):
        load_config(write_config(tmp_path, content), environ={"A2M_MQTT_HOST": "broker"})


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
