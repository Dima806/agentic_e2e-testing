from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentic_e2e.agents.llm import InfrastructureError
from agentic_e2e.artifacts import ArtifactWriter, FeatureReport
from agentic_e2e.browser import BrowserError
from agentic_e2e.config import Config
from agentic_e2e.models import ExecutorRun, Feature, StepStatus, Verdict
from agentic_e2e.runner import FeatureRunner
from tests.fakes import (
    FakeBrowser,
    ScriptedEvaluator,
    ScriptedExecutor,
    done,
    factory_for,
    fail,
    make_scenario,
    make_step,
)

TABLE = [["Status", "Amount"], ["Successful", "272.00"]]


def feature(*scenarios) -> Feature:
    return Feature(path=Path("features/demo.feature"), name="Demo", scenarios=list(scenarios))


def three_steps(name: str = "S1"):
    return make_scenario(
        name,
        make_step(0, "a user is on the dashboard", "context"),
        make_step(1, "the user opens Payments"),
        make_step(2, "the payments page should be visible", "outcome"),
    )


async def run(
    tmp_path: Path,
    feat: Feature,
    *,
    executor: ScriptedExecutor | None = None,
    evaluator: ScriptedEvaluator | None = None,
    browser: FakeBrowser | None = None,
    starts: list[Path] | None = None,
    launch_error: Exception | None = None,
    cfg: Config | None = None,
) -> tuple[FeatureReport, ArtifactWriter]:
    writer = ArtifactWriter(tmp_path / "demo")
    runner = FeatureRunner(
        cfg or Config(),
        executor or ScriptedExecutor(),
        evaluator or ScriptedEvaluator(),
        factory_for(browser or FakeBrowser(), starts, launch_error),
        writer,
    )
    with writer:
        report = await runner.run(feat, "demo")
    return report, writer


def statuses(report: FeatureReport, scenario: int = 0) -> list[str]:
    return [s.status.value for s in report.scenarios[scenario].steps]


def on_disk(writer: ArtifactWriter) -> FeatureReport:
    return FeatureReport.model_validate_json((writer.dir / "report.json").read_text())


async def test_all_steps_pass(tmp_path: Path) -> None:
    report, writer = await run(tmp_path, feature(three_steps()))
    assert report.status == "passed"
    assert statuses(report) == ["PASSED"] * 3
    step = report.scenarios[0].steps[1]
    assert (step.attempts, step.retried) == (1, False)
    assert step.screenshots == ["screenshots/s01-st01-a1.png"]
    assert (writer.dir / step.screenshots[0]).is_file()
    assert on_disk(writer) == report
    assert report.totals.steps_passed == 3


async def test_pass_on_second_attempt_records_retry(tmp_path: Path) -> None:
    evaluator = ScriptedEvaluator({1: [fail("execution"), Verdict.ok("evaluator")]})
    report, _ = await run(tmp_path, feature(three_steps()), evaluator=evaluator)
    step = report.scenarios[0].steps[1]
    assert (step.status, step.attempts, step.retried) == (StepStatus.PASSED, 2, True)
    assert step.screenshots == ["screenshots/s01-st01-a1.png", "screenshots/s01-st01-a2.png"]
    assert report.status == "passed"


async def test_retryable_failure_stops_after_max_attempts(tmp_path: Path) -> None:
    executor = ScriptedExecutor()
    evaluator = ScriptedEvaluator({1: [fail("execution", "spinner"), fail("execution", "spinner")]})
    report, _ = await run(tmp_path, feature(three_steps()), executor=executor, evaluator=evaluator)
    step = report.scenarios[0].steps[1]
    assert (step.status, step.attempts, step.failure_kind) == (StepStatus.FAILED, 2, "execution")
    assert statuses(report) == ["PASSED", "FAILED", "SKIPPED"]
    assert executor.calls == [0, 1, 1]
    assert report.status == "failed"


