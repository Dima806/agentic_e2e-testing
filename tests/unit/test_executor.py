from __future__ import annotations

import pytest

from agentic_e2e.agents.executor import TOOL_PARAMS, Executor, task_prompt
from agentic_e2e.config import Config
from agentic_e2e.models import ExecutorResult
from agentic_e2e.trace import NullTrace
from tests.fakes import FakeBrowser, FakeMessages, make_step, message, text_block, tool_use, usage

ANSWER = {"completed": True, "notes": "clicked it", "target_ref": None}


def executor(messages: FakeMessages, **cfg: object) -> Executor:
    return Executor(messages, Config(**cfg), NullTrace())  # type: ignore[arg-type]


async def test_runs_tools_then_returns_the_structured_answer() -> None:
    messages = FakeMessages(
        message(tool_use("click", element="Payments link", ref="e4"), stop_reason="tool_use"),
        message(text_block(ANSWER)),
    )
    browser = FakeBrowser()
    run = await executor(messages).run(make_step(1, "the user opens Payments"), browser)

    assert run.problem is None
    assert run.result == ExecutorResult(**ANSWER)
    assert ("click", "Payments link", "e4") in browser.calls
    assert [(a.tool, a.ok) for a in run.actions] == [("click", True)]
    assert run.tokens.prompt == 200 and run.tokens.completion == 40

    second = messages.requests[1]["messages"]
    assert [m["role"] for m in second] == ["user", "assistant", "user"]
    result = second[2]["content"][0]
    assert (result["type"], result["tool_use_id"], result["is_error"]) == (
        "tool_result",
        "toolu_1",
        False,
    )


async def test_request_shape_follows_the_opus_rules() -> None:
    messages = FakeMessages(message(text_block(ANSWER)))
    await executor(messages, executor_effort="high").run(make_step(0, "x"), FakeBrowser())
    request = messages.requests[0]
    assert request["role"] == "executor"
    assert request["model"] == "claude-opus-5-5"
    assert request["effort"] == "high"
    assert request["output_model"] is ExecutorResult
    assert request["tools"] == TOOL_PARAMS
    assert "tool_choice" not in request
    assert "temperature" not in request


def test_tools_are_strict_intent_level_and_selector_free() -> None:
    names = [t["name"] for t in TOOL_PARAMS]
    assert names == [
        "navigate",
        "click",
        "hover",
        "type_text",
        "select_option",
        "press_key",
        "go_back",
        "find_text",
        "wait_for_text",
        "snapshot",
    ]
    for tool in TOOL_PARAMS:
        assert tool["strict"] is True
        schema = tool["input_schema"]
        assert schema["additionalProperties"] is False
        assert set(schema.get("required", [])) == set(schema.get("properties", {}))
        assert "selector" not in str(schema).lower()


async def test_bad_ref_and_bad_arguments_come_back_as_tool_errors() -> None:
    messages = FakeMessages(
        message(
            tool_use("click", "t1", element="Submit", ref="#submit"),
            tool_use("click", "t2", element="Submit"),  # missing ref
            tool_use("teleport", "t3"),
            stop_reason="tool_use",
        ),
        message(text_block({**ANSWER, "completed": False})),
    )
    run = await executor(messages).run(make_step(0, "submit"), FakeBrowser())
    results = messages.requests[1]["messages"][2]["content"]
    assert [r["is_error"] for r in results] == [True, True, True]
    assert "invalid arguments" in results[1]["content"]
    assert "unknown tool" in results[2]["content"]
    assert run.result is not None and run.result.completed is False


async def test_tool_call_cap_ends_the_attempt() -> None:
    messages = FakeMessages(
        *[message(tool_use("snapshot", f"t{i}"), stop_reason="tool_use") for i in range(3)]
    )
    run = await executor(messages, max_tool_calls_per_attempt=2).run(
        make_step(0, "x"), FakeBrowser()
    )
    assert run.result is None
    assert run.problem is not None and "cap of 2" in run.problem
    assert len(run.actions) == 2


@pytest.mark.parametrize(
    ("response", "problem"),
    [
        (
            message(
                stop_reason="refusal",
                stop_details={"type": "refusal", "category": "cyber", "explanation": "x"},
            ),
            "model refused (category: cyber)",
        ),
        (message(text_block('{"completed": tr'), stop_reason="max_tokens"), "max_tokens"),
        (message(text_block("I clicked it!")), "not valid JSON"),
        (message(), "without a final answer"),
    ],
)
async def test_unusable_responses_become_problems(response, problem: str) -> None:
    run = await executor(FakeMessages(response)).run(make_step(0, "x"), FakeBrowser())
    assert run.result is None
    assert run.problem is not None and problem in run.problem


async def test_only_the_current_step_is_sent_never_the_scenario() -> None:
    messages = FakeMessages(message(text_block(ANSWER)))
    step = make_step(3, "the user submits the refund request")
    await executor(messages, base_url="http://app.test/").run(step, FakeBrowser())
    prompt = messages.requests[0]["messages"][0]["content"]
    assert prompt.startswith("Step: When the user submits the refund request")
    assert "Application base URL: http://app.test/" in prompt
    assert prompt.count("Step:") == 1


def test_expected_values_are_never_handed_to_the_executor() -> None:
    table = [["Status", "Amount"], ["Successful", "272.00"]]
    step = make_step(0, "the refunds table should be shown", "outcome", table=table)
    prompt = task_prompt(step, "page", None)
    assert "Status | Amount" in prompt  # the header identifies the table
    assert "272.00" not in prompt and "Successful" not in prompt


def test_input_tables_are_handed_to_the_executor() -> None:
    step = make_step(0, "the user fills the form", table=[["Field", "Value"], ["Amount", "10.00"]])
    assert "Amount | 10.00" in task_prompt(step, "page", None)


async def test_pre_fallback_blocks_are_not_echoed_back() -> None:
    declined = message(
        {"type": "thinking", "thinking": "", "signature": "sig"},
        tool_use("click", "t0", element="x", ref="e1"),
        {
            "type": "fallback",
            "from": {"model": "claude-opus-5-5"},
            "to": {"model": "claude-opus-4-8"},
            "trigger": {"type": "refusal"},
        },
        tool_use("hover", "t1", element="row", ref="e2"),
        stop_reason="tool_use",
        usage_=usage(output_tokens_details={"thinking_tokens": 7}),
    )
    messages = FakeMessages(declined, message(text_block(ANSWER)))
    run = await executor(messages).run(make_step(0, "x"), FakeBrowser())
    echoed = messages.requests[1]["messages"][1]["content"]
    assert [b.type for b in echoed] == ["fallback", "tool_use"]
    assert echoed[1].id == "t1"
    assert [a.tool for a in run.actions] == ["hover"]  # the declined model's call never ran
    assert run.tokens.reasoning is None  # the final response lacked the breakdown


async def test_declined_tool_calls_before_a_fallback_are_not_executed() -> None:
    messages = FakeMessages(
        message(
            tool_use("click", "t0", element="x", ref="e1"),
            {
                "type": "fallback",
                "from": {"model": "claude-opus-5-5"},
                "to": {"model": "claude-opus-4-8"},
                "trigger": {"type": "refusal"},
            },
            text_block(ANSWER),
        )
    )
    browser = FakeBrowser()
    run = await executor(messages).run(make_step(0, "x"), browser)
    assert run.result is not None
    assert not any(call[0] == "click" for call in browser.calls)
