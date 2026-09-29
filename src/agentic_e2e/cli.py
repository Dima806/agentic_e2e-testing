"""`agentic-e2e validate|run|summarize`.

Exit codes: 0 all passed, 1 any feature failed, 2 any feature blocked or invalid input.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from pathlib import Path

import anthropic

from agentic_e2e.agents import AnthropicMessages, Evaluator, Executor, InfrastructureError
from agentic_e2e.agents.llm import preflight
from agentic_e2e.artifacts import (
    ArtifactError,
    ArtifactWriter,
    FeatureReport,
    render_markdown,
    summarize,
)
from agentic_e2e.artifacts.report import FeatureRef, ScenarioReport, StepReport
from agentic_e2e.browser.base import Browser
from agentic_e2e.browser.client import launch
from agentic_e2e.config import Config, ConfigError
from agentic_e2e.features import FeatureSource, FeatureValidationError, discover, validate
from agentic_e2e.models import Feature
from agentic_e2e.runner import FeatureRunner

log = logging.getLogger("agentic_e2e")

EXIT_PASSED, EXIT_FAILED, EXIT_BLOCKED = 0, 1, 2


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    try:
        return int(args.handler(args))
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return EXIT_BLOCKED


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentic-e2e",
        description="Run Gherkin .feature files as live browser E2E tests, no step definitions.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("validate", help="validate Gherkin only; no browser, no model")
    p.add_argument("paths", nargs="+", type=Path, help=".feature files or directories")
    p.add_argument("--json", action="store_true", help="print valid features as JSON (for CI)")
    p.set_defaults(handler=cmd_validate)

    p = sub.add_parser("run", help="run features end-to-end, one at a time")
    p.add_argument("paths", nargs="+", type=Path, help=".feature files or directories")
    p.add_argument("--out", type=Path, default=Path("artifacts"), help="artifacts directory")
    p.add_argument("--base-url", help="application under test (AGENTIC_E2E_BASE_URL)")
    p.add_argument("--headed", action="store_true", help="show the browser window")
    p.set_defaults(handler=cmd_run)

    p = sub.add_parser("summarize", help="aggregate report.json files into a suite summary")
    p.add_argument("out", type=Path, help="artifacts directory holding <slug>/report.json")
    p.add_argument(
        "--features", nargs="*", type=Path, help="scheduled features; unreported ones are missing"
    )
    p.add_argument("--max-parallel", type=int, help="max parallel jobs, for the summary")
    p.add_argument("--matrix-result", help="CI matrix result, for the summary")
    p.set_defaults(handler=cmd_summarize)
    return parser


def cmd_validate(args: argparse.Namespace) -> int:
    loaded, errors = validate(args.paths)
    for source, feature in loaded:
        print(
            f"ok     {source.path}  ({len(feature.scenarios)} scenarios, "
            f"{feature.step_count} steps)",
            file=sys.stderr,
        )
    for _, error in errors:
        print(f"error  {error}", file=sys.stderr)
    if args.json:
        print(json.dumps([{"path": str(s.path), "slug": s.slug} for s, _ in loaded]))
    return EXIT_BLOCKED if errors else EXIT_PASSED


def cmd_run(args: argparse.Namespace) -> int:
    cfg = Config.from_env(base_url=args.base_url, headless=False if args.headed else None)
    loaded, errors = validate(args.paths)
    reports: list[FeatureReport] = []
    for source, error in errors:
        if source is None:  # discovery failed: nothing sensible to run
            print(f"error  {error}", file=sys.stderr)
            return EXIT_BLOCKED
        print(f"error  {error}", file=sys.stderr)
        reports.append(_write_invalid(args.out, source, error, cfg))
    if loaded:
        reports += asyncio.run(_run_all(cfg, loaded, args.out))
    return _exit_code(reports)


async def _run_all(
    cfg: Config, loaded: list[tuple[FeatureSource, Feature]], out: Path
) -> list[FeatureReport]:
    def browser_factory(workdir: Path) -> AbstractAsyncContextManager[Browser]:
        return launch(cfg, workdir)

    client = anthropic.AsyncAnthropic()
    try:
        # Fail before any browser starts if the API cannot be used at all.
        await preflight(client, [cfg.executor_model, cfg.evaluator_model])
    except InfrastructureError as exc:
        print(f"error  {exc}", file=sys.stderr)
        return [
            _write_blocked(out, _blocked_report(source, feature, str(exc), cfg), "preflight_failed")
            for source, feature in loaded
        ]

    messages = AnthropicMessages(
        client, max_tokens=cfg.max_tokens, refusal_fallbacks=cfg.refusal_fallbacks
    )
    reports: list[FeatureReport] = []
    # Sequential by design: one browser at a time on a 2-CPU container.
    for source, feature in loaded:
        log.info("Feature: %s (%s)", feature.name, source.path)
        try:
            writer = ArtifactWriter(out / source.slug)
        except ArtifactError as exc:
            print(f"error  {exc}", file=sys.stderr)
            reports.append(_blocked_report(source, feature, str(exc), cfg))
            continue
        with writer:
            runner = FeatureRunner(
                cfg,
                Executor(messages, cfg, writer),
                Evaluator(messages, cfg, writer),
                browser_factory,
                writer,
            )
            report = await runner.run(feature, source.slug)
        log.info("  => %s  (%s)", report.status, out / source.slug / "report.json")
        reports.append(report)
    return reports


def _write_invalid(
    out: Path, source: FeatureSource, error: FeatureValidationError, cfg: Config
) -> FeatureReport:
    report = _blocked_report(source, None, f"invalid feature: {error}", cfg)
    return _write_blocked(out, report, "feature_invalid")


def _write_blocked(out: Path, report: FeatureReport, event: str) -> FeatureReport:
    try:
        with ArtifactWriter(out / report.feature.slug) as writer:
            writer.event(event, error=report.error)
            writer.write_report(report)
    except ArtifactError as exc:
        print(f"error  {exc}", file=sys.stderr)
    return report


def _blocked_report(
    source: FeatureSource, feature: Feature | None, error: str, cfg: Config
) -> FeatureReport:
    """A report for a feature that never ran. Its steps, when known, are recorded as SKIPPED."""
    scenarios = [
        ScenarioReport(
            name=s.name,
            line=s.line,
            steps=[StepReport.pending(step, f"not run: {error}") for step in s.steps],
        )
        for s in (feature.scenarios if feature else [])
    ]
    report = FeatureReport(
        feature=FeatureRef(
            path=str(source.path),
            name=feature.name if feature else source.path.stem,
            slug=source.slug,
        ),
        status="blocked",
        error=error,
        models={"executor": cfg.executor_model, "evaluator": cfg.evaluator_model},
        started_at=datetime.now(UTC).isoformat(timespec="seconds"),
        scenarios=scenarios,
    )
    report.compute_totals()
    return report


def _exit_code(reports: list[FeatureReport]) -> int:
    if any(r.status == "blocked" for r in reports):
        return EXIT_BLOCKED
    if any(r.status == "failed" for r in reports):
        return EXIT_FAILED
    return EXIT_PASSED


def cmd_summarize(args: argparse.Namespace) -> int:
    scheduled: list[str] | None = None
    if args.features:
        try:
            scheduled = [s.slug for s in discover(args.features)]
        except FeatureValidationError as exc:
            print(f"error  {exc}", file=sys.stderr)
            return EXIT_BLOCKED
    summary = summarize(
        args.out,
        scheduled,
        max_parallel_jobs=args.max_parallel,
        matrix_result=args.matrix_result,
    )
    markdown = render_markdown(summary)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "suite-summary.json").write_text(
        summary.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    (args.out / "suite-summary.md").write_text(markdown, encoding="utf-8")
    print(markdown)
    return summary.exit_code


if __name__ == "__main__":
    sys.exit(main())