async def test_max_attempts_comes_from_config(tmp_path: Path) -> None:
    evaluator = ScriptedEvaluator({1: [fail(), fail(), fail()]})
    cfg = Config(max_attempts=3)
    report, _ = await run(tmp_path, feature(three_steps()), evaluator=evaluator, cfg=cfg)
    assert report.scenarios[0].steps[1].attempts == 3


async def test_real_failure_stops_the_scenario_and_skips_the_rest(tmp_path: Path) -> None:
    executor = ScriptedExecutor()
    evaluator = ScriptedEvaluator({1: [fail("defect", "found the login page")]})
    report, _ = await run(tmp_path, feature(three_steps()), executor=executor, evaluator=evaluator)
    failed, skipped = report.scenarios[0].steps[1], report.scenarios[0].steps[2]
    assert (failed.attempts, failed.failure_kind, failed.reason) == (
        1,
        "defect",
        "found the login page",
    )
    assert skipped.status == StepStatus.SKIPPED
    assert "step 2" in (skipped.reason or "") and "failed" in (skipped.reason or "")
    assert executor.calls == [0, 1]  # nothing after the failure ran


async def test_next_scenario_still_runs_after_a_failed_one(tmp_path: Path) -> None:
    evaluator = ScriptedEvaluator({1: [fail("defect")]})
    starts: list[Path] = []
    report, _ = await run(
        tmp_path, feature(three_steps("S1"), three_steps("S2")), evaluator=evaluator, starts=starts
    )
    # ScriptedEvaluator's queue for step 1 is used up by S1, so S2 passes.
    assert [s.status for s in report.scenarios] == ["failed", "passed"]
    assert len(starts) == 2  # a fresh browser session per scenario
    assert report.status == "failed"


async def test_code_verdict_beats_the_evaluator(tmp_path: Path) -> None:
    browser = FakeBrowser()
    browser.tables["e9"] = [["Status", "Amount"], ["Successful", "272"]]
    evaluator = ScriptedEvaluator()  # would pass anything it is asked about
    executor = ScriptedExecutor({0: [done(target_ref="e9")]})
    scenario = make_scenario("S", make_step(0, "the table shows", "outcome", table=TABLE))
    report, _ = await run(
        tmp_path, feature(scenario), executor=executor, evaluator=evaluator, browser=browser
    )
    step = report.scenarios[0].steps[0]
    assert (step.status, step.failure_kind, step.attempts) == (StepStatus.FAILED, "defect", 1)
    assert [(m.expected, m.actual) for m in step.mismatches] == [("272.00", "272")]
    assert evaluator.calls == []


async def test_expected_data_passes_when_values_match_exactly(tmp_path: Path) -> None:
    browser = FakeBrowser()
    browser.tables["e9"] = [["Status", "Amount"], ["Successful", " 272.00 "]]
    browser.texts["e4"] = "Refund payment P-1001"
    executor = ScriptedExecutor({0: [done("e9")], 1: [done("e4")]})
    scenario = make_scenario(
        "S",
        make_step(0, "the table shows", "outcome", table=TABLE),
        make_step(1, 'the refund page for "P-1001" is shown', "outcome", literals=["P-1001"]),
    )
    report, _ = await run(tmp_path, feature(scenario), executor=executor, browser=browser)
    assert statuses(report) == ["PASSED", "PASSED"]
    assert ("read_table", "e9") in browser.calls
    assert ("read_element_text", "e4") in browser.calls


@pytest.mark.parametrize(
    "run_",
    [done(target_ref=None), done(target_ref="e404"), ExecutorRun(problem="model refused")],
)
async def test_unlocated_or_unreadable_data_is_retried(tmp_path: Path, run_: ExecutorRun) -> None:
    executor = ScriptedExecutor({0: [run_, run_]})
    scenario = make_scenario("S", make_step(0, "the table shows", "outcome", table=TABLE))
    report, _ = await run(tmp_path, feature(scenario), executor=executor)
    step = report.scenarios[0].steps[0]
    assert (step.status, step.attempts, step.failure_kind) == (StepStatus.FAILED, 2, "execution")


