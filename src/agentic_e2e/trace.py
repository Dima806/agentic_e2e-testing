"""The trace sink agents and the runner write events to (implemented by ArtifactWriter)."""

from __future__ import annotations

from typing import Protocol


class Trace(Protocol):
    def event(self, kind: str, **fields: object) -> None: ...


class NullTrace:
    def event(self, kind: str, **fields: object) -> None:
        return None
