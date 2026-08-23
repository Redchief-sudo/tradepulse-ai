from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any, Mapping


class DomainValidationError(ValueError):
    """Canonical model invariant violation."""


def require_text(value: str, field: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise DomainValidationError(f"{field} is required")
    return normalized


def require_aware(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise DomainValidationError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def decimal_value(value: Decimal | str | int | float, field: str, *, positive: bool = False, nonnegative: bool = False) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise DomainValidationError(f"{field} must be a decimal") from exc
    if not result.is_finite():
        raise DomainValidationError(f"{field} must be finite")
    if positive and result <= 0:
        raise DomainValidationError(f"{field} must be positive")
    if nonnegative and result < 0:
        raise DomainValidationError(f"{field} must be nonnegative")
    return result


def immutable_metadata(value: Mapping[str, Any] | None) -> Mapping[str, Any]:
    return MappingProxyType(dict(value or {}))
