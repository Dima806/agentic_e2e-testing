"""Turn pytest-bdd's parsed feature into ordered, typed steps.

Pure functions: no I/O, no browser, no model.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path

from pytest_bdd.gherkin_parser import DataTable
from pytest_bdd.parser import Feature as BddFeature
from pytest_bdd.parser import ScenarioTemplate
from pytest_bdd.parser import Step as BddStep

from agentic_e2e.features.errors import FeatureValidationError
from agentic_e2e.models import Feature, Scenario, Step, StepType

_TYPE_BY_BDD: dict[str, StepType] = {"given": "context", "when": "action", "then": "outcome"}

_QUOTED = re.compile(r'"([^"]*)"')
# A number standing on its own: not part of an identifier such as "P-1001", "v2" or "2nd".
_NUMBER = re.compile(r"(?<![\w.,+\-])[-+]?\d+(?:[.,]\d+)*(?!\w|[.,]\d)")


def build_feature(bdd: BddFeature, path: Path) -> Feature:
    scenarios: list[Scenario] = []
    for template in bdd.scenarios.values():
        if template.templated:
            scenarios.extend(_expand_outline(template, path))
        else:
            scenarios.append(_scenario(template.name, template.line_number, template.steps, path))
    return Feature(path=path, name=bdd.name or path.stem, scenarios=scenarios)


def _expand_outline(template: ScenarioTemplate, path: Path) -> list[Scenario]:
    """Expand each Examples row into its own scenario; never run the unexpanded template."""
    expanded: list[Scenario] = []
    for examples in template.examples:
        for context in examples.as_contexts():
            rendered = template.render(context)
            params = ", ".join(f"{k}={v}" for k, v in context.items())
            name = f"{template.name} [#{len(expanded) + 1}: {params}]"
            expanded.append(_scenario(name, template.line_number, rendered.steps, path))
    if not expanded:
        raise FeatureValidationError(
            path, template.line_number, f"Scenario Outline {template.name!r} has no Examples rows"
        )
    return expanded


def _scenario(name: str, line: int, bdd_steps: Sequence[BddStep], path: Path) -> Scenario:
    if not bdd_steps:
        raise FeatureValidationError(path, line, f"scenario {name!r} has no steps")
    return Scenario(
        name=name,
        line=line,
        steps=[_step(i, s, path) for i, s in enumerate(bdd_steps)],
    )


def _step(index: int, bdd: BddStep, path: Path) -> Step:
    effective = _TYPE_BY_BDD.get(bdd.type)
    if effective is None:
        raise FeatureValidationError(path, bdd.line_number, f"unknown step type {bdd.type!r}")
    data_table = _data_table(bdd.datatable)
    docstring = bdd.docstring
    table = data_table
    if table is None and docstring is not None:
        table = parse_pipe_table(docstring, path=path, line=bdd.line_number)
    literals: list[str] = []
    if effective == "outcome":
        literals = extract_literals(bdd.name, docstring if table is None else None)
    return Step(
        index=index,
        line=bdd.line_number,
        keyword=bdd.keyword.strip(),
        effective_type=effective,
        text=bdd.name,
        data_table=data_table,
        docstring=docstring,
        table=table,
        expected_literals=literals,
    )


def _data_table(table: DataTable | None) -> list[list[str]] | None:
    if table is None:
        return None
    # pytest-bdd doubles every backslash in cell values; halve them back to the exact text.
    return [[cell.value.replace("\\\\", "\\").strip() for cell in row.cells] for row in table.rows]


def parse_pipe_table(
    docstring: str, *, path: Path | None = None, line: int | None = None
) -> list[list[str]] | None:
    """Parse a docstring that is entirely a pipe table; return None for any other docstring.

    Cells are stripped of surrounding whitespace and otherwise kept exactly. Gherkin cell
    escapes are honoured: \\| is a literal pipe, \\\\ a backslash, \\n a newline.
    """
    lines = [raw.strip() for raw in docstring.splitlines() if raw.strip()]
    if not lines or not all(
        ln.startswith("|") and ln.endswith("|") and len(ln) > 1 for ln in lines
    ):
        return None
    rows = [_split_row(ln) for ln in lines]
    width = len(rows[0])
    for number, row in enumerate(rows[1:], start=2):
        if len(row) != width:
            raise FeatureValidationError(
                path or Path("<docstring>"),
                line,
                f"docstring table row {number} has {len(row)} cells, expected {width}",
            )
    return rows


def _split_row(line: str) -> list[str]:
    cells: list[str] = []
    current: list[str] = []
    chars = iter(line[1:])  # skip the leading pipe
    for ch in chars:
        if ch == "\\":
            nxt = next(chars, "")
            current.append({"|": "|", "\\": "\\", "n": "\n"}.get(nxt, "\\" + nxt))
        elif ch == "|":
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    return cells


def extract_literals(text: str, docstring: str | None = None) -> list[str]:
    """Exact values an outcome step expects on screen.

    Double-quoted strings, standalone numbers outside quotes (kept as written, so "272.00"
    stays "272.00"), and a plain (non-table) docstring.
    """
    found = [m for m in _QUOTED.findall(text) if m]
    found += _NUMBER.findall(_QUOTED.sub(" ", text))
    if docstring is not None and docstring.strip():
        found.append(docstring.strip())
    return list(dict.fromkeys(found))
