"""Fakes for the deterministic core: no network, no browser, no API key."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from typing import Any

from anthropic.types.beta import BetaMessage

from agentic_e2e.browser.base import ActionOutcome, Browser, ElementReadError
from agentic_e2e.models import (
    ExecutorResult,
    ExecutorRun,
    Scenario,
    Step,
    StepType,
    TokenUsage,
    Verdict,
)

# --- model -----------------------------------------------------------------------------------


def make_step(
    index: int,
    text: str,
    effective_type: StepType = "action",
    *,
    table: list[list[str]] | None = None,
    literals: list[str] | None = None,
) -> Step:
    keyword = {"context": "Given", "action": "When", "outcome": "Then"}[effective_type]
    return Step(
        index=index,
        line=index + 3,
        keyword=keyword,
        effective_type=effective_type,
        text=text,
        table=table,
        expected_literals=literals or [],
    )


def make_scenario(name: str, *steps: Step) -> Scenario:
    return Scenario(name=name, line=1, steps=list(steps))


def done(target_ref: str | None = None, tokens: int = 10) -> ExecutorRun:
    return ExecutorRun(
        result=ExecutorResult(completed=True, notes="done", target_ref=target_ref),
        tokens=TokenUsage(prompt=tokens, completion=1, reasoning=0),
    )


def usage(**overrides: Any) -> dict[str, Any]:
    return {"input_tokens": 100, "output_tokens": 20, **overrides}


def message(
    *content: dict[str, Any],
    stop_reason: str = "end_turn",
    stop_details: dict[str, Any] | None = None,
    usage_: dict[str, Any] | None = None,
) -> BetaMessage:
    return BetaMessage.model_validate(
        {
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": "claude-opus-5-5",
            "content": list(content),
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "stop_details": stop_details,
            "usage": usage_ or usage(),
        }
    )


def text_block(payload: object) -> dict[str, Any]:
    return {"type": "text", "text": payload if isinstance(payload, str) else json.dumps(payload)}


def tool_use(name: str, tool_id: str = "toolu_1", **args: object) -> dict[str, Any]:
    return {"type": "tool_use", "id": tool_id, "name": name, "input": args}


class FakeMessages:
    """Scripted MessagesClient: returns (or raises) the queued responses in order."""

    def __init__(self, *responses: BetaMessage | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> BetaMessage:
        # Snapshot the history: the agent keeps appending to the same list.
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


# --- browser ---------------------------------------------------------------------------------


class FakeBrowser:
    def __init__(self, page: str = 'Page URL: http://app.test/\n- heading "Home" [ref=e1]') -> None:
        self.page = page
        self.tables: dict[str, list[list[str]]] = {}
        self.texts: dict[str, str] = {}
        self.calls: list[tuple[Any, ...]] = []
        self.screenshots: list[Path] = []

    def _ok(self, *call: Any) -> ActionOutcome:
        self.calls.append(call)
        return ActionOutcome(True, f"OK\n\n{self.page}")

    async def snapshot(self) -> str:
        self.calls.append(("snapshot",))
        return self.page

    async def navigate(self, url: str) -> ActionOutcome:
        return self._ok("navigate", url)

    async def click(self, element: str, ref: str) -> ActionOutcome:
        if not ref.startswith("e"):
            self.calls.append(("click", element, ref))
            return ActionOutcome(False, f"ERROR: {ref!r} is not a snapshot ref")
        return self._ok("click", element, ref)

    async def hover(self, element: str, ref: str) -> ActionOutcome:
        return self._ok("hover", element, ref)

    async def type_text(self, element: str, ref: str, text: str, submit: bool) -> ActionOutcome:
        return self._ok("type_text", element, ref, text, submit)

    async def select_option(self, element: str, ref: str, values: list[str]) -> ActionOutcome:
        return self._ok("select_option", element, ref, values)

    async def press_key(self, key: str) -> ActionOutcome:
        return self._ok("press_key", key)

    async def go_back(self) -> ActionOutcome:
        return self._ok("go_back")

    async def find_text(self, text: str) -> ActionOutcome:
        return self._ok("find_text", text)

    async def wait_for_text(self, text: str) -> ActionOutcome:
        return self._ok("wait_for_text", text)

    async def read_element_text(self, ref: str) -> str:
        self.calls.append(("read_element_text", ref))
        if ref not in self.texts:
            raise ElementReadError(f"Ref {ref} not found")
        return self.texts[ref]

    async def read_table(self, ref: str) -> list[list[str]]:
        self.calls.append(("read_table", ref))
        if ref not in self.tables:
            raise ElementReadError(f"Ref {ref} not found")
        return self.tables[ref]

    async def screenshot(self, path: Path) -> bool:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x89PNG\r\n\x1a\n fake")
        self.screenshots.append(path)
        return True


def factory_for(
    browser: Browser, starts: list[Path] | None = None, fail: Exception | None = None
) -> Callable[[Path], AbstractAsyncContextManager[Browser]]:
    @asynccontextmanager
    async def factory(workdir: Path) -> AsyncIterator[Browser]:
        if starts is not None:
            starts.append(workdir)
        if fail is not None:
            raise fail
        yield browser

    return factory


# --- agents ----------------------------------------------------------------------------------


class ScriptedExecutor:
    """Returns queued runs per step index; defaults to a completed run."""

    def __init__(self, script: dict[int, list[ExecutorRun | Exception]] | None = None) -> None:
        self.script = script or {}
        self.calls: list[int] = []

    async def run(self, step: Step, browser: Browser) -> ExecutorRun:
        self.calls.append(step.index)
        queue = self.script.get(step.index)
        item = queue.pop(0) if queue else done()
        if isinstance(item, Exception):
            raise item
        return item


class ScriptedEvaluator:
    """Returns queued verdicts per step index; defaults to a pass."""

    def __init__(self, script: dict[int, list[Verdict]] | None = None) -> None:
        self.script = script or {}
        self.calls: list[int] = []

    async def judge(
        self, step: Step, run: ExecutorRun, snapshot: str
    ) -> tuple[Verdict, TokenUsage]:
        self.calls.append(step.index)
        queue = self.script.get(step.index)
        verdict = queue.pop(0) if queue else Verdict.ok("evaluator", "looks right")
        return verdict, TokenUsage(prompt=5, completion=2, cache_read=3, reasoning=1)


def fail(kind: str = "execution", reason: str = "not yet") -> Verdict:
    return Verdict.fail("evaluator", kind, reason, retryable=kind == "execution")  # type: ignore[arg-type]
