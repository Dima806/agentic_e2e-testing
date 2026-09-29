"""BrowserClient parsing and guard rails, against a fake MCP session (no server, no browser).

The response texts below are trimmed copies of real Playwright MCP 0.0.83 output.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from mcp import MCPError
from mcp.types import CallToolResult, ErrorData, TextContent

from agentic_e2e.browser import BrowserError, ElementReadError
from agentic_e2e.browser.client import (
    BrowserClient,
    format_snapshot,
    launch,
    server_args,
    split_sections,
)
from agentic_e2e.config import Config

SNAPSHOT = """### Page
- Page URL: http://127.0.0.1:8765/payments.html
- Page Title: Payments
- Console: 1 errors, 0 warnings
### Snapshot
```yaml
- generic [active] [ref=e1]:
  - table "Payments" [ref=e6]:
    - row [ref=e19]:
      - cell "272.00" [ref=e22]
```"""

CLICK = """### Ran Playwright code
```js
await page.getByRole('link', { name: 'Refund' }).click();
```
### Page
- Page URL: http://127.0.0.1:8765/refund.html
### Snapshot
- [Snapshot](.playwright-mcp/page-2026-09-29T15-47-31-968Z.yml)"""

EVALUATE = """### Result
[
  ["Status", "Amount"],
  ["Successful", "272.00"]
]
### Ran Playwright code
```js
await page.getByText('StatusAmount').evaluate('(el) => ...');
```"""


def result(text: str, is_error: bool = False) -> CallToolResult:
    return CallToolResult(content=[TextContent(text=text)], is_error=is_error)


class FakeSession:
    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, name: str, arguments: dict[str, Any], **_: Any) -> CallToolResult:
        self.calls.append((name, arguments))
        response = self.responses.get(name, result("### Result\nok"))
        if isinstance(response, Exception):
            raise response
        return response


def client(**responses: Any) -> tuple[BrowserClient, FakeSession]:
    session = FakeSession({"browser_snapshot": result(SNAPSHOT), **responses})
    return BrowserClient(session, timeout_s=5), session  # type: ignore[arg-type]


def test_split_sections() -> None:
    sections = split_sections(CLICK)
    assert list(sections) == ["Ran Playwright code", "Page", "Snapshot"]
    assert sections["Page"] == "- Page URL: http://127.0.0.1:8765/refund.html"


def test_format_snapshot_keeps_page_and_tree_and_drops_noise() -> None:
    assert format_snapshot(SNAPSHOT) == (
        "Page URL: http://127.0.0.1:8765/payments.html\n"
        "Page Title: Payments\n"
        '- generic [active] [ref=e1]:\n  - table "Payments" [ref=e6]:\n'
        '    - row [ref=e19]:\n      - cell "272.00" [ref=e22]'
    )


def test_server_args_pin_version_chromium_and_keep_images_away_from_the_model() -> None:
    args = server_args(Config(), Path("/tmp/work"))
    assert args[:2] == ["-y", "@playwright/mcp@0.0.83"]
    for flag in ("--isolated", "--headless"):
        assert flag in args
    assert args[args.index("--browser") + 1] == "chromium"
    assert args[args.index("--image-responses") + 1] == "omit"
    assert "--headless" not in server_args(Config(headless=False), Path("/tmp/work"))


async def test_actions_return_status_plus_a_fresh_snapshot_without_codegen() -> None:
    browser, session = client(browser_click=result(CLICK))
    outcome = await browser.click("Refund link", "e31")
    assert outcome.ok
    assert outcome.message.startswith("OK\n\nPage URL: http://127.0.0.1:8765/payments.html")
    assert "getByRole" not in outcome.message
    assert session.calls[0] == ("browser_click", {"element": "Refund link", "target": "e31"})


@pytest.mark.parametrize("ref", ["#submit", "button.primary", "//button", "text=Refund", "e"])
async def test_selectors_are_refused_without_reaching_the_browser(ref: str) -> None:
    browser, session = client()
    outcome = await browser.click("Submit", ref)
    assert not outcome.ok
    assert "not a snapshot ref" in outcome.message
    assert session.calls == []
    with pytest.raises(ElementReadError):
        await browser.read_table(ref)


@pytest.mark.parametrize("ref", ["e12", "f2e7"])
async def test_snapshot_refs_are_accepted(ref: str) -> None:
    browser, _ = client()
    assert (await browser.hover("row", ref)).ok


async def test_tool_errors_are_reported_to_the_model_not_raised() -> None:
    error = result("### Error\nError: Ref e999 not found in the current page snapshot.", True)
    browser, _ = client(browser_click=error)
    outcome = await browser.click("x", "e999")
    assert not outcome.ok
    assert outcome.message.startswith("ERROR: Error: Ref e999 not found")


async def test_protocol_errors_are_tool_errors_but_crashes_are_infrastructure() -> None:
    browser, _ = client(
        browser_click=MCPError.from_error_data(ErrorData(code=-32001, message="timeout"))
    )
    assert not (await browser.click("x", "e1")).ok
    browser, _ = client(browser_hover=ConnectionResetError("server gone"))
    with pytest.raises(BrowserError, match="browser_hover failed"):
        await browser.hover("x", "e1")


async def test_read_table_parses_the_evaluate_result_exactly() -> None:
    browser, session = client(browser_evaluate=result(EVALUATE))
    assert await browser.read_table("f2e7") == [["Status", "Amount"], ["Successful", "272.00"]]
    name, args = session.calls[0]
    assert (name, args["target"]) == ("browser_evaluate", "f2e7")


@pytest.mark.parametrize(
    "response",
    [
        result("### Error\nRef e5 not found", is_error=True),
        result("### Result\nnot json"),
        result('### Result\n{"a": 1}'),
    ],
)
async def test_unreadable_tables_raise_element_read_error(response: CallToolResult) -> None:
    browser, _ = client(browser_evaluate=response)
    with pytest.raises(ElementReadError):
        await browser.read_table("e5")


async def test_read_element_text() -> None:
    browser, _ = client(browser_evaluate=result('### Result\n"Refund payment P-1001"'))
    assert await browser.read_element_text("e3") == "Refund payment P-1001"


async def test_launch_failure_is_a_browser_error(tmp_path: Path) -> None:
    cfg = Config(npx_command=str(tmp_path / "no-such-npx"))
    with pytest.raises(BrowserError, match="could not start Playwright MCP"):
        async with launch(cfg, tmp_path):
            pass
