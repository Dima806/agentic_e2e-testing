"""Per-step loop controller: deterministic Python around the only agentic part.

Contract (CLAUDE.md): one step at a time; at most `max_attempts` per step; the first real failure
ends the scenario; every step not run is recorded as SKIPPED; code verdicts beat the evaluator;
infrastructure failures block the feature; report.json is written on every path.
"""

from __future__ import annotations

import logging
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from agentic_e2e.agents.llm import InfrastructureError
from agentic_e2e.artifacts.report import FeatureRef, FeatureReport, ScenarioReport, StepReport
from agentic_e2e.artifacts.writer import ArtifactWriter
from agentic_e2e.assertions import check_literals, check_table
from agentic_e2e.browser.base import Browser, BrowserError, BrowserFactory, ElementReadError
from agentic_e2e.config import Config
from agentic_e2e.models import (
    ExecutorRun,
    Feature,
    Scenario,
    Step,
    StepStatus,
    TokenUsage,
    Verdict,
)

log = logging.getLogger("agentic_e2e")

_INFRASTRUCTURE = (InfrastructureError, BrowserError)


class StepExecutor(Protocol):
    async def run(self, step: Step, browser: Browser) -> ExecutorRun: ...


class StepEvaluator(Protocol):
    async def judge(
        self, step: Step, run: ExecutorRun, snapshot: str
    ) -> tuple[Verdict, TokenUsage]: ...


class _Blocked(Exception):
    """Internal: stop the feature because of an infrastructure failure."""


