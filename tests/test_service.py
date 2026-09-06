from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from airstage2mqtt.airstage import AcceptedCommand, DeviceSnapshot, UnitError
from airstage2mqtt.config import AppConfig, UnitConfig
from airstage2mqtt.service import BridgeService, UnitWorker, healthcheck


class FakeAdapter:
    def __init__(self, results: list[DeviceSnapshot | Exception]) -> None:
        self.results = results
        self.commands: list[dict[str, object]] = []
        self.refresh_count = 0
        self.refreshed = asyncio.Event()

    async def refresh(self) -> DeviceSnapshot:
        self.refresh_count += 1
        self.refreshed.set()
        result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(result, Exception):
            raise result
        return result

    def accept(self, command: dict[str, object], current: DeviceSnapshot) -> AcceptedCommand:
        state = {**current.state, **command}
        return AcceptedCommand(
            values=dict(command),
            snapshot=DeviceSnapshot(state, current.capabilities, current.model),
        )

    async def apply(self, command: AcceptedCommand) -> None:
        self.commands.append(command.values)


class FailingWriteAdapter(FakeAdapter):
    async def apply(self, command: AcceptedCommand) -> None:
        await super().apply(command)
        raise UnitError("write failed")


class FakeMqttClient:
    def __init__(self, messages: list[SimpleNamespace]) -> None:
        self.messages = self._messages(messages)
        self.published: list[tuple[str, str | bytes, int, bool]] = []

    async def _messages(self, messages: list[SimpleNamespace]) -> AsyncIterator[SimpleNamespace]:
        for message in messages:
            yield message

    async def publish(self, topic: str, payload: str | bytes, *, qos: int, retain: bool) -> None:
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
async def test_worker_publishes_optimistic_state_before_write_then_reconciles(
    unit_config: UnitConfig,
) -> None:
    before = DeviceSnapshot(
        {"state": "ON", "target_temperature": 20.0},
        frozenset({"state", "target_temperature"}),
        "test",
    )
    after = DeviceSnapshot(
        {"state": "ON", "target_temperature": 21.5},
        before.capabilities,
        "test",
    )
    adapter = FakeAdapter([after])
    states: list[DeviceSnapshot] = []

    async def on_state(_unit: UnitConfig, state: DeviceSnapshot) -> None:
        if not states:
            assert adapter.commands == []
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
        command_refresh_delay=0.01,
    )
    await worker.handle_command({"target_temperature": 21.5})

    assert adapter.commands == [{"target_temperature": 21.5}]
    assert states == [after]
    assert adapter.refresh_count == 0

    run_task = asyncio.create_task(worker.run())
    try:
        await asyncio.wait_for(adapter.refreshed.wait(), timeout=1)
    finally:
        run_task.cancel()
        await asyncio.gather(run_task, return_exceptions=True)

    assert adapter.refresh_count == 1
    assert states == [after, after]


@pytest.mark.asyncio
async def test_matching_reconciliation_does_not_republish_mqtt_state(
    app_config: AppConfig, unit_config: UnitConfig
) -> None:
    before = DeviceSnapshot(
        {"state": "ON", "target_temperature": 20.0},
        frozenset({"state", "target_temperature"}),
        "test",
    )
    after = DeviceSnapshot(
        {"state": "ON", "target_temperature": 21.5},
        before.capabilities,
        "test",
    )
    adapter = FakeAdapter([after])
    client = FakeMqttClient([])
    service = BridgeService(app_config)

    async def on_state(selected: UnitConfig, snapshot: DeviceSnapshot) -> None:
        await service._publish_snapshot(client, selected, snapshot)  # type: ignore[arg-type]

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
        command_refresh_delay=0.01,
    )
    await service._publish_snapshot(client, unit_config, before)  # type: ignore[arg-type]
    await worker.handle_command({"target_temperature": 21.5})

    run_task = asyncio.create_task(worker.run())
    try:
        await asyncio.wait_for(adapter.refreshed.wait(), timeout=1)
    finally:
        run_task.cancel()
        await asyncio.gather(run_task, return_exceptions=True)

    state_messages = [
        payload
        for topic, payload, *_ in client.published
        if topic == "airstage2mqtt/living_room"
    ]
    assert len(state_messages) == 2


