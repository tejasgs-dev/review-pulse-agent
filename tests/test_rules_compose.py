import re
from dataclasses import replace

import pytest

from pulse.analysis.catalogue import load_catalogue
from pulse.analysis.compose import ComposeError, build_contexts
from pulse.analysis.engines.local_cluster import run_local_clustering
from pulse.analysis.engines.rules_compose import (
    NameFilter,
    best_window,
    compose_pulse_rules,
    score_candidates,
    write_from_templates,
)
from pulse.models import CleanReview
from tests.local_corpus import WINDOW, corpus, topic_of
from tests.stub_embedder import HashingEmbedder

# Stands in for a week of reviews: words seen in lowercase at least twice count as common.
CORPUS_WORDS = ["the support team said my ticket was closed and the money is stuck",
                "worst app ever, the charts lag and the app is slow"] * 2
ALLOW = ["Groww", "UPI", "IPO", "Nifty", "iPad", "Monday", "November", "KYC"]


@pytest.fixture
def catalogue(cfg):
    return load_catalogue(cfg.analysis.catalogue_path)


def clustered(cfg, catalogue, reviews=None):
    reviews = reviews or corpus()
    return reviews, run_local_clustering(reviews, cfg, week="2026-W46", window=WINDOW,
                                         embedder=HashingEmbedder(), catalogue=catalogue)


def rv(text, rating=1, i=1):
    return CleanReview(id=f"r{i:04d}", key=f"k{i}", source="play_store", rating=rating,
                       review_date=WINDOW[1], canonical_text=text)


# ------------------------------------------------------------------ name filter (PII-11)


@pytest.mark.parametrize("text", [
    "Rahul from support was rude and closed my ticket",
    "I am Priya Sharma and my account is blocked",
    "Called Mr Verma twice and nothing happened",
    "The agent Neha said wait for two more days",
    "My name is something and my money is stuck",
    "the executive named Arjun did not help at all",
    "I spoke to Deepak yesterday about the ticket",
])
def test_names_are_flagged(text):
    assert NameFilter(CORPUS_WORDS, ALLOW).names_person(text)


@pytest.mark.parametrize("text", [
    "UPI payment failed and the IPO money is stuck",
    "Charts lag on my iPad every Monday at open",
    "Groww support never replied about my KYC in November",
    "The app is slow. The support team closed my ticket.",   # sentence starts are fine
    "Worst app ever, the Support Team closed my Ticket",      # common words, capitalized
])
def test_non_names_pass(text):
    assert not NameFilter(CORPUS_WORDS, ALLOW).names_person(text)


# ------------------------------------------------------------------ quote windows


def test_best_window_prefers_keyword_sentences_within_length():
    r = rv("Hello there. My withdrawal money is stuck and not credited to my bank account yet. "
           "Thanks a lot for reading this message today.")
    first, last, hits = best_window(r, {"withdrawal", "bank", "credited"}, min_words=8,
                                    max_words=35)
    assert (first, last) == (2, 2) and hits == 3


def test_best_window_joins_two_sentences_when_one_is_too_short():
    r = rv("Withdrawal stuck. The money never reached my bank account after a week.")
    first, last, _ = best_window(r, {"withdrawal", "bank"}, min_words=8, max_words=35)
    assert (first, last) == (1, 2)


def test_best_window_none_when_nothing_fits():
    assert best_window(rv("Too short. Also short."), {"x"}, min_words=8, max_words=35) is None


def test_scoring_skips_named_people_and_is_deterministic(cfg, catalogue):
    reviews, cluster = clustered(cfg, catalogue)
    support_ids = [r.id for r in reviews if topic_of(r) == "support"]
    named = next(i for i, r in enumerate(reviews) if r.id == support_ids[-1])
    reviews[named] = replace(reviews[named], canonical_text=(
        "Rahul from support closed my ticket and the chat agent never replied to my complaint."))
    reviews, cluster = clustered(cfg, catalogue, reviews)
    contexts = build_contexts(cluster, reviews, cfg)
    names = NameFilter([r.canonical_text for r in reviews], cfg.analysis.local.name_allowlist)
    for c in contexts:
        if c.id != "support":
            continue
        tokens = {t for k in cluster.quality["keywords"]["support"] for t in k.split()}
        scored = score_candidates(c, cluster, tokens, names, min_words=8, max_words=35)
        assert all("Rahul" not in s.review.canonical_text for s in scored)
        again = score_candidates(c, cluster, tokens, names, min_words=8, max_words=35)
        assert [s.review.id for s in scored] == [s.review.id for s in again]


