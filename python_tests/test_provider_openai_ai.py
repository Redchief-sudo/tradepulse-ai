import json

import httpx
import pytest
import respx

from tradepulse.providers import OpenAIProvider, ProviderDataFailure, ProviderHttpFailure, build_scan_request
from tradepulse.providers.openai_ai import SCAN_TOOL_NAME


def _function_call_response(candidates: list[dict], *, arguments_json: str | None = None) -> dict:
    arguments = arguments_json if arguments_json is not None else json.dumps({"candidates": candidates})
    return {
        "id": "resp_1",
        "model": "gpt-4o-mini",
        "output": [
            {"id": "fc_1", "call_id": "call_1", "type": "function_call", "name": SCAN_TOOL_NAME, "arguments": arguments},
        ],
    }


@respx.mock
async def test_scan_candidates_parses_valid_function_call_response() -> None:
    respx.post("https://api.openai.com/v1/responses").mock(
        return_value=httpx.Response(
            200,
            json=_function_call_response(
                [{"symbol": "aapl", "recommendation": "BUY", "confidence": 82, "summary": "Strong momentum."}]
            ),
        )
    )
    provider = OpenAIProvider("key", "gpt-4o-mini", 10)
    try:
        request = build_scan_request("req-1", "corr-1", "scan the market")
        ai_response, candidates = await provider.scan_candidates(request)
    finally:
        await provider.aclose()

    assert len(candidates) == 1
    assert candidates[0].symbol == "AAPL"
    assert candidates[0].recommendation == "BUY"
    assert candidates[0].confidence == 82
    assert ai_response.provider == "openai"
    assert ai_response.request_id == "req-1"


@respx.mock
async def test_scan_candidates_raises_on_missing_function_call() -> None:
    respx.post("https://api.openai.com/v1/responses").mock(
        return_value=httpx.Response(200, json={"id": "resp_2", "model": "gpt-4o-mini", "output": [{"type": "message", "content": "no tools used"}]})
    )
    provider = OpenAIProvider("key", "gpt-4o-mini", 10)
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
    respx.post("https://api.openai.com/v1/responses").mock(
        return_value=httpx.Response(
            200,
            json=_function_call_response(
                [
                    {"symbol": "AAPL", "recommendation": "BUY", "confidence": 82, "summary": "ok"},
                    {"symbol": "TSLA", "recommendation": "MAYBE_BUY", "confidence": 50, "summary": "bad enum"},
                ]
            ),
        )
    )
    provider = OpenAIProvider("key", "gpt-4o-mini", 10)
    try:
        request = build_scan_request("req-3", "corr-3", "scan the market")
        with pytest.raises(ProviderDataFailure) as exc_info:
            await provider.scan_candidates(request)
    finally:
        await provider.aclose()
    assert exc_info.value.error_code == "AI_CANDIDATE_RECOMMENDATION_INVALID"


@respx.mock
async def test_scan_candidates_rejects_confidence_out_of_range() -> None:
    respx.post("https://api.openai.com/v1/responses").mock(
        return_value=httpx.Response(
            200,
            json=_function_call_response([{"symbol": "AAPL", "recommendation": "BUY", "confidence": 150, "summary": "ok"}]),
        )
    )
    provider = OpenAIProvider("key", "gpt-4o-mini", 10)
    try:
        request = build_scan_request("req-4", "corr-4", "scan the market")
        with pytest.raises(ProviderDataFailure) as exc_info:
            await provider.scan_candidates(request)
    finally:
        await provider.aclose()
    assert exc_info.value.error_code == "AI_CANDIDATE_CONFIDENCE_INVALID"


@respx.mock
async def test_non_success_response_raises_typed_http_failure() -> None:
    respx.post("https://api.openai.com/v1/responses").mock(
        return_value=httpx.Response(429, json={"error": {"message": "rate limited"}})
    )
    provider = OpenAIProvider("key", "gpt-4o-mini", 10)
    try:
        request = build_scan_request("req-5", "corr-5", "scan the market")
        with pytest.raises(ProviderHttpFailure) as exc_info:
            await provider.scan_candidates(request)
    finally:
        await provider.aclose()
    assert exc_info.value.http_status == 429


@respx.mock
async def test_scan_candidates_raises_on_malformed_json_arguments() -> None:
    """OpenAI-specific failure mode: a function_call item is present, but
    its `arguments` string is not valid JSON -- must fail closed, not crash
    with an unhandled json.JSONDecodeError."""
    respx.post("https://api.openai.com/v1/responses").mock(
        return_value=httpx.Response(200, json=_function_call_response([], arguments_json="{not valid json"))
    )
    provider = OpenAIProvider("key", "gpt-4o-mini", 10)
    try:
        request = build_scan_request("req-6", "corr-6", "scan the market")
        with pytest.raises(ProviderDataFailure) as exc_info:
            await provider.scan_candidates(request)
    finally:
        await provider.aclose()
    assert exc_info.value.error_code == "AI_TOOL_INPUT_MALFORMED"
