"""Deterministic theme ranking (architecture §4.4, plan Phase 3 S6). Pure functions, no I/O.

    volume_share   = theme reviews / all reviews in the window
    negative_share = theme reviews rated 1–2★ / theme reviews      (rating, not LLM sentiment)
    recency_weight = clamp(share_last_N_days / volume_share, 0.5, 2.0)
                     (1.0 if the theme has fewer than 5 reviews in the last N days)
    score          = volume_share × (1 + negative_share) × recency_weight

Only clustered themes are ranked; `general_praise` and `other` never are. Ties are broken by
the count of negative reviews, then by theme ID. Themes with no reviews score 0 and are never
selected.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date, timedelta

from pulse.models import CleanReview

GENERAL_PRAISE = "general_praise"
OTHER = "other"
RESERVED = (GENERAL_PRAISE, OTHER)

RECENCY_MIN, RECENCY_MAX = 0.5, 2.0
MIN_RECENT_REVIEWS = 5


class RankError(ValueError):
    """Too few themes with reviews to fill the pulse."""


@dataclass(frozen=True)
class ThemeStats:
    theme_id: str
    count: int
    negative_count: int        # rated 1–2★
    avg_rating: float
    volume_share: float
    negative_share: float
    recent_count: int
    recent_share: float        # share of the recent window's reviews
    recency_weight: float
    score: float
    secondary_count: int = 0   # reviews that name this theme as their secondary issue
    rank: int | None = None    # 1-based among ranked themes; None for unranked rows

    def to_dict(self) -> dict:
        d = asdict(self)
        for k in ("avg_rating", "volume_share", "negative_share", "recent_share",
                  "recency_weight", "score"):
            d[k] = round(d[k], 4)
        return d


def recent_start(window_end: date, recency_days: int) -> date:
    """First day of the recent window: the last `recency_days` days, inclusive of window_end."""
    return window_end - timedelta(days=recency_days - 1)


def recency_weight(recent_count: int, recent_share: float, volume_share: float) -> float:
    if volume_share <= 0 or recent_count < MIN_RECENT_REVIEWS:
        return 1.0
    return min(RECENCY_MAX, max(RECENCY_MIN, recent_share / volume_share))


def theme_stats(
    theme_ids: Iterable[str],
    reviews: Sequence[CleanReview],
    primary: Mapping[str, str],
    *,
    window_end: date,
    recency_days: int,
    secondary: Mapping[str, str | None] | None = None,
) -> list[ThemeStats]:
    """Stats for each theme ID (and for the reserved buckets present in `theme_ids`).

    `primary` maps review ID -> theme ID (or a reserved label); `secondary` maps review ID ->
    secondary theme ID or None. Shares use every review in `reviews` as the denominator.
    """
    total = len(reviews)
    cutoff = recent_start(window_end, recency_days)
    recent_total = sum(1 for r in reviews if r.review_date >= cutoff)
    secondary = secondary or {}
    out = []
    for tid in theme_ids:
        members = [r for r in reviews if primary.get(r.id) == tid]
        count = len(members)
        negative = sum(1 for r in members if r.rating <= 2)
        recent = sum(1 for r in members if r.review_date >= cutoff)
        volume_share = count / total if total else 0.0
        negative_share = negative / count if count else 0.0
        recent_share = recent / recent_total if recent_total else 0.0
        weight = recency_weight(recent, recent_share, volume_share)
        score = volume_share * (1 + negative_share) * weight if tid not in RESERVED else 0.0
        out.append(ThemeStats(
            theme_id=tid, count=count, negative_count=negative,
            avg_rating=sum(r.rating for r in members) / count if count else 0.0,
            volume_share=volume_share, negative_share=negative_share,
            recent_count=recent, recent_share=recent_share, recency_weight=weight, score=score,
            secondary_count=sum(1 for v in secondary.values() if v == tid),
        ))
    return out


def rank_themes(stats: Iterable[ThemeStats], *, top_n: int = 3) -> list[ThemeStats]:
    """All rankable themes, best first, with `rank` set. Raises if fewer than `top_n` have
    reviews. Reserved buckets are left out."""
    rankable = [s for s in stats if s.theme_id not in RESERVED]
    ordered = sorted(rankable, key=lambda s: (-s.score, -s.negative_count, s.theme_id))
    ranked = [_with_rank(s, i) for i, s in enumerate(ordered, start=1)]
    with_reviews = [s for s in ranked if s.count > 0]
    if len(with_reviews) < top_n:
        raise RankError(f"only {len(with_reviews)} themes have reviews; the pulse needs {top_n}")
    return ranked


def top_themes(ranked: Sequence[ThemeStats], top_n: int = 3) -> list[ThemeStats]:
    return [s for s in ranked if s.count > 0][:top_n]


def _with_rank(s: ThemeStats, rank: int) -> ThemeStats:
    return ThemeStats(**{**asdict(s), "rank": rank})
