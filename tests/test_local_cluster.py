import json
from collections import Counter
from dataclasses import replace

import numpy as np
import pytest

from pulse.analysis.catalogue import load_catalogue
from pulse.analysis.cluster import ClusterError, themes_payload
from pulse.analysis.engines.local_cluster import (
    LocalEngineError,
    choose_k,
    ctfidf_keywords,
    is_general_praise,
    run_local_clustering,
    text_keywords,
    word_pattern,
)
from pulse.analysis.rank import GENERAL_PRAISE, OTHER
from pulse.models import CleanReview
from tests.local_corpus import WINDOW, corpus, topic_of
from tests.stub_embedder import FixedEmbedder, HashingEmbedder


@pytest.fixture
def catalogue(cfg):
    return load_catalogue(cfg.analysis.catalogue_path)


def run(cfg, catalogue, reviews, embedder=None):
    return run_local_clustering(reviews, cfg, week="2026-W46", window=WINDOW,
                                embedder=embedder or HashingEmbedder(), catalogue=catalogue)


def rv(i, text, rating=1):
    return CleanReview(id=f"r{i:04d}", key=f"k{i}", source="play_store", rating=rating,
                       review_date=WINDOW[1], canonical_text=text)


# ------------------------------------------------------------------ L1 praise rule


@pytest.mark.parametrize(("text", "rating", "praise"), [
    ("Very good app and easy to use for investing", 5, True),
    ("Good app but please add a dark mode for charts", 5, False),     # specific word
    ("Great app, no issues so far with anything", 4, False),         # "no", "issue"
    ("Very good app and easy to use for investing", 3, False),        # rating too low
    ("The platform works for me every single day", 5, False),         # no praise word
    ("Super smooth and user friendly experience overall", 5, True),   # multi-word entry
])
def test_general_praise_rule(cfg, text, rating, praise):
    local = cfg.analysis.local
    assert is_general_praise(rv(1, text, rating), word_pattern(local.praise_words),
                             word_pattern(local.specific_words)) is praise


# ------------------------------------------------------------------ the whole engine


def test_topics_become_catalogue_themes(cfg, catalogue):
    reviews = corpus()
    result = run(cfg, catalogue, reviews)
    labels = {r.id: result.assignments[r.id].theme_id for r in reviews}
    pairs = Counter((topic_of(r), labels[r.id]) for r in reviews)
    assert pairs == {("support", "support"): 24, ("charges", "charges"): 24,
                     ("withdrawals", "withdrawals"): 24, ("bugs", "performance_bugs"): 24,
                     ("praise", GENERAL_PRAISE): 20, ("vague", OTHER): 6}
    assert {t.id for t in result.themes} == {"support", "charges", "withdrawals",
                                             "performance_bugs"}
    assert len(result.top) == 3 and len(result.assignments) == len(reviews)
    assert result.engine == "local" and result.model == "stub-hashing@test"
    assert result.quality["stability"]["median_ari"] >= cfg.analysis.local.min_stability
    assert all(len(t.label.split()) <= 4 for t in result.themes)
    assert all(t.example_ids for t in result.themes)


def test_split_topics_merge_into_one_catalogue_theme(cfg, catalogue):
    # 4 real topics but k=5 is accepted: two clusters map to the same entry and merge.
    result = run(cfg, catalogue, corpus())
    assert result.quality["k"] == 5 and len(result.themes) == 4


def test_vague_micro_cluster_goes_to_other(cfg, catalogue):
    result = run(cfg, catalogue, corpus(vague=12))
    assert result.buckets[OTHER].count >= 12
    assert result.quality["micro_cluster_routes"].get(OTHER, 0) >= 1


def test_text_keywords_skip_label_words_and_repeats():
    vague = word_pattern(["worst"])
    assert text_keywords(["customer", "support", "customer support", "chat", "worst",
                          "ticket"], "Customer support", vague) == ["chat", "ticket"]
    assert text_keywords(["stock", "stocks", "trading", "market"], "Stocks & portfolio",
                         vague) == ["trading", "market"]


def test_ctfidf_prefers_words_unique_to_a_class():
    texts = ["refund bank delay", "refund bank stuck", "chart lag crash", "chart crash slow"]
    kws = ctfidf_keywords(texts, ["a", "a", "b", "b"], n=3)
    assert "bank" in kws["a"] and "chart" in kws["b"]
    assert ctfidf_keywords(["a b"], ["x"]) == {"x": []}  # all stop words / too short


