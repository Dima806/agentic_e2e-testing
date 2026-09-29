from __future__ import annotations

import anthropic
import httpx2
import pytest
from anthropic.types.beta import BetaUsage

from agentic_e2e.agents.llm import (
    FALLBACK_BETA,
    NO_CREDENTIALS,
    AnthropicMessages,
    InfrastructureError,
    call,
    preflight,
    stop_problem,
    token_usage,
)
from agentic_e2e.models import ExecutorResult, TokenUsage
from tests.fakes import FakeMessages, message

REQUEST = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


def test_token_usage_maps_api_fields_to_report_fields() -> None:
    usage = BetaUsage.model_validate(
        {
            "input_tokens": 100,
            "output_tokens": 40,
            "cache_read_input_tokens": 900,
            "cache_creation_input_tokens": 50,
            "output_tokens_details": {"thinking_tokens": 25},
        }
    )
    assert token_usage(usage) == TokenUsage(
        prompt=1050, completion=40, cache_read=900, cache_write=50, reasoning=25
    )


def test_missing_thinking_breakdown_is_unknown_not_zero() -> None:
    usage = token_usage(BetaUsage.model_validate({"input_tokens": 10, "output_tokens": 5}))
    assert usage.reasoning is None
    assert (usage + TokenUsage(reasoning=3)).reasoning is None
    assert (TokenUsage(reasoning=2) + TokenUsage(reasoning=3)).reasoning == 5


def body(**kwargs):
    client = AnthropicMessages(anthropic.AsyncAnthropic(api_key="test"), **kwargs)
    return client.request(
        model="claude-opus-5-5",
        effort="medium",
        system="SYSTEM",
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
        output_model=ExecutorResult,
    )


def test_request_body_uses_effort_structured_output_caching_and_fallbacks() -> None:
    request = body()
    assert request["output_config"]["effort"] == "medium"
    schema = request["output_config"]["format"]["schema"]
    assert request["output_config"]["format"]["type"] == "json_schema"
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"completed", "notes", "target_ref"}
    assert request["cache_control"] == {"type": "ephemeral"}
    assert request["betas"] == [FALLBACK_BETA]
    assert request["fallbacks"] == "default"
    assert "tools" not in request
    for forbidden in ("temperature", "top_p", "thinking", "tool_choice"):
        assert forbidden not in request


def test_fallbacks_can_be_disabled() -> None:
    request = body(refusal_fallbacks=False)
    assert "betas" not in request and "fallbacks" not in request


@pytest.mark.parametrize(
    "error",
    [
        anthropic.APIConnectionError(request=REQUEST),
        anthropic.RateLimitError(
            "slow down", response=httpx2.Response(429, request=REQUEST), body=None
        ),
        TypeError("Could not resolve authentication method. Expected one of api_key"),
    ],
)
async def test_api_failures_become_infrastructure_errors(error: Exception) -> None:
    with pytest.raises(InfrastructureError):
        await call(FakeMessages(error), messages=[])


async def test_unrelated_type_errors_are_bugs_not_infrastructure() -> None:
    with pytest.raises(TypeError):
        await call(FakeMessages(TypeError("unsupported operand")), messages=[])


@pytest.mark.parametrize(
    ("stop_reason", "expected"),
    [
        ("end_turn", None),
        ("tool_use", None),
        ("max_tokens", "max_tokens"),
        ("pause_turn", "unexpected stop_reason"),
        ("refusal", "model refused (category: unspecified)"),
    ],
)
def test_stop_problem(stop_reason: str, expected: str | None) -> None:
    problem = stop_problem(message(stop_reason=stop_reason))
    assert (problem is None) if expected is None else (expected in (problem or ""))


class FakeModels:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.retrieved: list[str] = []

    async def retrieve(self, model: str) -> object:
        self.retrieved.append(model)
        if self.error is not None:
            raise self.error
        return object()


class FakeClient:
    def __init__(self, error: Exception | None = None) -> None:
        self.models = FakeModels(error)


async def test_preflight_checks_each_distinct_model_once() -> None:
    client = FakeClient()
    await preflight(client, ["claude-opus-5-5", "claude-opus-5-5", "claude-sonnet-5-5"])  # type: ignore[arg-type]
    assert client.models.retrieved == ["claude-opus-5-5", "claude-sonnet-5-5"]


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (TypeError("Could not resolve authentication method."), "export ANTHROPIC_API_KEY"),
        (
            anthropic.AuthenticationError(
                "bad key", response=httpx2.Response(401, request=REQUEST), body=None
            ),
            r"rejected the credentials \(401\)",
        ),
        (
            anthropic.NotFoundError(
                "nope", response=httpx2.Response(404, request=REQUEST), body=None
            ),
            "is not available to these credentials",
        ),
        (anthropic.APIConnectionError(request=REQUEST), "cannot reach the Claude API"),
    ],
)
async def test_preflight_failures_are_actionable(error: Exception, message: str) -> None:
    with pytest.raises(InfrastructureError, match=message):
        await preflight(FakeClient(error), ["claude-opus-5-5"])  # type: ignore[arg-type]


def test_missing_credentials_message_fits_codespaces() -> None:
    assert "Codespaces secret named ANTHROPIC_API_KEY" in NO_CREDENTIALS
    assert "stop and restart the codespace" in NO_CREDENTIALS
    assert "ant auth" not in NO_CREDENTIALS
