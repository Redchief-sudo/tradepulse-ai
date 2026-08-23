from enum import StrEnum


class AssetClass(StrEnum):
    EQUITY = "equity"
    CRYPTO = "crypto"
    FOREX = "forex"
    COMMODITY = "commodity"
    FIXED_INCOME = "fixed_income"


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class ExecutionMode(StrEnum):
    PAPER = "paper"
    LIVE = "live"


class OrderStatus(StrEnum):
    CREATED = "created"
    SUBMITTED = "submitted"
    ACCEPTED = "accepted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    EXPIRED = "expired"


class SettlementStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    RETRYABLE_FAILED = "retryable_failed"
    TERMINAL_FAILED = "terminal_failed"
    INTEGRITY_BLOCKED = "integrity_blocked"


class TradeIntentStatus(StrEnum):
    PROPOSED = "proposed"
    RISK_APPROVED = "risk_approved"
    SUBMITTED = "submitted"
    ACCEPTED = "accepted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    REJECTED = "rejected"
    CANCELED = "canceled"
    EXPIRED = "expired"
    FAILED = "failed"
    # Broker submission outcome could not be established (network/timeout/
    # 5xx/429 -- anything short of a definitive rejection). Never treat this
    # as a rejection or silently resubmit; it requires recovery via
    # get_order_by_client_order_id before any further action.
    SUBMISSION_UNKNOWN = "submission_unknown"


class SessionState(StrEnum):
    DISABLED = "disabled"
    ACTIVE = "active"
    RISK_STOPPED = "risk_stopped"
    SYSTEM_DEGRADED = "system_degraded"
    BROKER_UNAVAILABLE = "broker_unavailable"
    MARKET_CLOSED = "market_closed"
    MANUALLY_STOPPED = "manually_stopped"
    FINANCIAL_INTEGRITY_BLOCKED = "financial_integrity_blocked"


class ScanRunStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class ReconciliationOutcome(StrEnum):
    MATCHED = "matched"
    DRIFT_DETECTED = "drift_detected"
    CORRECTED = "corrected"


class ScanTrigger(StrEnum):
    SCHEDULED = "scheduled"
    MANUAL = "manual"