class FeatureRunner:
    def __init__(
        self,
        cfg: Config,
        executor: StepExecutor,
        evaluator: StepEvaluator,
        browser_factory: BrowserFactory,
        writer: ArtifactWriter,
    ) -> None:
        self._cfg = cfg
        self._executor = executor
        self._evaluator = evaluator
        self._browser_factory = browser_factory
        self._writer = writer

    async def run(self, feature: Feature, slug: str) -> FeatureReport:
        started = time.monotonic()
        report = FeatureReport(
            feature=FeatureRef(path=str(feature.path), name=feature.name, slug=slug),
            status="blocked",
            models={"executor": self._cfg.executor_model, "evaluator": self._cfg.evaluator_model},
            started_at=datetime.now(UTC).isoformat(timespec="seconds"),
            # Every step starts as SKIPPED, so nothing is silently dropped whatever happens next.
            scenarios=[
                ScenarioReport(
                    name=s.name,
                    line=s.line,
                    steps=[StepReport.pending(step, "not run") for step in s.steps],
                )
                for s in feature.scenarios
            ],
        )
        self._writer.event("feature_start", path=str(feature.path), name=feature.name)
        error: str | None = None
        try:
            for number, (scenario, scenario_report) in enumerate(
                zip(feature.scenarios, report.scenarios, strict=True), start=1
            ):
                await self._run_scenario(number, scenario, scenario_report)
        except _Blocked as exc:
            error = str(exc)
        except Exception as exc:
            error = f"internal error: {type(exc).__name__}: {exc}"
            self._writer.event("internal_error", traceback=traceback.format_exc())
        except BaseException:
            error = "run interrupted"
            raise
        finally:
            if error is not None:
                report.status = "blocked"
                report.error = error
                for scenario_report in report.scenarios:
                    for step_report in scenario_report.steps:
                        if step_report.status == StepStatus.SKIPPED and step_report.attempts == 0:
                            step_report.reason = f"not run: feature blocked ({error})"
            elif any(s.status == "failed" for s in report.scenarios):
                report.status = "failed"
            else:
                report.status = "passed"
            report.duration_s = round(time.monotonic() - started, 3)
            self._writer.set_context()
            self._writer.event("feature_end", status=report.status, error=report.error)
            self._writer.write_report(report)
        return report

    async def _run_scenario(
        self, number: int, scenario: Scenario, scenario_report: ScenarioReport
    ) -> None:
        log.info("  Scenario: %s", scenario.name)
        self._writer.set_context(scenario=number)
        self._writer.event("scenario_start", name=scenario.name)
        blocked_reason: str | None = None
        try:
            async with self._browser_factory(self._writer.dir) as browser:
                stopped_after: Step | None = None
                for step, step_report in zip(scenario.steps, scenario_report.steps, strict=True):
                    if stopped_after is not None:
                        step_report.reason = (
                            f"skipped: step {stopped_after.index + 1} "
                            f"({stopped_after.keyword} {stopped_after.text}) failed"
                        )
                        continue
                    await self._run_step(number, step, step_report, browser)
                    if step_report.status == StepStatus.FAILED:
                        stopped_after = step
                        if step_report.failure_kind == "infrastructure":
                            blocked_reason = step_report.reason
                            break
        except Exception as exc:
            if blocked_reason is None and not _is_infrastructure(exc):
                raise
            # A crash while starting or closing the browser; keep the first cause if we have one.
            blocked_reason = blocked_reason or f"{type(exc).__name__}: {exc}"

        if blocked_reason is not None:
            scenario_report.status = "blocked"
            self._writer.event("scenario_end", status="blocked", reason=blocked_reason)
            raise _Blocked(blocked_reason)
        failed = any(s.status == StepStatus.FAILED for s in scenario_report.steps)
        scenario_report.status = "failed" if failed else "passed"
        self._writer.event("scenario_end", status=scenario_report.status)

    async def _run_step(
        self, scenario_number: int, step: Step, report: StepReport, browser: Browser
    ) -> None:
        started = time.monotonic()
        by_role = {"executor": TokenUsage(), "evaluator": TokenUsage()}
        for attempt in range(1, self._cfg.max_attempts + 1):
            self._writer.set_context(scenario=scenario_number, step=step.index, attempt=attempt)
            self._writer.event("attempt_start", text=step.display())
            try:
                run = await self._executor.run(step, browser)
                by_role["executor"] = by_role["executor"] + run.tokens
                verdict = await self._judge(step, run, browser, by_role)
            except _INFRASTRUCTURE as exc:
                verdict = Verdict.fail("runner", "infrastructure", str(exc), retryable=False)

            shot = self._writer.screenshot_path(scenario_number, step.index, attempt)
            if await _screenshot(browser, shot):
                report.screenshots.append(self._writer.relative(shot))
            self._writer.event("verdict", **verdict.model_dump(mode="json"))
            report.attempts = attempt

            if verdict.passed:
                report.status = StepStatus.PASSED
                report.reason = verdict.reason or None
                report.failure_kind = None
                break
            if not verdict.retryable or attempt == self._cfg.max_attempts:
                report.status = StepStatus.FAILED
                report.failure_kind = verdict.failure_kind
                report.reason = verdict.reason
                report.mismatches = verdict.mismatches
                break
            self._writer.event("retry", reason=verdict.reason)

        report.retried = report.attempts > 1
        report.tokens_by_role = by_role
        report.tokens = by_role["executor"] + by_role["evaluator"]
        report.duration_s = round(time.monotonic() - started, 3)
        log.info(
            "    %-7s %s (attempt %d)%s",
            report.status.value,
            step.display(),
            report.attempts,
            f": {report.reason}" if report.status == StepStatus.FAILED else "",
        )

    async def _judge(
        self, step: Step, run: ExecutorRun, browser: Browser, by_role: dict[str, TokenUsage]
    ) -> Verdict:
        if run.problem is not None:
            return Verdict.fail("executor", "execution", f"executor: {run.problem}", retryable=True)
        if step.has_expected_data:
            # Code decides; the evaluator is never asked and never sees the expected values.
            return await self._assert(step, run, browser)
        snapshot = await browser.snapshot()
        verdict, usage = await self._evaluator.judge(step, run, snapshot)
        by_role["evaluator"] = by_role["evaluator"] + usage
        return verdict

    async def _assert(self, step: Step, run: ExecutorRun, browser: Browser) -> Verdict:
        result = run.result
        ref = result.target_ref if result is not None else None
        if not ref:
            notes = f" (executor: {result.notes})" if result is not None else ""
            return Verdict.fail(
                "assertions",
                "execution",
                f"the element holding the expected data was not located{notes}",
                retryable=True,
            )
        try:
            if step.table is not None:
                rows = await browser.read_table(ref)
                self._writer.event("read_table", ref=ref, rows=rows)
                return check_table(step.table, rows)
            text = await browser.read_element_text(ref)
            self._writer.event("read_text", ref=ref, text=text)
            return check_literals(step.expected_literals, text)
        except ElementReadError as exc:
            return Verdict.fail(
                "assertions", "execution", f"could not read element {ref}: {exc}", retryable=True
            )


async def _screenshot(browser: Browser, path: Path) -> bool:
    try:
        return await browser.screenshot(path)
    except Exception:  # evidence is best-effort; a dead browser is reported by the step itself
        return False


def _is_infrastructure(exc: BaseException) -> bool:
    if isinstance(exc, _INFRASTRUCTURE):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return any(_is_infrastructure(e) for e in exc.exceptions)
    return False
