from __future__ import annotations

from pathlib import Path

import pytest

from airstage2mqtt.config import (
    AppConfig,
    HomeAssistantConfig,
    MqttConfig,
    PollingConfig,
    UnitConfig,
)


@pytest.fixture
def unit_config() -> UnitConfig:
    return UnitConfig(
        name="living_room",
        mac="E8FB1C000000",
        ip="192.168.1.40",
        turn_on_before_set_temperature=True,
    )


@pytest.fixture
def app_config(tmp_path: Path, unit_config: UnitConfig) -> AppConfig:
    return AppConfig(
        mqtt=MqttConfig(host="mqtt.local"),
        polling=PollingConfig(),
        homeassistant=HomeAssistantConfig(enabled=True),
        bridge_key="testbridge001",
        units=(unit_config,),
        data_dir=tmp_path,
    )
