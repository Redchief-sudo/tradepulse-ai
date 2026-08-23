from __future__ import annotations

import json
from dataclasses import fields, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, MappingProxyType):
        return dict(value)
    if is_dataclass(value) and not isinstance(value, type):
        # A shallow field-by-field dict, NOT dataclasses.asdict(): asdict()
        # recursively deepcopies every nested value, and copy.deepcopy cannot
        # handle MappingProxyType (used throughout the models for immutable
        # metadata) -- it fails with "cannot pickle 'mappingproxy' object".
        # Building one shallow level here and letting json.dumps re-invoke
        # this function for each nested value avoids deepcopy entirely.
        return {f.name: getattr(value, f.name) for f in fields(value)}
    raise TypeError(f"Unsupported persistence value: {type(value).__name__}")


def encode_payload(value: Any) -> str:
    return json.dumps(value, default=_json_default, separators=(",", ":"), sort_keys=True)


def decode_payload(value: str) -> Mapping[str, Any]:
    decoded = json.loads(value)
    if not isinstance(decoded, dict):
        raise ValueError("persisted payload must decode to an object")
    return decoded
