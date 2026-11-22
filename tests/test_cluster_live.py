"""Live clustering on ~200 synthetic reviews. Calls the real Claude API and costs money.

Run on demand:  pytest -m slow tests/test_cluster_live.py
"""

from __future__ import annotations

import random
from collections import Counter
from datetime import date, timedelta

import pytest

from pulse.analysis.cluster import run_clustering
from pulse.analysis.llm import build_llm
from pulse.analysis.rank import GENERAL_PRAISE
from pulse.models import CleanReview

pytestmark = pytest.mark.slow

TOPICS = {
    "withdrawal": (1, [
        "I requested a withdrawal of my funds four days ago and the money has still not "
        "reached my bank account",
        "Withdrawal is stuck in processing for a week now, this is my own money and I need it",
        "Sold my mutual fund units but the redemption amount is not credited to my bank yet",
    ]),
    "performance": (1, [
        "The app freezes every morning right at market open and the order screen never loads",
        "Charts keep lagging and the app crashes whenever the market is volatile, fix it",
        "After the latest update the app is very slow and hangs when I open my positions",
    ]),
    "charges": (2, [
        "Brokerage and hidden charges are too high, I was charged more than the profit I made",
        "There are unexpected charges deducted from my account every month without any clear "
        "explanation",
        "Why do you charge so much for intraday trades now, the fee structure is not "
        "transparent at all",
    ]),
    "support": (1, [
        "Customer support never replies to my emails and the chat bot closes my ticket "
        "without solving anything",
        "Waited two hours on the support call and nobody answered, very poor customer service",
        "Support agents give copy paste answers and close the complaint without reading it",
    ]),
    "praise": (5, [
        "Very good app, simple and easy to use for investing in stocks and mutual funds",
        "Best investing app I have used so far, smooth interface and everything works well",
        "Nice and clean app, very easy for beginners to start investing with small amounts",
    ]),
}
# Multi-topic reviews whose primary topic follows the root-cause rule.
MULTI = [
    ("performance", "The app froze during market hours so my stop loss never executed, I lost "
                    "a lot of money and support did not even reply"),
    ("withdrawal", "My withdrawal has been pending for ten days and customer support keeps "
                   "telling me to wait, nobody is helping"),
    ("performance", "Order screen kept loading for five minutes at open, I missed my exit and "
                    "took a big loss because of this lag"),
]


def synthetic() -> tuple[list[CleanReview], dict[str, str]]:
    rng = random.Random(42)
    end = date(2026, 11, 15)
    rows: list[tuple[str, int, str]] = []
    for topic, (rating, texts) in TOPICS.items():
        for i in range(38):
            rows.append((topic, rating, f"{texts[i % len(texts)]} (case {i})"))
    for i in range(4):
        for topic, text in MULTI:
            rows.append((topic, 1, f"{text} (case {i})"))
    rng.shuffle(rows)
    reviews, truth = [], {}
    for n, (topic, rating, text) in enumerate(rows, start=1):
        rid = f"r{n:04d}"
        reviews.append(CleanReview(id=rid, key=f"k{n}", source="play_store", rating=rating,
                                   review_date=end - timedelta(days=rng.randrange(70)),
                                   canonical_text=text))
        truth[rid] = topic
    return reviews, truth


def test_live_clustering_on_synthetic_reviews(cfg):
    reviews, truth = synthetic()
    multi_ids = {r.id for r in reviews if "(case" in r.canonical_text
                 and any(m[1] in r.canonical_text for m in MULTI)}
    llm = build_llm(cfg.llm)
    result = run_clustering(llm, reviews, cfg, week="2026-W46",
                            window=(date(2026, 9, 7), date(2026, 11, 15)))

    assert len(result.themes) <= 5
    assert len(result.assignments) == len(reviews)
    labels = {rid: a.theme_id for rid, a in result.assignments.items()}

    # Each pure topic should map mostly to one label; praise to general_praise.
    majority = {}
    for topic in TOPICS:
        ids = [rid for rid, t in truth.items() if t == topic and rid not in multi_ids]
        label, n = Counter(labels[i] for i in ids).most_common(1)[0]
        majority[topic] = label
        assert n / len(ids) >= 0.85, f"{topic} split across labels"
    assert majority["praise"] == GENERAL_PRAISE

    # Multi-topic reviews follow the root-cause rule.
    correct = sum(labels[rid] == majority[truth[rid]] for rid in multi_ids)
    assert correct / len(multi_ids) >= 0.75
    print(f"\nusage: {result.usage.to_dict()}")
