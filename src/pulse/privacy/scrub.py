"""PII scrubber: Review → CleanReview (architecture §4.3 layer 2, plan Phase 2).

This runs before any review text reaches the LLM or any artifact. `canonical_text`
is the scrubbed text, and it is the only text used downstream: quotes are checked
against it, so a quote with a redaction token in it still counts as verbatim.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from pulse.fileio import write_jsonl
from pulse.models import CleanReview, Review
from pulse.privacy.scan import find_pii

_MAX_PASSES = 5


class ScrubError(RuntimeError):
    """Redaction did not converge. Should never happen; it means a detector bug."""


def redact(
    text: str, *, number_min_digits: int = 8, reference_min_digits: int = 5
) -> tuple[str, Counter[str]]:
    """Replace every PII span with its placeholder. Returns the text and hits per kind.

    Redaction repeats until the scanner finds nothing, so a replacement can never leave
    (or create) a detectable identifier. This is what makes
    `find_pii(redact(x)[0]) == []` hold for every input.
    """
    counts: Counter[str] = Counter()
    for _ in range(_MAX_PASSES):
        hits = find_pii(text, number_min_digits=number_min_digits,
                        reference_min_digits=reference_min_digits)
        if not hits:
            return text, counts
        parts, pos = [], 0
        for h in hits:
            parts.append(text[pos:h.start])
            parts.append(h.placeholder)
            pos = h.end
            counts[h.kind] += 1
        parts.append(text[pos:])
        text = "".join(parts)
    if find_pii(text, number_min_digits=number_min_digits,
                reference_min_digits=reference_min_digits):
        raise ScrubError("redaction did not converge")
    return text, counts


def scrub(
    review: Review, review_id: str, *, number_min_digits: int = 8, reference_min_digits: int = 5
) -> CleanReview:
    """Scrub one review. `review_id` is its short run-local ID, e.g. "r0042"."""
    text, _ = redact(review.text, number_min_digits=number_min_digits,
                     reference_min_digits=reference_min_digits)
    return CleanReview(id=review_id, key=review.key, source=review.source, rating=review.rating,
                       review_date=review.review_date, canonical_text=text)


@dataclass
class ScrubResult:
    reviews: list[CleanReview]
    hits: Counter[str] = field(default_factory=Counter)  # redactions per kind
    reviews_with_pii: int = 0


def scrub_all(
    reviews: Iterable[Review], *, number_min_digits: int = 8, reference_min_digits: int = 5
) -> ScrubResult:
    """Scrub reviews and assign run-local IDs r0001… in (date, key) order, so IDs are stable."""
    ordered = sorted(reviews, key=lambda r: (r.review_date, r.key))
    width = max(4, len(str(len(ordered))))
    result = ScrubResult(reviews=[])
    for i, review in enumerate(ordered, start=1):
        text, counts = redact(review.text, number_min_digits=number_min_digits,
                              reference_min_digits=reference_min_digits)
        result.reviews.append(CleanReview(
            id=f"r{i:0{width}d}", key=review.key, source=review.source, rating=review.rating,
            review_date=review.review_date, canonical_text=text,
        ))
        if counts:
            result.hits.update(counts)
            result.reviews_with_pii += 1
    return result


def clean_reviews_path(runs_dir: Path, week: str) -> Path:
    return runs_dir / week / "clean_reviews.jsonl"


def write_clean_reviews(reviews: list[CleanReview], runs_dir: Path, week: str) -> Path:
    """Write runs/{week}/clean_reviews.jsonl: the scrubbed input for this run."""
    path = clean_reviews_path(runs_dir, week)
    write_jsonl(path, [{
        "id": r.id, "key": r.key, "source": r.source, "rating": r.rating,
        "review_date": r.review_date.isoformat(), "canonical_text": r.canonical_text,
    } for r in reviews])
    return path
