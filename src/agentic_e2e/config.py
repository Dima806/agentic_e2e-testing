"""Runtime configuration: a frozen dataclass with AGENTIC_E2E_* environment overrides."""

from __future__ import annotations

import dataclasses
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, get_args

Effort = Literal["low", "medium", "high", "xhigh", "max"]

ENV_PREFIX = "AGENTIC_E2E_"


class ConfigError(ValueError):
    """An environment override could not be parsed."""


@dataclass(frozen=True)
class Config:
    # Loop policy (CLAUDE.md invariants 4 and 5).
    max_attempts: int = 2
    max_tool_calls_per_attempt: int = 15

    # Agents. Exact model IDs, no date suffixes; effort is set explicitly because
    # claude-opus-5-5 defaults to "medium" and ignores sampling parameters.
    executor_model: str = "claude-opus-5-5"
    evaluator_model: str = "claude-opus-5-5"
    executor_effort: Effort = "medium"
    evaluator_effort: Effort = "medium"
    max_tokens: int = 16000
    # Server-side refusal fallbacks (Claude API only; disable on Bedrock/Vertex/Foundry).
    refusal_fallbacks: bool = True

    # Browser. Keep playwright_mcp_version in sync with PLAYWRIGHT_MCP_VERSION in the Makefile.
    playwright_mcp_version: str = "0.0.83"
    npx_command: str = "npx"
    headless: bool = True
    browser_call_timeout_s: float = 60.0

    # Application under test; handed to the executor so "the dashboard page" can be resolved.
    base_url: str | None = None

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ConfigError("max_attempts must be at least 1")
        if self.max_tool_calls_per_attempt < 1:
            raise ConfigError("max_tool_calls_per_attempt must be at least 1")
        for name in ("executor_effort", "evaluator_effort"):
            if getattr(self, name) not in get_args(Effort):
                raise ConfigError(f"{name} must be one of {', '.join(get_args(Effort))}")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, **overrides: Any) -> Config:
        """Build a config from defaults, then AGENTIC_E2E_* variables, then explicit overrides."""
        env = os.environ if env is None else env
        values: dict[str, Any] = {}
        for field in dataclasses.fields(cls):
            raw = env.get(ENV_PREFIX + field.name.upper())
            if raw is not None:
                values[field.name] = _coerce(field.name, raw, field.default)
        values.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**values)

    def replace(self, **changes: Any) -> Config:
        return dataclasses.replace(self, **changes)


def _coerce(name: str, raw: str, default: object) -> object:
    if isinstance(default, bool):
        lowered = raw.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
        raise ConfigError(f"{ENV_PREFIX}{name.upper()}={raw!r} is not a boolean")
    if isinstance(default, int):
        try:
            return int(raw)
        except ValueError:
            raise ConfigError(f"{ENV_PREFIX}{name.upper()}={raw!r} is not an integer") from None
    if isinstance(default, float):
        try:
            return float(raw)
        except ValueError:
            raise ConfigError(f"{ENV_PREFIX}{name.upper()}={raw!r} is not a number") from None
    return raw
