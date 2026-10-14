# Copyright 2026 UniFi Insights contributors
"""Rule change detection for the UniFi Insights config coordinator."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from .config_sections import (
    SECTION_OUTCOME_OK,
    is_predefined_firewall_rule,
    is_predefined_nat_rule,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

_LOGGER = logging.getLogger(__name__)

RULE_CHANGE_ADDED: Final = "added"
RULE_CHANGE_MODIFIED: Final = "modified"
RULE_CHANGE_DELETED: Final = "deleted"
RULE_CHANGE_TYPES: Final[tuple[str, ...]] = (
    RULE_CHANGE_ADDED,
    RULE_CHANGE_MODIFIED,
    RULE_CHANGE_DELETED,
)


# Changes kept per site. A poll that produces more drops the oldest ones.
RULE_CHANGE_BUFFER_SIZE: Final = 100

# Keys that describe rule order or bookkeeping, not rule content. A reorder
# renumbers every following rule, so these never count as a modification.
VOLATILE_RULE_KEYS: Final[frozenset[str]] = frozenset(
    {"index", "rule_index", "metadata"}
)


@dataclass(frozen=True, kw_only=True)
class RuleFamily:
    """One audited rule family and where its records live in coordinator data."""

    rule_type: str
    data_key: str
    name_fields: tuple[str, ...]
    is_excluded: Callable[[dict[str, Any]], bool] | None = None


RULE_FAMILIES: Final[tuple[RuleFamily, ...]] = (
    RuleFamily(
        rule_type="firewall_rule",
        data_key="firewall_rules",
        name_fields=("name",),
        is_excluded=is_predefined_firewall_rule,
    ),
    RuleFamily(
        rule_type="policy_based_route",
        data_key="policy_based_routes",
        name_fields=("description", "name"),
    ),
    RuleFamily(
        rule_type="port_forward",
        data_key="port_forwards",
        name_fields=("name",),
    ),
    RuleFamily(
        rule_type="traffic_rule",
        data_key="traffic_rules",
        name_fields=("description",),
    ),
    RuleFamily(
        rule_type="nat_rule",
        data_key="nat_rules",
        name_fields=("description",),
        is_excluded=is_predefined_nat_rule,
    ),
    RuleFamily(
        rule_type="static_route",
        data_key="static_routes",
        name_fields=("name",),
    ),
)

RULE_TYPES: Final[tuple[str, ...]] = tuple(family.rule_type for family in RULE_FAMILIES)


@dataclass(frozen=True, kw_only=True)
class _RuleSnapshot:
    """Immutable copy of the parts of one rule the audit compares."""

    name: str | None
    enabled: bool | None
    fields: dict[str, str]


def _rule_name(family: RuleFamily, record: dict[str, Any]) -> str | None:
    """Return the first non-blank name of a rule, or None."""
    for field in family.name_fields:
        value = record.get(field)
        if isinstance(value, str) and (stripped := value.strip()):
            return stripped
    return None


def _snapshot(family: RuleFamily, record: dict[str, Any]) -> _RuleSnapshot:
    """
    Build the comparable copy of one rule record.

    Every value is stored as its canonical JSON text, so the snapshot shares
    no object with coordinator data: a switch that edits ``enabled`` in place
    for instant feedback cannot change the baseline it is compared with.
    """
    enabled = record.get("enabled")
    return _RuleSnapshot(
        name=_rule_name(family, record),
        enabled=enabled if isinstance(enabled, bool) else None,
        fields={
            str(key): json.dumps(value, sort_keys=True, default=str)
            for key, value in record.items()
            if key not in VOLATILE_RULE_KEYS
        },
    )


def _change(
    change_type: str,
    family: RuleFamily,
    site_id: str,
    rule_id: str,
    snapshot: _RuleSnapshot,
    *,
    changed_fields: list[str],
) -> dict[str, Any]:
    """Build one change record. It holds field names, never field values."""
    return {
        "change_type": change_type,
        "site_id": site_id,
        "rule_type": family.rule_type,
        "rule_id": rule_id,
        "rule_name": snapshot.name,
        "enabled": snapshot.enabled,
        "changed_fields": changed_fields,
    }


def _changed_fields(old: _RuleSnapshot, new: _RuleSnapshot) -> list[str]:
    """Return the sorted names of the fields that differ between two snapshots."""
    return sorted(
        key
        for key in old.fields.keys() | new.fields.keys()
        if old.fields.get(key) != new.fields.get(key)
    )


def _diff(
    family: RuleFamily,
    site_id: str,
    previous: dict[str, _RuleSnapshot],
    current: dict[str, _RuleSnapshot],
) -> list[dict[str, Any]]:
    """Return the changes between two snapshots of one family on one site."""
    added = [
        _change(
            RULE_CHANGE_ADDED,
            family,
            site_id,
            rule_id,
            current[rule_id],
            changed_fields=[],
        )
        for rule_id in sorted(current.keys() - previous.keys())
    ]
    modified = [
        _change(
            RULE_CHANGE_MODIFIED,
            family,
            site_id,
            rule_id,
            current[rule_id],
            changed_fields=names,
        )
        for rule_id in sorted(current.keys() & previous.keys())
        if (names := _changed_fields(previous[rule_id], current[rule_id]))
    ]
    deleted = [
        _change(
            RULE_CHANGE_DELETED,
            family,
            site_id,
            rule_id,
            previous[rule_id],
            changed_fields=[],
        )
        for rule_id in sorted(previous.keys() - current.keys())
    ]
    return [*added, *modified, *deleted]


class RuleAuditTracker:
    """
    Detect added, modified and deleted rules between config refreshes.

    A section is compared only when this refresh fetched it successfully
    (outcome ``ok``). The first successful fetch of a section is its baseline
    and reports nothing. An unsupported or failed fetch keeps the baseline, so
    an outage never looks like "every rule was deleted". A section that turns
    empty is reported only when the next successful fetch is empty too.
    """

    def __init__(self) -> None:
        """Initialize an empty tracker."""
        self._baselines: dict[tuple[str, str], dict[str, _RuleSnapshot]] = {}
        self._changes: dict[str, list[dict[str, Any]]] = {}
        self._pending_empty: set[tuple[str, str]] = set()
        self._failing_sites: set[str] = set()
        self._sequence = 0

    def process(
        self,
        data: Mapping[str, Any],
        outcomes: Mapping[tuple[str, str], str],
    ) -> dict[str, list[dict[str, Any]]]:
        """Compare coordinator data with the baselines; return changes by site."""
        sites = data.get("sites")
        site_ids: set[str] = set(sites) if isinstance(sites, dict) else set()

        for key in [key for key in self._baselines if key[1] not in site_ids]:
            _LOGGER.debug(
                "Rule audit: site %s is gone; its %s baseline was dropped",
                key[1],
                key[0],
            )
            del self._baselines[key]
        self._pending_empty = {key for key in self._pending_empty if key[1] in site_ids}
        self._failing_sites &= site_ids
        for site_id in [
            site_id for site_id in self._changes if site_id not in site_ids
        ]:
            del self._changes[site_id]

        for site_id in sorted(site_ids):
            try:
                baselines, pending, settled, new_changes = self._diff_site(
                    data, outcomes, site_id
                )
            except Exception:
                # One site's comparison must never stop the others. Nothing was
                # committed for this site, so its changes are reported by the
                # next comparison that succeeds.
                if site_id not in self._failing_sites:
                    self._failing_sites.add(site_id)
                    _LOGGER.exception(
                        "Rule audit: comparing the rules of site %s failed; its "
                        "changes are reported once the comparison works again",
                        site_id,
                    )
                continue
            self._failing_sites.discard(site_id)
            self._baselines.update(baselines)
            self._pending_empty -= settled
            self._pending_empty |= pending
            if not new_changes:
                continue
            for change in new_changes:
                self._sequence += 1
                change["sequence"] = self._sequence
            buffered = [*self._changes.get(site_id, []), *new_changes]
            if len(new_changes) > RULE_CHANGE_BUFFER_SIZE:
                _LOGGER.warning(
                    "Rule audit: %d rule changes for site %s in one refresh; "
                    "only the newest %d are reported",
                    len(new_changes),
                    site_id,
                    RULE_CHANGE_BUFFER_SIZE,
                )
            self._changes[site_id] = buffered[-RULE_CHANGE_BUFFER_SIZE:]

        return {site_id: list(changes) for site_id, changes in self._changes.items()}

    def _diff_site(
        self,
        data: Mapping[str, Any],
        outcomes: Mapping[tuple[str, str], str],
        site_id: str,
    ) -> tuple[
        dict[tuple[str, str], dict[str, _RuleSnapshot]],
        set[tuple[str, str]],
        set[tuple[str, str]],
        list[dict[str, Any]],
    ]:
        """
        Compare every rule family of one site without changing the tracker.

        Returns the new baselines, the sections seen empty for the first time,
        the sections whose state is settled, and the changes found.
        """
        baselines: dict[tuple[str, str], dict[str, _RuleSnapshot]] = {}
        pending: set[tuple[str, str]] = set()
        settled: set[tuple[str, str]] = set()
        changes: list[dict[str, Any]] = []
        for family in RULE_FAMILIES:
            if outcomes.get((family.data_key, site_id)) != SECTION_OUTCOME_OK:
                continue
            current = self._current(family, data, site_id)
            if current is None:
                continue
            key = (family.data_key, site_id)
            previous = self._baselines.get(key)
            if previous and not current and key not in self._pending_empty:
                # "Every rule is gone" is believed only when the next successful
                # fetch says so too: a console that answers with an empty body
                # looks exactly like one whose rules were all deleted. The
                # baseline is kept, so nothing is lost if the rules come back.
                pending.add(key)
                continue
            settled.add(key)
            baselines[key] = current
            if previous is not None:
                changes.extend(_diff(family, site_id, previous, current))
        return baselines, pending, settled, changes

    @staticmethod
    def _current(
        family: RuleFamily, data: Mapping[str, Any], site_id: str
    ) -> dict[str, _RuleSnapshot] | None:
        """Snapshot one family on one site, or None if its data is malformed."""
        section = data.get(family.data_key)
        rules = section.get(site_id) if isinstance(section, dict) else None
        if not isinstance(rules, dict):
            return None
        return {
            rule_id: _snapshot(family, record)
            for rule_id, record in rules.items()
            if isinstance(rule_id, str)
            and isinstance(record, dict)
            and not (family.is_excluded is not None and family.is_excluded(record))
        }
