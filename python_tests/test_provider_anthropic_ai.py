import httpx
import pytest
import respx

from tradepulse.providers import AnthropicAIProvider, ProviderDataFailure, ProviderHttpFailure, build_scan_request
from tradepulse.providers.anthropic_ai import SCAN_TOOL_NAME


def _tool_use_response(candidates: list[dict]) -> dict:
    return {
        "model": "claude-haiku-4-5-20251001",
        "content": [
            {"type": "tool_use", "name": SCAN_TOOL_NAME, "input": {"candidates": candidates}},
        ],
    }


@respx.mock
async def test_scan_candidates_parses_valid_tool_use_response() -> None:
    respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(
            200,
            json=_tool_use_response(
                [{"symbol": "aapl", "recommendation": "BUY", "confidence": 82, "summary": "Strong momentum."}]
            ),
        )
    )
    provider = AnthropicAIProvider("key", "claude-haiku-4-5-20251001", 10)
    try:
        request = build_scan_request("req-1", "corr-1", "scan the market")
        ai_response, candidates = await provider.scan_candidates(request)
    finally:
        await provider.aclose()

    assert len(candidates) == 1
    assert candidates[0].symbol == "AAPL"
    assert candidates[0].recommendation == "BUY"
    assert candidates[0].confidence == 82
    assert ai_response.provider == "anthropic"
    assert ai_response.request_id == "req-1"


@respx.mock
async def test_scan_candidates_raises_on_missing_tool_use_block() -> None:
    respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(200, json={"model": "claude-haiku-4-5-20251001", "content": [{"type": "text", "text": "no tools used"}]})
    )
    provider = AnthropicAIProvider("key", "claude-haiku-4-5-20251001", 10)
    try:
        request = build_scan_request("req-2", "corr-2", "scan the market")
        with pytest.raises(ProviderDataFailure) as exc_info:
            await provider.scan_candidates(request)
    finally:
        await provider.aclose()
    assert exc_info.value.error_code == "AI_TOOL_USE_MISSING"


@respx.mock
async def test_scan_candidates_rejects_entire_response_on_invalid_candidate() -> None:
    """Fail-closed: one malformed candidate invalidates the whole batch --
    it must not silently drop just the bad entry and keep the rest."""
    respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(
            200,
            json=_tool_use_response(
                [
                    {"symbol": "AAPL", "recommendation": "BUY", "confidence": 82, "summary": "ok"},
                    {"symbol": "TSLA", "recommendation": "MAYBE_BUY", "confidence": 50, "summary": "bad enum"},
                ]
            ),
        )
    )
    provider = AnthropicAIProvider("key", "claude-haiku-4-5-20251001", 10)
    try:
        request = build_scan_request("req-3", "corr-3", "scan the market")
        with pytest.raises(ProviderDataFailure) as exc_info:
            await provider.scan_candidates(request)
    finally:
        await provider.aclose()
    assert exc_info.value.error_code == "AI_CANDIDATE_RECOMMENDATION_INVALID"


@respx.mock
async def test_scan_candidates_rejects_confidence_out_of_range() -> None:
    respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(
            200,
            json=_tool_use_response([{"symbol": "AAPL", "recommendation": "BUY", "confidence": 150, "summary": "ok"}]),
        )
    )
    provider = AnthropicAIProvider("key", "claude-haiku-4-5-20251001", 10)
    try:
        request = build_scan_request("req-4", "corr-4", "scan the market")
        with pytest.raises(ProviderDataFailure) as exc_info:
            await provider.scan_candidates(request)
    finally:
        await provider.aclose()
    assert exc_info.value.error_code == "AI_CANDIDATE_CONFIDENCE_INVALID"


@respx.mock
async def test_non_success_response_raises_typed_http_failure() -> None:
    respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(429, json={"error": {"message": "rate limited"}})
    )
    provider = AnthropicAIProvider("key", "claude-haiku-4-5-20251001", 10)
    try:
        request = build_scan_request("req-5", "corr-5", "scan the market")
        with pytest.raises(ProviderHttpFailure) as exc_info:
            await provider.scan_candidates(request)
    finally:
        await provider.aclose()
    assert exc_info.value.http_status == 429
