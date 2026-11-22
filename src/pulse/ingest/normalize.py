"""Normalize, dedupe, filter, window (architecture §4.2).

Order for every raw review:
1. Validity: drop an invalid rating, a missing date, or a date after today.
2. Dedupe by `key`; the newest version of an edited review wins. Rows with no
   source ID fall back to a content hash. Two users posting the same text stay
   two reviews (edge case NRM-05).
3. Cleaning rules, applied to that newest version, in this order:
   a. strip emojis from the text;
   b. drop it if no text is left (rating-only or emoji-only);
   c. drop it if it has fewer than `min_words` words (default 9);
   d. drop it unless it is English.
Every drop is counted per source and per rule.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime

from pulse.ingest.base import RawReview
from pulse.ingest.language import detect_language
from pulse.models import Review, review_key
from pulse.textutil import has_emoji, normalize_for_hash, strip_emojis, word_count
from pulse.weeks import IST

KEEP_LANGUAGE = "en"

# Counter keys, in report order.
DROPPED_BAD_RATING = "dropped_bad_rating"
DROPPED_BAD_DATE = "dropped_bad_date"
DROPPED_FUTURE_DATE = "dropped_future_date"
DUPLICATE = "duplicate"
DROPPED_NO_TEXT = "dropped_no_text"
DROPPED_TOO_SHORT = "dropped_too_short"
DROPPED_NON_ENGLISH = "dropped_non_english"
EMOJIS_REMOVED = "emojis_removed"  # informational: reviews that had emojis stripped
DROP_RULES = (DROPPED_BAD_RATING, DROPPED_BAD_DATE, DROPPED_FUTURE_DATE,
              DROPPED_NO_TEXT, DROPPED_TOO_SHORT, DROPPED_NON_ENGLISH)


@dataclass
class SourceStats:
    input: int = 0
    kept: int = 0
    counts: Counter[str] = field(default_factory=Counter)
    dropped_languages: Counter[str] = field(default_factory=Counter)  # detected language

    def to_json(self) -> dict:
        return {
            "input": self.input,
            "kept": self.kept,
            "dropped": {rule: self.counts.get(rule, 0) for rule in DROP_RULES},
            "duplicates": self.counts.get(DUPLICATE, 0),
            "emojis_removed": self.counts.get(EMOJIS_REMOVED, 0),
            "dropped_languages": dict(self.dropped_languages.most_common()),
        }


@dataclass
class NormalizeResult:
    reviews: list[Review]
    stats: dict[str, SourceStats]
    # Reviews whose newest version failed a cleaning rule, with that version's time.
    # The store removes an older stored copy of these, so an edit can drop a review.
    rejected: dict[str, datetime]


def ist_date(dt: datetime) -> date:
    return dt.astimezone(IST).date()


def _key(raw: RawReview, text: str, review_date: date) -> str:
    if raw.source_review_id:
        return review_key(raw.source, raw.source_review_id)
    digest = hashlib.sha256(
        f"{raw.rating}|{review_date.isoformat()}|{normalize_for_hash(text)}".encode()
    ).hexdigest()
    return review_key(raw.source, f"content:{digest}")


def normalize(
    raws: Iterable[RawReview], *, today: date, min_words: int = 9
) -> NormalizeResult:
    """Map raw reviews to `Review`, keeping only valid, English reviews of `min_words`+ words.

    `today` is the current IST date; anything dated after it is dropped (NRM-03).
    """
    stats: dict[str, SourceStats] = {}

    # Steps 1–2: validity, then newest version per key.
    newest: dict[str, tuple[Review, bool]] = {}  # key -> (review, had_emoji)
    for raw in raws:
        st = stats.setdefault(raw.source, SourceStats())
        st.input += 1
        if raw.rating is None or not 1 <= raw.rating <= 5:  # ING-07
            st.counts[DROPPED_BAD_RATING] += 1
            continue
        version_time = raw.updated_at or raw.submitted_at
        if version_time is None:
            st.counts[DROPPED_BAD_DATE] += 1
            continue
        review_date = ist_date(version_time)  # ING-08: IST date, not UTC date
        if review_date > today:
            st.counts[DROPPED_FUTURE_DATE] += 1
            continue

        original = (raw.text or "").strip()
        text = strip_emojis(original)  # 3a: strip emojis first, then everything else
        review = Review(
            key=_key(raw, text, review_date),
            source=raw.source,
            rating=raw.rating,
            title=strip_emojis(raw.title or "") or None,
            text=text,
            review_date=review_date,
            language="",
            app_version=raw.app_version,
            updated_at=version_time.astimezone(UTC),
        )
        prev = newest.get(review.key)
        if prev is not None:
            st.counts[DUPLICATE] += 1
            if review.updated_at <= prev[0].updated_at:  # ING-10: newest version wins
                continue
        newest[review.key] = (review, has_emoji(original))

    # Step 3: cleaning rules on each newest version.
    kept: list[Review] = []
    rejected: dict[str, datetime] = {}
    for review, had_emoji in newest.values():
        st = stats[review.source]
        if had_emoji:
            st.counts[EMOJIS_REMOVED] += 1
        if not review.text:
            rule = DROPPED_NO_TEXT                     # 3b: rating-only or emoji-only
        elif word_count(review.text) < min_words:
            rule = DROPPED_TOO_SHORT                   # 3c
        else:
            language = detect_language(review.text)
            if language != KEEP_LANGUAGE:
                rule = DROPPED_NON_ENGLISH             # 3d
                st.dropped_languages[language] += 1
            else:
                kept.append(replace(review, language=language))
                st.kept += 1
                continue
        st.counts[rule] += 1
        rejected[review.key] = review.updated_at
    return NormalizeResult(reviews=kept, stats=stats, rejected=rejected)


def in_window(reviews: Iterable[Review], start: date, end: date) -> list[Review]:
    """Reviews whose date is within [start, end], both ends inclusive (NRM-02)."""
    return [r for r in reviews if start <= r.review_date <= end]
