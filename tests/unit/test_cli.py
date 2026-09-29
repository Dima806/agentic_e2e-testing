from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agentic_e2e.artifacts import FeatureReport
from agentic_e2e.cli import main


def test_validate_ok_with_json_for_ci(fixtures: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["validate", str(fixtures / "valid.feature"), "--json"])
    out, err = capsys.readouterr()
    assert code == 0
    assert json.loads(out) == [{"path": str(fixtures / "valid.feature"), "slug": "valid"}]
    assert "ok     " in err and "(2 scenarios, 12 steps)" in err


def test_validate_reports_file_and_line(fixtures: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bad = fixtures / "invalid" / "two_backgrounds.feature"
    assert main(["validate", str(bad)]) == 2
    assert f"error  {bad}:6: Multiple 'Background'" in capsys.readouterr().err


def test_run_blocks_invalid_features_before_any_browser_or_model(
    fixtures: Path, tmp_path: Path
) -> None:
    bad = fixtures / "invalid" / "duplicate.feature"
    code = main(["run", str(bad), "--out", str(tmp_path)])
    assert code == 2
    report = FeatureReport.model_validate_json((tmp_path / "duplicate" / "report.json").read_text())
    assert report.status == "blocked"
    assert report.error is not None and "duplicate scenario name" in report.error


def test_run_with_a_missing_path_exits_2(tmp_path: Path) -> None:
    assert main(["run", str(tmp_path / "nope.feature"), "--out", str(tmp_path / "a")]) == 2


def test_summarize_writes_json_and_markdown(
    fixtures: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    main(["run", str(fixtures / "invalid" / "duplicate.feature"), "--out", str(tmp_path)])
    capsys.readouterr()
    code = main(
        [
            "summarize",
            str(tmp_path),
            "--features",
            str(fixtures / "valid.feature"),
            "--max-parallel",
            "2",
        ]
    )
    out = capsys.readouterr().out
    assert code == 2  # one blocked, one scheduled but missing
    assert "| Missing report | 1 |" in out
    assert "| Max parallel jobs | 2 |" in out
    assert (tmp_path / "suite-summary.md").read_text() == out.rstrip("\n") + "\n"
    assert json.loads((tmp_path / "suite-summary.json").read_text())["blocked"] == 1


def test_bad_environment_override_is_a_clear_error(
    monkeypatch: pytest.MonkeyPatch, fixtures: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AGENTIC_E2E_MAX_ATTEMPTS", "two")
    assert main(["run", str(fixtures / "valid.feature")]) == 2
    assert "AGENTIC_E2E_MAX_ATTEMPTS='two' is not an integer" in capsys.readouterr().err


def test_run_without_credentials_blocks_before_any_browser_starts(
    monkeypatch: pytest.MonkeyPatch,
    fixtures: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Reproduce a Codespace with no key, no `ant` profile and no federation settings.
    for name in [n for n in os.environ if n.startswith("ANTHROPIC_")]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "home" / ".config"))

    def no_browser(*args: object) -> None:
        raise AssertionError("a browser was started without credentials")

    monkeypatch.setattr("agentic_e2e.cli.launch", no_browser)

    code = main(["run", str(fixtures / "valid.feature"), "--out", str(tmp_path / "out")])

    assert code == 2
    err = capsys.readouterr().err
    assert err.count("export ANTHROPIC_API_KEY") == 1
    report = FeatureReport.model_validate_json(
        (tmp_path / "out" / "valid" / "report.json").read_text()
    )
    assert report.status == "blocked"
    assert "ANTHROPIC_API_KEY" in (report.error or "")
    assert (report.totals.steps_total, report.totals.steps_skipped) == (12, 12)
    assert report.scenarios[0].steps[0].reason.startswith("not run: no Claude API credentials")
