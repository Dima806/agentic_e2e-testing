from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentic_e2e.artifacts import ArtifactError, ArtifactWriter, FeatureReport
from agentic_e2e.artifacts.report import FeatureRef


def events(writer: ArtifactWriter) -> list[dict]:
    return [json.loads(line) for line in (writer.dir / "trace.jsonl").read_text().splitlines()]


def test_events_are_streamed_as_jsonl_with_context(tmp_path: Path) -> None:
    with ArtifactWriter(tmp_path / "f") as writer:
        writer.event("feature_start", name="Demo")
        writer.set_context(scenario=1, step=0, attempt=2)
        writer.event("verdict", outcome="pass")
        # Visible on disk before close: flushed per event.
        assert len(events(writer)) == 2
    first, second = events(writer)
    assert (first["seq"], first["event"], first["name"]) == (1, "feature_start", "Demo")
    assert "scenario" not in first
    assert (second["scenario"], second["step"], second["attempt"]) == (1, 0, 2)


def test_long_text_is_offloaded_to_snapshot_files(tmp_path: Path) -> None:
    with ArtifactWriter(tmp_path / "f", blob_threshold=10) as writer:
        writer.event("tool_result", message="x" * 50, nested={"snapshot": "y" * 20}, ok=True)
    (record,) = events(writer)
    ref = record["message"]["$ref"]
    assert ref.startswith("snapshots/") and ref.endswith(".json")
    assert record["message"]["chars"] == 50
    blob = json.loads((writer.dir / ref).read_text())
    assert blob == {"seq": 1, "field": "message", "chars": 50, "text": "x" * 50}
    nested = json.loads((writer.dir / record["nested"]["snapshot"]["$ref"]).read_text())
    assert nested["text"] == "y" * 20
    assert record["ok"] is True


def test_report_is_written_atomically_and_round_trips(tmp_path: Path) -> None:
    report = FeatureReport(
        feature=FeatureRef(path="features/x.feature", name="X", slug="x"),
        status="passed",
        started_at="2026-09-29T00:00:00+00:00",
    )
    with ArtifactWriter(tmp_path / "x") as writer:
        path = writer.write_report(report)
    assert path == tmp_path / "x" / "report.json"
    loaded = json.loads(path.read_text())
    assert loaded["schema_version"] == 1
    assert loaded["totals"]["tokens"]["reasoning"] == 0
    assert not list((tmp_path / "x").glob("*.tmp"))


def test_rerun_replaces_its_own_previous_artifacts(tmp_path: Path) -> None:
    with ArtifactWriter(tmp_path / "f") as writer:
        writer.event("old")
        (writer.dir / "screenshots" / "stale.png").write_bytes(b"x")
    with ArtifactWriter(tmp_path / "f") as writer:
        writer.event("new")
    assert [e["event"] for e in events(writer)] == ["new"]
    assert not (tmp_path / "f" / "screenshots" / "stale.png").exists()


def test_refuses_to_wipe_a_directory_it_did_not_create(tmp_path: Path) -> None:
    precious = tmp_path / "src"
    precious.mkdir()
    (precious / "main.py").write_text("print('hi')")
    with pytest.raises(ArtifactError, match="refusing to overwrite"):
        ArtifactWriter(precious)
    assert (precious / "main.py").exists()


def test_screenshot_paths_name_scenario_step_and_attempt(tmp_path: Path) -> None:
    with ArtifactWriter(tmp_path / "f") as writer:
        path = writer.screenshot_path(1, 3, 2)
    assert writer.relative(path) == "screenshots/s01-st03-a2.png"
