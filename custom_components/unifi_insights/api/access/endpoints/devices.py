# Copyright 2026 UniFi Insights contributors
"""Device endpoints for the UniFi Access developer API."""

from __future__ import annotations

from typing import TYPE_CHECKING

from custom_components.unifi_insights.api.access.models import (
    AccessDevice,
    parse_records,
)

if TYPE_CHECKING:
    from custom_components.unifi_insights.api.access.client import UniFiAccessClient


class DevicesEndpoint:
    """List Access devices (hubs, readers and intercoms)."""

    def __init__(self, client: UniFiAccessClient) -> None:
        """Initialize the endpoint group."""
        self._client = client

    async def get_all(self) -> list[AccessDevice]:
        """Return every Access device (``view:device``)."""
        response = await self._client._get("devices")
        return parse_records(response, AccessDevice, "devices")
