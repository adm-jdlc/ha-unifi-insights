# Copyright 2026 UniFi Insights contributors
"""Emergency-state endpoints for the UniFi Access developer API."""

from __future__ import annotations

from typing import TYPE_CHECKING

from custom_components.unifi_insights.api.access.errors import ensure_success_envelope
from custom_components.unifi_insights.api.access.models import (
    AccessEmergencyStatus,
    parse_record,
)
from custom_components.unifi_insights.api.exceptions import UniFiValidationError

if TYPE_CHECKING:
    from custom_components.unifi_insights.api.access.client import UniFiAccessClient

_EMERGENCY_PATH = "doors/settings/emergency"


class EmergencyEndpoint:
    """Read and set console-wide Lockdown and Evacuation (Access 1.24.6+)."""

    def __init__(self, client: UniFiAccessClient) -> None:
        """Initialize the endpoint group."""
        self._client = client

    async def get(self) -> AccessEmergencyStatus:
        """Return the current emergency state (``view:space``)."""
        response = await self._client._get(_EMERGENCY_PATH)
        return parse_record(response, AccessEmergencyStatus, _EMERGENCY_PATH)

    async def set(self, *, lockdown: bool, evacuation: bool) -> None:
        """
        Set the emergency state (``edit:space``).

        ``lockdown`` keeps every door locked. ``evacuation`` keeps every door
        unlocked. Both False restores normal operation. The official API
        accepts both flags in one request, so the caller reads the state back
        after each change instead of trusting the request.
        """
        # Preserve runtime validation for callers outside the typed coordinator.
        values: tuple[object, object] = (lockdown, evacuation)
        if not all(isinstance(value, bool) for value in values):
            msg = "Lockdown and evacuation must be booleans"
            raise UniFiValidationError(msg)
        response = await self._client._put(
            _EMERGENCY_PATH,
            json_data={"lockdown": lockdown, "evacuation": evacuation},
            log_body=False,
        )
        ensure_success_envelope(response, _EMERGENCY_PATH)
