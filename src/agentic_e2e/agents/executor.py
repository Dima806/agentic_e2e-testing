"""Executor agent: performs exactly one step through intent-level browser tools."""

from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import files

import anthropic
from anthropic.types.beta import BetaMessageParam, BetaToolParam, BetaToolResultBlockParam
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agentic_e2e.agents.llm import (
    MessagesClient,
    call,
    echo_content,
    final_text,
    stop_problem,
    token_usage,
)
from agentic_e2e.browser.base import ActionOutcome, Browser
from agentic_e2e.config import Config
from agentic_e2e.models import ActionRecord, ExecutorResult, ExecutorRun, Step
from agentic_e2e.trace import Trace

SYSTEM_PROMPT = (files("agentic_e2e.agents") / "prompts" / "executor.md").read_text("utf-8")


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NavigateArgs(_Args):
    url: str = Field(description="Absolute URL to open.")


class ElementArgs(_Args):
    element: str = Field(description="Plain-words description of the element, e.g. 'Refund link'.")
    ref: str = Field(description="The element's ref from the latest snapshot, e.g. e12.")


class TypeArgs(ElementArgs):
    text: str = Field(description="Text to type into the element.")
    submit: bool = Field(description="Press Enter after typing.")


class SelectArgs(ElementArgs):
    values: list[str] = Field(description="Option labels or values to select.")


class KeyArgs(_Args):
    key: str = Field(description="Key name, e.g. Enter, Escape, ArrowDown.")


class TextArgs(_Args):
    text: str = Field(description="Visible text to look for.")


class NoArgs(_Args):
    pass


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    args: type[_Args]


TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec("navigate", "Open a URL in the browser.", NavigateArgs),
    ToolSpec("click", "Click an element.", ElementArgs),
    ToolSpec(
        "hover", "Move the pointer over an element, e.g. to reveal hidden controls.", ElementArgs
    ),
    ToolSpec("type_text", "Type text into an input or editable element.", TypeArgs),
    ToolSpec("select_option", "Choose option(s) in a select element.", SelectArgs),
    ToolSpec("press_key", "Press a keyboard key on the focused element.", KeyArgs),
    ToolSpec("go_back", "Go back to the previous page.", NoArgs),
    ToolSpec(
        "find_text",
        "Search the page for text; returns matching snapshot nodes with their refs.",
        TextArgs,
    ),
    ToolSpec("wait_for_text", "Wait until the given text appears on the page.", TextArgs),
    ToolSpec("snapshot", "Capture a fresh accessibility snapshot of the page.", NoArgs),
)
_SPECS = {spec.name: spec for spec in TOOLS}

# Built once: the tool list must be byte-identical across requests for prompt caching.
TOOL_PARAMS: list[BetaToolParam] = [
    {
        "name": spec.name,
        "description": spec.description,
        "input_schema": anthropic.transform_schema(spec.args),
        "strict": True,
    }
    for spec in TOOLS
]


