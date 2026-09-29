"""The browser interface the executor and runner depend on.

Intent-level operations only. Element refs come from the accessibility snapshot; no selector,
wait or MCP tool name crosses this boundary.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class BrowserError(Exception):
    """The browser or its MCP server failed. Infrastructure: the feature is blocked."""


class ElementReadError(Exception):
    """A located element could not be read (stale or unknown ref). Retryable."""


@dataclass(frozen=True)
class ActionOutcome:
    ok: bool
    message: str


class Browser(Protocol):
    async def snapshot(self) -> str: ...

    async def navigate(self, url: str) -> ActionOutcome: ...

    async def click(self, element: str, ref: str) -> ActionOutcome: ...

    async def hover(self, element: str, ref: str) -> ActionOutcome: ...

    async def type_text(self, element: str, ref: str, text: str, submit: bool) -> ActionOutcome: ...

    async def select_option(self, element: str, ref: str, values: list[str]) -> ActionOutcome: ...

    async def press_key(self, key: str) -> ActionOutcome: ...

    async def go_back(self) -> ActionOutcome: ...

    async def find_text(self, text: str) -> ActionOutcome: ...

    async def wait_for_text(self, text: str) -> ActionOutcome: ...

    async def read_element_text(self, ref: str) -> str: ...

    async def read_table(self, ref: str) -> list[list[str]]: ...

    async def screenshot(self, path: Path) -> bool: ...


# Opens a fresh browser session whose files (screenshots, server log) live under the given dir.
BrowserFactory = Callable[[Path], AbstractAsyncContextManager[Browser]]
