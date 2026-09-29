"""Thin wrapper over the Anthropic Messages API: one place for request shape, usage and errors."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import anthropic
from anthropic.types.beta import (
    BetaContentBlock,
    BetaMessage,
    BetaMessageParam,
    BetaToolParam,
    BetaUsage,
)
from pydantic import BaseModel

from agentic_e2e.config import Effort
from agentic_e2e.models import TokenUsage

# Server-side refusal fallback, routed by refusal category ("default" form).
FALLBACK_BETA = "server-side-fallback-2026-07-01"

_DROP_BEFORE_FALLBACK = {"thinking", "redacted_thinking", "tool_use", "server_tool_use"}


class InfrastructureError(Exception):
    """The model API failed after the SDK's own retries. The feature is blocked."""


class MessagesClient(Protocol):
    """What the agents need from the model API; faked in unit tests."""

    async def create(
        self,
        *,
        role: str,
        model: str,
        effort: Effort,
        system: str,
        messages: Sequence[BetaMessageParam],
        tools: Sequence[BetaToolParam] = (),
        output_model: type[BaseModel] | None = None,
    ) -> BetaMessage: ...


@dataclass
class AnthropicMessages:
    client: anthropic.AsyncAnthropic
    max_tokens: int = 16000
    refusal_fallbacks: bool = True

    async def create(
        self,
        *,
        role: str,
        model: str,
        effort: Effort,
        system: str,
        messages: Sequence[BetaMessageParam],
        tools: Sequence[BetaToolParam] = (),
        output_model: type[BaseModel] | None = None,
    ) -> BetaMessage:
        body = self.request(
            model=model,
            effort=effort,
            system=system,
            messages=messages,
            tools=tools,
            output_model=output_model,
        )
        message: BetaMessage = await self.client.beta.messages.create(**body)
        return message

    def request(
        self,
        *,
        model: str,
        effort: Effort,
        system: str,
        messages: Sequence[BetaMessageParam],
        tools: Sequence[BetaToolParam],
        output_model: type[BaseModel] | None,
    ) -> dict[str, Any]:
        """The request body. Byte-stable prefix (tools, system) so prompt caching can hit.

        No temperature/top_p/thinking settings: claude-opus-5-5 rejects them; effort is the only
        control. tool_choice stays at its default ("auto"): forced tool choice is rejected.
        """
        output_config: dict[str, Any] = {"effort": effort}
        if output_model is not None:
            output_config["format"] = {
                "type": "json_schema",
                "schema": anthropic.transform_schema(output_model),
            }
        body: dict[str, Any] = {
            "model": model,
            "max_tokens": self.max_tokens,
            "system": system,
            "messages": list(messages),
            "output_config": output_config,
            "cache_control": {"type": "ephemeral"},
        }
        if tools:
            body["tools"] = list(tools)
        if self.refusal_fallbacks:
            body["betas"] = [FALLBACK_BETA]
            body["fallbacks"] = "default"
        return body


NO_CREDENTIALS = (
    "no Claude API credentials found: ANTHROPIC_API_KEY is not set in this shell. "
    "Run `export ANTHROPIC_API_KEY=<your key>` (key from "
    "https://console.anthropic.com/settings/keys), or in GitHub Codespaces store it as a "
    "Codespaces secret named ANTHROPIC_API_KEY. A secret added while the codespace is running "
    "is not visible to existing terminals: stop and restart the codespace, then check with "
    "`echo ${ANTHROPIC_API_KEY:+set}`"
)


def api_error(exc: BaseException) -> InfrastructureError | None:
    """Translate an SDK failure into an actionable InfrastructureError; None if it is a bug."""
    if isinstance(exc, TypeError):
        # The SDK signals missing credentials with a TypeError at request time.
        return InfrastructureError(NO_CREDENTIALS) if "authentication" in str(exc) else None
    if isinstance(exc, anthropic.AuthenticationError):
        return InfrastructureError(
            "the Claude API rejected the credentials (401): check ANTHROPIC_API_KEY"
        )
    if isinstance(exc, anthropic.PermissionDeniedError):
        return InfrastructureError(f"these credentials may not use this model (403): {exc.message}")
    if isinstance(exc, anthropic.APIStatusError):
        return InfrastructureError(
            f"Claude API error {exc.status_code} (request {exc.request_id}): {exc.message}"
        )
    if isinstance(exc, anthropic.APIConnectionError):
        return InfrastructureError(f"cannot reach the Claude API: {exc}")
    if isinstance(exc, anthropic.AnthropicError):
        return InfrastructureError(f"Claude API client error: {exc}")
    return None


async def call(messages_client: MessagesClient, **kwargs: Any) -> BetaMessage:
    """Call the model; API failures (after SDK retries) become InfrastructureError."""
    try:
        return await messages_client.create(**kwargs)
    except (anthropic.AnthropicError, TypeError) as exc:
        error = api_error(exc)
        if error is None:
            raise
        raise error from exc


async def preflight(client: anthropic.AsyncAnthropic, models: Iterable[str]) -> None:
    """Check credentials and model access before any browser starts. Spends no tokens."""
    for model in dict.fromkeys(models):
        try:
            await client.models.retrieve(model)
        except anthropic.NotFoundError as exc:
            raise InfrastructureError(
                f"model {model!r} is not available to these credentials (404)"
            ) from exc
        except (anthropic.AnthropicError, TypeError) as exc:
            error = api_error(exc)
            if error is None:
                raise
            raise error from exc


def token_usage(usage: BetaUsage) -> TokenUsage:
    """Map API usage to report fields (CLAUDE.md, Token accounting)."""
    cache_read = usage.cache_read_input_tokens or 0
    cache_write = usage.cache_creation_input_tokens or 0
    details = usage.output_tokens_details
    return TokenUsage(
        prompt=usage.input_tokens + cache_read + cache_write,
        completion=usage.output_tokens,
        cache_read=cache_read,
        cache_write=cache_write,
        reasoning=details.thinking_tokens if details is not None else None,
    )


def echo_content(content: Iterable[BetaContentBlock]) -> list[BetaContentBlock]:
    """Assistant content to append to the history, unchanged except after a refusal fallback.

    Blocks of the declined model before the last fallback marker (thinking, tool_use) must not be
    echoed; everything else is appended exactly as returned.
    """
    blocks = list(content)
    last_fallback = max((i for i, b in enumerate(blocks) if b.type == "fallback"), default=-1)
    return [
        b
        for i, b in enumerate(blocks)
        if not (i < last_fallback and b.type in _DROP_BEFORE_FALLBACK)
    ]


def final_text(message: BetaMessage) -> str | None:
    texts = [b.text for b in message.content if b.type == "text"]
    return texts[-1] if texts else None


def stop_problem(message: BetaMessage) -> str | None:
    """Why a response cannot be used, or None. Always checked before reading content."""
    reason = message.stop_reason
    if reason == "refusal":
        details = message.stop_details
        category = getattr(details, "category", None) if details is not None else None
        return f"model refused (category: {category or 'unspecified'})"
    if reason == "max_tokens":
        return "model hit max_tokens before finishing"
    if reason in {"end_turn", "tool_use"}:
        return None
    return f"unexpected stop_reason {reason!r}"
