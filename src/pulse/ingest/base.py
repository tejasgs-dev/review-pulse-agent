"""Source adapter interface (architecture §4.1).

Adapters only fetch and map fields. They never decide anything about content:
range checks, windowing, dedupe and language detection all live in normalize.py.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Protocol

from pulse.models import Source


class SourceError(RuntimeError):
    """A source (or one of its files or pages) could not be read."""


@dataclass(frozen=True)
class RawReview:
    """One review as mapped from a source. Holds no reviewer identity fields."""

    source: Source
    source_review_id: str | None
    rating: int | None          # parsed, not yet range-checked
    title: str | None
    text: str | None
    submitted_at: datetime | None  # timezone-aware
    updated_at: datetime | None    # timezone-aware; None when the source has no edit time
    app_version: str | None

    def to_json(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "source_review_id": self.source_review_id,
            "rating": self.rating,
            "title": self.title,
            "text": self.text,
            "submitted_at": self.submitted_at.isoformat() if self.submitted_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "app_version": self.app_version,
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> RawReview:
        def ts(v: str | None) -> datetime | None:
            return datetime.fromisoformat(v) if v else None

        return cls(
            source=d["source"],
            source_review_id=d.get("source_review_id"),
            rating=d.get("rating"),
            title=d.get("title"),
            text=d.get("text"),
            submitted_at=ts(d.get("submitted_at")),
            updated_at=ts(d.get("updated_at")),
            app_version=d.get("app_version"),
        )


@dataclass
class FetchResult:
    """What one adapter returned. Iterating it yields the reviews."""

    source: Source
    reviews: list[RawReview] = field(default_factory=list)
    skipped: Counter[str] = field(default_factory=Counter)
    warnings: list[str] = field(default_factory=list)
    complete: bool = True       # False when later pages or files failed
    # For feed sources: the earliest IST date this fetch is known to cover without gaps
    # (the `since` it was asked for, once paging reached it). None when unknown.
    covered_since: date | None = None

    def __iter__(self) -> Iterator[RawReview]:
        return iter(self.reviews)

    def __len__(self) -> int:
        return len(self.reviews)


class ReviewSource(Protocol):
    name: Source

    def fetch(self, since: date, until: date) -> FetchResult: ...
