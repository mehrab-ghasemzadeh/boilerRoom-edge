"""
command.execute handling (see DEVICE.md).

The server queues a command, pushes ``command.execute``, and expects
``command.ack`` immediately followed by ``command.result``. Commands are
re-dispatched after every hello for anything still queued/sent, so execution
must be idempotent: results are cached by command_id and replayed rather than
re-run.

Relay actuation goes through the schedule module's target->relay mapping, so
commands and schedules drive exactly the same relays.
"""

from __future__ import annotations

import datetime
from collections import OrderedDict
from typing import Any, NamedTuple

from config import RELAYS
from device_config import config_store
from schedule_runner import Target, relay_for_target, schedule_runner

# How many command outcomes to remember for replay on re-dispatch.
PROCESSED_HISTORY = 64

VALID_MODES = ("automatic", "manual")

STATUS_EXECUTED = "executed"
STATUS_FAILED = "failed"
STATUS_EXPIRED = "expired"


class CommandOutcome(NamedTuple):
    status: str
    reported_state: dict[str, Any] | None = None
    error: str | None = None
    detail: str | None = None

    @property
    def accepted(self) -> bool:
        return self.status == STATUS_EXECUTED

    def result_payload(self, command_id: str) -> dict[str, Any]:
        payload: dict[str, Any] = {"command_id": command_id, "status": self.status}
        if self.reported_state is not None:
            payload["reported_state"] = self.reported_state
        if self.error:
            payload["error"] = self.error
        if self.detail:
            payload["detail"] = self.detail
        return payload


def _failed(message: str) -> CommandOutcome:
    return CommandOutcome(STATUS_FAILED, error=message)


def _target_index(target: dict[str, Any], key: str) -> int | None:
    value = target.get(key)
    return value if isinstance(value, int) else None


def _relay_with_role(role: str) -> int | None:
    """First relay carrying a role that is not tied to a unit (alarm, light)."""
    for relay_id, cfg in sorted(RELAYS.items()):
        if cfg.get("role") == role:
            return relay_id
    return None


async def _switch_target(
    state,
    kind: str,
    index_key: str,
    target: dict[str, Any],
    turn_on: bool,
) -> CommandOutcome:
    index = _target_index(target, index_key)
    if index is None:
        return _failed(f"target.{index_key} is required")

    relay_controller = state.relay_controller
    if relay_controller is None:
        return _failed("relay controller is not ready")

    schedule_target = Target(kind, index)
    relay_id = relay_for_target(schedule_target)
    if relay_id is None:
        return _failed(f"no relay mapped for {kind} {index}")

    if turn_on and await state.is_limit_blocked(schedule_target):
        return _failed(
            f"{kind} {index} is cut off by a temperature limit and cannot be switched on"
        )

    if turn_on:
        await relay_controller.turn_on(relay_id)
    else:
        await relay_controller.turn_off(relay_id)

    # command.result carries reported_state, but DEVICE.md has telemetry as
    # what actually settles a unit's stored state, so post that too.
    await state.notify_state_change(
        f"relay {relay_id} {'on' if turn_on else 'off'} (command)"
    )

    return CommandOutcome(
        STATUS_EXECUTED,
        reported_state={index_key: index, "state": "on" if turn_on else "off"},
    )


async def _set_mode(
    state,
    kind: str,
    index_key: str,
    target: dict[str, Any],
    parameters: dict[str, Any],
) -> CommandOutcome:
    index = _target_index(target, index_key)
    if index is None:
        return _failed(f"target.{index_key} is required")

    mode = str(parameters.get("mode", "")).strip().lower()
    if mode not in VALID_MODES:
        return _failed(f"parameters.mode must be one of {list(VALID_MODES)}")

    schedule_target = Target(kind, index)
    if relay_for_target(schedule_target) is None:
        return _failed(f"no relay mapped for {kind} {index}")

    await state.set_mode(schedule_target, mode)

    if mode == "automatic":
        # Let the schedule re-assert this target on the next evaluation instead
        # of waiting for its next transition.
        schedule_runner.forget(schedule_target)
        await schedule_runner.evaluate(state)

    return CommandOutcome(
        STATUS_EXECUTED,
        reported_state={index_key: index, "mode": mode},
    )


async def build_state_payload(state) -> dict[str, Any]:
    """
    The ``device.state`` body: everything this device currently believes.

    Shared by the ``device.request_state`` command and by the push that follows
    a local relay change, so a dashboard sees the same shape either way.
    """
    snapshot = await state.get_snapshot()
    relay_controller = state.relay_controller

    relays = {}
    if relay_controller is not None:
        relays = {
            str(relay_id): "on" if relay_controller.get_state(relay_id) else "off"
            for relay_id in sorted(RELAYS)
        }

    config_version, schedule_version = await state.get_active_versions()
    read_at = snapshot["read_at"]

    return {
        "temperatures_c": {str(k): v for k, v in snapshot["temperatures"].items()},
        "gas": {str(k): v for k, v in snapshot["gas"].items()},
        "relays": relays,
        "modes": {str(t): m for t, m in (await state.get_modes()).items()},
        "active_config_version": config_version,
        "active_schedule_version": schedule_version,
        # A schedule edited on the device keeps the published version number —
        # there is no API to tell the server otherwise — so this is the only
        # way a dashboard can see that the room is not running v<n> as published.
        "schedule_locally_modified": schedule_runner.is_locally_modified,
        "schedule_local_revision": schedule_runner.local_revision,
        "config_locally_modified": config_store.is_locally_modified,
        "config_local_revision": config_store.local_revision,
        "read_at": read_at.isoformat() if read_at else None,
    }


