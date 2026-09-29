"""The full pipeline against the demo site: real Claude agents, real browser.

Spends API tokens. Run with `make test-live` (needs Claude API credentials and Chromium).
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from pathlib import Path

import anthropic
import pytest

from agentic_e2e.agents import AnthropicMessages, Evaluator, Executor
from agentic_e2e.artifacts import ArtifactWriter
from agentic_e2e.browser.base import Browser
from agentic_e2e.browser.client import launch
from agentic_e2e.config import Config
from agentic_e2e.features import load_feature
from agentic_e2e.runner import FeatureRunner

pytestmark = pytest.mark.live

ROOT = Path(__file__).resolve().parents[2]


async def test_refund_feature_passes_end_to_end(demo_site_url: str, tmp_path: Path) -> None:
    cfg = Config.from_env(base_url=demo_site_url)
    feature = load_feature(ROOT / "features" / "refund.feature")
    messages = AnthropicMessages(anthropic.AsyncAnthropic(), cfg.max_tokens, cfg.refusal_fallbacks)

    def factory(workdir: Path) -> AbstractAsyncContextManager[Browser]:
        return launch(cfg, workdir)

    with ArtifactWriter(tmp_path / "refund") as writer:
        runner = FeatureRunner(
            cfg, Executor(messages, cfg, writer), Evaluator(messages, cfg, writer), factory, writer
        )
        report = await runner.run(feature, "refund")

    failures = [
        f"{s.keyword} {s.text}: {s.status} {s.reason}"
        for sc in report.scenarios
        for s in sc.steps
        if s.status != "PASSED"
    ]
    assert report.status == "passed", f"{report.error or ''}\n" + "\n".join(failures)
    assert report.totals.tokens.prompt > 0
    assert all(s.screenshots for sc in report.scenarios for s in sc.steps)
