"""BrowserClient over the Playwright MCP server (stdio).

The ONLY module that knows MCP tool names, JavaScript, selectors and wait/settle behaviour.
"""

from __future__ import annotations

import json
import re
import tempfile
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any

from mcp import ClientSession, MCPError, StdioServerParameters, stdio_client
from mcp.types import TextContent

from agentic_e2e.browser.base import ActionOutcome, BrowserError, ElementReadError
from agentic_e2e.config import Config

# Snapshot refs look like e12 (or f1e3 inside a frame). Anything else is treated as a selector
# and refused, so the model can never drive the page by selector.
_REF = re.compile(r"(?:f\d+)?e\d+")
_YAML_BLOCK = re.compile(r"```(?:yaml)?\n(.*?)```", re.DOTALL)

REQUIRED_TOOLS = frozenset(
    {
        "browser_navigate",
        "browser_navigate_back",
        "browser_snapshot",
        "browser_click",
        "browser_hover",
        "browser_type",
        "browser_select_option",
        "browser_press_key",
        "browser_find",
        "browser_wait_for",
        "browser_evaluate",
        "browser_take_screenshot",
    }
)

# Reads rendered cell text (innerText: what the user sees) from the located table, or from the
# table enclosing the located element. The values go to the assertion engine, never to a model.
_READ_TABLE_JS = """(element) => {
  const root = element.closest('table,[role="table"],[role="grid"],[role="treegrid"]') || element;
  const rows = root.tagName === 'TABLE'
    ? Array.from(root.rows)
    : Array.from(root.querySelectorAll('[role="row"]'));
  const cellSelector = '[role="cell"],[role="gridcell"],[role="columnheader"],[role="rowheader"]';
  return rows.map((row) => Array.from(
    row.tagName === 'TR' ? row.cells : row.querySelectorAll(cellSelector),
    (cell) => cell.innerText,
  ));
}"""
_READ_TEXT_JS = "(element) => element.innerText"


SERVER_LOG = "mcp-server-log.json"


def server_args(cfg: Config, scratch: Path) -> list[str]:
    args = [
        "-y",
        f"@playwright/mcp@{cfg.playwright_mcp_version}",
        "--isolated",
        "--browser",
        "chromium",
        # Screenshots are evidence for humans, written to disk; never sent back to the model.
        "--image-responses",
        "omit",
        # The server's own side files (page .yml snapshots, console .log) go to a scratch
        # directory that is deleted with the session, so artifacts hold only JSON and PNG.
        "--output-dir",
        str(scratch),
    ]
    if cfg.headless:
        args.append("--headless")
    return args


@asynccontextmanager
async def launch(cfg: Config, workdir: Path) -> AsyncIterator[BrowserClient]:
    """Start a fresh Playwright MCP server (and browser) for one scenario.

    The server runs with `workdir` (the feature's artifact directory) as its working directory,
    which is what allows it to write screenshots there. Anything the server prints to stderr is
    kept as `mcp-server-log.json` in `workdir`, and only when there is something to keep.
    """
    workdir = _ensure_dir(workdir)
    try:
        async with AsyncExitStack() as stack:
            scratch = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="agentic-e2e-")))
            stderr_path = scratch / "stderr.log"
            # Runs after the server has exited and before the scratch directory is removed.
            stack.callback(_keep_server_log, stderr_path, workdir / SERVER_LOG)
            errlog = stack.enter_context(stderr_path.open("w", encoding="utf-8"))
            params = StdioServerParameters(
                command=cfg.npx_command, args=server_args(cfg, scratch), cwd=workdir
            )
            try:
                read, write = await stack.enter_async_context(stdio_client(params, errlog=errlog))
                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                available = {tool.name for tool in (await session.list_tools()).tools}
            except Exception as exc:
                raise BrowserError(
                    f"could not start Playwright MCP {cfg.playwright_mcp_version} "
                    f"(server output, if any: {workdir / SERVER_LOG}): {exc!r}"
                ) from exc
            missing = sorted(REQUIRED_TOOLS - available)
            if missing:
                raise BrowserError(
                    f"Playwright MCP {cfg.playwright_mcp_version} lacks tools {missing}; "
                    "the pinned version and this wrapper disagree"
                )
            yield BrowserClient(session, cfg.browser_call_timeout_s)
    except BaseExceptionGroup as group:
        # The MCP client's task groups wrap whatever the session body raised; hand callers the
        # original exception when there is exactly one.
        leaf = _single_leaf(group)
        if leaf is None:
            raise
        leaf.__suppress_context__ = True
        raise leaf from leaf.__cause__


