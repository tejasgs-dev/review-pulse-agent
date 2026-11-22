from datetime import date, timedelta

import pytest

from pulse.analysis.rank import (
    GENERAL_PRAISE,
    OTHER,
    RankError,
    rank_themes,
    recency_weight,
    theme_stats,
    top_themes,
)
from pulse.models import CleanReview

END = date(2026, 11, 15)
OLD = END - timedelta(days=40)   # outside the 14-day recent window
NEW = END - timedelta(days=3)    # inside it


def rv(i: int, rating: int, day: date) -> CleanReview:
    return CleanReview(id=f"r{i:04d}", key=f"k{i}", source="play_store", rating=rating,
                       review_date=day, canonical_text="text")


def build(spec):
    """spec: list of (theme, rating, day) -> reviews, primary map."""
    reviews, primary = [], {}
    for i, (tid, rating, day) in enumerate(spec, start=1):
        r = rv(i, rating, day)
        reviews.append(r)
        primary[r.id] = tid
    return reviews, primary


def stats_for(spec, ids, **kw):
    reviews, primary = build(spec)
    return {s.theme_id: s for s in theme_stats(ids, reviews, primary, window_end=END,
                                                recency_days=14, **kw)}


def test_formula_matches_definition():
    # 10 reviews: theme a has 4 (2 negative), all old -> few recent -> weight 1.0
    spec = [("a", 1, OLD), ("a", 2, OLD), ("a", 5, OLD), ("a", 4, OLD)] + \
           [("b", 5, OLD)] * 6
    s = stats_for(spec, ["a", "b"])["a"]
    assert s.count == 4 and s.negative_count == 2
    assert s.volume_share == pytest.approx(0.4)
    assert s.negative_share == pytest.approx(0.5)
    assert s.recency_weight == 1.0
    assert s.score == pytest.approx(0.4 * 1.5 * 1.0)


def test_recency_weight_rules():
    assert recency_weight(4, 0.9, 0.1) == 1.0          # under 5 recent reviews
    assert recency_weight(10, 0.9, 0.1) == 2.0         # clamped high
    assert recency_weight(10, 0.01, 0.5) == 0.5        # clamped low
    assert recency_weight(10, 0.3, 0.2) == pytest.approx(1.5)
    assert recency_weight(10, 0.3, 0.0) == 1.0         # no volume


def test_recent_theme_gets_boost():
    # "hot" is 10 of 30 overall but all 10 of the 10 recent reviews -> clamp at 2.0
    spec = [("hot", 1, NEW)] * 10 + [("cold", 1, OLD)] * 20
    s = stats_for(spec, ["hot", "cold"])
    assert s["hot"].recency_weight == 2.0
    assert s["cold"].recency_weight == 1.0  # 0 recent reviews -> under the minimum


def test_ties_break_by_negative_count_then_id():
    # Identical shares and scores are impossible to separate except by tie-breaks.
    spec = [("b", 1, OLD), ("b", 5, OLD), ("a", 1, OLD), ("a", 5, OLD),
            ("c", 1, OLD), ("c", 5, OLD)]
    ranked = rank_themes(stats_for(spec, ["c", "b", "a"]).values())
    assert [s.theme_id for s in ranked] == ["a", "b", "c"]
    assert [s.rank for s in ranked] == [1, 2, 3]

    # Same score, different negative counts: equal score needs equal shares and negative
    # shares, so compare directly with a crafted tie.
    from pulse.analysis.rank import ThemeStats
    base = dict(count=2, avg_rating=3.0, volume_share=0.2, negative_share=0.5,
                recent_count=0, recent_share=0.0, recency_weight=1.0, score=0.3)
    x = ThemeStats(theme_id="x", negative_count=1, **base)
    y = ThemeStats(theme_id="y", negative_count=2, **base)
    z = ThemeStats(theme_id="z", negative_count=2, **base)
    assert [s.theme_id for s in rank_themes([x, z, y])] == ["y", "z", "x"]


def test_theme_with_no_reviews_scores_zero_and_is_never_top():
    spec = [("a", 1, OLD)] * 3 + [("b", 2, OLD)] * 2 + [("c", 5, OLD)]
    s = stats_for(spec, ["a", "b", "c", "empty"])
    assert s["empty"].count == 0 and s["empty"].score == 0.0
    ranked = rank_themes(s.values())
    assert ranked[-1].theme_id == "empty"
    assert "empty" not in [t.theme_id for t in top_themes(ranked)]


def test_reserved_buckets_are_never_ranked():
    spec = [(GENERAL_PRAISE, 5, OLD)] * 50 + [(OTHER, 1, OLD)] * 40 + \
           [("a", 1, OLD), ("b", 1, OLD), ("c", 1, OLD)]
    s = stats_for(spec, ["a", "b", "c", GENERAL_PRAISE, OTHER])
    assert s[GENERAL_PRAISE].score == 0.0
    ranked = rank_themes(s.values())
    assert {x.theme_id for x in ranked} == {"a", "b", "c"}
    # shares still use every review as the denominator
    assert s["a"].volume_share == pytest.approx(1 / 93)


def test_fewer_than_three_rankable_themes_fails():
    spec = [("a", 1, OLD)] * 5 + [("b", 1, OLD)] * 5
    with pytest.raises(RankError, match="only 2 themes"):
        rank_themes(stats_for(spec, ["a", "b", "c"]).values())


def test_secondary_counts():
    reviews, primary = build([("a", 1, OLD), ("b", 1, OLD), ("c", 1, OLD)])
    sec = {"r0001": "c", "r0002": "c", "r0003": None}
    s = {x.theme_id: x for x in theme_stats(["a", "b", "c"], reviews, primary,
                                             window_end=END, recency_days=14, secondary=sec)}
    assert s["c"].secondary_count == 2 and s["a"].secondary_count == 0


def test_deterministic():
    spec = [("a", 1, NEW)] * 6 + [("b", 3, OLD)] * 7 + [("c", 2, NEW)] * 5
    first = rank_themes(stats_for(spec, ["a", "b", "c"]).values())
    second = rank_themes(stats_for(spec, ["c", "a", "b"]).values())
    assert first == second
