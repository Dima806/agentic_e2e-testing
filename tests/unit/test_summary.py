from __future__ import annotations

from pathlib import Path

from agentic_e2e.artifacts import FeatureReport, render_markdown, summarize
from agentic_e2e.artifacts.report import FeatureRef, ScenarioReport, StepReport
from agentic_e2e.models import StepStatus, TokenUsage


def write_report(
    out: Path,
    slug: str,
    status: str,
    steps: list[StepStatus],
    tokens: TokenUsage,
    duration: float = 10.0,
) -> None:
    step_reports = [
        StepReport(
            index=i,
            line=i,
            keyword="When",
            text=f"step {i}",
            status=s,
            reason="found the login page" if s == StepStatus.FAILED else None,
            tokens=tokens if i == 0 else TokenUsage(),
        )
        for i, s in enumerate(steps)
    ]
    report = FeatureReport(
        feature=FeatureRef(path=f"features/{slug}.feature", name=slug.title(), slug=slug),
        status=status,  # type: ignore[arg-type]
        started_at="2026-09-29T00:00:00+00:00",
        duration_s=duration,
        scenarios=[ScenarioReport(name="S", line=1, status="passed", steps=step_reports)],
    )
    report.compute_totals()
    (out / slug).mkdir(parents=True)
    (out / slug / "report.json").write_text(report.model_dump_json())


P, F, S = StepStatus.PASSED, StepStatus.FAILED, StepStatus.SKIPPED


def test_aggregates_counts_steps_tokens_and_missing_reports(tmp_path: Path) -> None:
    write_report(
        tmp_path,
        "refund",
        "passed",
        [P, P, P],
        TokenUsage(prompt=1000, completion=100, cache_read=600, reasoning=40),
    )
    write_report(
        tmp_path,
        "checkout",
        "failed",
        [P, F, S],
        TokenUsage(prompt=1000, completion=50, cache_read=200, reasoning=10),
        20.0,
    )
    (tmp_path / "crashed").mkdir()  # job died before writing report.json

    summary = summarize(
        tmp_path, ["refund", "checkout", "search"], max_parallel_jobs=2, matrix_result="failure"
    )

    assert summary.features_scheduled == 4  # 3 scheduled + 1 directory found on disk
    assert (summary.passed, summary.failed, summary.blocked, summary.missing) == (1, 1, 0, 2)
    assert (summary.steps_passed, summary.steps_total) == (4, 6)
    assert summary.tokens == TokenUsage(prompt=2000, completion=150, cache_read=800, reasoning=50)
    assert summary.cached_pct == 40.0
    assert summary.duration_s == 30.0
    assert summary.status == "blocked" and summary.exit_code == 2
    checkout = next(f for f in summary.features if f.slug == "checkout")
    assert checkout.note == "When step 1: found the login page"


def test_markdown_lists_the_ci_metrics(tmp_path: Path) -> None:
    write_report(
        tmp_path,
        "refund",
        "passed",
        [P] * 47,
        TokenUsage(prompt=1000, completion=100, cache_read=250, reasoning=None),
    )
    summary = summarize(tmp_path, max_parallel_jobs=2, matrix_result="success")
    md = render_markdown(summary)
    assert summary.exit_code == 0
    for expected in [
        "## Agentic E2E suite: 🟢 passed",
        "| Features scheduled | 1 |",
        "| Max parallel jobs | 2 |",
        "| Matrix result | success |",
        "| Passed / failed / blocked | 1 / 0 / 0 |",
        "| Missing report | 0 |",
        "| Steps passed | 47/47 |",
        "| Tokens total | 1,100 |",
        "| Prompt / completion | 1,000 / 100 |",
        "| Cached (% of prompt) | 25.0% |",
        "| Reasoning | n/a |",
        "| Total duration | 10.0s |",
        "| Refund (`refund`) | 🟢 passed | 47/47 |",
    ]:
        assert expected in md


def test_failed_only_exits_1_and_no_prompt_means_no_cache_ratio(tmp_path: Path) -> None:
    write_report(tmp_path, "a", "failed", [F], TokenUsage())
    summary = summarize(tmp_path)
    assert summary.exit_code == 1
    assert summary.cached_pct is None
    assert "| Cached (% of prompt) | n/a |" in render_markdown(summary)


def test_unreadable_report_counts_as_missing(tmp_path: Path) -> None:
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "report.json").write_text("{not json")
    summary = summarize(tmp_path)
    assert summary.missing == 1
    assert summary.features[0].note is not None and "unreadable" in summary.features[0].note


def test_missing_out_dir_with_schedule_reports_all_missing(tmp_path: Path) -> None:
    summary = summarize(tmp_path / "nope", ["a", "b"])
    assert (summary.features_scheduled, summary.missing) == (2, 2)
