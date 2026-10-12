"""Real-registry regressions for console-scoped logical-site identities."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import attr
import pytest
from homeassistant.const import CONF_API_KEY, CONF_HOST, CONF_VERIFY_SSL
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.unifi_insights import (
    button as button_platform,
)
from custom_components.unifi_insights import (
    image as image_platform,
)
from custom_components.unifi_insights import (
    number as number_platform,
)
from custom_components.unifi_insights import (
    sensor as sensor_platform,
)
from custom_components.unifi_insights.config_flow import UnifiInsightsConfigFlow
from custom_components.unifi_insights.console_identity import (
    build_legacy_site_unique_id,
    build_site_unique_id,
    normalize_console_namespace,
    resolve_console_namespace,
)
from custom_components.unifi_insights.const import (
    CONF_CONNECTION_TYPE,
    CONF_CONSOLE_ID,
    CONNECTION_TYPE_LOCAL,
    CONNECTION_TYPE_REMOTE,
    DOMAIN,
    SITE_ENTITY_SUFFIXES,
)
from custom_components.unifi_insights.helpers import (
    SiteEntityIdentityError,
    async_migrate_site_entity,
    async_rekey_site_entities,
    owned_site_entity_unique_id,
)
from custom_components.unifi_insights.internet_activity_snapshot import (
    _resolve_entity_ids,
)
from custom_components.unifi_insights.number import VOUCHER_NUMBER_DESCRIPTIONS
from custom_components.unifi_insights.sensor import SITE_CLIENT_SENSOR_TYPES
from custom_components.unifi_insights.site_internet_activity_sensor import (
    SITE_INTERNET_ACTIVITY_SENSOR_TYPES,
)
from custom_components.unifi_insights.voucher_sensor import VOUCHER_SENSOR_TYPES

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


def _entry(hass, namespace="console_a", entry_id="entry_a"):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_CONSOLE_ID: namespace, CONF_API_KEY: "synthetic_credential"},
        entry_id=entry_id,
        unique_id=namespace,
    )
    entry.add_to_hass(hass)
    return entry


def _row(registry, entry, domain, unique_id, key, **kwargs):
    return registry.async_get_or_create(
        domain, DOMAIN, unique_id, config_entry=entry, translation_key=key, **kwargs
    )


@pytest.mark.parametrize(
    ("value", "credentials", "expected"),
    [
        ("  AA-BB-CC-DD-EE-FF  ", (), "aa:bb:cc:dd:ee:ff"),
        (" CLOUD_ID ", (), "cloud_id"),
        ("", (), None),
        (" \t ", (), None),
        (None, (), None),
        (123, (), None),
        ("bad id", (), None),
        ("bad\x00id", (), None),
        ("API-KEY", ("API-KEY",), None),
        ("api:key", (" API-KEY ",), None),
        ("API-KEY", ("api:key",), None),
        ("console", (None, "", 5), "console"),
    ],
)
def test_namespace_validation(value, credentials, expected):
    assert normalize_console_namespace(value, credentials=credentials) == expected


def test_namespace_uses_only_stored_console(hass):
    entry = _entry(hass)
    assert resolve_console_namespace(entry) == "console_a"
    hass.config_entries.async_update_entry(
        entry, data={CONF_API_KEY: "key", "host": "host", "console_name": "Display"}
    )
    assert resolve_console_namespace(entry) is None
    with pytest.raises(ValueError, match="validated console namespace"):
        build_site_unique_id(None, "site_shared", "key")
    assert build_legacy_site_unique_id("site_shared", "key") == "site_shared_key"


@pytest.mark.parametrize(
    ("domain", "key"),
    [
        (domain, key)
        for domain, keys in SITE_ENTITY_SUFFIXES.items()
        for key in sorted(keys)
    ],
)
async def test_all_families_migrate_preserving_metadata(
    hass, entity_registry, device_registry, area_registry, domain, key
):
    entry = _entry(hass)
    area = area_registry.async_create("Synthetic area")
    device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, "site_site_shared")}
    )
    legacy = build_legacy_site_unique_id("site_shared", key)
    row = _row(entity_registry, entry, domain, legacy, key, device_id=device.id)
    row = entity_registry.async_update_entity(
        row.entity_id,
        name="Custom name",
        area_id=area.id,
        disabled_by=er.RegistryEntryDisabler.USER,
        hidden_by=er.RegistryEntryHider.USER,
    )
    before = attr.asdict(row)
    target = build_site_unique_id("console_a", "site_shared", key)
    assert (
        async_migrate_site_entity(hass, entry, domain, "console_a", "site_shared", key)
        == target
    )
    after = attr.asdict(entity_registry.async_get(row.entity_id))
    assert {
        k: v
        for k, v in after.items()
        if k not in {"unique_id", "previous_unique_id", "modified_at"}
    } == {
        k: v
        for k, v in before.items()
        if k not in {"unique_id", "previous_unique_id", "modified_at"}
    }
    assert (
        async_migrate_site_entity(hass, entry, domain, "console_a", "site_shared", key)
        == target
    )
    assert attr.asdict(entity_registry.async_get(row.entity_id)) == after


@pytest.mark.parametrize("target_owner", ["same", "foreign"])
async def test_owned_legacy_priority_over_occupied_target(
    hass, entity_registry, caplog, target_owner
):
    entry = _entry(hass)
    other = _entry(hass, "console_b", "entry_b")
    key = "internet_download_1h"
    legacy = _row(entity_registry, entry, "sensor", "site_shared_" + key, key)
    target = _row(
        entity_registry,
        entry if target_owner == "same" else other,
        "sensor",
        "console_a_site_shared_" + key,
        key,
        disabled_by=er.RegistryEntryDisabler.USER,
    )
    assert (
        async_migrate_site_entity(
            hass, entry, "sensor", "console_a", "site_shared", key
        )
        == legacy.unique_id
    )
    assert _resolve_entity_ids(
        entity_registry,
        entry_id=entry.entry_id,
        namespace="console_a",
        site_id="site_shared",
    ) == {"download_1h": legacy.entity_id}
    assert (
        entity_registry.async_get(target.entity_id).config_entry_id
        == target.config_entry_id
    )
    assert "target identity is occupied" in caplog.text


async def test_foreign_rows_missing_namespace_and_occupied_target(
    hass, entity_registry
):
    entry = _entry(hass)
    other = _entry(hass, "console_b", "entry_b")
    key = "site_total_clients"
    legacy = _row(entity_registry, other, "sensor", "site_shared_" + key, key)
    assert (
        async_migrate_site_entity(hass, entry, "sensor", None, "site_shared", key)
        is None
    )
    assert (
        async_migrate_site_entity(
            hass, entry, "sensor", "console_a", "site_shared", key
        )
        == "console_a_site_shared_" + key
    )
    target = _row(
        entity_registry,
        other,
        "sensor",
        "console_a_site_shared_" + key,
        key,
        disabled_by=er.RegistryEntryDisabler.USER,
    )
    assert (
        async_migrate_site_entity(
            hass, entry, "sensor", "console_a", "site_shared", key
        )
        is None
    )
    assert entity_registry.async_get(legacy.entity_id).config_entry_id == other.entry_id
    assert entity_registry.async_get(target.entity_id).config_entry_id == other.entry_id
    assert (
        _resolve_entity_ids(
            entity_registry,
            entry_id=entry.entry_id,
            namespace="console_a",
            site_id="site_shared",
        )
        == {}
    )
    own = _row(entity_registry, entry, "sensor", "site_other_" + key, key)
    assert (
        async_migrate_site_entity(hass, entry, "sensor", None, "site_other", key)
        == own.unique_id
    )
    assert (
        async_migrate_site_entity(hass, entry, "sensor", "site", "other", key)
        == own.unique_id
    )


async def test_migration_race_keeps_owned_legacy(hass, entity_registry, caplog):
    entry = _entry(hass)
    row = _row(
        entity_registry,
        entry,
        "sensor",
        "site_shared_active_vouchers",
        "active_vouchers",
    )
    with patch.object(
        entity_registry, "async_update_entity", side_effect=ValueError("conflict")
    ):
        assert (
            async_migrate_site_entity(
                hass, entry, "sensor", "console_a", "site_shared", "active_vouchers"
            )
            == row.unique_id
        )
    assert "registry update failed" in caplog.text
    assert entity_registry.async_get(row.entity_id).unique_id == row.unique_id


@pytest.mark.parametrize("metadata_changes", [False, True])
async def test_migration_value_error_after_native_update_uses_actual_owned_identity(
    hass, entity_registry, caplog, metadata_changes
):
    entry = _entry(hass)
    key = "active_vouchers"
    row = _row(entity_registry, entry, "sensor", "site_shared_" + key, key)
    native = entity_registry.async_update_entity

    def update(entity_id, **kwargs):
        native(
            entity_id,
            **kwargs,
            **({"translation_key": "physical_key"} if metadata_changes else {}),
        )
        err_msg = "Synthetic post-mutation ValueError with an opaque marker"
        raise ValueError(err_msg)

    with patch.object(entity_registry, "async_update_entity", side_effect=update):
        effective = async_migrate_site_entity(
            hass, entry, "sensor", "console_a", "site_shared", key
        )
    assert effective == (None if metadata_changes else "console_a_site_shared_" + key)
    assert (
        entity_registry.async_get(row.entity_id).unique_id
        == "console_a_site_shared_" + key
    )
    assert entity_registry.async_get_entity_id("sensor", DOMAIN, row.unique_id) is None
    assert len(entity_registry.entities) == 1
    assert "opaque marker" not in caplog.text


async def test_unexpected_migration_failure_propagates_and_later_discovery_is_safe(
    hass, entity_registry
):
    entry = _entry(hass)
    row = _row(
        entity_registry,
        entry,
        "sensor",
        "site_shared_active_vouchers",
        "active_vouchers",
    )
    native = entity_registry.async_update_entity

    def update(*args, **kwargs):
        native(*args, **kwargs)
        err_msg = "Synthetic unexpected post-mutation failure"
        raise RuntimeError(err_msg)

    with (
        patch.object(entity_registry, "async_update_entity", side_effect=update),
        pytest.raises(RuntimeError, match="unexpected"),
    ):
        async_migrate_site_entity(
            hass, entry, "sensor", "console_a", "site_shared", "active_vouchers"
        )
    assert (
        async_migrate_site_entity(
            hass, entry, "sensor", "console_a", "site_shared", "active_vouchers"
        )
        == "console_a_site_shared_active_vouchers"
    )
    assert (
        entity_registry.async_get(row.entity_id).unique_id
        == "console_a_site_shared_active_vouchers"
    )


async def test_missing_lookup_record_is_not_claimed():
    registry = MagicMock()
    registry.async_get_entity_id.side_effect = [None, "sensor.gone"]
    registry.async_get.return_value = None
    assert (
        owned_site_entity_unique_id(
            registry, "entry", "sensor", "legacy", "target", "key"
        )
        is None
    )
    registry.async_get_entity_id.side_effect = ["sensor.gone", None]
    assert (
        owned_site_entity_unique_id(
            registry, "entry", "sensor", "legacy", "target", "key"
        )
        == "target"
    )
    registry.async_get_entity_id.side_effect = ["sensor.legacy", None, None]
    registry.async_get.return_value = SimpleNamespace(
        config_entry_id="entry", translation_key="key"
    )
    with patch(
        "custom_components.unifi_insights.helpers.er.async_get", return_value=registry
    ):
        assert (
            async_migrate_site_entity(
                MagicMock(),
                SimpleNamespace(entry_id="entry"),
                "sensor",
                "console",
                "site",
                "key",
            )
            is None
        )


async def test_rekey_exact_families_preserves_physical_resource_and_foreign_rows(
    hass, entity_registry
):
    entry = _entry(hass)
    other = _entry(hass, "console_b", "entry_b")
    expected = []
    for domain, keys in SITE_ENTITY_SUFFIXES.items():
        for key in keys:
            row = _row(
                entity_registry,
                entry,
                domain,
                build_site_unique_id("console_a", "site_shared", key),
                key,
            )
            expected.append(
                (row.entity_id, build_site_unique_id("new_console", "site_shared", key))
            )
    untouched = [
        _row(
            entity_registry,
            entry,
            "sensor",
            "console_a_site_shared_router_site_wired_clients",
            "wired_clients",
        ),
        _row(
            entity_registry,
            entry,
            "sensor",
            "console_a_site_shared_wifi_resource_clients",
            "wifi_clients",
        ),
        _row(
            entity_registry,
            entry,
            "sensor",
            "site_shared_active_vouchers",
            "active_vouchers",
        ),
        _row(
            entity_registry,
            other,
            "sensor",
            "console_a_site_foreign_active_vouchers",
            "active_vouchers",
        ),
        entity_registry.async_get_or_create(
            "sensor",
            "other_platform",
            "console_a_site_shared_active_vouchers",
            config_entry=entry,
        ),
    ]
    async_rekey_site_entities(hass, entry, " NEW_CONSOLE ", pending_api_key="new_key")
    for entity_id, unique_id in expected:
        assert entity_registry.async_get(entity_id).unique_id == unique_id
    for row in untouched:
        assert entity_registry.async_get(row.entity_id).unique_id == row.unique_id
    assert entry.data[CONF_CONSOLE_ID] == "console_a"


@pytest.mark.parametrize("owner", ["same", "foreign"])
async def test_rekey_conflict_preflight_is_atomic(hass, entity_registry, owner):
    entry = _entry(hass)
    other = _entry(hass, "console_b", "entry_b")
    rows = [
        _row(entity_registry, entry, "sensor", "console_a_site_" + key, key)
        for key in ("active_vouchers", "latest_voucher_code")
    ]
    _row(
        entity_registry,
        entry if owner == "same" else other,
        "sensor",
        "new_site_latest_voucher_code",
        "latest_voucher_code",
        disabled_by=er.RegistryEntryDisabler.USER,
    )
    with patch.object(
        entity_registry,
        "async_update_entity",
        wraps=entity_registry.async_update_entity,
    ) as update:
        with pytest.raises(SiteEntityIdentityError, match="occupied"):
            async_rekey_site_entities(hass, entry, "new")
        update.assert_not_called()
    assert [entity_registry.async_get(row.entity_id).unique_id for row in rows] == [
        row.unique_id for row in rows
    ]


async def test_rekey_unexpected_update_failure_rolls_back(hass, entity_registry):
    entry = _entry(hass)
    rows = [
        _row(entity_registry, entry, "sensor", "console_a_site_" + key, key)
        for key in ("active_vouchers", "latest_voucher_code")
    ]
    native = entity_registry.async_update_entity
    count = 0

    def update(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            err_msg = "synthetic update failure"
            raise RuntimeError(err_msg)
        return native(*args, **kwargs)

    with (
        patch.object(entity_registry, "async_update_entity", side_effect=update),
        pytest.raises(RuntimeError, match="synthetic"),
    ):
        async_rekey_site_entities(hass, entry, "new")
    assert count == 3
    assert [entity_registry.async_get(row.entity_id).unique_id for row in rows] == [
        row.unique_id for row in rows
    ]


@pytest.mark.parametrize(
    ("unique_id", "key"),
    [
        ("console_a_site_active_vouchers", None),
        ("console_a_active_vouchers", "active_vouchers"),
        ("console_a_site_wrong_key", "active_vouchers"),
    ],
)
async def test_rekey_rejects_ambiguous_metadata(hass, entity_registry, unique_id, key):
    entry = _entry(hass)
    _row(entity_registry, entry, "sensor", unique_id, key)
    with pytest.raises(SiteEntityIdentityError, match="metadata"):
        async_rekey_site_entities(hass, entry, "new")


async def test_rekey_namespace_noops_and_credentials(hass, entity_registry):
    entry = _entry(hass)
    async_rekey_site_entities(hass, entry, "CONSOLE_A")
    for value in ("", 5, "synthetic_credential", "pending:key"):
        with pytest.raises(SiteEntityIdentityError):
            async_rekey_site_entities(hass, entry, value, pending_api_key="PENDING-KEY")
    with pytest.raises(SiteEntityIdentityError, match="pending credential"):
        async_rekey_site_entities(hass, entry, "new", pending_api_key="CONSOLE_A")
    entry2 = _entry(hass, None, "entry_b")
    async_rekey_site_entities(hass, entry2, "new")
    async_rekey_site_entities(hass, entry2, None)


def test_all_description_keys_are_in_exact_family_mapping():
    assert {
        d.key
        for d in (
            *SITE_CLIENT_SENSOR_TYPES,
            *SITE_INTERNET_ACTIVITY_SENSOR_TYPES,
            *VOUCHER_SENSOR_TYPES,
        )
    } == SITE_ENTITY_SUFFIXES["sensor"]
    assert {d.key for d in VOUCHER_NUMBER_DESCRIPTIONS} == SITE_ENTITY_SUFFIXES[
        "number"
    ]


def test_identity_conflict_message_has_english_parity_and_truthful_recovery():
    root = Path(__file__).parents[1] / "custom_components" / DOMAIN
    messages = [
        json.loads((root / path).read_text())["config"]["error"][
            "site_entity_identity_conflict"
        ]
        for path in ("strings.json", "translations/en.json")
    ]
    assert messages[0] == messages[1]
    assert "site entity identities" in messages[0]
    assert "saved connection settings remain unchanged" in messages[0]
    assert "Check the logs" in messages[0]


@pytest.mark.parametrize(
    "connection_type", [CONNECTION_TYPE_LOCAL, CONNECTION_TYPE_REMOTE]
)
async def test_reauth_identity_failure_reports_preservation_error_without_mutation(
    hass, entity_registry, caplog, connection_type
):
    entry = _entry(hass)
    hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            CONF_CONNECTION_TYPE: connection_type,
            CONF_HOST: "https://console.example",
        },
    )
    before_data = dict(entry.data)
    row = _row(
        entity_registry,
        entry,
        "sensor",
        "console_a_site_shared_active_vouchers",
        "active_vouchers",
    )
    before = attr.asdict(row)
    with (
        patch.object(
            UnifiInsightsConfigFlow,
            "_async_validate_local_connection",
            AsyncMock(return_value=(True, None, {})),
        ),
        patch.object(
            UnifiInsightsConfigFlow,
            "_async_discover_remote_consoles",
            AsyncMock(return_value=[{"id": "console_a"}]),
        ),
        patch.object(
            UnifiInsightsConfigFlow,
            "_normalize_remote_console_id",
            return_value="console_a",
        ),
        patch.object(
            UnifiInsightsConfigFlow,
            "_async_validate_remote_console",
            AsyncMock(return_value=True),
        ),
        patch.object(
            entity_registry,
            "async_update_entity",
            wraps=entity_registry.async_update_entity,
        ) as update,
    ):
        flow = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": "reauth", "entry_id": entry.entry_id},
            data=entry.data,
        )
        result = await hass.config_entries.flow.async_configure(
            flow["flow_id"], user_input={CONF_API_KEY: "CONSOLE_A"}
        )
    assert result["type"] == FlowResultType.FORM
    assert result["errors"]["base"] == "site_entity_identity_conflict"
    assert dict(entry.data) == before_data
    assert attr.asdict(entity_registry.async_get(row.entity_id)) == before
    update.assert_not_called()
    assert "Cannot change console identity safely" in caplog.text


@pytest.mark.parametrize("namespace", [None, "", " ", "bad namespace", "CONSOLE_A"])
def test_builder_requires_a_valid_canonical_namespace(namespace):
    with pytest.raises(ValueError, match="validated console namespace"):
        build_site_unique_id(namespace, "site_shared", "site_total_clients")


async def test_known_physical_legacy_is_never_migrated_or_claimed(
    hass, entity_registry
):
    entry = _entry(hass)
    physical = _row(
        entity_registry,
        entry,
        "sensor",
        "site_shared_site_wired_clients",
        "wired_clients",
    )
    original = attr.asdict(physical)
    target = "console_a_site_shared_site_wired_clients"
    assert (
        async_migrate_site_entity(
            hass, entry, "sensor", "console_a", "site_shared", "site_wired_clients"
        )
        == target
    )
    assert attr.asdict(entity_registry.async_get(physical.entity_id)) == original
    assert (
        async_migrate_site_entity(
            hass, entry, "sensor", None, "site_shared", "site_wired_clients"
        )
        is None
    )
    logical = _row(entity_registry, entry, "sensor", target, "site_wired_clients")
    assert logical.entity_id != physical.entity_id
    assert (
        async_migrate_site_entity(
            hass, entry, "sensor", "console_a", "site_shared", "site_wired_clients"
        )
        == logical.unique_id
    )
    # Also preserve the exact physical counterexample during namespace re-key.
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_CONSOLE_ID: "site"}
    )
    exact_physical = _row(
        entity_registry,
        entry,
        "sensor",
        "site_shared_router_site_wired_clients",
        "wired_clients",
    )
    async_rekey_site_entities(hass, entry, "new")
    assert (
        entity_registry.async_get(exact_physical.entity_id).unique_id
        == exact_physical.unique_id
    )


@pytest.mark.parametrize("with_legacy", [False, True])
async def test_owned_incompatible_target_is_not_reused(
    hass, entity_registry, with_legacy
):
    entry = _entry(hass)
    key = "internet_download_1h"
    target = _row(
        entity_registry,
        entry,
        "sensor",
        "console_a_site_shared_" + key,
        "physical_download",
    )
    if with_legacy:
        legacy = _row(entity_registry, entry, "sensor", "site_shared_" + key, key)
    effective = async_migrate_site_entity(
        hass, entry, "sensor", "console_a", "site_shared", key
    )
    assert effective == (legacy.unique_id if with_legacy else None)
    snapshot = _resolve_entity_ids(
        entity_registry,
        entry_id=entry.entry_id,
        namespace="console_a",
        site_id="site_shared",
    )
    assert snapshot == ({"download_1h": legacy.entity_id} if with_legacy else {})
    assert (
        entity_registry.async_get(target.entity_id).translation_key
        == "physical_download"
    )


@pytest.mark.parametrize(
    ("candidate", "metadata"),
    [
        ("legacy", None),
        ("legacy", ""),
        ("target", None),
        ("target", ""),
    ],
)
async def test_ambiguous_owned_candidate_skips_and_is_retried(
    hass, entity_registry, mock_coordinator, caplog, candidate, metadata
):
    entry = _entry(hass)
    key = "internet_download_1h"
    unique_id = (
        "site_shared_" if candidate == "legacy" else "console_a_site_shared_"
    ) + key
    row = _row(entity_registry, entry, "sensor", unique_id, metadata)
    mock_coordinator.protect_client = None
    mock_coordinator.data = {
        "sites": {"site_shared": {"id": "site_shared"}},
        "internet_activity": {"site_shared": {"1h": {"rx_bytes": 1}}},
    }
    entry.runtime_data = SimpleNamespace(
        coordinator=mock_coordinator, mobility_coordinator=None
    )
    added = []
    callbacks = []
    mock_coordinator.async_add_listener.side_effect = lambda listener: callbacks.append(
        listener
    ) or (lambda: None)
    await sensor_platform.async_setup_entry(hass, entry, added.extend)
    assert added == []
    assert entity_registry.async_get(row.entity_id).unique_id == unique_id
    assert (
        _resolve_entity_ids(
            entity_registry,
            entry_id=entry.entry_id,
            namespace="console_a",
            site_id="site_shared",
        )
        == {}
    )
    assert "registry metadata" in caplog.text
    entity_registry.async_update_entity(row.entity_id, translation_key=key)
    for callback in callbacks:
        callback()
    assert [entity.unique_id for entity in added] == ["console_a_site_shared_" + key]
    assert entity_registry.async_get(row.entity_id).unique_id == added[0].unique_id


@pytest.mark.parametrize(
    "failure", ["after_native_update", "schedule_save", "rollback_save"]
)
async def test_native_post_mutation_failure_restores_all_attempted_ids(
    hass, entity_registry, caplog, failure
):
    entry = _entry(hass)
    rows = [
        _row(entity_registry, entry, "sensor", "console_a_site_" + key, key)
        for key in ("active_vouchers", "latest_voucher_code")
    ]
    before = [attr.asdict(row) for row in rows]
    native_update = entity_registry.async_update_entity
    native_save = entity_registry.async_schedule_save
    count = 0

    def update(*args, **kwargs):
        nonlocal count
        count += 1
        result = native_update(*args, **kwargs)
        if count == 2:
            err_msg = "Synthetic exception after native mutation"
            raise RuntimeError(err_msg)
        return result

    def save():
        nonlocal count
        count += 1
        if count == 2 or (failure == "rollback_save" and count == 3):
            err_msg = "Synthetic schedule-save failure"
            raise RuntimeError(err_msg)
        return native_save()

    method = (
        "async_update_entity"
        if failure == "after_native_update"
        else "async_schedule_save"
    )
    side_effect = update if failure == "after_native_update" else save
    with (
        patch.object(entity_registry, method, side_effect=side_effect),
        pytest.raises(RuntimeError, match="Synthetic"),
    ):
        async_rekey_site_entities(hass, entry, "new")
    assert count == 4
    assert [entity_registry.async_get(row.entity_id).unique_id for row in rows] == [
        row.unique_id for row in rows
    ]
    for row, original in zip(rows, before, strict=True):
        after = attr.asdict(entity_registry.async_get(row.entity_id))
        assert {
            k: v
            for k, v in after.items()
            if k not in {"unique_id", "previous_unique_id", "modified_at"}
        } == {
            k: v
            for k, v in original.items()
            if k not in {"unique_id", "previous_unique_id", "modified_at"}
        }
    assert entry.data[CONF_CONSOLE_ID] == "console_a"
    assert ("Unable to fully roll back" in caplog.text) == (failure == "rollback_save")


async def _discover_all(hass, entry, coordinator):
    entry.runtime_data = SimpleNamespace(
        coordinator=coordinator, mobility_coordinator=None
    )
    coordinator.hass = hass
    entities = []
    for platform in (sensor_platform, number_platform, button_platform, image_platform):
        await platform.async_setup_entry(hass, entry, entities.extend)
    return entities


async def test_two_consoles_all_families_real_registry(
    hass, entity_registry, mock_coordinator
):
    """Actual platform discovery yields independent rows for every affected key."""
    mock_coordinator.protect_client = None
    mock_coordinator.data = {
        "sites": {"site_shared": {"id": "site_shared", "name": "Synthetic site"}},
        "clients": {"site_shared": {}},
        "vouchers": {"site_shared": {}},
        "internet_activity": {
            "site_shared": {
                window: {"rx_bytes": 1, "tx_bytes": 2}
                for window in ("1h", "1d", "1w", "1m")
            }
        },
    }
    for namespace, entry_id in (("console_a", "entry_a"), ("console_b", "entry_b")):
        entry = _entry(hass, namespace, entry_id)
        entities = await _discover_all(hass, entry, mock_coordinator)
        assert len(entities) == sum(len(keys) for keys in SITE_ENTITY_SUFFIXES.values())
        assert {entity._site_id for entity in entities} == {"site_shared"}
        for entity in entities:
            domain = next(
                domain
                for domain, keys in SITE_ENTITY_SUFFIXES.items()
                if entity.translation_key in keys
            )
            key = entity.translation_key
            assert entity.unique_id == build_site_unique_id(
                namespace, "site_shared", key
            )
            row = _row(entity_registry, entry, domain, entity.unique_id, key)
            assert row.config_entry_id == entry.entry_id
            assert (DOMAIN, "site_site_shared") in entity.device_info["identifiers"]
        activity_ids = _resolve_entity_ids(
            entity_registry,
            entry_id=entry.entry_id,
            namespace=namespace,
            site_id="site_shared",
        )
        assert len(activity_ids) == 8
        assert all(
            entity_registry.async_get(entity_id).config_entry_id == entry.entry_id
            for entity_id in activity_ids.values()
        )
    assert len(entity_registry.entities) == 2 * sum(
        len(keys) for keys in SITE_ENTITY_SUFFIXES.values()
    )
    assert mock_coordinator.data["sites"] == {
        "site_shared": {"id": "site_shared", "name": "Synthetic site"}
    }


async def test_missing_namespace_skips_until_later_discovery_and_reload(
    hass, entity_registry, mock_coordinator
):
    entry = _entry(hass, None)
    mock_coordinator.protect_client = None
    mock_coordinator.data = {
        "sites": {"site_shared": {"id": "site_shared"}},
        "clients": {"site_shared": {}},
        "vouchers": {"site_shared": {}},
        "internet_activity": {"site_shared": {"1h": {"rx_bytes": 1, "tx_bytes": 2}}},
    }
    owned_legacy = _row(
        entity_registry,
        entry,
        "sensor",
        "site_shared_site_total_clients",
        "site_total_clients",
    )
    added = []
    callbacks = []
    mock_coordinator.async_add_listener.side_effect = lambda listener: callbacks.append(
        listener
    ) or (lambda: None)
    entry.runtime_data = SimpleNamespace(
        coordinator=mock_coordinator, mobility_coordinator=None
    )
    for platform in (sensor_platform, number_platform, button_platform, image_platform):
        await platform.async_setup_entry(hass, entry, added.extend)
    assert [e.unique_id for e in added] == [owned_legacy.unique_id]
    assert (
        _resolve_entity_ids(
            entity_registry,
            entry_id=entry.entry_id,
            namespace=None,
            site_id="site_shared",
        )
        == {}
    )
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_CONSOLE_ID: "console_a"}
    )
    for callback in callbacks:
        callback()
    expected_count = 3 + 2 + 3 + 5 + 1 + 1
    assert len(added) == expected_count
    for entity in added:
        domain = next(
            domain
            for domain, keys in SITE_ENTITY_SUFFIXES.items()
            if entity.translation_key in keys
        )
        row = _row(
            entity_registry, entry, domain, entity.unique_id, entity.translation_key
        )
        assert row.config_entry_id == entry.entry_id
    # The retained legacy entity stays known until reload; its owned row then migrates.
    reloaded = await _discover_all(hass, entry, mock_coordinator)
    assert len(reloaded) == expected_count
    assert (
        entity_registry.async_get(owned_legacy.entity_id).unique_id
        == "console_a_site_shared_site_total_clients"
    )
    assert not any(entity.unique_id == owned_legacy.unique_id for entity in reloaded)
    assert len(entity_registry.entities) == expected_count


async def test_platform_discovery_never_claims_disabled_foreign_targets(
    hass, entity_registry, mock_coordinator
):
    entry = _entry(hass)
    other = _entry(hass, "console_b", "entry_b")
    mock_coordinator.protect_client = None
    mock_coordinator.data = {
        "sites": {"site_shared": {"id": "site_shared"}},
        "clients": {"site_shared": {}},
        "vouchers": {"site_shared": {}},
        "internet_activity": {"site_shared": {"1h": {"rx_bytes": 1, "tx_bytes": 2}}},
    }
    foreign = []
    for domain, keys in SITE_ENTITY_SUFFIXES.items():
        foreign.extend(
            _row(
                entity_registry,
                other,
                domain,
                build_site_unique_id("console_a", "site_shared", key),
                key,
                disabled_by=er.RegistryEntryDisabler.USER,
            )
            for key in keys
        )
    legacy = _row(
        entity_registry,
        entry,
        "sensor",
        "site_shared_site_total_clients",
        "site_total_clients",
    )
    entities = await _discover_all(hass, entry, mock_coordinator)
    assert [entity.unique_id for entity in entities] == [legacy.unique_id]
    assert entities[0].native_value == 0
    mock_coordinator.data["clients"]["site_shared"]["synthetic_client"] = {
        "type": "WIRED"
    }
    assert entities[0].native_value == 1
    assert entity_registry.async_get(legacy.entity_id).unique_id == legacy.unique_id
    assert all(
        entity_registry.async_get(row.entity_id).config_entry_id == other.entry_id
        for row in foreign
    )


@pytest.mark.parametrize(
    "outcome", ["success", "conflict", "failure", "post_mutation_failure"]
)
async def test_local_flow_rekeys_before_persisting_and_blocks_partial_change(
    hass, entity_registry, outcome
):
    entry = _entry(hass, "site_fallback")
    hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            CONF_CONNECTION_TYPE: CONNECTION_TYPE_LOCAL,
            CONF_HOST: "https://console.example",
        },
    )
    before_data = dict(entry.data)
    rows = [
        _row(
            entity_registry,
            entry,
            "sensor",
            build_site_unique_id("site_fallback", "site_shared", key),
            key,
        )
        for key in ("site_total_clients", "site_wired_clients")
    ]
    new_namespace = "aa:bb:cc:dd:ee:ff"
    if outcome == "conflict":
        other = _entry(hass, "console_b", "entry_b")
        _row(
            entity_registry,
            other,
            "sensor",
            build_site_unique_id(new_namespace, "site_shared", "site_wired_clients"),
            "site_wired_clients",
            disabled_by=er.RegistryEntryDisabler.USER,
        )
    native = entity_registry.async_update_entity
    count = 0

    def update(*args, **kwargs):
        nonlocal count
        count += 1
        if outcome == "failure" and count == 2:
            err_msg = "Synthetic registry failure"
            raise RuntimeError(err_msg)
        result = native(*args, **kwargs)
        if outcome == "post_mutation_failure" and count == 2:
            err_msg = "Synthetic registry failure after mutation"
            raise RuntimeError(err_msg)
        return result

    with (
        patch.object(
            UnifiInsightsConfigFlow,
            "_async_validate_local_connection",
            AsyncMock(return_value=(True, None, {"mac": new_namespace})),
        ),
        patch.object(entity_registry, "async_update_entity", side_effect=update),
        patch("custom_components.unifi_insights.async_setup_entry", return_value=True),
        patch(
            "custom_components.unifi_insights.async_migrate_entry", return_value=True
        ),
    ):
        flow = await entry.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            flow["flow_id"],
            user_input={
                CONF_HOST: "https://new-console.example",
                CONF_API_KEY: "pending_credential",
                CONF_VERIFY_SSL: False,
            },
        )
        await hass.async_block_till_done()
    if outcome == "success":
        assert result["type"] == FlowResultType.ABORT
        assert entry.data[CONF_CONSOLE_ID] == new_namespace
        assert entry.unique_id == new_namespace
        assert [entity_registry.async_get(row.entity_id).unique_id for row in rows] == [
            build_site_unique_id(new_namespace, "site_shared", key)
            for key in ("site_total_clients", "site_wired_clients")
        ]
    else:
        assert result["type"] == FlowResultType.FORM
        assert result["errors"]["base"] == (
            "site_entity_identity_conflict" if outcome == "conflict" else "unknown"
        )
        assert dict(entry.data) == before_data
        assert entry.data[CONF_CONSOLE_ID] == "site_fallback"
        assert entry.unique_id == "site_fallback"
        assert [entity_registry.async_get(row.entity_id).unique_id for row in rows] == [
            row.unique_id for row in rows
        ]
        assert count == (
            0
            if outcome == "conflict"
            else 4
            if outcome == "post_mutation_failure"
            else 3
        )


@pytest.mark.parametrize("source", ["reconfigure", "reauth"])
async def test_remote_flow_rekeys_or_reuses_normalized_namespace(
    hass, entity_registry, source
):
    old = "console_a" if source == "reconfigure" else "CONSOLE_A"
    entry = _entry(hass, old)
    hass.config_entries.async_update_entry(
        entry,
        data={**entry.data, CONF_CONNECTION_TYPE: CONNECTION_TYPE_REMOTE},
        unique_id="synthetic_credential" if source == "reconfigure" else old,
    )
    row = _row(
        entity_registry,
        entry,
        "sensor",
        "console_a_site_shared_active_vouchers",
        "active_vouchers",
    )
    new = "console_b" if source == "reconfigure" else "console_a"
    with (
        patch.object(
            UnifiInsightsConfigFlow,
            "_async_discover_remote_consoles",
            AsyncMock(return_value=[{"id": new}]),
        ),
        patch.object(
            UnifiInsightsConfigFlow, "_normalize_remote_console_id", return_value=new
        ),
        patch.object(
            UnifiInsightsConfigFlow,
            "_async_validate_remote_console",
            AsyncMock(return_value=True),
        ),
        patch("custom_components.unifi_insights.async_setup_entry", return_value=True),
        patch(
            "custom_components.unifi_insights.async_migrate_entry", return_value=True
        ),
    ):
        if source == "reconfigure":
            flow = await entry.start_reconfigure_flow(hass)
        else:
            flow = await hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": "reauth", "entry_id": entry.entry_id},
                data=entry.data,
            )
        result = await hass.config_entries.flow.async_configure(
            flow["flow_id"],
            user_input={
                CONF_API_KEY: "pending_credential",
                **({CONF_CONSOLE_ID: new} if source == "reconfigure" else {}),
            },
        )
        await hass.async_block_till_done()
    assert result["type"] == FlowResultType.ABORT
    assert entry.data[CONF_CONSOLE_ID] == new
    assert (
        entity_registry.async_get(row.entity_id).unique_id
        == new + "_site_shared_active_vouchers"
    )