@pytest.mark.parametrize(
    "error", [InfrastructureError("API down"), BrowserError("chromium crashed")]
)
async def test_infrastructure_error_blocks_the_feature(tmp_path: Path, error: Exception) -> None:
    executor = ScriptedExecutor({1: [error]})
    report, writer = await run(
        tmp_path, feature(three_steps("S1"), three_steps("S2")), executor=executor
    )
    assert report.status == "blocked"
    assert report.error is not None and str(error) in report.error
    failed = report.scenarios[0].steps[1]
    assert (failed.status, failed.failure_kind, failed.attempts) == (
        StepStatus.FAILED,
        "infrastructure",
        1,
    )
    assert [s.status for s in report.scenarios] == ["blocked", "skipped"]
    remaining = [report.scenarios[0].steps[2], *report.scenarios[1].steps]
    assert all(s.status == StepStatus.SKIPPED for s in remaining)
    assert all("feature blocked" in (s.reason or "") for s in remaining)
    assert on_disk(writer).status == "blocked"


async def test_browser_start_failure_blocks_with_report(tmp_path: Path) -> None:
    report, writer = await run(
        tmp_path, feature(three_steps()), launch_error=BrowserError("npx not found")
    )
    assert report.status == "blocked"
    assert "npx not found" in (report.error or "")
    assert statuses(report) == ["SKIPPED"] * 3
    assert on_disk(writer).totals.steps_skipped == 3


async def test_unexpected_exception_is_reported_not_swallowed(tmp_path: Path) -> None:
    executor = ScriptedExecutor({0: [RuntimeError("bug")]})
    report, writer = await run(tmp_path, feature(three_steps()), executor=executor)
    assert report.status == "blocked"
    assert report.error == "internal error: RuntimeError: bug"
    trace = [json.loads(line) for line in (writer.dir / "trace.jsonl").read_text().splitlines()]
    assert any(e["event"] == "internal_error" for e in trace)


async def test_tokens_are_accounted_per_role_step_and_feature(tmp_path: Path) -> None:
    report, _ = await run(tmp_path, feature(three_steps()))
    step = report.scenarios[0].steps[0]
    assert step.tokens_by_role["executor"].prompt == 10
    assert step.tokens_by_role["evaluator"].prompt == 5
    assert step.tokens.prompt == 15
    assert step.tokens.reasoning == 1
    assert report.totals.tokens.prompt == 45
    assert report.totals.tokens.cache_read == 9


async def test_trace_records_attempts_and_verdicts_with_context(tmp_path: Path) -> None:
    _, writer = await run(tmp_path, feature(three_steps()))
    events = [json.loads(line) for line in (writer.dir / "trace.jsonl").read_text().splitlines()]
    verdicts = [e for e in events if e["event"] == "verdict"]
    assert [(e["scenario"], e["step"], e["attempt"]) for e in verdicts] == [
        (1, 0, 1),
        (1, 1, 1),
        (1, 2, 1),
    ]
    starts = [e for e in events if e["event"] == "attempt_start"]
    assert [(e["step"], e["text"]) for e in starts][1] == (1, "When the user opens Payments")
    assert events[0]["event"] == "feature_start"
    assert events[-1]["event"] == "feature_end"


async def test_artifacts_hold_only_json_results_and_png_visuals(tmp_path: Path) -> None:
    evaluator = ScriptedEvaluator({1: [fail("execution"), fail("defect")]})
    _, writer = await run(tmp_path, feature(three_steps()), evaluator=evaluator)
    files = [p for p in writer.dir.rglob("*") if p.is_file()]
    suffixes = {p.suffix for p in files if p.name != ".agentic-e2e"}
    assert suffixes == {".json", ".jsonl", ".png"}
    for path in files:
        if path.suffix == ".png":
            assert path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
        elif path.suffix == ".json" or path.name == ".agentic-e2e":
            json.loads(path.read_text())
        else:
            assert all(json.loads(line) for line in path.read_text().splitlines())
