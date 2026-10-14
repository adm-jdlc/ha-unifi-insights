# Copyright 2026 UniFi Insights contributors
"""Verify optional Access failures do not interrupt other console applications."""

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components import webhook
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_registry import async_get as async_get_entity_registry

from custom_components.unifi_insights import (
    _async_setup_access,
    async_remove_entry,
    async_unload_entry,
)
from custom_components.unifi_insights.access_entity import access_unique_id
from custom_components.unifi_insights.api import (
    UniFiAuthenticationError,
    UniFiConnectionError,
    UniFiNotFoundError,
    UniFiValidationError,
)
from custom_components.unifi_insights.api.access.models import AccessWebhookEndpoint
from custom_components.unifi_insights.const import (
    CONF_ACCESS_TOKEN,
    CONF_ACCESS_WEBHOOK,
    CONF_ACCESS_WEBHOOK_ID,
    CONF_CONSOLE_ID,
    DOMAIN,
)
from custom_components.unifi_insights.probe import ProbeResult, ProbeStatus
from tests.test_access_coordinator import DEVICES, DOORS, NAMESPACE, _client


@pytest.mark.parametrize(
    "error",
    [
        UniFiAuthenticationError("Rejected", status_code=401),
        UniFiAuthenticationError("Denied", status_code=403),
        UniFiConnectionError("Unavailable"),
        UniFiNotFoundError("Absent", status_code=404),
    ],
)
async def test_access_probe_failure_preserves_existing_applications(
    *,
    hass,
    mock_config_entry,
    mock_network_client,
    mock_protect_client,
    mock_local_auth,
    enable_custom_integrations,
    error,
):
    """A rejected or unreachable Access application never starts entry reauth."""
    client = MagicMock()
    client.doors.get_all = AsyncMock(side_effect=error)
    client.close = AsyncMock()
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry,
        data={**mock_config_entry.data, CONF_ACCESS_TOKEN: "test-access-token"},
    )
    with patch(
        "custom_components.unifi_insights.UniFiAccessClient", return_value=client
    ):
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert mock_config_entry.runtime_data.access_coordinator is None
    assert mock_config_entry.runtime_data.protect_coordinator is not None
    assert mock_config_entry.runtime_data.network_client is mock_network_client
    client.close.assert_awaited_once()


async def test_access_is_not_created_without_a_token(
    *,
    hass,
    mock_config_entry,
    mock_network_client,
    mock_protect_client,
    mock_local_auth,
    enable_custom_integrations,
):
    """Existing entries keep their behavior and do not open an Access connection."""
    mock_config_entry.add_to_hass(hass)
    with patch("custom_components.unifi_insights.UniFiAccessClient") as client:
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()
    client.assert_not_called()
    assert mock_config_entry.runtime_data.access_coordinator is None
    assert mock_config_entry.runtime_data.access_client is None


@pytest.mark.parametrize("auth_failed", [False, True])
async def test_access_first_refresh_failure_closes_client(
    hass, mock_config_entry, auth_failed
):
    """A failed first refresh releases resources and remains isolated."""
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry,
        data={**mock_config_entry.data, CONF_ACCESS_TOKEN: "test-access-token"},
    )
    client = MagicMock()
    client.close = AsyncMock()
    coordinator = MagicMock()
    coordinator.auth_failed = auth_failed
    coordinator.async_config_entry_first_refresh = AsyncMock(
        side_effect=None if auth_failed else ConfigEntryNotReady
    )
    with (
        patch(
            "custom_components.unifi_insights.UniFiAccessClient", return_value=client
        ),
        patch(
            "custom_components.unifi_insights.async_probe_access",
            return_value=ProbeResult(ProbeStatus.EMPTY),
        ),
        patch(
            "custom_components.unifi_insights.UnifiInsightsAccessCoordinator",
            return_value=coordinator,
        ),
    ):
        assert await _async_setup_access(
            hass,
            mock_config_entry,
            async_get_clientsession(hass),
            verify_ssl=False,
        ) == (None, None)
    client.close.assert_awaited_once()
    coordinator.async_start_websocket.assert_not_called()


