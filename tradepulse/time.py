"""One strict UTC boundary for mandatory financial evidence timestamps."""
from datetime import UTC, datetime


def aware_utc(value: datetime | str, *, field_name: str = "timestamp") -> datetime:
    """Reject missing, malformed and naive values; preserve the represented instant."""
    try:
        stamp = datetime.fromisoformat(value) if isinstance(value, str) else value
        if not isinstance(stamp, datetime) or stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError("aware datetime required")
        return stamp.astimezone(UTC)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError(f"{field_name}: mandatory aware timestamp invalid") from exc