@pytest.mark.asyncio
async def test_mismatching_reconciliation_publishes_corrected_mqtt_state(
    app_config: AppConfig, unit_config: UnitConfig
) -> None:
    before = DeviceSnapshot(
        {"state": "ON", "target_temperature": 20.0},
        frozenset({"state", "target_temperature"}),
        "test",
    )
    corrected = DeviceSnapshot(
        {"state": "ON", "target_temperature": 21.0},
        before.capabilities,
        "test",
    )
    adapter = FakeAdapter([corrected])
    client = FakeMqttClient([])
    service = BridgeService(app_config)

    async def on_state(selected: UnitConfig, snapshot: DeviceSnapshot) -> None:
        await service._publish_snapshot(client, selected, snapshot)  # type: ignore[arg-type]

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
        command_refresh_delay=0.01,
    )
    await service._publish_snapshot(client, unit_config, before)  # type: ignore[arg-type]
    await worker.handle_command({"target_temperature": 21.5})

    run_task = asyncio.create_task(worker.run())
    try:
        await asyncio.wait_for(adapter.refreshed.wait(), timeout=1)
    finally:
        run_task.cancel()
        await asyncio.gather(run_task, return_exceptions=True)

    state_messages = [
        payload
        for topic, payload, *_ in client.published
        if topic == "airstage2mqtt/living_room"
    ]
    assert len(state_messages) == 3
    assert '"target_temperature":21.0' in state_messages[-1]


@pytest.mark.asyncio
async def test_explicit_refresh_overrides_delayed_reconciliation(unit_config: UnitConfig) -> None:
    before = DeviceSnapshot({"state": "OFF"}, frozenset({"state"}), "test")
    after = DeviceSnapshot({"state": "ON"}, before.capabilities, "test")
    adapter = FakeAdapter([after])

    async def on_state(_unit: UnitConfig, _state: DeviceSnapshot) -> None:
        return None

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
        command_refresh_delay=60,
    )
    await worker.handle_command({"state": "ON"})
    worker.request_refresh()

    run_task = asyncio.create_task(worker.run())
    try:
        await asyncio.wait_for(adapter.refreshed.wait(), timeout=1)
    finally:
        run_task.cancel()
        await asyncio.gather(run_task, return_exceptions=True)

    assert adapter.refresh_count == 1


@pytest.mark.asyncio
async def test_failed_write_still_schedules_reconciliation(unit_config: UnitConfig) -> None:
    before = DeviceSnapshot({"state": "OFF"}, frozenset({"state"}), "test")
    adapter = FailingWriteAdapter([before])
    states: list[DeviceSnapshot] = []

    async def on_state(_unit: UnitConfig, snapshot: DeviceSnapshot) -> None:
        states.append(snapshot)

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
        command_refresh_delay=0.01,
    )
    await worker.handle_command({"state": "ON"})

    run_task = asyncio.create_task(worker.run())
    try:
        await asyncio.wait_for(adapter.refreshed.wait(), timeout=1)
    finally:
        run_task.cancel()
        await asyncio.gather(run_task, return_exceptions=True)

    assert [snapshot.state["state"] for snapshot in states] == ["ON", "OFF"]


@pytest.mark.asyncio
async def test_initial_poll_publishes_state_before_availability(unit_config: UnitConfig) -> None:
    current = DeviceSnapshot({"state": "ON"}, frozenset({"state"}), "test")
    adapter = FakeAdapter([current])
    events: list[str] = []

    async def on_state(_unit: UnitConfig, _state: DeviceSnapshot) -> None:
        events.append("state")

    async def on_availability(_unit: UnitConfig, available: bool) -> None:
        events.append(f"availability:{available}")

    worker = UnitWorker(
        unit_config,
        adapter,
        interval=10,
        offline_after_failures=2,
        on_snapshot=on_state,
        on_availability=on_availability,
    )

    await worker.initialise()

    assert events == ["state", "availability:True"]


@pytest.mark.asyncio
async def test_initial_poll_establishes_unreachable_unit_as_offline(
    unit_config: UnitConfig,
) -> None:
    adapter = FakeAdapter([UnitError("down")])
    availability: list[bool] = []

    async def on_state(_unit: UnitConfig, _state: DeviceSnapshot) -> None:
        raise AssertionError("an unreachable unit must not publish state")

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

    await worker.initialise()

    assert availability == [False]


