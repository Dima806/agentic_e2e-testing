"""Architecture rules from CLAUDE.md, checked over the source tree."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "agentic_e2e"
BROWSER = SRC / "browser"
OUTSIDE_BROWSER = sorted(p for p in SRC.rglob("*.py") if BROWSER not in p.parents)


def rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def imported_modules(path: Path) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            modules |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


@pytest.mark.parametrize("path", OUTSIDE_BROWSER, ids=rel)
def test_only_browser_talks_to_mcp(path: Path) -> None:
    offending = {m for m in imported_modules(path) if m == "mcp" or m.startswith("mcp.")}
    assert not offending, f"{rel(path)} imports {offending}"


MCP_TOOL_NAME = re.compile(r"\bbrowser_[a-z_]+\b")
# Sleeps and timed waits, and selector APIs, never appear outside browser/.
FORBIDDEN = {
    "sleep or timed wait": re.compile(
        r"\bsleep\s*\(|wait_for_timeout|wait_for_selector|waitFor|setTimeout"
    ),
    "selector API": re.compile(
        r"querySelector|\.locator\(|\bgetBy[A-Z]|xpath=|css=|document\.getElementById"
    ),
}


def string_literals(path: Path) -> list[str]:
    return [
        node.value
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


@pytest.mark.parametrize("path", OUTSIDE_BROWSER, ids=rel)
def test_no_mcp_tool_names_outside_browser(path: Path) -> None:
    names = [m for s in string_literals(path) for m in MCP_TOOL_NAME.findall(s)]
    assert not names, f"{rel(path)} names MCP tools {names}"


@pytest.mark.parametrize("path", OUTSIDE_BROWSER, ids=rel)
def test_no_waits_or_selectors_outside_browser(path: Path) -> None:
    text = path.read_text()
    found = {name: pattern.findall(text) for name, pattern in FORBIDDEN.items()}
    assert not any(found.values()), f"{rel(path)}: {found}"


def test_the_checks_would_catch_a_violation() -> None:
    assert MCP_TOOL_NAME.findall('call("browser_click")') == ["browser_click"]
    assert FORBIDDEN["sleep or timed wait"].search("await asyncio.sleep(1)")
    assert FORBIDDEN["selector API"].search("page.locator('#submit')")


def test_feature_files_contain_no_selectors_or_waits() -> None:
    for path in [*ROOT.joinpath("features").rglob("*.feature")]:
        text = path.read_text()
        assert not re.search(r"[#.][a-z][\w-]*\s*[{>]|xpath|css=|wait \d+ ?(ms|s|seconds)", text), (
            rel(path)
        )


def test_assertion_engine_never_converts_or_rounds_values() -> None:
    tree = ast.parse((SRC / "assertions" / "engine.py").read_text())
    calls = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert not calls & {"float", "int", "round", "Decimal", "format"}


def test_no_step_definitions_anywhere() -> None:
    step_apis = {"given", "when", "then", "step", "scenario", "scenarios", "parsers"}
    for path in [*SRC.rglob("*.py"), *(ROOT / "tests").rglob("*.py")]:
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("pytest_bdd"):
                names = {alias.name for alias in node.names}
                assert not names & step_apis, f"{rel(path)} imports step-definition APIs"
