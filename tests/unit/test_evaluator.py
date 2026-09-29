from __future__ import annotations

import pytest

from agentic_e2e.agents.evaluator import Evaluator, EvaluatorAnswer
from agentic_e2e.config import Config
from agentic_e2e.models import ActionRecord, ExecutorResult, ExecutorRun
from agentic_e2e.trace import NullTrace
from tests.fakes import FakeMessages, make_step, message, text_block

RUN = ExecutorRun(
    result=ExecutorResult(completed=True, notes="opened the page", target_ref=None),
    actions=[
        ActionRecord(tool="click", args={"element": "Payments", "ref": "e4"}, ok=True, summary="OK")
    ],
)
STEP = make_step(1, "the user opens the Payments section")


def answer(outcome: str, retryable: bool, kind: str | None, reason: str = "because") -> dict:
    return {"outcome": outcome, "retryable": retryable, "failure_kind": kind, "reason": reason}


async def judge(*responses):
    messages = FakeMessages(*responses)
    verdict, tokens = await Evaluator(messages, Config(), NullTrace()).judge(STEP, RUN, "SNAPSHOT")
    return verdict, tokens, messages


async def test_pass() -> None:
    verdict, tokens, messages = await judge(
        message(text_block(answer("pass", False, None, "Payments heading shown")))
    )
    assert verdict.passed and verdict.source == "evaluator"
    assert verdict.reason == "Payments heading shown"
    assert tokens.prompt == 100
    request = messages.requests[0]
    assert request["output_model"] is EvaluatorAnswer
    assert request["role"] == "evaluator"
    assert "tools" not in request


@pytest.mark.parametrize(
    ("model_says", "kind", "retryable"),
    [
        (answer("fail", True, "defect"), "defect", False),  # defects never retry
        (answer("fail", False, "execution"), "execution", True),  # execution issues may
        (answer("fail", False, None), "defect", False),
    ],
)
async def test_failure_kind_decides_retryability(model_says, kind: str, retryable: bool) -> None:
    verdict, _, _ = await judge(message(text_block(model_says)))
    assert (verdict.passed, verdict.failure_kind, verdict.retryable) == (False, kind, retryable)


@pytest.mark.parametrize(
    "response",
    [
        message(
            stop_reason="refusal",
            stop_details={"type": "refusal", "category": None, "explanation": "x"},
        ),
        message(text_block("looks fine to me")),
        message(text_block('{"outcome": "pa'), stop_reason="max_tokens"),
        message(),
    ],
)
async def test_unusable_responses_are_retryable_execution_issues(response) -> None:
    verdict, _, _ = await judge(response)
    assert (verdict.passed, verdict.failure_kind, verdict.retryable) == (False, "execution", True)
    assert verdict.reason.startswith("could not judge")


async def test_prompt_carries_step_report_actions_and_snapshot() -> None:
    _, _, messages = await judge(message(text_block(answer("pass", False, None))))
    prompt = messages.requests[0]["messages"][0]["content"]
    assert "Step: When the user opens the Payments section" in prompt
    assert "completed=True; notes: opened the page" in prompt
    assert "1. click(element='Payments', ref='e4') -> OK" in prompt
    assert prompt.endswith("Page after the attempt:\nSNAPSHOT")
