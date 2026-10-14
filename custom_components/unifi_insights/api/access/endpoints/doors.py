# Copyright 2026 UniFi Insights contributors
"""Door endpoints for the UniFi Access developer API."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from custom_components.unifi_insights.api.access.errors import ensure_success_envelope
from custom_components.unifi_insights.api.access.models import (
    AccessDoor,
    parse_record,
    parse_records,
)
from custom_components.unifi_insights.api.exceptions import UniFiValidationError

from .common import safe_id

if TYPE_CHECKING:
    from custom_components.unifi_insights.api.access.client import UniFiAccessClient
    from custom_components.unifi_insights.api.access.models.emergency import (
        LockRuleType,
    )

# Motor commands for gate hubs. The official API documents them as the
# ``control_cmd`` query parameter on the unlock endpoint.
GateCommand = Literal["open", "close", "stop"]
GATE_COMMANDS: tuple[GateCommand, ...] = ("open", "close", "stop")
# Only a custom lock rule takes a duration; the others must not send one.
_CUSTOM_RULE: LockRuleType = "custom"


class DoorsEndpoint:
    """Read doors, unlock them, and set temporary lock rules."""

    def __init__(self, client: UniFiAccessClient) -> None:
        """Initialize the endpoint group."""
        self._client = client

    async def get_all(self) -> list[AccessDoor]:
        """Return every door (``view:space``)."""
        response = await self._client._get("doors")
        return parse_records(response, AccessDoor, "doors")

    async def get(self, door_id: str) -> AccessDoor:
        """Return one door by ID (``view:space``)."""
        path = f"doors/{safe_id(door_id)}"
        response = await self._client._get(path)
        return parse_record(response, AccessDoor, path)

    async def unlock(
        self,
        door_id: str,
        *,
        control_cmd: GateCommand | None = None,
    ) -> None:
        """
        Remotely unlock a door, or drive a gate hub's motor (``edit:space``).

        Without ``control_cmd`` this is the documented remote unlock. With
        ``control_cmd`` ("open", "close" or "stop") it drives a gate. The
        command travels as the ``control_cmd`` query parameter, as the
        third-party ``pyua`` client sends it. This is not in the 2.x official
        PDF, so a gate command should be treated as unverified on hardware.
        """
        if control_cmd is not None and control_cmd not in GATE_COMMANDS:
            msg = "Gate command must be open, close or stop"
            raise UniFiValidationError(msg)
        path = f"doors/{safe_id(door_id)}/unlock"
        params = {"control_cmd": control_cmd} if control_cmd is not None else None
        response = await self._client._put(path, params=params, log_body=False)
        ensure_success_envelope(response, path)

    async def set_lock_rule(
        self,
        door_id: str,
        rule_type: LockRuleType,
        *,
        interval: int | None = None,
    ) -> None:
        """
        Set a temporary lock rule (``edit:space``, Access 1.24.6+).

        ``interval`` is in minutes and only applies to ``custom``. A ``custom``
        rule requires it. Other rule types must not pass it. The result is not
        returned: the coordinator reads the relay state back afterwards, so a
        rule the console did not apply is never shown as applied.
        """
        body: dict[str, Any] = {"type": rule_type}
        if rule_type == _CUSTOM_RULE:
            if interval is None or interval < 1:
                msg = "A custom lock rule needs an interval of at least one minute"
                raise UniFiValidationError(msg)
            body["interval"] = interval
        elif interval is not None:
            msg = "Only a custom lock rule takes an interval"
            raise UniFiValidationError(msg)

        path = f"doors/{safe_id(door_id)}/lock_rule"
        response = await self._client._put(path, json_data=body, log_body=False)
        ensure_success_envelope(response, path)
