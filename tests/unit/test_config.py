from __future__ import annotations

import re
from pathlib import Path

import pytest

from agentic_e2e.config import Config, ConfigError

ROOT = Path(__file__).resolve().parents[2]


def test_defaults_match_the_spec() -> None:
    cfg = Config()
    assert cfg.max_attempts == 2
    assert cfg.max_tool_calls_per_attempt == 15
    assert (cfg.executor_model, cfg.evaluator_model) == ("claude-opus-5-5", "claude-opus-5-5")
    assert (cfg.executor_effort, cfg.evaluator_effort) == ("medium", "medium")
    assert cfg.max_tokens == 16000
    assert cfg.headless and cfg.refusal_fallbacks


def test_environment_overrides_are_typed() -> None:
    cfg = Config.from_env(
        {
            "AGENTIC_E2E_MAX_ATTEMPTS": "3",
            "AGENTIC_E2E_EVALUATOR_MODEL": "claude-sonnet-5-5",
            "AGENTIC_E2E_HEADLESS": "false",
            "AGENTIC_E2E_BROWSER_CALL_TIMEOUT_S": "12.5",
            "AGENTIC_E2E_BASE_URL": "http://app.test/",
        }
    )
    assert cfg.max_attempts == 3
    assert cfg.evaluator_model == "claude-sonnet-5-5"
    assert cfg.executor_model == "claude-opus-5-5"
    assert cfg.headless is False
    assert cfg.browser_call_timeout_s == 12.5
    assert cfg.base_url == "http://app.test/"


def test_explicit_overrides_win_and_none_is_ignored() -> None:
    cfg = Config.from_env({"AGENTIC_E2E_BASE_URL": "http://env/"}, base_url=None, headless=False)
    assert cfg.base_url == "http://env/"
    assert cfg.headless is False
    assert (
        Config.from_env({"AGENTIC_E2E_BASE_URL": "http://env/"}, base_url="http://cli/").base_url
        == "http://cli/"
    )


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"AGENTIC_E2E_MAX_ATTEMPTS": "two"}, "not an integer"),
        ({"AGENTIC_E2E_HEADLESS": "maybe"}, "not a boolean"),
        ({"AGENTIC_E2E_MAX_ATTEMPTS": "0"}, "at least 1"),
        ({"AGENTIC_E2E_EXECUTOR_EFFORT": "extreme"}, "executor_effort must be one of"),
    ],
)
def test_invalid_overrides_are_rejected(env: dict[str, str], message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        Config.from_env(env)


def test_playwright_mcp_pin_matches_the_makefile() -> None:
    makefile = (ROOT / "Makefile").read_text()
    match = re.search(r"^PLAYWRIGHT_MCP_VERSION \?= (\S+)$", makefile, re.MULTILINE)
    assert match is not None
    assert match.group(1) == Config().playwright_mcp_version