def _keep_server_log(stderr_path: Path, target: Path) -> None:
    """Append this session's server stderr to a JSON list in the artifacts, if it said anything."""
    try:
        text = stderr_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    if not text.strip():
        return
    entries: list[object] = []
    if target.is_file():
        try:
            loaded = json.loads(target.read_text(encoding="utf-8"))
            entries = loaded if isinstance(loaded, list) else [loaded]
        except (OSError, json.JSONDecodeError):
            entries = []
    entries.append({"source": "playwright-mcp stderr", "session": len(entries) + 1, "text": text})
    target.write_text(json.dumps(entries, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _single_leaf(group: BaseExceptionGroup[BaseException]) -> BaseException | None:
    leaves: list[BaseException] = []
    stack: list[BaseException] = [group]
    while stack:
        exc = stack.pop()
        if isinstance(exc, BaseExceptionGroup):
            stack.extend(exc.exceptions)
        else:
            leaves.append(exc)
    return leaves[0] if len(leaves) == 1 else None


class BrowserClient:
    def __init__(self, session: ClientSession, timeout_s: float) -> None:
        self._session = session
        self._timeout_s = timeout_s

    # --- executor-facing actions -------------------------------------------------------------

    async def snapshot(self) -> str:
        is_error, text = await self._call("browser_snapshot", {})
        if is_error:
            raise BrowserError(f"snapshot failed: {text}")
        return format_snapshot(text)

    async def navigate(self, url: str) -> ActionOutcome:
        return await self._act("browser_navigate", {"url": url})

    async def click(self, element: str, ref: str) -> ActionOutcome:
        return await self._act_on(ref, "browser_click", {"element": element, "target": ref})

    async def hover(self, element: str, ref: str) -> ActionOutcome:
        return await self._act_on(ref, "browser_hover", {"element": element, "target": ref})

    async def type_text(self, element: str, ref: str, text: str, submit: bool) -> ActionOutcome:
        args = {"element": element, "target": ref, "text": text, "submit": submit}
        return await self._act_on(ref, "browser_type", args)

    async def select_option(self, element: str, ref: str, values: list[str]) -> ActionOutcome:
        args = {"element": element, "target": ref, "values": values}
        return await self._act_on(ref, "browser_select_option", args)

    async def press_key(self, key: str) -> ActionOutcome:
        return await self._act("browser_press_key", {"key": key})

    async def go_back(self) -> ActionOutcome:
        return await self._act("browser_navigate_back", {})

    async def find_text(self, text: str) -> ActionOutcome:
        is_error, raw = await self._call("browser_find", {"text": text})
        sections = split_sections(raw)
        return ActionOutcome(not is_error, sections.get("Result") or sections.get("Error") or raw)

    async def wait_for_text(self, text: str) -> ActionOutcome:
        return await self._act("browser_wait_for", {"text": text})

    # --- deterministic reads for the assertion engine ----------------------------------------

    async def read_element_text(self, ref: str) -> str:
        value = await self._evaluate(ref, _READ_TEXT_JS)
        if not isinstance(value, str):
            raise ElementReadError(f"element {ref} returned {type(value).__name__}, not text")
        return value

    async def read_table(self, ref: str) -> list[list[str]]:
        value = await self._evaluate(ref, _READ_TABLE_JS)
        if not isinstance(value, list) or not all(
            isinstance(row, list) and all(isinstance(cell, str) for cell in row) for row in value
        ):
            raise ElementReadError(f"element {ref} did not yield a table of text cells")
        return value

    async def screenshot(self, path: Path) -> bool:
        """Save a PNG of the viewport. True only if a real PNG file landed at `path`."""
        if path.suffix != ".png":
            raise ValueError(f"screenshots are PNG files; got {path.name}")
        path = _ensure_dir(path.parent) / path.name
        args = {"type": "png", "scale": "css", "filename": str(path)}
        is_error, _ = await self._call("browser_take_screenshot", args)
        return not is_error and is_png(path)

    # --- plumbing ----------------------------------------------------------------------------

    async def _evaluate(self, ref: str, function: str) -> Any:
        if not _REF.fullmatch(ref):
            raise ElementReadError(f"{ref!r} is not a snapshot ref")
        args = {"element": "element holding the asserted data", "target": ref, "function": function}
        is_error, text = await self._call("browser_evaluate", args)
        sections = split_sections(text)
        if is_error:
            raise ElementReadError(sections.get("Error") or text)
        try:
            return json.loads(sections.get("Result", ""))
        except json.JSONDecodeError as exc:
            raise ElementReadError(f"unreadable evaluate result for {ref}: {exc}") from exc

    async def _act_on(self, ref: str, tool: str, args: dict[str, Any]) -> ActionOutcome:
        if not _REF.fullmatch(ref):
            return ActionOutcome(
                False,
                f"{ref!r} is not a snapshot ref. Use a ref such as e12 from the latest snapshot; "
                "selectors are not allowed.",
            )
        return await self._act(tool, args)

    async def _act(self, tool: str, args: dict[str, Any]) -> ActionOutcome:
        is_error, text = await self._call(tool, args)
        sections = split_sections(text)
        if is_error:
            status = "ERROR: " + (sections.get("Error") or text)
        else:
            status = "OK" + (f"\n{sections['Result']}" if sections.get("Result") else "")
        # Every action result carries a fresh snapshot so the next decision uses current refs.
        return ActionOutcome(not is_error, f"{status}\n\n{await self.snapshot()}")

    async def _call(self, tool: str, args: dict[str, Any]) -> tuple[bool, str]:
        try:
            result = await self._session.call_tool(tool, args, read_timeout_seconds=self._timeout_s)
        except MCPError as exc:  # the server rejected or timed out this call; the session lives
            return True, f"### Error\n{exc}"
        except Exception as exc:
            raise BrowserError(f"Playwright MCP call {tool} failed: {exc!r}") from exc
        text = "\n".join(c.text for c in result.content if isinstance(c, TextContent))
        return result.is_error, text


def _ensure_dir(directory: Path) -> Path:
    # Tiny local filesystem calls; not worth a thread hop.
    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    return directory


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def is_png(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(len(PNG_SIGNATURE)) == PNG_SIGNATURE
    except OSError:
        return False


def split_sections(text: str) -> dict[str, str]:
    """Split a Playwright MCP response into its '### Heading' sections."""
    sections: dict[str, str] = {}
    heading: str | None = None
    lines: list[str] = []
    for line in text.splitlines():
        if line.startswith("### "):
            if heading is not None:
                sections[heading] = "\n".join(lines).strip()
            heading, lines = line[4:].strip(), []
        else:
            lines.append(line)
    if heading is not None:
        sections[heading] = "\n".join(lines).strip()
    return sections


def format_snapshot(text: str) -> str:
    """Page URL/title plus the accessibility tree, without code-generation noise."""
    sections = split_sections(text)
    page = "\n".join(
        line.removeprefix("- ")
        for line in sections.get("Page", "").splitlines()
        if line.strip() and not line.removeprefix("- ").startswith("Console:")
    )
    body = sections.get("Snapshot", text)
    match = _YAML_BLOCK.search(body)
    tree = (match.group(1) if match else body).strip() or "(empty page)"
    return f"{page}\n{tree}".strip()