@pytest.mark.asyncio
async def test_state_and_bridge_info_publish_only_when_changed(
    app_config: AppConfig, unit_config: UnitConfig
) -> None:
    client = FakeMqttClient([])
    service = BridgeService(app_config)
    first = DeviceSnapshot({"state": "ON"}, frozenset({"state"}), "test")
    changed = DeviceSnapshot({"state": "OFF"}, frozenset({"state"}), "test")

    await service._publish_snapshot(client, unit_config, first)  # type: ignore[arg-type]
    await service._publish_snapshot(client, unit_config, first)  # type: ignore[arg-type]
    await service._publish_bridge_info(client)  # type: ignore[arg-type]
    await service._publish_bridge_info(client)  # type: ignore[arg-type]
    await service._publish_snapshot(client, unit_config, changed)  # type: ignore[arg-type]

    state_topic = "airstage2mqtt/living_room"
    info_topic = "airstage2mqtt/bridges/testbridge001/info"
    assert [topic for topic, *_ in client.published].count(state_topic) == 2
    assert [topic for topic, *_ in client.published].count(info_topic) == 1


@pytest.mark.asyncio
async def test_availability_publishes_only_on_transitions(
    app_config: AppConfig, unit_config: UnitConfig
) -> None:
    client = FakeMqttClient([])
    service = BridgeService(app_config)

    await service._publish_availability(client, unit_config, True)  # type: ignore[arg-type]
    await service._publish_availability(client, unit_config, True)  # type: ignore[arg-type]
    await service._publish_availability(client, unit_config, False)  # type: ignore[arg-type]
    await service._publish_availability(client, unit_config, False)  # type: ignore[arg-type]

    assert [payload for _, payload, *_ in client.published] == ["online", "offline"]


@pytest.mark.asyncio
async def test_initialisation_publishes_bridge_online_last(
    app_config: AppConfig, unit_config: UnitConfig
) -> None:
    client = FakeMqttClient([])
    service = BridgeService(app_config)
    other = UnitConfig("office", "E8FB1C000001", "192.168.1.41")
    service.config = AppConfig(
        mqtt=app_config.mqtt,
        polling=app_config.polling,
        homeassistant=app_config.homeassistant,
        bridge_key=app_config.bridge_key,
        units=(unit_config, other),
        data_dir=app_config.data_dir,
    )
    service.discovery.config = service.config
    service.manifest.config = service.config

    def make_worker(unit: UnitConfig, adapter: FakeAdapter) -> UnitWorker:
        async def on_state(selected: UnitConfig, state: DeviceSnapshot) -> None:
            service._snapshots[selected.name] = state
            await service._publish_snapshot(client, selected, state)  # type: ignore[arg-type]

        async def on_availability(selected: UnitConfig, available: bool) -> None:
            await service._publish_availability(client, selected, available)  # type: ignore[arg-type]

        return UnitWorker(
            unit,
            adapter,
            interval=10,
            offline_after_failures=2,
            on_snapshot=on_state,
            on_availability=on_availability,
        )

    workers = {
        unit_config.name: make_worker(
            unit_config,
            FakeAdapter([DeviceSnapshot({"state": "ON"}, frozenset({"state"}), "test")]),
        ),
        other.name: make_worker(other, FakeAdapter([UnitError("down")])),
    }

    await service._establish_initial_state(client, workers)  # type: ignore[arg-type]

    published = [(topic, payload) for topic, payload, *_ in client.published]
    assert published[-1] == ("airstage2mqtt/bridges/testbridge001/state", "online")
    assert ("airstage2mqtt/living_room/availability", "online") in published[:-1]
    assert ("airstage2mqtt/office/availability", "offline") in published[:-1]
    assert any(topic == "airstage2mqtt/living_room" for topic, _ in published[:-1])


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


@pytest.mark.asyncio
async def test_ha_birth_does_not_republish_unchanged_discovery(
    app_config: AppConfig, unit_config: UnitConfig
) -> None:
    snapshot = DeviceSnapshot({"state": "ON"}, frozenset({"state"}), "test")
    message = SimpleNamespace(topic="homeassistant/status", payload=b"online", retain=False)
    client = FakeMqttClient([message])
    service = BridgeService(app_config)
    service._snapshots[unit_config.name] = snapshot
    await service.discovery.publish(client, unit_config, snapshot)
    published_before_birth = list(client.published)

    await service._consume_messages(client, {})

    assert client.published == published_before_birth


def test_healthcheck_uses_recent_timestamp(tmp_path: Path) -> None:
    path = tmp_path / "health"
    path.write_text(str(time.time()), encoding="ascii")
    assert healthcheck(path, maximum_age=10)
    path.write_text(str(time.time() - 20), encoding="ascii")
    assert not healthcheck(path, maximum_age=10)
