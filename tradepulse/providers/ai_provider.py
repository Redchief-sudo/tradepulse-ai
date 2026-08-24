"""The provider-agnostic AI discovery contract -- shared by every AI backend
(tradepulse/providers/anthropic_ai.py, openai_ai.py, ...).

This module holds the parts of the discovery pipeline that must NOT diverge
between backends: the candidate shape, the tool schema, and -- most
importantly -- the fail-closed validation of a model's response. Per-backend
modules only handle that backend's HTTP request/response shape (headers,
body, where the tool-call payload lives in the response) and then hand the
raw candidate list to `parse_scan_candidates` here. Keeping validation in
one place means a future fix to it applies to every backend at once; two
independent copies could silently drift, reopening the exact fail-closed
guarantee this system is built around (see the per-backend modules' own
docstrings for the full governance rationale: the AI proposes
{symbol, recommendation, confidence, summary} and nothing else -- never a
price, quantity, stop-loss, or target).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Mapping, Protocol

from tradepulse.models import AIRequest, AIResponse

from .errors import ProviderDataFailure

SCAN_TOOL_NAME = "report_scan_candidates"
SCAN_TOOL_DESCRIPTION = "Report market-scan candidates with a recommendation and confidence for each."

_ALLOWED_RECOMMENDATIONS = frozenset({"STRONG_BUY", "BUY", "HOLD", "SELL", "STRONG_SELL"})

# Standard JSON Schema, reused as-is for both Anthropic's `input_schema` and
# OpenAI's `parameters` -- the two APIs both accept plain JSON Schema here,
# so there's no per-backend divergence to hand-maintain.
SCAN_CANDIDATES_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "recommendation": {"type": "string", "enum": sorted(_ALLOWED_RECOMMENDATIONS)},
                    "confidence": {"type": "number"},
                    "summary": {"type": "string"},
                },
                "required": ["symbol", "recommendation", "confidence", "summary"],
            },
        },
    },
    "required": ["candidates"],
}


@dataclass(frozen=True, slots=True)
class OpportunityCandidate:
    """One fail-closed-validated opportunity extracted from an AI scan response."""

    symbol: str
    recommendation: str
    confidence: float
    summary: str


class AIProvider(Protocol):
    """Structural contract every AI backend satisfies -- no inheritance
    required, callers (tradepulse/scanner/coordinator.py) just need an
    object with these two async methods."""

    async def scan_candidates(self, request: AIRequest) -> tuple[AIResponse, list[OpportunityCandidate]]: ...

    async def aclose(self) -> None: ...


def parse_scan_candidates(raw_candidates: object, provider_name: str) -> list[OpportunityCandidate]:
    """The provider-agnostic half of response validation: given whatever a
    backend extracted as the "candidates" value from its tool-call payload,
    validate every field on every candidate. Any missing/malformed field on
    ANY candidate rejects the ENTIRE response (fail-closed) rather than
    silently dropping just the bad candidate, since a partially-parseable
    response indicates the model deviated from the requested schema and the
    remaining candidates cannot be trusted either."""
    if not isinstance(raw_candidates, list):
        raise ProviderDataFailure(provider_name, "scan", "scan_candidates", "AI_CANDIDATES_MISSING", "candidates field missing or not a list")

    parsed: list[OpportunityCandidate] = []
    for index, raw in enumerate(raw_candidates):
        if not isinstance(raw, dict):
            raise ProviderDataFailure(provider_name, "scan", "scan_candidates", "AI_CANDIDATE_MALFORMED", f"candidate[{index}] is not an object")
        symbol = raw.get("symbol")
        recommendation = raw.get("recommendation")
        confidence = raw.get("confidence")
        summary = raw.get("summary")
        if not isinstance(symbol, str) or not symbol.strip():
            raise ProviderDataFailure(provider_name, "scan", "scan_candidates", "AI_CANDIDATE_SYMBOL_INVALID", f"candidate[{index}] missing symbol")
        if recommendation not in _ALLOWED_RECOMMENDATIONS:
            raise ProviderDataFailure(
                provider_name, "scan", "scan_candidates", "AI_CANDIDATE_RECOMMENDATION_INVALID",
                f"candidate[{index}] recommendation {recommendation!r} not in {sorted(_ALLOWED_RECOMMENDATIONS)}",
            )
        if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 100:
            raise ProviderDataFailure(provider_name, "scan", "scan_candidates", "AI_CANDIDATE_CONFIDENCE_INVALID", f"candidate[{index}] confidence {confidence!r} out of [0,100]")
        if not isinstance(summary, str) or not summary.strip():
            raise ProviderDataFailure(provider_name, "scan", "scan_candidates", "AI_CANDIDATE_SUMMARY_INVALID", f"candidate[{index}] missing summary")
        parsed.append(
            OpportunityCandidate(symbol=symbol.strip().upper(), recommendation=recommendation, confidence=float(confidence), summary=summary.strip())
        )
    return parsed


def build_scan_request(request_id: str, correlation_id: str, prompt: str) -> AIRequest:
    return AIRequest(
        request_id=request_id,
        correlation_id=correlation_id,
        operation="scan_candidates",
        schema_version="1.0",
        created_at=datetime.now(UTC),
        payload={"prompt": prompt},
    )
