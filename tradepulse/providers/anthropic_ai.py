"""Async Anthropic Messages API client -- one of the configurable AI
discovery backends (see providers/ai_provider.py for the shared,
provider-agnostic contract every backend satisfies; see providers/openai_ai.py
for the other one).

Mirrors tradepulse/broker/alpaca_client.py's shape (httpx.AsyncClient, typed
errors, decimal-safe parsing). Per the base44 audit history (base44/shared
runAutonomousScanCycle.ts committee/risk-officer passes), the LLM is treated
as an untrusted, fallible discovery source: every response is validated
against an explicit schema and a missing/malformed field is REJECTED, never
defaulted to an approving/optimistic value. No call site may treat a parse
failure as "no opinion" -- it must raise ProviderDataFailure and the caller
must fail closed (skip the candidate/pass), matching the fail-closed
`consensusMap[...] === true` pattern already used in base44.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any, Mapping

import httpx

from tradepulse.models import AIRequest, AIResponse

from .ai_provider import (
    SCAN_CANDIDATES_SCHEMA,
    SCAN_TOOL_NAME,
    OpportunityCandidate,
    parse_scan_candidates,
)
from .errors import ProviderDataFailure, ProviderHttpFailure

MESSAGES_PATH = "/v1/messages"
DEFAULT_BASE_URL = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"
PROVIDER_NAME = "anthropic"

_SCAN_TOOL_SCHEMA: Mapping[str, Any] = {
    "name": SCAN_TOOL_NAME,
    "description": "Report market-scan candidates with a recommendation and confidence for each.",
    "input_schema": SCAN_CANDIDATES_SCHEMA,
}


class AnthropicAIProvider:
    """Discovery-only LLM client. Never returns a numeric position size,
    stop-loss, or target price -- those remain the deterministic risk/
    strategy layer's responsibility (see tradepulse/risk/engine.py and
    tradepulse/strategy/factors.py), matching the base44 governance
    principle that the LLM proposes hypotheses/candidates, never trade
    parameters."""

    def __init__(self, api_key: str, model: str, timeout_seconds: int, base_url: str | None = None) -> None:
        self._api_key = api_key
        self._model = model
        self._client = httpx.AsyncClient(base_url=base_url or DEFAULT_BASE_URL, timeout=timeout_seconds)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> AnthropicAIProvider:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self._api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }

    async def scan_candidates(self, request: AIRequest) -> tuple[AIResponse, list[OpportunityCandidate]]:
        """Send a discovery prompt and return both the raw AIResponse (for
        audit persistence) and the fail-closed-validated candidate list.
        A response that cannot be validated raises ProviderDataFailure --
        it is never silently treated as zero candidates found."""
        body = {
            "model": self._model,
            "max_tokens": 4096,
            "messages": [{"role": "user", "content": str(request.payload.get("prompt", ""))}],
            "tools": [_SCAN_TOOL_SCHEMA],
            "tool_choice": {"type": "tool", "name": SCAN_TOOL_NAME},
        }
        started_at = datetime.now(UTC)
        try:
            response = await self._client.post(MESSAGES_PATH, headers=self._headers, json=body)
        except httpx.HTTPError as exc:
            raise ProviderHttpFailure(PROVIDER_NAME, "scan", request.operation, None, str(exc)) from exc
        if not response.is_success:
            raise ProviderHttpFailure(
                PROVIDER_NAME, "scan", request.operation, response.status_code, _error_message(response)
            )
        latency_ms = int((datetime.now(UTC) - started_at).total_seconds() * 1000)
        data = response.json()
        candidates = _parse_scan_candidates(data)
        ai_response = AIResponse(
            request_id=request.request_id,
            provider=PROVIDER_NAME,
            model=str(data.get("model") or self._model),
            schema_version=request.schema_version,
            completed_at=datetime.now(UTC),
            result={"candidates": [asdict(c) for c in candidates]},
            latency_ms=latency_ms,
        )
        return ai_response, candidates


def _error_message(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return f"Anthropic HTTP {response.status_code}"
    return str((data.get("error") or {}).get("message") or f"Anthropic HTTP {response.status_code}")


def _parse_scan_candidates(data: Mapping[str, Any]) -> list[OpportunityCandidate]:
    """Extract the tool_use block -- the Anthropic-specific half of response
    validation. Field-level validation of the extracted candidates is
    shared with every other backend via ai_provider.parse_scan_candidates."""
    content = data.get("content")
    if not isinstance(content, list):
        raise ProviderDataFailure(PROVIDER_NAME, "scan", "scan_candidates", "AI_RESPONSE_MALFORMED", "response has no content blocks")

    tool_block = next((block for block in content if isinstance(block, dict) and block.get("type") == "tool_use"), None)
    if tool_block is None or tool_block.get("name") != SCAN_TOOL_NAME:
        raise ProviderDataFailure(
            PROVIDER_NAME, "scan", "scan_candidates", "AI_TOOL_USE_MISSING",
            "model did not return the required tool_use block", retryable=True,
        )

    tool_input = tool_block.get("input")
    if not isinstance(tool_input, dict):
        raise ProviderDataFailure(PROVIDER_NAME, "scan", "scan_candidates", "AI_TOOL_INPUT_MALFORMED", "tool_use input is not an object")

    return parse_scan_candidates(tool_input.get("candidates"), PROVIDER_NAME)


__all__ = ["AnthropicAIProvider"]