def test_deterministic(cfg, catalogue):
    first = themes_payload(run(cfg, catalogue, corpus()))
    second = themes_payload(run(cfg, catalogue, list(reversed(corpus()))))
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_unmatched_theme_gets_keyword_label(cfg, catalogue):
    reviews = corpus()
    # Replace the charges topic with vocabulary no catalogue entry knows.
    reviews = [replace(r, canonical_text=f"The zebra giraffe safari tour is lovely (#{i})")
               if topic_of(r) == "charges" else r for i, r in enumerate(reviews)]
    result = run(cfg, catalogue, reviews)
    unmatched = [t for t in result.themes if t.id.startswith("kw_")]
    assert len(unmatched) == 1
    assert {"Zebra", "Giraffe", "Safari", "Tour", "Lovely"} & set(
        unmatched[0].label.replace("&", " ").split())
    assert any(w["code"] == "unmatched_label" for w in result.warnings)


# ------------------------------------------------------------------ L5 choosing k


def blobs(sizes, dim=8, spread=0.05, seed=0):
    rng = np.random.default_rng(seed)
    pts = []
    for i, n in enumerate(sizes):
        centre = np.zeros(dim)
        centre[i % dim] = 1.0
        pts.append(centre + rng.normal(0, spread, size=(n, dim)))
    x = np.vstack(pts)
    return x / np.linalg.norm(x, axis=1, keepdims=True)


def test_choose_k_takes_the_largest_stable_k(cfg):
    choice = choose_k(blobs([40] * 5), total_reviews=200, local=cfg.analysis.local,
                      min_share=0.03, max_k=5, min_k=3)
    assert choice.k == 5 and min(choice.aris) == 1.0


def test_choose_k_steps_down_when_clusters_are_too_small(cfg):
    # The fifth blob holds 2 of 200 reviews (1%), under the 3% minimum.
    choice = choose_k(blobs([50, 50, 50, 48, 2]), total_reviews=200,
                      local=cfg.analysis.local, min_share=0.03, max_k=5, min_k=3)
    assert choice.k == 4
    assert [t["k"] for t in choice.tried] == [5, 4]


def test_choose_k_fails_below_three(cfg):
    with pytest.raises(LocalEngineError, match="no stable clustering with at least 3"):
        choose_k(blobs([60, 2, 2, 2]), total_reviews=66, local=cfg.analysis.local,
                 min_share=0.10, max_k=5, min_k=3)


# ------------------------------------------------------------------ L6 other / secondary


def test_far_review_goes_to_other_and_near_one_gets_a_secondary(cfg, catalogue):
    reviews = corpus()
    emb = HashingEmbedder()
    vectors = {r.canonical_text: v for r, v in zip(reviews, emb.encode(
        [r.canonical_text for r in reviews]), strict=True)}
    support = [r for r in reviews if topic_of(r) == "support"]
    charges = [r for r in reviews if topic_of(r) == "charges"]
    s_mean = np.mean([vectors[r.canonical_text] for r in support], axis=0)
    c_mean = np.mean([vectors[r.canonical_text] for r in charges], axis=0)
    far = rv(9001, "An odd one: nothing like the others at all, totally unrelated words")
    between = rv(9002, "Support and charges both, halfway between two topics here")
    vectors[far.canonical_text] = s_mean / np.linalg.norm(s_mean) * 0.2 + np.eye(
        len(s_mean))[7] * 2   # pointing away from every topic
    mix = s_mean / np.linalg.norm(s_mean) + c_mean / np.linalg.norm(c_mean)
    vectors[between.canonical_text] = mix
    result = run(cfg, catalogue, [*reviews, far, between], FixedEmbedder(vectors))
    assert result.assignments[far.id].theme_id == OTHER
    b = result.assignments[between.id]
    assert {b.theme_id, b.secondary_theme_id} == {"support", "charges"}


def test_too_few_reviews_after_praise(cfg, catalogue):
    with pytest.raises(LocalEngineError, match="only 2 reviews are left"):
        run(cfg, catalogue, [rv(1, "Very good app and easy to use", 5),
                             rv(2, "Broken"), rv(3, "Bad")])


def test_no_reviews(cfg, catalogue):
    with pytest.raises(ClusterError, match="no reviews"):
        run(cfg, catalogue, [])


def test_payload_records_engine_and_no_prompts(cfg, catalogue):
    payload = themes_payload(run(cfg, catalogue, corpus()))
    assert payload["engine"] == "local" and "prompt_versions" not in payload
    assert {a["sentiment"] for a in payload["assignments"]} == {None}
    assert payload["buckets"]["general_praise"]["count"] == 20
