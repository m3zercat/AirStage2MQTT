from __future__ import annotations

import time
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from airstage2mqtt.airstage import DeviceSnapshot, UnitError
from airstage2mqtt.config import AppConfig, UnitConfig
from airstage2mqtt.service import BridgeService, UnitWorker, healthcheck


class FakeAdapter:
    def __init__(self, results: list[DeviceSnapshot | Exception]) -> None:
        self.results = results
        self.commands: list[dict[str, object]] = []

    async def refresh(self) -> DeviceSnapshot:
        result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(result, Exception):
            raise result
        return result

    async def apply(self, command: dict[str, object], current: DeviceSnapshot | None) -> None:
        self.commands.append(command)


class FakeMqttClient:
    def __init__(self, messages: list[SimpleNamespace]) -> None:
        self.messages = self._messages(messages)
        self.published: list[tuple[str, bytes, int, bool]] = []

    async def _messages(self, messages: list[SimpleNamespace]) -> AsyncIterator[SimpleNamespace]:
        for message in messages:
            yield message

    async def publish(self, topic: str, payload: bytes, *, qos: int, retain: bool) -> None:
        self.published.append((topic, payload, qos, retain))


@pytest.mark.asyncio
async def test_worker_marks_offline_without_raising(unit_config: UnitConfig) -> None:
    good = DeviceSnapshot({"state": "ON"}, frozenset({"state"}), "test")
    adapter = FakeAdapter([good, UnitError("down"), UnitError("down")])
    availability: list[bool] = []
    states: list[DeviceSnapshot] = []

    async def on_state(_unit: UnitConfig, state: DeviceSnapshot) -> None:
        states.append(state)

    async def on_availability(_unit: UnitConfig, available: bool) -> None:
        availability.append(available)

    worker = UnitWorker(
        unit_config,
        adapter,
        interval=10,
        offline_after_failures=2,
        on_snapshot=on_state,
        on_availability=on_availability,
    )
    await worker.poll()
    await worker.poll()
    await worker.poll()

    assert states == [good]
    assert availability == [True, False]


@pytest.mark.asyncio
async def test_worker_serializes_command_and_confirmed_refresh(unit_config: UnitConfig) -> None:
    before = DeviceSnapshot({"state": "OFF"}, frozenset({"state"}), "test")
    after = DeviceSnapshot({"state": "ON"}, frozenset({"state"}), "test")
    adapter = FakeAdapter([after])
    states: list[DeviceSnapshot] = []

    async def on_state(_unit: UnitConfig, state: DeviceSnapshot) -> None:
        states.append(state)

    async def on_availability(_unit: UnitConfig, _available: bool) -> None:
        return None

    worker = UnitWorker(
        unit_config,
        adapter,
        interval=10,
        offline_after_failures=2,
        on_snapshot=on_state,
        on_availability=on_availability,
        initial_snapshot=before,
    )
    await worker.handle_command({"state": "ON"})

    assert adapter.commands == [{"state": "ON"}]
    assert states == [after]


@pytest.mark.parametrize(
    ("parts", "payload", "expected"),
    [
        (["unit", "set"], b'{"state":"ON"}', {"state": "ON"}),
        (["unit", "set", "target_temperature"], b"21.5", {"target_temperature": 21.5}),
        (["unit", "set", "state"], b"ON", {"state": "ON"}),
    ],
)
def test_decodes_zigbee2mqtt_style_commands(
    parts: list[str], payload: bytes, expected: dict[str, object]
) -> None:
    assert BridgeService._decode_command(parts, payload) == expected


@pytest.mark.asyncio
async def test_ignores_and_clears_retained_commands(app_config: AppConfig) -> None:
    topic = "airstage2mqtt/living_room/set/mode"
    client = FakeMqttClient([SimpleNamespace(topic=topic, payload=b"heat", retain=True)])
    service = BridgeService(app_config)

    await service._consume_messages(client, {})  # type: ignore[arg-type]

    assert client.published == [(topic, b"", app_config.mqtt.qos, True)]


def test_healthcheck_uses_recent_timestamp(tmp_path: Path) -> None:
    path = tmp_path / "health"
    path.write_text(str(time.time()), encoding="ascii")
    assert healthcheck(path, maximum_age=10)
    path.write_text(str(time.time() - 20), encoding="ascii")
    assert not healthcheck(path, maximum_age=10)