async def test_access_close_failure_does_not_block_other_applications(
    *,
    hass,
    mock_config_entry,
    mock_network_client,
    mock_protect_client,
    mock_local_auth,
    enable_custom_integrations,
):
    """Even an Access teardown error cannot prevent Network and Protect setup."""
    client = MagicMock()
    client.doors.get_all = AsyncMock(side_effect=UniFiConnectionError("Unavailable"))
    client.close = AsyncMock(side_effect=RuntimeError("Close failed"))
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry,
        data={**mock_config_entry.data, CONF_ACCESS_TOKEN: "test-access-token"},
    )
    with patch(
        "custom_components.unifi_insights.UniFiAccessClient", return_value=client
    ):
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.LOADED


async def test_invalid_saved_access_settings_preserve_existing_applications(
    *,
    hass,
    mock_config_entry,
    mock_network_client,
    mock_protect_client,
    mock_local_auth,
    enable_custom_integrations,
):
    """Client validation errors remain isolated, including before a probe."""
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry,
        data={**mock_config_entry.data, CONF_ACCESS_TOKEN: "invalid-stored-token"},
    )
    with patch(
        "custom_components.unifi_insights.UniFiAccessClient",
        side_effect=UniFiValidationError("Invalid saved settings"),
    ):
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()
    assert mock_config_entry.runtime_data.access_coordinator is None
    assert mock_config_entry.runtime_data.protect_coordinator is not None


@pytest.mark.parametrize("forward_failure", [False, True])
async def test_access_resources_follow_the_entry_lifecycle(
    *,
    hass,
    mock_config_entry,
    mock_network_client,
    mock_protect_client,
    mock_local_auth,
    enable_custom_integrations,
    forward_failure,
):
    """Real HA setup, unload and failed forwarding release optional resources."""
    client = _client(doors=DOORS, devices=DEVICES)
    client.webhooks.get_all = AsyncMock(return_value=[])
    client.webhooks.create = AsyncMock(
        return_value=AccessWebhookEndpoint.model_validate(
            {"id": "endpoint-lifecycle", "secret": "test-lifecycle-signing-secret"}
        )
    )
    client.webhooks.delete = AsyncMock()
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry,
        data={
            **mock_config_entry.data,
            CONF_ACCESS_TOKEN: "test-access-token",
            CONF_CONSOLE_ID: NAMESPACE,
        },
        options={**mock_config_entry.options, CONF_ACCESS_WEBHOOK: True},
    )

    async def _run_socket(*_args, **_kwargs):
        await asyncio.Event().wait()

    with (
        patch(
            "custom_components.unifi_insights.UniFiAccessClient", return_value=client
        ),
        patch(
            "custom_components.unifi_insights.access_webhook.webhook.async_generate_url",
            return_value="http://ha.test.local:8123/api/webhook/test-lifecycle",
        ),
        patch(
            "custom_components.unifi_insights.coordinators.access."
            "AccessNotificationSocket.run",
            side_effect=_run_socket,
        ),
    ):
        if forward_failure:
            with patch.object(
                hass.config_entries,
                "async_forward_entry_setups",
                side_effect=RuntimeError("Platform forwarding failed"),
            ):
                assert not await hass.config_entries.async_setup(
                    mock_config_entry.entry_id
                )
            await hass.async_block_till_done()
            assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR
            client.close.assert_awaited_once()
        else:
            assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
            await hass.async_block_till_done()
            data = mock_config_entry.runtime_data
            assert data.access_client is client
            assert data.access_coordinator.webhook_active
            task = data.access_coordinator._ws_task
            assert task is not None
            assert not task.done()
            with patch.object(
                hass.config_entries, "async_unload_platforms", return_value=False
            ):
                assert not await async_unload_entry(hass, mock_config_entry)
            assert not task.done()
            assert data.access_coordinator.webhook_active
            client.close.assert_not_awaited()
            entity_id = async_get_entity_registry(hass).async_get_entity_id(
                "lock",
                DOMAIN,
                access_unique_id(NAMESPACE, "door-plain", "lock"),
            )
            assert entity_id is not None
            assert hass.states.get(entity_id).state == "unlocked"
            client.doors.unlock.side_effect = UniFiAuthenticationError(
                "Rejected", status_code=401
            )
            with pytest.raises(HomeAssistantError):
                await data.access_coordinator.async_unlock_door("door-plain")
            await hass.async_block_till_done()
            assert hass.states.get(entity_id).state == "unavailable"
            assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
            await hass.async_block_till_done()
            assert task.cancelled()
            assert not data.access_coordinator.webhook_active
            assert client.close.await_count >= 1

    assert (
        mock_config_entry.data[CONF_ACCESS_WEBHOOK_ID] not in hass.data[webhook.DOMAIN]
    )
    # Unloading keeps the remote endpoint, so a reload can reuse it.
    client.webhooks.delete.assert_not_awaited()


