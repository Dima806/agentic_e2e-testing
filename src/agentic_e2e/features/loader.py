"""Discover .feature files and validate them with pytest-bdd's Gherkin parser.

No step definitions are ever registered: pytest-bdd is used only to parse.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from pytest_bdd.exceptions import GherkinParseError
from pytest_bdd.gherkin_parser import GherkinDocument, get_gherkin_document
from pytest_bdd.parser import FeatureParser

from agentic_e2e.features.errors import FeatureValidationError
from agentic_e2e.features.parser import build_feature
from agentic_e2e.models import Feature


@dataclass(frozen=True)
class FeatureSource:
    path: Path
    slug: str  # artifacts/<slug>/ directory name


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "feature"


def discover(paths: Sequence[Path]) -> list[FeatureSource]:
    """Expand files and directories into feature sources with unique artifact slugs."""
    sources: list[FeatureSource] = []
    for root in paths:
        if root.is_dir():
            found = sorted(root.rglob("*.feature"))
            if not found:
                raise FeatureValidationError(root, None, "no .feature files found")
            sources += [
                FeatureSource(p, slugify(str(p.relative_to(root).with_suffix("")))) for p in found
            ]
        elif root.is_file():
            if root.suffix != ".feature":
                raise FeatureValidationError(root, None, "not a .feature file")
            sources.append(FeatureSource(root, slugify(root.stem)))
        else:
            raise FeatureValidationError(root, None, "no such file or directory")

    unique: dict[Path, FeatureSource] = {}
    by_slug: dict[str, Path] = {}
    for source in sources:
        resolved = source.path.resolve()
        if resolved in unique:
            continue
        if source.slug in by_slug:
            raise FeatureValidationError(
                source.path,
                None,
                f"artifact name {source.slug!r} clashes with {by_slug[source.slug]}; "
                "rename one of the files",
            )
        unique[resolved] = source
        by_slug[source.slug] = source.path
    return list(unique.values())


def load_feature(path: Path) -> Feature:
    """Parse and validate one feature file. Raises FeatureValidationError with file and line."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise FeatureValidationError(path, None, f"cannot read file: {exc}") from exc
    if not text.strip():
        raise FeatureValidationError(path, None, "file is empty")

    absolute = path.resolve()
    try:
        document = get_gherkin_document(str(absolute))
        bdd = FeatureParser(basedir=str(absolute.parent), filename=absolute.name).parse()
    except GherkinParseError as exc:
        raise FeatureValidationError(path, exc.line, exc.message) from exc
    except KeyError as exc:  # pytest-bdd assumes a Feature exists
        raise FeatureValidationError(path, None, "no 'Feature:' found") from exc

    _reject_duplicate_scenarios(document, path)
    feature = build_feature(bdd, path)
    if not feature.scenarios:
        raise FeatureValidationError(path, bdd.line_number, "feature has no scenarios")
    return feature


def _reject_duplicate_scenarios(document: GherkinDocument, path: Path) -> None:
    # pytest-bdd keys scenarios by name, so a duplicate would silently replace the first one.
    seen: set[str] = set()
    for child in document.feature.children:
        scenarios = [child.scenario] if child.scenario else []
        if child.rule:
            scenarios += [c.scenario for c in child.rule.children if c.scenario]
        for scenario in scenarios:
            if scenario is None:
                continue
            if scenario.name in seen:
                raise FeatureValidationError(
                    path, scenario.location.line, f"duplicate scenario name {scenario.name!r}"
                )
            seen.add(scenario.name)


Loaded = list[tuple[FeatureSource, Feature]]
Invalid = list[tuple[FeatureSource | None, FeatureValidationError]]


def validate(paths: Sequence[Path]) -> tuple[Loaded, Invalid]:
    """Discover and load everything; collect every error instead of stopping at the first.

    A discovery error (missing path, clashing names) has no source and stops discovery.
    """
    try:
        sources = discover(paths)
    except FeatureValidationError as exc:
        return [], [(None, exc)]
    loaded: Loaded = []
    errors: Invalid = []
    for source in sources:
        try:
            loaded.append((source, load_feature(source.path)))
        except FeatureValidationError as exc:
            errors.append((source, exc))
    return loaded, errors