# ------------------------------------------------------------------ templates


def test_templates_use_this_weeks_figures_and_keywords(cfg, catalogue):
    reviews, cluster = clustered(cfg, catalogue)
    contexts = build_contexts(cluster, reviews, cfg)
    writing = write_from_templates(contexts, cluster, catalogue, short=False)
    stats = {c.id: c.stats for c in contexts}
    for a in writing.actions:
        s = stats[a.theme_id]
        assert str(s.count) in a.action or str(s.recent_count) in a.action
        assert str(s.recent_count) in a.signal
    for summary in writing.summaries:
        assert not re.search(r"\d", summary.summary)          # CMP-22: no numbers
        assert "{" not in summary.summary
    short = write_from_templates(contexts, cluster, catalogue, short=True)
    assert sum(len(x.action.split()) for x in short.actions) < sum(
        len(x.action.split()) for x in writing.actions)


# ------------------------------------------------------------------ the composer


def test_compose_passes_validation_first_time(cfg, catalogue):
    reviews, cluster = clustered(cfg, catalogue)
    result = compose_pulse_rules(cluster, reviews, cfg, catalogue)
    assert result.report.ok and len(result.attempts) == 1 and result.engine == "local"
    p = result.pulse
    assert [t.id for t in p.top_themes] == cluster.top
    assert len({q.review_id for q in p.quotes}) == 3
    by_id = {r.id: r for r in reviews}
    for q in p.quotes:   # verbatim and on the right theme
        assert q.text == by_id[q.review_id].canonical_text[q.span[0]:q.span[1]]
        assert cluster.assignments[q.review_id].theme_id == q.theme_id


def _word_counts(cfg, catalogue):
    """Body word counts of the full pulse and of the shortest fallback."""
    from pulse.analysis.compose import assemble
    from pulse.render.markdown import render_body
    from pulse.textutil import word_count

    reviews, cluster = clustered(cfg, catalogue)
    full = compose_pulse_rules(cluster, reviews, cfg, catalogue)
    contexts = build_contexts(cluster, reviews, cfg)
    short = write_from_templates(contexts, cluster, catalogue, short=True)
    pulse = assemble(cluster, contexts, reviews, full.pulse.quotes, short)
    return full.report.word_count, word_count(render_body(pulse))


def test_word_overflow_falls_back_to_short_templates(cfg, catalogue, tmp_config):
    from pulse.config import load_config
    full, short = _word_counts(cfg, catalogue)
    assert short < full
    limit = (full + short) // 2
    tight = load_config(tmp_config(lambda d: d["analysis"].__setitem__("max_words", limit)),
                        load_env_file=False)
    reviews, cluster = clustered(tight, catalogue)
    result = compose_pulse_rules(cluster, reviews, tight, catalogue)
    assert result.report.ok
    assert len(result.attempts) >= 2
    assert result.attempts[0]["checks"]["word_count"] == "fail"
    assert result.attempts[-1]["short_templates"] is True


def test_gives_up_with_every_attempt_recorded(cfg, catalogue, tmp_config):
    from pulse.config import load_config
    impossible = load_config(tmp_config(lambda d: d["analysis"].__setitem__("max_words", 40)),
                             load_env_file=False)
    reviews, cluster = clustered(impossible, catalogue)
    with pytest.raises(ComposeError) as e:
        compose_pulse_rules(cluster, reviews, impossible, catalogue)
    assert len(e.value.attempts) == 3
    assert all(a["checks"]["word_count"] == "fail" for a in e.value.attempts)


def test_theme_whose_candidates_all_name_people_fails_clearly(cfg, catalogue):
    reviews, cluster = clustered(cfg, catalogue)
    top = cluster.top[0]
    topic = {"performance_bugs": "bugs"}.get(top, top)
    # Every review of the top theme now names someone (same topic words, plus a name).
    named = [replace(r, canonical_text=f"Ravindra said: {r.canonical_text}")
             if topic_of(r) == topic else r for r in reviews]
    with pytest.raises(ComposeError, match=f"theme {top} has no quotable review"):
        compose_pulse_rules(cluster, named, cfg, catalogue)
