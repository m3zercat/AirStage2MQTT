"""MQTT bridge service and per-unit polling workers."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import ssl
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path

import aiohttp
import aiomqtt

from . import __version__
from .airstage import (
    CommandError,
    DeviceSnapshot,
    PyairstageLocalUnit,
    UnitAdapter,
)
from .config import AppConfig, UnitConfig
from .discovery import DiscoveryManager
from .manifest import ManifestManager

_LOGGER = logging.getLogger(__name__)
SnapshotCallback = Callable[[UnitConfig, DeviceSnapshot], Awaitable[None]]
AvailabilityCallback = Callable[[UnitConfig, bool], Awaitable[None]]


class HealthReporter:
    """Expose service liveness to a Docker health-check subprocess."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def online(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(str(time.time()), encoding="ascii")

    def offline(self) -> None:
        with suppress(FileNotFoundError):
            self.path.unlink()


class UnitWorker:
    """Poll and command one unit without blocking any other unit."""

    def __init__(
        self,
        config: UnitConfig,
        adapter: UnitAdapter,
        *,
        interval: float,
        offline_after_failures: int,
        on_snapshot: SnapshotCallback,
        on_availability: AvailabilityCallback,
        initial_snapshot: DeviceSnapshot | None = None,
    ) -> None:
        self.config = config
        self.adapter = adapter
        self.interval = interval
        self.offline_after_failures = offline_after_failures
        self.on_snapshot = on_snapshot
        self.on_availability = on_availability
        self.snapshot = initial_snapshot
        self._lock = asyncio.Lock()
        self._refresh = asyncio.Event()
        self._failures = 0
        self._available = False

    async def run(self) -> None:
        await self.on_availability(self.config, False)
        while True:
            await self.poll()
            try:
                await asyncio.wait_for(self._refresh.wait(), timeout=self.interval)
                self._refresh.clear()
            except TimeoutError:
                pass

    def request_refresh(self) -> None:
        self._refresh.set()

    async def poll(self) -> None:
        async with self._lock:
            await self._poll_locked()

    async def _poll_locked(self) -> None:
        try:
            snapshot = await self.adapter.refresh()
        except Exception as exc:  # Unit adapters also surface low-level library exceptions.
            self._failures += 1
            _LOGGER.warning(
                "Poll failed for %s (%s/%s): %s",
                self.config.name,
                self._failures,
                self.offline_after_failures,
                exc,
            )
            if self._failures >= self.offline_after_failures and self._available:
                self._available = False
                await self.on_availability(self.config, False)
            return
        self.snapshot = snapshot
        self._failures = 0
        if not self._available:
            self._available = True
            await self.on_availability(self.config, True)
        await self.on_snapshot(self.config, snapshot)

    async def handle_command(self, command: dict[str, object]) -> None:
        async with self._lock:
            try:
                await self.adapter.apply(command, self.snapshot)
                await self._poll_locked()
            except CommandError as exc:
                _LOGGER.warning("Rejected command for %s: %s", self.config.name, exc)
            except Exception as exc:
                # Keep malformed hardware responses and third-party errors isolated to this unit.
                _LOGGER.error("Command failed for %s: %s", self.config.name, exc)


