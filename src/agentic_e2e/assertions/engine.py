"""Deterministic assertions: expected data is compared in code, as exact strings.

No value is ever converted to a number, rounded or reformatted: "272.00" only matches "272.00".
Cells are stripped of surrounding whitespace, nothing else.
"""

from __future__ import annotations

import re

from agentic_e2e.models import Mismatch, Verdict

# An expected cell written as an unrendered <placeholder> (as in the PRD example's <id>, <ref>)
# matches any non-empty value. Outline parameters are rendered before this point.
_PLACEHOLDER = re.compile(r"<[^<>]+>")
_NUMERIC = re.compile(r"[-+]?\d+(?:[.,]\d+)*")
_MAX_REASON_ITEMS = 5


def _cell(value: str) -> str:
    return value.strip()


def _matches(expected: str, actual: str) -> bool:
    if _PLACEHOLDER.fullmatch(expected):
        return actual != ""
    return expected == actual


def check_table(expected: list[list[str]], actual: list[list[str]]) -> Verdict:
    """Compare an expected table (header row first) with the rows read from the page."""
    if not actual or all(not any(_cell(c) for c in row) for row in actual):
        return Verdict.fail(
            "assertions",
            "execution",
            "the located table is empty or not rendered yet",
            retryable=True,
        )

    exp = [[_cell(c) for c in row] for row in expected]
    act = [[_cell(c) for c in row] for row in actual]
    header = exp[0]
    mismatches: list[Mismatch] = []

    if len(act[0]) != len(header):
        mismatches.append(
            Mismatch(
                kind="shape",
                row=0,
                expected=f"{len(header)} columns: {header}",
                actual=f"{len(act[0])} columns: {act[0]}",
            )
        )
        return _verdict(mismatches)

    for col, (want, got) in enumerate(zip(header, act[0], strict=True)):
        if not _matches(want, got):
            mismatches.append(
                Mismatch(
                    kind="header", row=0, column=col, column_name=want, expected=want, actual=got
                )
            )

    for row_index in range(1, max(len(exp), len(act))):
        if row_index >= len(act):
            mismatches.append(
                Mismatch(kind="missing_row", row=row_index, expected=" | ".join(exp[row_index]))
            )
            continue
        if row_index >= len(exp):
            mismatches.append(
                Mismatch(kind="extra_row", row=row_index, actual=" | ".join(act[row_index]))
            )
            continue
        want_row, got_row = exp[row_index], act[row_index]
        for col, want in enumerate(want_row):
            cell = got_row[col] if col < len(got_row) else None
            if cell is None or not _matches(want, cell):
                mismatches.append(
                    Mismatch(
                        kind="cell",
                        row=row_index,
                        column=col,
                        column_name=header[col] if col < len(header) else None,
                        expected=want,
                        actual=cell,
                    )
                )
        for col in range(len(want_row), len(got_row)):
            mismatches.append(Mismatch(kind="cell", row=row_index, column=col, actual=got_row[col]))

    return _verdict(mismatches)


def contains_literal(text: str, literal: str) -> bool:
    """Exact, case-sensitive containment. Numbers must stand alone: 272.00 is not in 1272.00."""
    if _NUMERIC.fullmatch(literal):
        pattern = rf"(?<![\d.,]){re.escape(literal)}(?!\d|[.,]\d)"
        return re.search(pattern, text) is not None
    return literal in text


def check_literals(literals: list[str], text: str) -> Verdict:
    """Every expected literal must appear exactly in the located element's text."""
    if not text.strip():
        return Verdict.fail(
            "assertions",
            "execution",
            "the located element has no text yet",
            retryable=True,
        )
    mismatches = [
        Mismatch(kind="literal", expected=lit, actual=_excerpt(text))
        for lit in literals
        if not contains_literal(text, lit)
    ]
    return _verdict(mismatches)


def _verdict(mismatches: list[Mismatch]) -> Verdict:
    if not mismatches:
        return Verdict.ok("assertions", "all expected values matched exactly")
    shown = "; ".join(m.describe() for m in mismatches[:_MAX_REASON_ITEMS])
    more = len(mismatches) - _MAX_REASON_ITEMS
    suffix = f"; and {more} more" if more > 0 else ""
    return Verdict.fail(
        "assertions",
        "defect",
        f"{len(mismatches)} mismatch(es): {shown}{suffix}",
        retryable=False,
        mismatches=mismatches,
    )


def _excerpt(text: str, limit: int = 200) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"
