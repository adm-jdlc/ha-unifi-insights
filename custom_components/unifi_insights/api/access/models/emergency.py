# Copyright 2026 UniFi Insights contributors
"""Emergency and lock-rule models for the UniFi Access developer API."""

from __future__ import annotations

from typing import Literal

from .common import AccessRecord

# Documented lock-rule types. ``lock_now`` is not documented in the official
# API, so it is not offered here.
LockRuleType = Literal["keep_lock", "keep_unlock", "custom", "reset", "lock_early"]
LOCK_RULE_TYPES: tuple[LockRuleType, ...] = (
    "keep_lock",
    "keep_unlock",
    "custom",
    "reset",
    "lock_early",
)


class AccessEmergencyStatus(AccessRecord):
    """Console-wide emergency state from ``GET /doors/settings/emergency``."""

    lockdown: bool | None = None
    evacuation: bool | None = None


class AccessLockRuleStatus(AccessRecord):
    """Result of ``PUT /doors/:id/lock_rule``: the rule the console applied."""

    type: str | None = None
    ended_time: int | None = None
