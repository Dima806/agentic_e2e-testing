from __future__ import annotations

from pathlib import Path

import pytest

from agentic_e2e.features import FeatureValidationError, load_feature
from agentic_e2e.features.parser import extract_literals, parse_pipe_table


@pytest.fixture
def valid(fixtures: Path):
    return load_feature(fixtures / "valid.feature")


def test_and_but_inherit_the_previous_step_type(valid) -> None:
    steps = valid.scenarios[0].steps
    by_text = {s.text: s for s in steps}
    assert by_text["the user hovers over payment P-1001"].keyword == "And"
    assert by_text["the user hovers over payment P-1001"].effective_type == "action"
    assert by_text["the user does not refund payment P-1000"].effective_type == "action"
    assert by_text['the total should be 272.00 for "Alice"'].effective_type == "outcome"
    assert by_text["the summary table shows"].effective_type == "outcome"


def test_background_is_prepended_to_every_scenario(valid) -> None:
    for scenario in valid.scenarios:
        first = scenario.steps[0]
        assert (first.keyword, first.text) == ("Given", "a user is on the dashboard page")
        assert first.effective_type == "context"
        assert [s.index for s in scenario.steps] == list(range(len(scenario.steps)))


def test_docstring_table_is_parsed_exactly(valid) -> None:
    step = next(s for s in valid.scenarios[0].steps if s.text.startswith("the refunds table"))
    assert step.table == [
        ["Status", "Payment", "Amount"],
        ["Successful", "P-1001", "272.00"],
        ["a|b", "c\\d", "<ref>"],
    ]
    assert step.data_table is None
    assert step.has_expected_data
    assert step.expected_literals == []


def test_native_data_table_is_parsed_exactly(valid) -> None:
    step = next(s for s in valid.scenarios[0].steps if s.text == "the summary table shows")
    assert step.data_table == [["Label", "Amount"], ["x\\y", "1,234.50"]]
    assert step.table == step.data_table
    assert step.has_expected_data


def test_plain_docstring_on_an_outcome_step_is_an_expected_literal(valid) -> None:
    step = next(s for s in valid.scenarios[0].steps if s.text == "the confirmation reads")
    assert step.table is None
    assert step.expected_literals == ["Refund requested"]


def test_quoted_strings_and_numbers_become_literals_verbatim(valid) -> None:
    step = next(s for s in valid.scenarios[0].steps if s.text.startswith("the total"))
    assert step.expected_literals == ["Alice", "272.00"]
    assert step.has_expected_data


def test_a_table_on_an_action_step_is_input_not_expected_data(valid) -> None:
    fill = valid.scenarios[1].steps[1]
    assert fill.effective_type == "action"
    assert fill.table == [["Field", "Value"], ["Amount", "10.00"]]
    assert not fill.has_expected_data
    assert fill.expected_literals == []


def test_visibility_outcome_has_no_expected_data(valid) -> None:
    step = next(
        s for s in valid.scenarios[0].steps if s.text == "the refund page should be visible"
    )
    assert not step.has_expected_data


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("the total should be 272.00", ["272.00"]),
        ("the total should be 272.00.", ["272.00"]),
        ("payment P-1001 should be listed", []),
        ("version v2 is on the 2nd row", []),
        ('amount "1,040.00" and 3 items', ["1,040.00", "3"]),
        ("the balance is -5 now", ["-5"]),
        ("costs $1,234.50 in total", ["1,234.50"]),
        ('an empty "" quote', []),
        ('"Alice" and "Alice" again', ["Alice"]),
    ],
)
def test_extract_literals(text: str, expected: list[str]) -> None:
    assert extract_literals(text) == expected


def test_pipe_table_parser_ignores_prose_and_rejects_uneven_rows() -> None:
    assert parse_pipe_table("Refund requested") is None
    assert parse_pipe_table("| a | b |\nnot a row") is None
    assert parse_pipe_table("|  a  |  b  |\n| 1 | 2 |") == [["a", "b"], ["1", "2"]]
    with pytest.raises(FeatureValidationError, match="row 2 has 1 cells, expected 2"):
        parse_pipe_table("| a | b |\n| 1 |")


def test_scenario_outline_expands_each_examples_row(fixtures: Path) -> None:
    feature = load_feature(fixtures / "outline.feature")
    assert [s.name for s in feature.scenarios] == [
        "Pay an amount [#1: amount=10.00]",
        "Pay an amount [#2: amount=20.50]",
    ]
    second = feature.scenarios[1]
    assert second.steps[0].text == "the user pays 20.50"
    assert second.steps[1].expected_literals == ["20.50"]
    assert all("<amount>" not in s.text for sc in feature.scenarios for s in sc.steps)
