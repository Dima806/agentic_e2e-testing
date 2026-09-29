"""Typed boundaries shared across the pipeline: Pydantic for every structured boundary."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

StepType = Literal["context", "action", "outcome"]
FailureKind = Literal["defect", "execution", "infrastructure"]


class Step(BaseModel):
    """One Gherkin step, resolved: effective type, tables and expected literals."""

    model_config = ConfigDict(frozen=True)

    index: int
    line: int
    keyword: str
    effective_type: StepType
    text: str
    data_table: list[list[str]] | None = None
    docstring: str | None = None
    # The table the step carries, from a native data table or a pipe table inside a docstring.
    table: list[list[str]] | None = None
    # Exact strings an outcome step expects on screen (quoted text, numbers, a plain docstring).
    expected_literals: list[str] = Field(default_factory=list)

    @property
    def has_expected_data(self) -> bool:
        """Outcome steps with a table or literals are judged by code, never by the evaluator."""
        return self.effective_type == "outcome" and (
            self.table is not None or bool(self.expected_literals)
        )

    def display(self) -> str:
        return f"{self.keyword} {self.text}"


class Scenario(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    line: int
    steps: list[Step]


class Feature(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: Path
    name: str
    scenarios: list[Scenario]

    @property
    def step_count(self) -> int:
        return sum(len(s.steps) for s in self.scenarios)


class TokenUsage(BaseModel):
    """Token accounting in report terms (see CLAUDE.md, Token accounting).

    `reasoning` is None when any contributing response lacked the thinking-token breakdown:
    an unknown part makes the sum unknown, and it is never estimated.
    """

    prompt: int = 0
    completion: int = 0
    cache_read: int = 0
    cache_write: int = 0
    reasoning: int | None = 0

    @property
    def total(self) -> int:
        return self.prompt + self.completion

    def __add__(self, other: TokenUsage) -> TokenUsage:
        reasoning = (
            None
            if self.reasoning is None or other.reasoning is None
            else self.reasoning + other.reasoning
        )
        return TokenUsage(
            prompt=self.prompt + other.prompt,
            completion=self.completion + other.completion,
            cache_read=self.cache_read + other.cache_read,
            cache_write=self.cache_write + other.cache_write,
            reasoning=reasoning,
        )

    @classmethod
    def sum(cls, usages: list[TokenUsage]) -> TokenUsage:
        total = cls()
        for usage in usages:
            total = total + usage
        return total


class ExecutorResult(BaseModel):
    """The executor's structured final answer. It never carries asserted values."""

    model_config = ConfigDict(extra="forbid")

    completed: bool = Field(description="True if the step's intent was carried out.")
    notes: str = Field(description="One or two sentences on what was done or what blocked it.")
    target_ref: str | None = Field(
        description=(
            "For steps that check on-screen data: the snapshot ref (for example e12) of the "
            "element holding that data. Otherwise null."
        )
    )


class ActionRecord(BaseModel):
    """One browser tool call the executor made, as recorded for the evaluator and the trace."""

    tool: str
    args: dict[str, object]
    ok: bool
    summary: str


class ExecutorRun(BaseModel):
    """Everything one executor attempt produced."""

    result: ExecutorResult | None = None
    # Why there is no usable result (refusal, max_tokens, tool-call cap, invalid answer).
    problem: str | None = None
    actions: list[ActionRecord] = Field(default_factory=list)
    tokens: TokenUsage = Field(default_factory=TokenUsage)


class Mismatch(BaseModel):
    kind: Literal["header", "cell", "missing_row", "extra_row", "literal", "shape"]
    row: int | None = None
    column: int | None = None
    column_name: str | None = None
    expected: str | None = None
    actual: str | None = None

    def describe(self) -> str:
        where = []
        if self.row is not None:
            where.append(f"row {self.row}")
        if self.column_name is not None:
            where.append(f"column {self.column_name!r}")
        elif self.column is not None:
            where.append(f"column {self.column}")
        location = f" at {', '.join(where)}" if where else ""
        return f"{self.kind}{location}: expected {self.expected!r}, got {self.actual!r}"


class Verdict(BaseModel):
    outcome: Literal["pass", "fail"]
    retryable: bool = False
    failure_kind: FailureKind | None = None
    reason: str = ""
    source: Literal["evaluator", "assertions", "executor", "runner"]
    mismatches: list[Mismatch] = Field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.outcome == "pass"

    @classmethod
    def ok(cls, source: Literal["evaluator", "assertions"], reason: str = "") -> Verdict:
        return cls(outcome="pass", source=source, reason=reason)

    @classmethod
    def fail(
        cls,
        source: Literal["evaluator", "assertions", "executor", "runner"],
        kind: FailureKind,
        reason: str,
        *,
        retryable: bool,
        mismatches: list[Mismatch] | None = None,
    ) -> Verdict:
        return cls(
            outcome="fail",
            source=source,
            failure_kind=kind,
            reason=reason,
            retryable=retryable,
            mismatches=mismatches or [],
        )


class StepStatus(StrEnum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
