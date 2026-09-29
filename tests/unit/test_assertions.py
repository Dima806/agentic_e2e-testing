from __future__ import annotations

import pytest

from agentic_e2e.assertions import check_literals, check_table, contains_literal

HEADER = ["Status", "Payment", "Amount"]


def table(*rows: list[str]) -> list[list[str]]:
    return [HEADER, *rows]


@pytest.mark.parametrize(
    ("shown", "passes"),
    [
        ("272.00", True),
        ("272.00 ", True),  # surrounding whitespace is stripped, nothing else
        ("\n272.00\t", True),
        ("272", False),
        ("272.0", False),
        ("272.000", False),
        ("0272.00", False),
        ("272,00", False),
    ],
)
def test_amounts_are_compared_as_exact_strings(shown: str, passes: bool) -> None:
    verdict = check_table(
        table(["Successful", "P-1", "272.00"]), table(["Successful", "P-1", shown])
    )
    assert verdict.passed is passes
    if not passes:
        assert verdict.failure_kind == "defect"
        assert not verdict.retryable
        assert verdict.mismatches[0].expected == "272.00"
        assert verdict.mismatches[0].actual == shown.strip()


def test_every_mismatch_is_reported_with_row_column_expected_and_actual() -> None:
    verdict = check_table(
        table(["Successful", "P-1", "272.00"], ["Successful", "P-2", "10.00"]),
        table(["Failed", "P-1", "272.00"], ["Successful", "P-2", "10"]),
    )
    assert not verdict.passed
    found = [(m.kind, m.row, m.column_name, m.expected, m.actual) for m in verdict.mismatches]
    assert found == [
        ("cell", 1, "Status", "Successful", "Failed"),
        ("cell", 2, "Amount", "10.00", "10"),
    ]
    assert "2 mismatch(es)" in verdict.reason


def test_header_mismatch_is_reported() -> None:
    actual = [["Status", "Payment ID", "Amount"], ["Successful", "P-1", "272.00"]]
    verdict = check_table(table(["Successful", "P-1", "272.00"]), actual)
    assert [(m.kind, m.column, m.expected, m.actual) for m in verdict.mismatches] == [
        ("header", 1, "Payment", "Payment ID")
    ]


def test_column_count_mismatch_is_a_shape_defect() -> None:
    verdict = check_table(
        table(["Successful", "P-1", "272.00"]), [["Status", "Amount"], ["x", "y"]]
    )
    assert verdict.mismatches[0].kind == "shape"
    assert verdict.failure_kind == "defect"


def test_missing_and_extra_rows_are_reported() -> None:
    missing = check_table(table(["A", "1", "2"], ["B", "3", "4"]), table(["A", "1", "2"]))
    assert [(m.kind, m.row) for m in missing.mismatches] == [("missing_row", 2)]
    extra = check_table(table(["A", "1", "2"]), table(["A", "1", "2"], ["B", "3", "4"]))
    assert [(m.kind, m.row, m.actual) for m in extra.mismatches] == [("extra_row", 2, "B | 3 | 4")]


def test_unrendered_placeholder_cells_match_any_non_empty_value() -> None:
    expected = table(["Successful", "<ref>", "272.00"])
    assert check_table(expected, table(["Successful", "R-4821", "272.00"])).passed
    verdict = check_table(expected, table(["Successful", "", "272.00"]))
    assert not verdict.passed
    assert verdict.mismatches[0].column_name == "Payment"


def test_an_empty_table_is_a_retryable_execution_issue() -> None:
    for actual in ([], [["", "", ""]]):
        verdict = check_table(table(["A", "1", "2"]), actual)
        assert verdict.failure_kind == "execution"
        assert verdict.retryable


@pytest.mark.parametrize(
    ("text", "literal", "found"),
    [
        ("Total: 272.00", "272.00", True),
        ("Total: 272.00.", "272.00", True),
        ("Total: 1272.00", "272.00", False),
        ("Total: 272.001", "272.00", False),
        ("Total: 272.00,5", "272.00", False),
        ("Total: 272", "272.00", False),
        ("Refund payment P-1001", "P-1001", True),
        ("refund payment p-1001", "P-1001", False),
        ("3 items", "3", True),
        ("13 items", "3", False),
    ],
)
def test_contains_literal_is_exact_and_number_aware(text: str, literal: str, found: bool) -> None:
    assert contains_literal(text, literal) is found


def test_check_literals_reports_each_missing_value() -> None:
    verdict = check_literals(["P-1001", "272.00", "Alice"], "Refund P-1001 for 272")
    assert not verdict.passed
    assert [m.expected for m in verdict.mismatches] == ["272.00", "Alice"]
    assert all(m.kind == "literal" for m in verdict.mismatches)


def test_check_literals_on_blank_text_is_retryable() -> None:
    verdict = check_literals(["272.00"], "   ")
    assert verdict.failure_kind == "execution"
    assert verdict.retryable


def test_check_literals_passes_when_all_present() -> None:
    assert check_literals(["P-1001", "272.00"], "Refund payment P-1001: 272.00").passed
