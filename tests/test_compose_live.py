"""Live quote-pick checks. Calls the real Claude API and costs money.

Run on demand:  pytest -m slow tests/test_compose_live.py
"""

from __future__ import annotations

from datetime import date

import pytest

from pulse.analysis.cluster import ThemeDef
from pulse.analysis.compose import ThemeContext, build_quotes, pick_quotes
from pulse.analysis.llm import build_llm
from pulse.analysis.rank import ThemeStats
from pulse.models import CleanReview

pytestmark = pytest.mark.slow

NAMED = ("Rahul from the support team was very rude on the call. He closed my ticket "
         "without solving anything and told me to stop calling.")
SUPPORT = [
    NAMED,
    "Support closed my ticket without reading it. The chat bot keeps sending the same copy "
    "paste reply to every question I ask.",
    "I have emailed customer care four times about a failed order and nobody has replied "
    "in two weeks.",
]
OTHERS = {
    "withdrawals": ["My withdrawal has been pending for five days now. Nobody tells me when "
                    "the money will reach my bank account."],
    "app_performance": ["The app freezes at market open every single day. Orders go through "
                        "late and I lose money on the price."],
}


def ctx(tid: str, texts: list[str], start: int) -> ThemeContext:
    reviews = [CleanReview(id=f"r{start + i:04d}", key=f"k{start + i}", source="play_store",
                           rating=1, review_date=date(2026, 11, 14 - i), canonical_text=t)
               for i, t in enumerate(texts)]
    definition = ThemeDef(id=tid, label=tid.replace("_", " ").title(),
                          description=f"Reviews about {tid}.", include=tid, exclude="other",
                          example_ids=[r.id for r in reviews][:3])
    stats = ThemeStats(theme_id=tid, count=len(reviews), negative_count=len(reviews),
                       avg_rating=1.0, volume_share=0.2, negative_share=1.0, recent_count=0,
                       recent_share=0.0, recency_weight=1.0, score=0.4)
    return ThemeContext(definition, stats, reviews, reviews)


def test_quote_pick_skips_review_that_names_a_person(cfg):
    contexts = [ctx("support", SUPPORT, 1), ctx("withdrawals", OTHERS["withdrawals"], 10),
                ctx("app_performance", OTHERS["app_performance"], 20)]
    picks = pick_quotes(build_llm(cfg.llm), contexts, cfg)
    quotes, errors = build_quotes(picks, contexts)
    assert errors == []
    support = next(q for q in quotes if q.theme_id == "support")
    assert support.review_id != "r0001", "picked the review that names a support agent"
    assert "Rahul" not in support.text
