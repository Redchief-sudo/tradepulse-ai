"""Fail-closed market-data provider errors -- port of the error contract in
base44/shared/marketDataAdapter.ts (providerHttpFailure/providerRequestFailure/
providerDataFailure). No provider call site in this package is permitted to
return a fabricated, zero-filled, or interpolated quote/candle in place of one
of these exceptions.
"""

from __future__ import annotations


class ProviderError(RuntimeError):
    def __init__(self, provider: str, symbol: str, operation: str, message: str) -> None:
        super().__init__(message)
        self.provider = provider
        self.symbol = symbol
        self.operation = operation
        self.message = message


class ProviderHttpFailure(ProviderError):
    """The provider's HTTP call itself failed (non-2xx, network error)."""

    def __init__(self, provider: str, symbol: str, operation: str, http_status: int | None, message: str) -> None:
        super().__init__(provider, symbol, operation, message)
        self.http_status = http_status


class ProviderDataFailure(ProviderError):
    """The HTTP call succeeded but the payload is not decision-grade: invalid
    bid/ask, missing/stale timestamp, or insufficient candle history."""

    def __init__(
        self, provider: str, symbol: str, operation: str, error_code: str, message: str, retryable: bool = False
    ) -> None:
        super().__init__(provider, symbol, operation, message)
        self.error_code = error_code
        self.retryable = retryable
