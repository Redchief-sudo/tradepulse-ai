"""Typed Alpaca API error handling — port of base44/shared/alpacaErrors.ts.

Preserves the X-Request-ID Alpaca returns on every response, needed to trace
support issues per Alpaca's own docs.
"""

from __future__ import annotations

from typing import Any, Mapping

import httpx


class AlpacaError(RuntimeError):
    def __init__(
        self,
        message: str,
        status_code: int,
        request_id: str | None,
        error_code: str | None,
        operation: str,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.request_id = request_id
        self.error_code = error_code
        self.operation = operation

    def is_auth_error(self) -> bool:
        return self.status_code in (401, 403)

    def is_rate_limit(self) -> bool:
        return self.status_code == 429

    def is_insufficient_buying_power(self) -> bool:
        text = self.message.lower()
        return self.status_code == 403 and ("buying power" in text or "insufficient" in text)

    def is_market_closed(self) -> bool:
        text = self.message.lower()
        return "market is closed" in text or "not open" in text

    def to_audit_detail(self) -> Mapping[str, Any]:
        return {
            "error_type": "AlpacaError",
            "status_code": self.status_code,
            "request_id": self.request_id,
            "error_code": self.error_code,
            "message": self.message,
            "operation": self.operation,
        }


def extract_request_id(response: httpx.Response) -> str | None:
    return response.headers.get("x-request-id")


def raise_alpaca_error(response: httpx.Response, operation: str) -> None:
    request_id = extract_request_id(response)
    try:
        data = response.json()
    except ValueError:
        data = {}
    message = data.get("message") or data.get("error") or f"Alpaca HTTP {response.status_code} ({operation})"
    error_code = data.get("code")
    raise AlpacaError(message, response.status_code, request_id, error_code, operation)