async def _request_state(state, session) -> CommandOutcome:
    await session.send_message("device.state", await build_state_payload(state))
    return CommandOutcome(STATUS_EXECUTED, detail="device.state pushed")


async def _restart_service(state, parameters: dict[str, Any]) -> CommandOutcome:
    """
    Soft restart: reload the device mapping and drop the WebSocket so the
    client reconnects. This process is not run under a supervisor here, so it
    deliberately does not terminate itself.
    """
    from config import load_device_mapping

    service = str(parameters.get("service") or "edge")
    try:
        await load_device_mapping()
    except Exception as exc:
        return _failed(f"mapping reload failed: {exc}")

    await state.request_ws_reconnect()
    return CommandOutcome(
        STATUS_EXECUTED,
        detail=f"{service}: mapping reloaded and WebSocket reconnecting",
    )


async def _acknowledge_alarm(state) -> CommandOutcome:
    relay_controller = state.relay_controller
    if relay_controller is None:
        return _failed("relay controller is not ready")

    relay_id = _relay_with_role("alarm")
    if relay_id is None:
        return _failed("no relay mapped with role 'alarm'")

    await relay_controller.turn_off(relay_id)
    await state.notify_state_change(f"relay {relay_id} off (alarm acknowledged)")
    return CommandOutcome(
        STATUS_EXECUTED,
        reported_state={"alarm": "off"},
        detail=f"relay {relay_id} cleared",
    )


def _parse_expires_at(raw: Any) -> datetime.datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


class CommandExecutor:
    """Executes commands once, and replays outcomes for repeat dispatches."""

    def __init__(self) -> None:
        self._processed: OrderedDict[str, CommandOutcome] = OrderedDict()

    def previous_outcome(self, command_id: str) -> CommandOutcome | None:
        return self._processed.get(command_id)

    def _remember(self, command_id: str, outcome: CommandOutcome) -> None:
        self._processed[command_id] = outcome
        self._processed.move_to_end(command_id)
        while len(self._processed) > PROCESSED_HISTORY:
            self._processed.popitem(last=False)

    async def execute(
        self,
        payload: dict[str, Any],
        state,
        session,
        *,
        expires_at: Any = None,
    ) -> CommandOutcome:
        command_id = str(payload.get("command_id") or "")
        name = str(payload.get("name") or "")
        target = payload.get("target") or {}
        parameters = payload.get("parameters") or {}

        if not isinstance(target, dict) or not isinstance(parameters, dict):
            return _failed("'target' and 'parameters' must be objects")

        deadline = _parse_expires_at(expires_at)
        if deadline is not None and datetime.datetime.now(datetime.UTC) > deadline:
            outcome = CommandOutcome(STATUS_EXPIRED, error=f"expired at {expires_at}")
            if command_id:
                self._remember(command_id, outcome)
            return outcome

        try:
            outcome = await self._dispatch(name, target, parameters, state, session)
        except Exception as exc:
            outcome = _failed(f"{type(exc).__name__}: {exc}")

        if command_id:
            self._remember(command_id, outcome)
            await state.set_last_processed_command_id(command_id)

        return outcome

    async def _dispatch(
        self,
        name: str,
        target: dict[str, Any],
        parameters: dict[str, Any],
        state,
        session,
    ) -> CommandOutcome:
        if name == "boiler.turn_on":
            return await _switch_target(state, "boiler", "boiler_index", target, True)
        if name == "boiler.turn_off":
            return await _switch_target(state, "boiler", "boiler_index", target, False)
        if name == "boiler.set_mode":
            return await _set_mode(state, "boiler", "boiler_index", target, parameters)
        if name == "pump.turn_on":
            return await _switch_target(state, "pump", "pump_index", target, True)
        if name == "pump.turn_off":
            return await _switch_target(state, "pump", "pump_index", target, False)
        if name == "pump.set_mode":
            return await _set_mode(state, "pump", "pump_index", target, parameters)
        if name == "device.request_state":
            return await _request_state(state, session)
        if name == "device.restart_service":
            return await _restart_service(state, parameters)
        if name == "alarm.acknowledge_local":
            return await _acknowledge_alarm(state)

        return _failed(f"unsupported command {name!r}")


command_executor = CommandExecutor()