class Executor:
    def __init__(self, messages: MessagesClient, cfg: Config, trace: Trace) -> None:
        self._messages = messages
        self._cfg = cfg
        self._trace = trace

    async def run(self, step: Step, browser: Browser) -> ExecutorRun:
        cap = self._cfg.max_tool_calls_per_attempt
        snapshot = await browser.snapshot()
        history: list[BetaMessageParam] = [
            {"role": "user", "content": task_prompt(step, snapshot, self._cfg.base_url)}
        ]
        run = ExecutorRun()
        calls = 0
        # One more model turn than tool calls: the last turn gives the final answer.
        for _ in range(cap + 1):
            message = await call(
                self._messages,
                role="executor",
                model=self._cfg.executor_model,
                effort=self._cfg.executor_effort,
                system=SYSTEM_PROMPT,
                messages=history,
                tools=TOOL_PARAMS,
                output_model=ExecutorResult,
            )
            usage = token_usage(message.usage)
            run.tokens = run.tokens + usage
            self._trace.event(
                "llm_response",
                role="executor",
                model=message.model,
                stop_reason=message.stop_reason,
                tokens=usage.model_dump(),
                text=final_text(message),
            )

            problem = stop_problem(message)
            if problem is not None:
                run.problem = problem
                return run

            # Only calls that survive echoing are run: a declined model's calls before a refusal
            # fallback are neither sent back nor executed.
            echoed = echo_content(message.content)
            tool_uses = [b for b in echoed if b.type == "tool_use"]
            if not tool_uses:
                return self._finish(run, message_text=final_text(message))

            history.append({"role": "assistant", "content": echoed})
            results: list[BetaToolResultBlockParam] = []
            for block in tool_uses:
                calls += 1
                if calls > cap:
                    run.problem = f"tool-call cap of {cap} reached before the step was done"
                    return run
                outcome, record = await self._perform(block.name, block.input, browser)
                run.actions.append(record)
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": outcome.message,
                        "is_error": not outcome.ok,
                    }
                )
            history.append({"role": "user", "content": results})

        run.problem = f"tool-call cap of {cap} reached before the step was done"
        return run

    def _finish(self, run: ExecutorRun, message_text: str | None) -> ExecutorRun:
        if message_text is None:
            run.problem = "executor ended without a final answer"
            return run
        try:
            run.result = ExecutorResult.model_validate_json(message_text)
        except ValidationError as exc:
            run.problem = f"executor's final answer is not valid JSON for the schema: {exc}"
        return run

    async def _perform(
        self, name: str, raw_input: object, browser: Browser
    ) -> tuple[ActionOutcome, ActionRecord]:
        spec = _SPECS.get(name)
        args_dict = raw_input if isinstance(raw_input, dict) else {}
        if spec is None:
            outcome = ActionOutcome(False, f"unknown tool {name!r}")
        else:
            try:
                args = spec.args.model_validate(raw_input)
            except ValidationError as exc:
                outcome = ActionOutcome(False, f"invalid arguments for {name}: {exc}")
            else:
                self._trace.event("tool_call", tool=name, args=args.model_dump())
                outcome = await dispatch(browser, name, args)
        self._trace.event("tool_result", tool=name, ok=outcome.ok, message=outcome.message)
        record = ActionRecord(
            tool=name,
            args={str(k): v for k, v in args_dict.items()},
            ok=outcome.ok,
            summary=outcome.message.split("\n", 1)[0][:300],
        )
        return outcome, record


async def dispatch(browser: Browser, name: str, args: BaseModel) -> ActionOutcome:
    """Map an intent-level tool call onto the Browser interface."""
    if isinstance(args, NavigateArgs):
        return await browser.navigate(args.url)
    if isinstance(args, TypeArgs):
        return await browser.type_text(args.element, args.ref, args.text, args.submit)
    if isinstance(args, SelectArgs):
        return await browser.select_option(args.element, args.ref, args.values)
    if isinstance(args, ElementArgs):
        if name == "hover":
            return await browser.hover(args.element, args.ref)
        return await browser.click(args.element, args.ref)
    if isinstance(args, KeyArgs):
        return await browser.press_key(args.key)
    if isinstance(args, TextArgs):
        if name == "find_text":
            return await browser.find_text(args.text)
        return await browser.wait_for_text(args.text)
    if name == "go_back":
        return await browser.go_back()
    return ActionOutcome(True, await browser.snapshot())


def task_prompt(step: Step, snapshot: str, base_url: str | None) -> str:
    """The single user message: this step only, never the scenario (CLAUDE.md invariant 3)."""
    parts = [f"Step: {step.display()}", f"Step type: {step.effective_type}"]
    if step.effective_type == "outcome" and step.table is not None:
        header = " | ".join(step.table[0])
        parts.append(
            "This step checks a table with the columns: "
            f"{header}. Locate that table and return its ref as target_ref."
        )
    elif step.has_expected_data:
        parts.append(
            "This step checks specific on-screen values. Locate the single element that should "
            "show them and return its ref as target_ref."
        )
    elif step.table is not None:
        rows = "\n".join(" | ".join(row) for row in step.table)
        parts.append(f"Input table for this step:\n{rows}")
    elif step.docstring is not None:
        parts.append(f"Input text for this step:\n{step.docstring}")
    if base_url:
        parts.append(f"Application base URL: {base_url}")
    parts.append(f"Current page:\n{snapshot}")
    return "\n\n".join(parts)
