"""Rev.140 quarantine records: append-only opening facts (spec §3.11, I9).

Every financial field is an exact Decimal. A float or bool is refused
outright rather than converted (I10); strings are accepted because that is
how hydration reads them back.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from .base import DomainValidationError, decimal_value, immutable_metadata, require_aware, require_text
from .enums import Side
from .market import AssetIdentity


def exact_decimal(value: Decimal | str, name: str, *, positive: bool = False, nonnegative: bool = False) -> Decimal:
    if isinstance(value, (bool, float)) or not isinstance(value, (Decimal, str, int)):
        raise DomainValidationError(f"{name} must be an exact Decimal, not {type(value).__name__}")
    return decimal_value(value, name, positive=positive, nonnegative=nonnegative)


def _set(record: object, name: str, value: object) -> None:
    object.__setattr__(record, name, value)


def _texts(record: object, *names: str) -> None:
    for name in names:
        _set(record, name, require_text(getattr(record, name), name))


def _decimals(record: object, *names: str, positive: bool = False, nonnegative: bool = False) -> None:
    for name in names:
        _set(record, name, exact_decimal(getattr(record, name), name, positive=positive, nonnegative=nonnegative))


def _times(record: object, *names: str) -> None:
    for name in names:
        _set(record, name, require_aware(getattr(record, name), name))


def _asset(record: Any) -> None:
    if not isinstance(record.asset, AssetIdentity):
        raise DomainValidationError("asset must be an AssetIdentity")


def _choice(record: object, name: str, allowed: tuple[str, ...]) -> None:
    if getattr(record, name) not in allowed:
        raise DomainValidationError(f"{name} must be one of {allowed}")


@dataclass(frozen=True, slots=True)
class ShortExposureLiability:
    """The excess of an actual broker oversell, preserved whole (§3.2)."""

    liability_id: str
    fill_id: str
    broker_order_id: str
    trade_intent_id: str
    asset: AssetIdentity
    quantity: Decimal
    sale_price: Decimal
    multiplier: Decimal
    proceeds: Decimal
    opened_at: datetime

    def __post_init__(self) -> None:
        _texts(self, "liability_id", "fill_id", "broker_order_id", "trade_intent_id")
        _asset(self)
        _decimals(self, "quantity", "sale_price", "multiplier", "proceeds", positive=True)
        _times(self, "opened_at")
        if self.proceeds != self.quantity * self.sale_price * self.multiplier:
            raise DomainValidationError("proceeds must equal quantity x sale_price x multiplier")


@dataclass(frozen=True, slots=True)
class UnmanagedLongInventory:
    """The surplus of a cover larger than the open liabilities (§3.8)."""

    record_id: str
    cover_activity_id: str
    asset: AssetIdentity
    quantity: Decimal
    cost_price: Decimal
    multiplier: Decimal
    opened_at: datetime

    def __post_init__(self) -> None:
        _texts(self, "record_id", "cover_activity_id")
        _asset(self)
        _decimals(self, "quantity", "cost_price", "multiplier", positive=True)
        _times(self, "opened_at")


@dataclass(frozen=True, slots=True)
class QuarantineExecution:
    """A broker execution against quarantined inventory: a cover or an unmanaged sale."""

    activity_id: str
    kind: Literal["liability_cover", "unmanaged_sale"]
    asset: AssetIdentity
    side: Side
    quantity: Decimal
    price: Decimal
    executed_at: datetime
    raw_sha256: str

    def __post_init__(self) -> None:
        _texts(self, "activity_id", "raw_sha256")
        _choice(self, "kind", ("liability_cover", "unmanaged_sale"))
        _asset(self)
        if not isinstance(self.side, Side):
            raise DomainValidationError("side must be a Side")
        _decimals(self, "quantity", "price", positive=True)
        _times(self, "executed_at")


@dataclass(frozen=True, slots=True)
class QuarantineResolution:
    record_id: str
    activity_id: str
    quantity: Decimal
    price: Decimal
    realized_pnl: Decimal
    resolved_at: datetime

    def __post_init__(self) -> None:
        _texts(self, "record_id", "activity_id")
        _decimals(self, "quantity", "price", positive=True)
        _decimals(self, "realized_pnl")
        _times(self, "resolved_at")


@dataclass(frozen=True, slots=True)
class QuarantineFeeAllocation:
    """A signed share of a canonical fee, allocated to one record and pool (§3.6)."""

    fee_canonical_id: str
    record_id: str
    pool: Literal["opening", "cover"]
    amount: Decimal

    def __post_init__(self) -> None:
        _texts(self, "fee_canonical_id", "record_id")
        _choice(self, "pool", ("opening", "cover"))
        _decimals(self, "amount")


@dataclass(frozen=True, slots=True)
class QuarantineFeeRecognition:
    record_id: str
    event_id: str
    pool: Literal["opening", "cover"]
    amount: Decimal
    recognized_at: datetime

    def __post_init__(self) -> None:
        _texts(self, "record_id", "event_id")
        _choice(self, "pool", ("opening", "cover"))
        _decimals(self, "amount")
        _times(self, "recognized_at")


@dataclass(frozen=True, slots=True)
class LegacyLotConversion:
    lot_id: str
    liability_id: str
    lot_digest: str
    converted_at: datetime
    reason: str

    def __post_init__(self) -> None:
        _texts(self, "lot_id", "liability_id", "lot_digest", "reason")
        _times(self, "converted_at")


@dataclass(frozen=True, slots=True)
class UnresolvedBrokerExecution:
    """A broker execution TradePulse has not accounted for (§3.1a)."""

    activity_id: str
    asset_key: str
    side: Side
    quantity: Decimal
    price: Decimal
    transaction_time: datetime
    raw_sha256: str
    first_seen_at: datetime
    pass_id: str

    def __post_init__(self) -> None:
        _texts(self, "activity_id", "asset_key", "raw_sha256", "pass_id")
        if not isinstance(self.side, Side):
            raise DomainValidationError("side must be a Side")
        _decimals(self, "quantity", "price", positive=True)
        _times(self, "transaction_time", "first_seen_at")


@dataclass(frozen=True, slots=True)
class UnresolvedExecutionOutcome:
    """How an unresolved execution was explained; written once (K1)."""

    activity_id: str
    outcome: Literal["matched_fill", "quarantine_execution", "pre_database", "legacy_reconciled"]
    matched_record_id: str
    evidence_sha256: str
    resolved_at: datetime

    def __post_init__(self) -> None:
        _texts(self, "activity_id", "matched_record_id", "evidence_sha256")
        _choice(self, "outcome", ("matched_fill", "quarantine_execution", "pre_database", "legacy_reconciled"))
        _times(self, "resolved_at")


@dataclass(frozen=True, slots=True)
class FillCursorBootstrap:
    """The one-time proof that the fill cursor starts from a complete history (§3.1a)."""

    account_ref: str
    boundary_activity_id: str
    activity_count: int
    page_sha256s: tuple[str, ...]
    position_results: Mapping[str, Any]
    cash_result: Mapping[str, Any]
    database_sha256_before: str
    reason: str
    created_at: datetime

    def __post_init__(self) -> None:
        _texts(self, "account_ref", "boundary_activity_id", "database_sha256_before", "reason")
        if isinstance(self.activity_count, bool) or not isinstance(self.activity_count, int) or self.activity_count < 0:
            raise DomainValidationError("activity_count must be a nonnegative integer")
        _set(self, "page_sha256s", tuple(require_text(page, "page_sha256s") for page in self.page_sha256s))
        _set(self, "position_results", immutable_metadata(self.position_results))
        _set(self, "cash_result", immutable_metadata(self.cash_result))
        _times(self, "created_at")
