"""BrowserClient against the real Playwright MCP server and Chromium. No model, no API key.

Run with `make test-browser` (needs Node/npx and `make browser`).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from agentic_e2e.browser import BrowserError
from agentic_e2e.browser.client import launch
from agentic_e2e.config import Config

pytestmark = pytest.mark.browser


def ref(snapshot: str, pattern: str) -> str:
    match = re.search(pattern + r".*?\[ref=(\w+)\]", snapshot)
    assert match, f"{pattern!r} not in snapshot:\n{snapshot}"
    return match.group(1)


async def test_refund_flow_with_exact_table_read(demo_site_url: str, tmp_path: Path) -> None:
    async with launch(Config(), tmp_path) as browser:
        page = (await browser.navigate(demo_site_url + "payments.html")).message
        assert 'link "Refund"' not in page  # hidden until the row is hovered

        hovered = (await browser.hover("payment P-1001", ref(page, r'cell "P-1001"'))).message
        opened = await browser.click("Refund link", ref(hovered, r'link "Refund"'))
        assert opened.ok and "refund.html?payment=P-1001" in opened.message

        refused = await browser.click("Submit", "button[type=submit]")
        assert not refused.ok and "not a snapshot ref" in refused.message

        submit = ref(await browser.snapshot(), r'button "Submit refund"')
        assert (await browser.click("Submit refund", submit)).ok
        assert (await browser.wait_for_text("Successful")).ok

        snapshot = await browser.snapshot()
        rows = await browser.read_table(ref(snapshot, r'table "Refunds"'))
        assert rows[0] == ["Status", "Payment", "Reference", "Amount"]
        assert rows[1][:2] == ["Successful", "P-1001"] and rows[1][3] == "272.00"

        heading = await browser.read_element_text(ref(snapshot, r'heading "Refund status"'))
        assert heading == "Refund status"
        assert await browser.screenshot(tmp_path / "screenshots" / "s01-st00-a1.png")
    shot = tmp_path / "screenshots" / "s01-st00-a1.png"
    assert shot.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert shot.stat().st_size > 1000
    # The server's own side files (.yml snapshots, console .log) never land in the artifacts.
    leftovers = [
        p.relative_to(tmp_path).as_posix()
        for p in tmp_path.rglob("*")
        if p.is_file() and p.suffix not in {".png", ".json"}
    ]
    assert leftovers == []


async def test_errors_inside_a_session_surface_unwrapped(
    demo_site_url: str, tmp_path: Path
) -> None:
    async def fail_inside_session() -> None:
        async with launch(Config(), tmp_path) as browser:
            await browser.navigate(demo_site_url)
            raise BrowserError("boom")

    # The MCP client's task groups would otherwise wrap this in nested ExceptionGroups.
    with pytest.raises(BrowserError, match="boom"):
        await fail_inside_session()
