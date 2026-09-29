from __future__ import annotations

from pathlib import Path

import pytest

from agentic_e2e.features import FeatureValidationError, discover, load_feature, validate


def test_discover_expands_directories_with_relative_slugs(fixtures: Path) -> None:
    sources = discover([fixtures])
    by_slug = {s.slug: s.path for s in sources}
    assert by_slug["valid"] == fixtures / "valid.feature"
    assert by_slug["invalid-duplicate"] == fixtures / "invalid" / "duplicate.feature"
    assert [s.path for s in sources] == sorted(s.path for s in sources)


def test_discover_single_file_and_deduplicates(fixtures: Path) -> None:
    path = fixtures / "valid.feature"
    sources = discover([path, path])
    assert [(s.path, s.slug) for s in sources] == [(path, "valid")]


@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("missing.feature", "no such file or directory"),
        ("../../conftest.py", "not a .feature file"),
    ],
)
def test_discover_rejects_bad_paths(fixtures: Path, name: str, message: str) -> None:
    with pytest.raises(FeatureValidationError, match=message):
        discover([fixtures / name])


def test_discover_rejects_an_empty_directory(tmp_path: Path) -> None:
    with pytest.raises(FeatureValidationError, match=r"no \.feature files"):
        discover([tmp_path])


def test_discover_rejects_clashing_artifact_names(tmp_path: Path) -> None:
    for folder in ("a", "b"):
        (tmp_path / folder).mkdir()
        (tmp_path / folder / "x.feature").write_text("Feature: x\n")
    with pytest.raises(FeatureValidationError, match="clashes"):
        discover([tmp_path / "a" / "x.feature", tmp_path / "b" / "x.feature"])


@pytest.mark.parametrize(
    ("name", "line", "message"),
    [
        ("two_backgrounds.feature", 6, "Multiple 'Background' sections"),
        ("empty.feature", None, "file is empty"),
        ("no_feature.feature", None, "no 'Feature:' found"),
        ("no_scenarios.feature", 1, "feature has no scenarios"),
        ("duplicate.feature", 6, "duplicate scenario name 'Same name'"),
        ("uneven_docstring_table.feature", 4, "row 2 has 1 cells"),
        ("outline_without_rows.feature", 3, "has no Examples rows"),
        ("starts_with_and.feature", 4, "must start with 'Given', 'When' or 'Then'"),
    ],
)
def test_invalid_features_fail_early_with_file_and_line(
    fixtures: Path, name: str, line: int | None, message: str
) -> None:
    path = fixtures / "invalid" / name
    with pytest.raises(FeatureValidationError) as info:
        load_feature(path)
    assert info.value.line == line
    assert message in info.value.message
    assert str(info.value).startswith(f"{path}:{line}:" if line else f"{path}:")


def test_validate_collects_every_error_and_every_valid_feature(fixtures: Path) -> None:
    loaded, errors = validate([fixtures])
    assert sorted(source.slug for source, _ in loaded) == ["outline", "valid"]
    assert len(errors) == len(list((fixtures / "invalid").glob("*.feature")))
    assert all(source is not None for source, _ in errors)


def test_validate_reports_a_discovery_error_without_a_source(fixtures: Path) -> None:
    loaded, errors = validate([fixtures / "nope"])
    assert loaded == []
    assert errors[0][0] is None
    assert "no such file" in str(errors[0][1])
