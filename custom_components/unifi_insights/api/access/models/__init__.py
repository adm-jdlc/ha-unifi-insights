# Copyright 2026 UniFi Insights contributors
"""Tolerant models for UniFi Access developer API payloads."""

from .common import AccessRecord, parse_record, parse_records
from .devices import AccessDevice
from .doors import AccessDoor, DoorLockRelayStatus, DoorPositionStatus
from .emergency import AccessEmergencyStatus, AccessLockRuleStatus, LockRuleType
from .users import AccessPinCredential, AccessUser, AccessUserStatus
from .webhooks import AccessWebhookEndpoint

__all__ = [
    "AccessDevice",
    "AccessDoor",
    "AccessEmergencyStatus",
    "AccessLockRuleStatus",
    "AccessPinCredential",
    "AccessRecord",
    "AccessUser",
    "AccessUserStatus",
    "AccessWebhookEndpoint",
    "DoorLockRelayStatus",
    "DoorPositionStatus",
    "LockRuleType",
    "parse_record",
    "parse_records",
]
