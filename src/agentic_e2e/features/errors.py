from __future__ import annotations

from pathlib import Path


class FeatureValidationError(Exception):
    """A feature file cannot be run. Raised before any browser or model starts."""

    def __init__(self, path: Path, line: int | None, message: str) -> None:
        super().__init__(message)
        self.path = path
        self.line = line
        self.message = message

    def __str__(self) -> str:
        location = f"{self.path}:{self.line}" if self.line is not None else str(self.path)
        return f"{location}: {self.message}"
