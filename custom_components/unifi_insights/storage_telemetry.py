# Copyright (c) 2026 Ruaan Deysel

"""
Normalize aggregate NVR storage already delivered by the coordinator.

Direct byte fields precede storageInfo, then storage_info. CamelCase precedes
snake_case within each source; invalid aliases fall through. Capacity tuples
stay within one source: contradictory tuples are rejected, never spliced.
Coherent byte counts precede storage.used (percentage only). Health prefers
storageInfo/storage_info over storage. Only literal booleans convey health.
Percentages round to one decimal before the inclusive 90% comparison, matching
console mounts. No telemetry is inferred from omitted model defaults.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .const import STORAGE_NEARLY_FULL_PERCENT

_FULL_PERCENT = 100


@dataclass(frozen=True, slots=True)
class UnifiInsightsStorageTelemetry:
    """Validated storage signals; None means unknown, including health."""

    used_bytes: int | None = None
    total_bytes: int | None = None
    available_bytes: int | None = None
    used_percent: float | None = None
    healthy: bool | None = None

    @property
    def degraded(self) -> bool | None:
        """Return a problem only when health was explicitly supplied."""
        return None if self.healthy is None else not self.healthy

    @property
    def nearly_full(self) -> bool | None:
        """Use the same displayed percentage boundary as console mounts."""
        if self.used_percent is None:
            return None
        return self.used_percent >= STORAGE_NEARLY_FULL_PERCENT


def _number(value: object) -> float | int | None:
    """Reject malformed, negative, non-finite and boolean readings."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        if not math.isfinite(value):
            return None
    except OverflowError:
        # The existing GB conversion cannot represent arbitrary-size integers.
        return None
    return value if value >= 0 else None


def _bytes(source: Mapping[str, Any], *aliases: str) -> int | None:
    """Pick the first valid alias, preserving zero."""
    for alias in aliases:
        if (value := _number(source.get(alias))) is not None:
            return int(value)
    return None


def normalize_nvr_storage(nvr: Mapping[str, Any]) -> UnifiInsightsStorageTelemetry:
    """Return optional capacities, percentage and health without mutating data."""
    nested = [
        value
        for key in ("storageInfo", "storage_info")
        if isinstance(value := nvr.get(key), Mapping)
    ]
    sources = [
        (
            _bytes(nvr, "storageUsedBytes", "storage_used_bytes"),
            _bytes(nvr, "storageTotalBytes", "storage_total_bytes"),
            _bytes(nvr, "storageAvailableBytes", "storage_available_bytes"),
        ),
        *[
            (
                _bytes(
                    info, "usedSize", "used_size", "usedSpaceBytes", "used_space_bytes"
                ),
                _bytes(
                    info,
                    "totalSize",
                    "total_size",
                    "totalSpaceBytes",
                    "total_space_bytes",
                ),
                _bytes(info, "availableSize", "available_size"),
            )
            for info in nested
        ],
    ]
    used = total = available = None
    for candidate_used, candidate_total, candidate_available in sources:
        if all(
            value is None
            for value in (candidate_used, candidate_total, candidate_available)
        ):
            continue
        if candidate_total is not None and (
            (candidate_used is not None and candidate_used > candidate_total)
            or (
                candidate_available is not None
                and candidate_available > candidate_total
            )
            or (
                candidate_used is not None
                and candidate_available is not None
                and candidate_used + candidate_available > candidate_total
            )
        ):
            continue
        used, total, available = candidate_used, candidate_total, candidate_available
        break

    percent = None
    if used is not None and total is not None:
        if available is None:
            available = total - used
        if total > 0:
            percent = round(used / total * 100, 1)
    storage = nvr.get("storage")
    if not isinstance(storage, Mapping):
        storage = {}
    if (
        percent is None
        and (value := _number(storage.get("used"))) is not None
        and value <= _FULL_PERCENT
    ):
        percent = round(float(value), 1)
    healthy = next(
        (
            value
            for info in [*nested, storage]
            if isinstance(value := info.get("healthy"), bool)
        ),
        None,
    )
    return UnifiInsightsStorageTelemetry(used, total, available, percent, healthy)
