"""Theme catalogue: labels, keywords and templates for the local engine (§4.4 step 7, §4.5).

Templates never contain typed numbers. Figures are placeholders filled in by code from the
computed stats, so they are always right and change from week to week. Summaries may only use
keyword placeholders, because the renderer already prints the shares next to them (CMP-22).
"""

from __future__ import annotations

import re
import string
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

KEYWORD_FIELDS = frozenset({"kw1", "kw2", "kw3"})
FIGURE_FIELDS = frozenset({"count", "recent_count", "share", "negative_share", "trend"})
_ID = re.compile(r"^[a-z][a-z0-9_]{1,39}$")


class CatalogueError(ValueError):
    """config/themes.yaml is missing or invalid."""


def _fields(template: str) -> set[str]:
    return {f for _, f, _, _ in string.Formatter().parse(template) if f is not None}


def _check(template: str, allowed: frozenset[str], where: str) -> str:
    try:
        used = _fields(template)
    except ValueError as e:
        raise ValueError(f"{where}: bad template syntax: {e}") from e
    if used - allowed:
        raise ValueError(f"{where}: unknown placeholders {sorted(used - allowed)}; "
                         f"allowed: {sorted(allowed)}")
    literal = "".join(text for text, _, _, _ in string.Formatter().parse(template))
    if any(ch.isdigit() for ch in literal):
        raise ValueError(f"{where}: templates must not contain digits; use a placeholder")
    if not template.strip():
        raise ValueError(f"{where}: empty template")
    return template


class Templates(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    summary: str
    summary_short: str
    action: str
    action_short: str
    signal: str

    @field_validator("summary", "summary_short")
    @classmethod
    def _summary(cls, v: str) -> str:
        return _check(v, KEYWORD_FIELDS, "summary")

    @field_validator("action", "action_short", "signal")
    @classmethod
    def _action(cls, v: str) -> str:
        return _check(v, KEYWORD_FIELDS | FIGURE_FIELDS, "action/signal")


class Entry(Templates):
    id: str
    label: str
    keywords: list[str] = Field(min_length=2)

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not _ID.match(v) or v in ("general_praise", "other"):
            raise ValueError(f"theme id {v!r} must be snake_case and not a reserved bucket")
        return v

    @field_validator("label")
    @classmethod
    def _label(cls, v: str) -> str:
        if not v.strip() or len(v.split()) > 4:
            raise ValueError(f"label {v!r} must be 1–4 words")
        return v

    @field_validator("keywords")
    @classmethod
    def _keywords(cls, v: list[str]) -> list[str]:
        return [k.strip().lower() for k in v if k.strip()]

    @property
    def tokens(self) -> frozenset[str]:
        return frozenset(t for k in self.keywords for t in k.split())


class Catalogue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    generic: Templates
    themes: list[Entry] = Field(min_length=1)

    @field_validator("themes")
    @classmethod
    def _unique(cls, v: list[Entry]) -> list[Entry]:
        ids = [e.id for e in v]
        if len(ids) != len(set(ids)):
            raise ValueError("theme ids must be unique")
        return v

    def match(self, keywords: Sequence[str], *, top: int = 6, min_hits: int = 2) -> Entry | None:
        """The entry sharing the most of the top keywords (a keyword counts when any of its
        words is one of the entry's words); ties go to the earlier entry."""
        best, best_hits = None, 0
        for entry in self.themes:
            hits = sum(1 for k in keywords[:top] if set(k.split()) & entry.tokens)
            if hits > best_hits:
                best, best_hits = entry, hits
        return best if best_hits >= min_hits else None


def load_catalogue(path: Path) -> Catalogue:
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise CatalogueError(f"theme catalogue not found: {path}") from e
    except yaml.YAMLError as e:
        raise CatalogueError(f"theme catalogue is not valid YAML: {path}: {e}") from e
    try:
        return Catalogue.model_validate(raw)
    except ValueError as e:
        raise CatalogueError(f"invalid theme catalogue {path}:\n{e}") from e


@dataclass(frozen=True)
class Figures:
    count: int
    recent_count: int
    share: str            # formatted, e.g. "18%"
    negative_share: str
    trend: str


def fill(template: str, keywords: Sequence[str], figures: Figures | None = None) -> str:
    """Fill a template. Missing keywords fall back to the last one available."""
    kws = list(keywords) or ["these reviews"]
    while len(kws) < 3:
        kws.append(kws[-1])
    values: Mapping[str, object] = {"kw1": kws[0], "kw2": kws[1], "kw3": kws[2]}
    if figures is not None:
        values = {**values, "count": f"{figures.count:,}",
                  "recent_count": f"{figures.recent_count:,}", "share": figures.share,
                  "negative_share": figures.negative_share, "trend": figures.trend}
    return template.format(**values)