class BridgeService:
    """Maintain MQTT connectivity and bridge configured local AirStage units."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.stop_event = asyncio.Event()
        self.discovery = DiscoveryManager(config)
        self.manifest = ManifestManager(config)
        health_path = os.getenv("A2M_HEALTH_FILE", "/tmp/airstage2mqtt-health")
        self.health = HealthReporter(Path(health_path))
        self._snapshots: dict[str, DeviceSnapshot] = {}
        self._command_tasks: set[asyncio.Task[None]] = set()

    def stop(self) -> None:
        self.stop_event.set()

    def _tls_context(self) -> ssl.SSLContext | None:
        if not self.config.mqtt.tls:
            return None
        context = ssl.create_default_context(cafile=self.config.mqtt.tls_ca_file)
        if self.config.mqtt.tls_insecure:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        return context

    async def run(self) -> None:
        delay = self.config.polling.reconnect_min_seconds
        while not self.stop_event.is_set():
            try:
                await self._run_connected()
                delay = self.config.polling.reconnect_min_seconds
            except aiomqtt.MqttError as exc:
                self.health.offline()
                if self.stop_event.is_set():
                    break
                _LOGGER.error("MQTT connection failed: %s; retrying in %.1fs", exc, delay)
                try:
                    await asyncio.wait_for(self.stop_event.wait(), timeout=delay)
                except TimeoutError:
                    pass
                delay = min(delay * 2, self.config.polling.reconnect_max_seconds)
        self.health.offline()

    async def _run_connected(self) -> None:
        mqtt = self.config.mqtt
        bridge_state_topic = f"{self.config.bridge_topic}/state"
        will = aiomqtt.Will(bridge_state_topic, "offline", qos=mqtt.qos, retain=True)
        async with aiomqtt.Client(
            hostname=mqtt.host,
            port=mqtt.port,
            username=mqtt.username,
            password=mqtt.password,
            identifier=mqtt.client_id,
            will=will,
            tls_context=self._tls_context(),
            tls_insecure=mqtt.tls_insecure if mqtt.tls else None,
        ) as client:
            _LOGGER.info("Connected to MQTT broker %s:%s", mqtt.host, mqtt.port)
            previous_manifest = await self._read_retained_manifest(client)
            await client.subscribe(f"{mqtt.base_topic}/+/set", qos=mqtt.qos)
            await client.subscribe(f"{mqtt.base_topic}/+/set/+", qos=mqtt.qos)
            await client.subscribe(f"{mqtt.base_topic}/+/get", qos=mqtt.qos)
            if self.config.homeassistant.enabled:
                await client.subscribe("homeassistant/status", qos=mqtt.qos)
            await client.publish(bridge_state_topic, "online", qos=mqtt.qos, retain=True)
            self.discovery.connection_reset()
            await self.manifest.reconcile(client, previous_manifest)
            await self.discovery.reconcile(client)
            await self.manifest.publish(client)
            await self._publish_bridge_info(client)
            self.health.online()

            async with aiohttp.ClientSession() as session:
                workers = self._create_workers(session, client)
                worker_tasks = {
                    asyncio.create_task(worker.run(), name=f"poll-{name}")
                    for name, worker in workers.items()
                }
                messages_task = asyncio.create_task(
                    self._consume_messages(client, workers), name="mqtt-messages"
                )
                health_task = asyncio.create_task(self._health_loop(), name="health-heartbeat")
                stop_task = asyncio.create_task(self.stop_event.wait(), name="stop-wait")
                tasks: set[asyncio.Task[object]] = {
                    *worker_tasks,
                    messages_task,
                    health_task,
                    stop_task,
                }
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                task_error: BaseException | None = None
                for task in done:
                    if task is not stop_task:
                        exception = task.exception()
                        if exception is not None and task_error is None:
                            task_error = exception
                for task in pending:
                    task.cancel()
                for task in self._command_tasks:
                    task.cancel()
                await asyncio.gather(*pending, *self._command_tasks, return_exceptions=True)
                self._command_tasks.clear()
                if task_error is not None:
                    raise task_error

            if self.stop_event.is_set():
                for unit in self.config.units:
                    await self._publish_availability(client, unit, False)
                await client.publish(bridge_state_topic, "offline", qos=mqtt.qos, retain=True)

    async def _read_retained_manifest(self, client: aiomqtt.Client) -> bytes | None:
        """Read the previous exact-key manifest before normal subscriptions start."""
        await client.subscribe(self.config.manifest_topic, qos=self.config.mqtt.qos)
        try:
            async with asyncio.timeout(1):
                message = await anext(client.messages)
                if str(message.topic) == self.config.manifest_topic:
                    return message.payload
        except TimeoutError:
            return None
        finally:
            await client.unsubscribe(self.config.manifest_topic)
        return None

    async def _health_loop(self) -> None:
        while True:
            self.health.online()
            await asyncio.sleep(30)

    def _create_workers(
        self, session: aiohttp.ClientSession, client: aiomqtt.Client
    ) -> dict[str, UnitWorker]:
        workers: dict[str, UnitWorker] = {}
        for unit in self.config.units:
            adapter = PyairstageLocalUnit(unit, self.config.polling, session)

            async def on_snapshot(
                selected: UnitConfig,
                snapshot: DeviceSnapshot,
                *,
                mqtt_client: aiomqtt.Client = client,
            ) -> None:
                self._snapshots[selected.name] = snapshot
                payload = json.dumps(snapshot.state, separators=(",", ":"), sort_keys=True)
                await mqtt_client.publish(
                    f"{self.config.mqtt.base_topic}/{selected.name}",
                    payload,
                    qos=self.config.mqtt.qos,
                    retain=True,
                )
                await self.discovery.publish(mqtt_client, selected, snapshot)
                await self._publish_bridge_info(mqtt_client)
                self.health.online()

            async def on_availability(
                selected: UnitConfig,
                available: bool,
                *,
                mqtt_client: aiomqtt.Client = client,
            ) -> None:
                await self._publish_availability(mqtt_client, selected, available)

            workers[unit.name] = UnitWorker(
                unit,
                adapter,
                interval=self.config.polling.interval_seconds,
                offline_after_failures=self.config.polling.offline_after_failures,
                on_snapshot=on_snapshot,
                on_availability=on_availability,
                initial_snapshot=self._snapshots.get(unit.name),
            )
        return workers

    async def _publish_availability(
        self, client: aiomqtt.Client, unit: UnitConfig, available: bool
    ) -> None:
        await client.publish(
            f"{self.config.mqtt.base_topic}/{unit.name}/availability",
            "online" if available else "offline",
            qos=self.config.mqtt.qos,
            retain=True,
        )

    async def _publish_bridge_info(self, client: aiomqtt.Client) -> None:
        devices = []
        for unit in self.config.units:
            snapshot = self._snapshots.get(unit.name)
            devices.append(
                {
                    "friendly_name": unit.display_name,
                    "device_id": unit.device_id,
                    "model": snapshot.model if snapshot else None,
                }
            )
        payload = {
            "version": __version__,
            "bridge_key": self.config.bridge_key,
            "homeassistant": self.config.homeassistant.enabled,
            "devices": devices,
        }
        await client.publish(
            f"{self.config.bridge_topic}/info",
            json.dumps(payload, separators=(",", ":"), sort_keys=True),
            qos=self.config.mqtt.qos,
            retain=True,
        )

    async def _consume_messages(
        self, client: aiomqtt.Client, workers: dict[str, UnitWorker]
    ) -> None:
        async for message in client.messages:
            topic = str(message.topic)
            if topic == "homeassistant/status":
                if message.payload.decode(errors="replace").strip().lower() == "online":
                    for unit in self.config.units:
                        snapshot = self._snapshots.get(unit.name)
                        if snapshot is not None:
                            await self.discovery.publish(client, unit, snapshot, force=True)
                continue
            prefix = f"{self.config.mqtt.base_topic}/"
            if not topic.startswith(prefix):
                continue
            parts = topic[len(prefix) :].split("/")
            is_command_topic = (len(parts) == 2 and parts[1] in {"set", "get"}) or (
                len(parts) == 3 and parts[1] == "set"
            )
            if message.retain and is_command_topic:
                _LOGGER.warning("Ignoring and clearing retained command on %s", topic)
                await client.publish(topic, b"", qos=self.config.mqtt.qos, retain=True)
                continue
            worker = workers.get(parts[0])
            if worker is None:
                _LOGGER.warning("Ignoring command for unknown unit topic %s", topic)
                continue
            if len(parts) == 2 and parts[1] == "get":
                worker.request_refresh()
                continue
            try:
                command = self._decode_command(parts, message.payload)
            except CommandError as exc:
                _LOGGER.warning("Ignoring invalid MQTT command on %s: %s", topic, exc)
                continue
            task = asyncio.create_task(
                worker.handle_command(command), name=f"command-{worker.config.name}"
            )
            self._command_tasks.add(task)
            task.add_done_callback(self._command_tasks.discard)

    @staticmethod
    def _decode_command(parts: list[str], payload: bytes) -> dict[str, object]:
        if len(parts) == 2 and parts[1] == "set":
            try:
                decoded = json.loads(payload)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise CommandError("the /set payload must be a JSON object") from exc
            if not isinstance(decoded, dict):
                raise CommandError("the /set payload must be a JSON object")
            return {str(key): value for key, value in decoded.items()}
        if len(parts) == 3 and parts[1] == "set":
            text = payload.decode("utf-8").strip()
            if not text:
                raise CommandError("a property command payload cannot be empty")
            try:
                value: object = json.loads(text)
            except json.JSONDecodeError:
                value = text
            return {parts[2]: value}
        raise CommandError("topic is not a supported command topic")


def healthcheck(path: str | Path | None = None, *, maximum_age: float = 90) -> bool:
    """Return whether the bridge has recently reported a healthy MQTT connection."""
    selected_value: str | Path = (
        path if path is not None else os.getenv("A2M_HEALTH_FILE", "/tmp/airstage2mqtt-health")
    )
    selected = Path(selected_value)
    try:
        timestamp = float(selected.read_text(encoding="ascii"))
    except (OSError, ValueError):
        return False
    return time.time() - timestamp <= maximum_age
