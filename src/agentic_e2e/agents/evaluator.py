"""Evaluator agent: judges one attempt of a step without expected data."""

from __future__ import annotations

from importlib.resources import files
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agentic_e2e.agents.llm import MessagesClient, call, final_text, stop_problem, token_usage
from agentic_e2e.config import Config
from agentic_e2e.models import ExecutorRun, Step, TokenUsage, Verdict
from agentic_e2e.trace import Trace

SYSTEM_PROMPT = (files("agentic_e2e.agents") / "prompts" / "evaluator.md").read_text("utf-8")


class EvaluatorAnswer(BaseModel):
    """The evaluator's structured verdict, as the model writes it."""

    model_config = ConfigDict(extra="forbid")

    outcome: Literal["pass", "fail"]
    retryable: bool
    failure_kind: Literal["defect", "execution"] | None
    reason: str = Field(description="One actionable sentence: what was expected, what is shown.")

    def to_verdict(self) -> Verdict:
        if self.outcome == "pass":
            return Verdict.ok("evaluator", self.reason)
        # The kind decides retryability: defects never retry, execution issues always may.
        kind = self.failure_kind or "defect"
        return Verdict.fail("evaluator", kind, self.reason, retryable=kind == "execution")


class Evaluator:
    def __init__(self, messages: MessagesClient, cfg: Config, trace: Trace) -> None:
        self._messages = messages
        self._cfg = cfg
        self._trace = trace

    async def judge(
        self, step: Step, run: ExecutorRun, snapshot: str
    ) -> tuple[Verdict, TokenUsage]:
        message = await call(
            self._messages,
            role="evaluator",
            model=self._cfg.evaluator_model,
            effort=self._cfg.evaluator_effort,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": judge_prompt(step, run, snapshot)}],
            output_model=EvaluatorAnswer,
        )
        usage = token_usage(message.usage)
        text = final_text(message)
        self._trace.event(
            "llm_response",
            role="evaluator",
            model=message.model,
            stop_reason=message.stop_reason,
            tokens=usage.model_dump(),
            text=text,
        )
        problem = stop_problem(message)
        if problem is None and text is None:
            problem = "evaluator returned no verdict"
        if problem is None and text is not None:
            try:
                return EvaluatorAnswer.model_validate_json(text).to_verdict(), usage
            except ValidationError as exc:
                problem = f"evaluator's verdict is not valid JSON for the schema: {exc}"
        return (
            Verdict.fail("evaluator", "execution", f"could not judge: {problem}", retryable=True),
            usage,
        )


def judge_prompt(step: Step, run: ExecutorRun, snapshot: str) -> str:
    result = run.result
    report = f"completed={result.completed}; notes: {result.notes}" if result else "no report"
    actions = (
        "\n".join(
            f"{i}. {a.tool}({', '.join(f'{k}={v!r}' for k, v in a.args.items())}) -> {a.summary}"
            for i, a in enumerate(run.actions, start=1)
        )
        or "(no browser actions)"
    )
    return "\n\n".join(
        [
            f"Step: {step.display()}",
            f"Step type: {step.effective_type}",
            f"Executor report: {report}",
            f"Browser actions:\n{actions}",
            f"Page after the attempt:\n{snapshot}",
        ]
    )
