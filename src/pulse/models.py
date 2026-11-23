"""Core data model (architecture §5)."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal

Source = Literal["play_store", "app_store"]
SOURCES: tuple[Source, ...] = ("play_store", "app_store")


def review_key(source: str, source_review_id: str) -> str:
    """Opaque, stable key for a review. Never published (§4.3 layer 1)."""
    return hashlib.sha256(f"{source}:{source_review_id}".encode()).hexdigest()


def _check_rating(rating: int) -> None:
    if isinstance(rating, bool) or not isinstance(rating, int) or not 1 <= rating <= 5:
        raise ValueError(f"rating must be an int from 1 to 5, got {rating!r}")


@dataclass(frozen=True)
class Review:
    key: str                 # sha256(source + source_review_id), opaque, never published
    source: Source
    rating: int              # 1–5
    title: str | None
    text: str                # emoji-free review text (kept locally only)
    review_date: date        # IST date of the latest version of the review
    language: str            # always "en": non-English reviews are dropped at ingest
    app_version: str | None  # aggregate use only, never shown per review
    updated_at: datetime     # UTC time of the latest version; the newest version wins

    def __post_init__(self) -> None:
        _check_rating(self.rating)
        if self.source not in SOURCES:
            raise ValueError(f"unknown source {self.source!r}")
        if self.updated_at.tzinfo is None:
            raise ValueError("updated_at must be timezone-aware")


@dataclass(frozen=True)
class CleanReview:
    id: str                  # short run-local ID, e.g. "r0042"
    key: str
    source: Source
    rating: int
    review_date: date
    canonical_text: str      # PII-scrubbed; the only text used downstream

    def __post_init__(self) -> None:
        _check_rating(self.rating)


@dataclass
class Theme:
    id: str                  # snake_case theme ID from Pass A, e.g. "withdrawals"
    label: str               # e.g. "Withdrawals & payouts"
    description: str
    review_ids: list[str] = field(default_factory=list)
    volume_share: float = 0.0
    negative_share: float = 0.0
    avg_rating: float = 0.0
    score: float = 0.0
    summary: str = ""        # one line written by the composer; numbers are added in code


@dataclass
class Quote:
    review_id: str
    theme_id: str
    span: tuple[int, int]    # character offsets into canonical_text
    text: str                # = canonical_text[span[0]:span[1]], filled in by code
    rating: int
    source: str = ""         # "play_store" | "app_store", shown next to the quote

    @classmethod
    def from_span(
        cls, review: CleanReview, theme_id: str, start: int, end: int
    ) -> Quote:
        """Build a quote by slicing the canonical text, so wording can never be invented."""
        if not 0 <= start < end <= len(review.canonical_text):
            raise ValueError(
                f"span ({start}, {end}) is out of range for review {review.id} "
                f"(length {len(review.canonical_text)})"
            )
        return cls(
            review_id=review.id,
            theme_id=theme_id,
            span=(start, end),
            text=review.canonical_text[start:end],
            rating=review.rating,
            source=review.source,
        )


@dataclass
class ActionIdea:
    theme_id: str
    action: str              # the concrete next step
    signal: str              # how we'd know it worked


@dataclass
class Pulse:
    week: str                # "2026-W46"
    window: tuple[date, date]
    review_count: int
    sources: dict[str, int]  # {"play_store": 812, "app_store": 143}
    top_themes: list[Theme]  # exactly 3
    quotes: list[Quote]      # exactly 3
    actions: list[ActionIdea]  # exactly 3
    general_praise_share: float = 0.0  # shown in the source line (§7.1)
    clustered_theme_count: int = 0     # themes found by clustering (5 or fewer)
    # Sources whose fetch failed this week (§10 row 1): named in the source line.
    unavailable_sources: list[str] = field(default_factory=list)
