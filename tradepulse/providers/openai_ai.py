"""Async OpenAI Responses API client -- one of the configurable AI discovery
backends (see providers/ai_provider.py for the shared, provider-agnostic
contract every backend satisfies, including the fail-closed candidate
validation this module relies on; see providers/anthropic_ai.py for the
other backend).

Mirrors anthropic_ai.py's shape and the same discovery-only governance
principle: this client never returns a numeric position size, stop-loss, or
target price -- only {symbol, recommendation, confidence, summary}, forced
via a tool call and validated against the same schema every backend shares.

Uses POST /v1/responses (OpenAI's current recommended surface for tool use
and stateful interactions, per OpenAI's own docs -- not the older /v1/
chat/completions endpoint). Request/response shape verified directly
against developers.openai.com's function-calling guide rather than assumed:
- Tool definitions are FLAT on the tool object (`{"type": "function", "name":
  ..., "parameters": ...}`), not nested under a `"function"` key the way
  Chat Completions nests them.
- `tool_choice` to force a specific tool is also flat: `{"type": "function",
  "name": ...}`.
- The forced call appears in `response["output"]` as an item with
  `"type": "function_call"`, carrying `"name"` and a JSON-encoded string
  `"arguments"` directly on the item (again, not nested).
- Output length is capped with `max_output_tokens`, not Chat Completions'
  `max_tokens`.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any, Mapping

import httpx

from tradepulse.models import AIRequest, AIResponse

from .ai_provider import (
    SCAN_CANDIDATES_SCHEMA,
    SCAN_TOOL_DESCRIPTION,
    SCAN_TOOL_NAME,
    OpportunityCandidate,
    parse_scan_candidates,
)
from .errors import ProviderDataFailure, ProviderHttpFailure

RESPONSES_PATH = "/responses"
DEFAULT_BASE_URL = "https://api.openai.com/v1"
PROVIDER_NAME = "openai"

_SCAN_TOOL_DEFINITION: Mapping[str, Any] = {
    "type": "function",
    "name": SCAN_TOOL_NAME,
    "description": SCAN_TOOL_DESCRIPTION,
    "parameters": SCAN_CANDIDATES_SCHEMA,
}


class OpenAIProvider:
    """Discovery-only LLM client -- see module docstring. Never returns a
    numeric position size, stop-loss, or target price; the deterministic
    risk/strategy layer (tradepulse/risk/engine.py, strategy/factors.py)
    owns those."""

    def __init__(self, api_key: str, model: str, timeout_seconds: int, base_url: str | None = None) -> None:
        self._api_key = api_key
        self._model = model
        self._client = httpx.AsyncClient(base_url=base_url or DEFAULT_BASE_URL, timeout=timeout_seconds)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> OpenAIProvider:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}", "content-type": "application/json"}

    async def scan_candidates(self, request: AIRequest) -> tuple[AIResponse, list[OpportunityCandidate]]:
        """Send a discovery prompt and return both the raw AIResponse (for
        audit persistence) and the fail-closed-validated candidate list.
        A response that cannot be validated raises ProviderDataFailure --
        it is never silently treated as zero candidates found."""
        body = {
            "model": self._model,
            "max_output_tokens": 4096,
            "input": [{"role": "user", "content": str(request.payload.get("prompt", ""))}],
            "tools": [_SCAN_TOOL_DEFINITION],
            "tool_choice": {"type": "function", "name": SCAN_TOOL_NAME},
        }
        started_at = datetime.now(UTC)
        try:
            response = await self._client.post(RESPONSES_PATH, headers=self._headers, json=body)
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
        return f"OpenAI HTTP {response.status_code}"
    return str((data.get("error") or {}).get("message") or f"OpenAI HTTP {response.status_code}")


def _parse_scan_candidates(data: Mapping[str, Any]) -> list[OpportunityCandidate]:
    """Extract the forced function_call output item -- the OpenAI-specific
    half of response validation. Field-level validation of the extracted
    candidates is shared with every other backend via
    ai_provider.parse_scan_candidates."""
    output = data.get("output")
    if not isinstance(output, list):
        raise ProviderDataFailure(PROVIDER_NAME, "scan", "scan_candidates", "AI_RESPONSE_MALFORMED", "response has no output items")

    call = next(
        (item for item in output if isinstance(item, dict) and item.get("type") == "function_call" and item.get("name") == SCAN_TOOL_NAME),
        None,
    )
    if call is None:
        raise ProviderDataFailure(
            PROVIDER_NAME, "scan", "scan_candidates", "AI_TOOL_USE_MISSING",
            "model did not return the required function_call output item", retryable=True,
        )

    raw_arguments = call.get("arguments")
    if not isinstance(raw_arguments, str):
        raise ProviderDataFailure(PROVIDER_NAME, "scan", "scan_candidates", "AI_TOOL_INPUT_MALFORMED", "function_call arguments missing")
    try:
        arguments = json.loads(raw_arguments)
    except ValueError as exc:
        raise ProviderDataFailure(PROVIDER_NAME, "scan", "scan_candidates", "AI_TOOL_INPUT_MALFORMED", f"function_call arguments are not valid JSON: {exc}") from exc
    if not isinstance(arguments, dict):
        raise ProviderDataFailure(PROVIDER_NAME, "scan", "scan_candidates", "AI_TOOL_INPUT_MALFORMED", "function_call arguments are not an object")

    return parse_scan_candidates(arguments.get("candidates"), PROVIDER_NAME)


__all__ = ["OpenAIProvider"]
