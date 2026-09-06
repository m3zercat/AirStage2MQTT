"""Local-only adapter around pyairstage."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any, Protocol

import aiohttp
import pyairstage.airstageApi as airstage_api
from pyairstage import constants
from pyairstage.airstageAC import AirstageAC, AirstageACError

from .config import DIAGNOSTIC_FIELDS, PollingConfig, UnitConfig

_LOGGER = logging.getLogger(__name__)


class UnitError(RuntimeError):
    """Base error raised by a unit adapter."""


class CommandError(UnitError):
    """Raised for an invalid or unsupported command."""


@dataclass(frozen=True, slots=True)
class DeviceSnapshot:
    """Normalized state read from one AirStage unit."""

    state: dict[str, object]
    capabilities: frozenset[str]
    model: str


@dataclass(frozen=True, slots=True)
class AcceptedCommand:
    """A validated command and the optimistic state it produces."""

    values: dict[str, object]
    snapshot: DeviceSnapshot


class UnitAdapter(Protocol):
    """Interface used by the MQTT worker and test doubles."""

    async def refresh(self) -> DeviceSnapshot: ...

    def accept(self, command: dict[str, object], current: DeviceSnapshot) -> AcceptedCommand: ...

    async def apply(self, command: AcceptedCommand) -> None: ...


def _normalized_enum(value: object) -> str:
    return str(value).strip().lower().replace("-", "_").replace(" ", "_")


def _raw_number(value: object) -> object:
    if isinstance(value, (int, float)):
        return value
    text = str(value)
    try:
        return int(text)
    except ValueError:
        try:
            return float(text)
        except ValueError:
            return text


def _on_off(value: object, name: str) -> constants.BooleanProperty:
    if isinstance(value, bool):
        return constants.BooleanProperty.ON if value else constants.BooleanProperty.OFF
    normalized = str(value).strip().lower()
    if normalized in {"on", "true", "1"}:
        return constants.BooleanProperty.ON
    if normalized in {"off", "false", "0"}:
        return constants.BooleanProperty.OFF
    raise CommandError(f"{name} must be ON or OFF")


class PyairstageLocalUnit:
    """A single Fujitsu unit accessed through pyairstage.ApiLocal."""

    _MODE_TO_LIBRARY = {
        "auto": constants.OperationMode.AUTO,
        "cool": constants.OperationMode.COOL,
        "dry": constants.OperationMode.DRY,
        "fan_only": constants.OperationMode.FAN,
        "fan": constants.OperationMode.FAN,
        "heat": constants.OperationMode.HEAT,
    }
    _FAN_TO_LIBRARY = {
        "auto": constants.FanSpeed.AUTO,
        "quiet": constants.FanSpeed.QUIET,
        "low": constants.FanSpeed.LOW,
        "medium": constants.FanSpeed.MEDIUM,
        "high": constants.FanSpeed.HIGH,
    }
    _BOOLEAN_SETTERS = {
        "economy": "set_economy_mode",
        "powerful": "set_powerful_mode",
        "outdoor_low_noise": "set_outdoor_low_noise",
        "energy_save_fan": "set_energy_save_fan",
        "minimum_heat": "set_minimum_heat",
        "indoor_led": "set_indoor_led",
        "human_detection_auto_save": "set_hmn_detection_auto_save",
    }
    _RAW_TO_STATE = {
        constants.ACParameter.POWER_CONSUMPTION.value: "power_consumption",
        constants.ACParameter.ERROR_CODE.value: "error_code",
        constants.ACParameter.DEMAND.value: "demand",
        constants.ACParameter.SIGN_RESET.value: "filter_sign_reset",
    }
    _CONFIGURABLE_DIAGNOSTICS = DIAGNOSTIC_FIELDS
    _TEMPERATURE_RANGES = {
        "auto": (constants.ACConstants.AUTO_MIN_TEMP, constants.ACConstants.AUTO_MAX_TEMP),
        "cool": (constants.ACConstants.COOL_MIN_TEMP, constants.ACConstants.COOL_MAX_TEMP),
        "dry": (constants.ACConstants.DRY_MIN_TEMP, constants.ACConstants.DRY_MAX_TEMP),
        "heat": (constants.ACConstants.HEAT_MIN_TEMP, constants.ACConstants.HEAT_MAX_TEMP),
    }

    def __init__(
        self,
        config: UnitConfig,
        polling: PollingConfig,
        session: aiohttp.ClientSession,
    ) -> None:
        self.config = config
        self._capabilities: set[str] = set(config.diagnostics)
        self._missing_diagnostics_warned: set[str] = set()
        self._api = airstage_api.ApiLocal(
            session=session,
            retry=polling.retries,
            timeout_seconds=polling.timeout_seconds,
            device_id=config.device_id,
            ip_address=config.ip,
            use_https=config.use_https,
        )

    async def refresh(self) -> DeviceSnapshot:
        try:
            devices = await self._api.get_devices()
            device = devices[self.config.device_id]
            ac = AirstageAC(self.config.device_id, self._api).refresh_parameters(data=device)
            return self._snapshot(ac, device)
        except (KeyError, TypeError, ValueError, AirstageACError, airstage_api.ApiError) as exc:
            raise UnitError(f"invalid response from {self.config.name}: {exc}") from exc
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise UnitError(f"cannot reach {self.config.name}: {exc}") from exc

    def _snapshot(self, ac: AirstageAC, device: dict[str, Any]) -> DeviceSnapshot:
        raw = {
            str(parameter.get("name")): parameter.get("value")
            for parameter in device.get("parameters", [])
            if isinstance(parameter, dict)
        }
        state: dict[str, object] = {}

        state["state"] = str(ac.get_device_on_off_state()).upper()
        mode = _normalized_enum(ac.get_operating_mode())
        state["mode"] = "fan_only" if mode == "fan" else mode
        state["fan_mode"] = _normalized_enum(ac.get_fan_speed())

        optional_getters: tuple[tuple[str, str], ...] = (
            ("target_temperature", "get_target_temperature"),
            ("current_temperature", "get_display_temperature"),
            ("outdoor_temperature", "get_outdoor_temperature"),
        )
        for field, getter_name in optional_getters:
            try:
                value = getattr(ac, getter_name)()
            except (KeyError, TypeError, ValueError, AirstageACError):
                value = None
            if value is not None:
                state[field] = value

        boolean_getters: tuple[tuple[str, str], ...] = (
            ("economy", "get_economy_mode"),
            ("powerful", "get_powerful_mode"),
            ("outdoor_low_noise", "get_outdoor_low_noise"),
            ("energy_save_fan", "get_energy_save_fan"),
            ("minimum_heat", "get_minimum_heat"),
            ("indoor_led", "get_indoor_led"),
            ("human_detection", "get_hmn_detection"),
            ("human_detection_auto_save", "get_hmn_detection_auto_save"),
        )
        for field, getter_name in boolean_getters:
            try:
                value = getattr(ac, getter_name)()
            except (KeyError, TypeError, ValueError, AirstageACError):
                value = None
            if value is not None:
                state[field] = str(value).upper()

        try:
            if str(ac.get_vertical_swing()).upper() == "ON":
                state["swing_mode"] = "vertical_swing"
            else:
                direction = ac.get_vertical_direction()
                if direction is not None:
                    state["swing_mode"] = _normalized_enum(direction)
        except (KeyError, TypeError, ValueError, AirstageACError):
            pass

        for raw_name, field in self._RAW_TO_STATE.items():
            if field in self._CONFIGURABLE_DIAGNOSTICS and field not in self.config.diagnostics:
                continue
            value = raw.get(raw_name)
            unavailable = (
                value is None
                or not str(value).strip()
                or str(value).strip() == constants.CAPABILITY_NOT_AVAILABLE
            )
            if not unavailable:
                state[field] = _raw_number(value)
            elif field in self.config.diagnostics:
                warned: set[str] = getattr(self, "_missing_diagnostics_warned", set())
                if field not in warned:
                    _LOGGER.warning(
                        "Configured diagnostic %s is not currently reported by %s",
                        field,
                        self.config.name,
                    )
                    warned.add(field)
                    self._missing_diagnostics_warned = warned

        model = str(device.get("model") or raw.get(constants.ACParameter.MODEL.value) or "AirStage")
        state["model"] = model
        capabilities: set[str] = getattr(self, "_capabilities", set())
        capabilities.update(state)
        capabilities.update(self.config.diagnostics)
        self._capabilities = capabilities
        return DeviceSnapshot(state=state, capabilities=frozenset(capabilities), model=model)

    def accept(self, command: dict[str, object], current: DeviceSnapshot) -> AcceptedCommand:
        if not command:
            raise CommandError("command must contain at least one property")
        unknown = set(command) - {
            "state",
            "mode",
            "target_temperature",
            "fan_mode",
            "swing_mode",
            *self._BOOLEAN_SETTERS,
        }
        if unknown:
            raise CommandError(f"unknown or read-only properties: {', '.join(sorted(unknown))}")
        unsupported = set(command) - current.capabilities
        # Power and mode are fundamental even if a malformed prior snapshot omitted them.
        unsupported -= {"state", "mode"}
        if unsupported:
            raise CommandError(f"unsupported properties: {', '.join(sorted(unsupported))}")

        values: dict[str, object] = {}
        state = dict(current.state)
        working_power = state.get("state") == "ON"
        turn_off_last = False

        requested_state = command.get("state")
        if requested_state is not None:
            state_text = str(requested_state).strip().upper()
            if state_text == "TOGGLE":
                state_text = "OFF" if current.state.get("state") == "ON" else "ON"
            if state_text == "ON":
                working_power = True
            elif state_text == "OFF":
                turn_off_last = True
            else:
                raise CommandError("state must be ON, OFF, or TOGGLE")
            values["state"] = state_text

        mode_value = command.get("mode")
        if mode_value is not None:
            mode = _normalized_enum(mode_value)
            if mode == "off":
                turn_off_last = True
            else:
                if mode not in self._MODE_TO_LIBRARY:
                    raise CommandError("mode must be off, auto, cool, dry, fan_only, or heat")
                mode = "fan_only" if mode == "fan" else mode
                working_power = True
            values["mode"] = mode

        if "target_temperature" in command:
            if not working_power:
                if self.config.turn_on_before_set_temperature:
                    working_power = True
                else:
                    raise CommandError(
                        "target_temperature cannot be set while the unit is off; "
                        "enable turn_on_before_set_temperature or send state=ON"
                    )
            try:
                target = float(str(command["target_temperature"]))
            except (TypeError, ValueError) as exc:
                raise CommandError("target_temperature must be numeric") from exc
            if not math.isfinite(target):
                raise CommandError("target_temperature must be finite")
            requested_mode = values.get("mode")
            effective_mode = str(
                requested_mode
                if requested_mode is not None and requested_mode != "off"
                else state.get("mode", "")
            )
            if effective_mode == "fan_only":
                raise CommandError("target_temperature cannot be set in fan_only mode")
            limits = self._TEMPERATURE_RANGES.get(effective_mode)
            if limits is not None and not limits[0] <= target <= limits[1]:
                raise CommandError(
                    f"target_temperature must be between {limits[0]} and {limits[1]} "
                    f"in {effective_mode} mode"
                )
            values["target_temperature"] = round(target * 2) / 2

        if "fan_mode" in command:
            fan = _normalized_enum(command["fan_mode"])
            if fan not in self._FAN_TO_LIBRARY:
                raise CommandError("fan_mode must be auto, quiet, low, medium, or high")
            values["fan_mode"] = fan

        if "swing_mode" in command:
            swing = _normalized_enum(command["swing_mode"])
            if swing != "vertical_swing":
                positions = {
                    _normalized_enum(position): position
                    for position in constants.VerticalSwingPositions
                }
                position = positions.get(swing)
                if position is None:
                    raise CommandError(
                        "swing_mode must be vertical_swing or a supported vertical position"
                    )
            values["swing_mode"] = swing

        for field in self._BOOLEAN_SETTERS:
            if field in command:
                boolean = _on_off(command[field], field)
                values[field] = "ON" if boolean is constants.BooleanProperty.ON else "OFF"

        state.update(values)
        state["state"] = "ON" if working_power else "OFF"

        if turn_off_last:
            state["state"] = "OFF"
            state["mode"] = "off"

        return AcceptedCommand(
            values=values,
            snapshot=DeviceSnapshot(
                state=state,
                capabilities=current.capabilities,
                model=current.model,
            ),
        )

    async def apply(self, command: AcceptedCommand) -> None:
        ac = await self._current_ac()
        values = command.values
        requested_state = values.get("state")
        turn_off_last = False
        if requested_state == "ON":
            await ac.turn_on()
        elif requested_state == "OFF":
            turn_off_last = True

        mode = values.get("mode")
        if mode is not None:
            if mode == "off":
                turn_off_last = True
            else:
                library_mode = self._MODE_TO_LIBRARY[str(mode)]
                if str(ac.get_device_on_off_state()).upper() == "OFF":
                    await ac.turn_on()
                await ac.set_operation_mode(library_mode)

        if "target_temperature" in values:
            if str(ac.get_device_on_off_state()).upper() == "OFF":
                if self.config.turn_on_before_set_temperature:
                    await ac.turn_on()
                else:
                    raise CommandError(
                        "target_temperature cannot be set while the unit is off; "
                        "enable turn_on_before_set_temperature or send state=ON"
                    )
            try:
                await ac.set_target_temperature(float(str(values["target_temperature"])))
            except AirstageACError as exc:
                raise CommandError(str(exc)) from exc

        if "fan_mode" in values:
            await ac.set_fan_speed(self._FAN_TO_LIBRARY[str(values["fan_mode"])])

        if "swing_mode" in values:
            swing = str(values["swing_mode"])
            if swing == "vertical_swing":
                await ac.set_vertical_swing(constants.BooleanProperty.ON)
            else:
                positions = {
                    _normalized_enum(position): position
                    for position in constants.VerticalSwingPositions
                }
                await ac.set_vertical_swing(constants.BooleanProperty.OFF)
                await ac.set_vertical_direction(positions[swing])

        for field, setter_name in self._BOOLEAN_SETTERS.items():
            if field in values:
                await getattr(ac, setter_name)(_on_off(values[field], field))

        if turn_off_last:
            await ac.turn_off()

    async def _current_ac(self) -> AirstageAC:
        try:
            devices = await self._api.get_devices()
            return AirstageAC(self.config.device_id, self._api).refresh_parameters(
                data=devices[self.config.device_id]
            )
        except (KeyError, TypeError, ValueError, AirstageACError, airstage_api.ApiError) as exc:
            raise UnitError(f"cannot obtain current state for {self.config.name}: {exc}") from exc