async def test_removing_access_entry_also_cleans_up_remote_webhook(
    hass, mock_config_entry
):
    """Entry removal invokes Access cleanup without changing other cleanup hooks."""
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry,
        data={**mock_config_entry.data, CONF_ACCESS_TOKEN: "test-access-token"},
    )
    with patch(
        "custom_components.unifi_insights.async_remove_access_webhook"
    ) as cleanup:
        await async_remove_entry(hass, mock_config_entry)
    cleanup.assert_awaited_once_with(hass, mock_config_entry)


@pytest.mark.parametrize(
    "record",
    [
        {"id": 123, "secret": "fixture-malformed-secret"},
        {"id": "broken-endpoint", "secret": {"value": "fixture-malformed-secret"}},
    ],
)
async def test_malformed_webhook_response_is_sanitized_and_isolated(
    *,
    hass,
    mock_config_entry,
    mock_network_client,
    mock_protect_client,
    mock_local_auth,
    enable_custom_integrations,
    caplog,
    record,
):
    """Actual endpoint parsing never leaks input or prevents other apps loading."""
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry,
        data={
            **mock_config_entry.data,
            CONF_ACCESS_TOKEN: "test-access-token",
            CONF_CONSOLE_ID: NAMESPACE,
        },
        options={CONF_ACCESS_WEBHOOK: True},
    )

    async def _get_response(path, **_kwargs):
        data = {
            "doors": [{"id": "door-native", "is_bind_hub": True}],
            "devices": [],
            "doors/settings/emergency": {"lockdown": False, "evacuation": False},
            "webhooks/endpoints": [record],
        }
        return {"code": "SUCCESS", "data": data[path]}

    async def _run_socket(*_args, **_kwargs):
        await asyncio.Event().wait()

    with (
        patch(
            "custom_components.unifi_insights.api.access.client.UniFiAccessClient._get",
            side_effect=_get_response,
        ),
        patch(
            "custom_components.unifi_insights.access_webhook.webhook.async_generate_url",
            return_value="http://ha.test.local:8123/api/webhook/test-malformed",
        ),
        patch(
            "custom_components.unifi_insights.coordinators.access."
            "AccessNotificationSocket.run",
            side_effect=_run_socket,
        ),
        caplog.at_level(logging.DEBUG),
    ):
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()
        assert mock_config_entry.state is ConfigEntryState.LOADED
        data = mock_config_entry.runtime_data
        assert data.access_coordinator is not None
        assert not data.access_coordinator.webhook_active
        assert data.protect_coordinator is not None
        assert data.network_client is mock_network_client
        assert "fixture-malformed-secret" not in caplog.text
        assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
