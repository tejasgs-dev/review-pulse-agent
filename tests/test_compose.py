from dataclasses import replace
from datetime import date, timedelta

import pytest

from pulse.analysis.cluster import Discovery, run_clustering
from pulse.analysis.compose import (
    ActionDraft,
    ComposeError,
    PulseWriting,
    QuotePick,
    QuotePicks,
    ThemeSummary,
    build_contexts,
    build_quotes,
    compose_pulse,
    load_prompt,
    select_candidates,
    sentence_spans,
    write_compose_artifacts,
)
from pulse.models import CleanReview
from tests.fake_llm import FakeLlm, default_write, theme

WINDOW = (date(2026, 9, 7), date(2026, 11, 15))
END = date(2026, 11, 15)
THEMES = ["withdrawals", "app_performance", "support"]
TEXTS = {
    "withdrawals": "My withdrawal has been pending for five days now. Nobody tells me when the "
                   "money will reach my bank account.",
    "app_performance": "The app freezes at market open every single day. Orders go through "
                       "late and I lose money on the price.",
    "support": "Support closed my ticket without reading it. The chat bot keeps sending the "
               "same copy paste reply to every question.",
}


def rv(i, text, rating=1, days_ago=1, source="play_store"):
    return CleanReview(id=f"r{i:04d}", key=f"k{i}", source=source, rating=rating,
                       review_date=END - timedelta(days=days_ago), canonical_text=text)


def corpus(n_per_theme=10):
    """Reviews r0001… cycling through THEMES; theme of rNNNN = THEMES[(N-1) % 3]."""
    out = []
    for i in range(1, 3 * n_per_theme + 1):
        tid = THEMES[(i - 1) % 3]
        out.append(rv(i, f"{TEXTS[tid]} Case {i}.", rating=1 + (i % 5), days_ago=i % 60))
    return out


def labeller(rid):
    return THEMES[(int(rid[1:]) - 1) % 3], None, "negative"


def clustered(cfg, reviews, **fake_kw):
    disc = Discovery(themes=[theme(t, ["r0001", "r0002", "r0003"], label=t.title())
                             for t in THEMES], taxonomy_changes=[])
    llm = FakeLlm([disc], labeller, **fake_kw)
    return llm, run_clustering(llm, reviews, cfg, week="2026-W46", window=WINDOW)


# ------------------------------------------------------------------ sentences


def test_sentence_spans_trim_and_split():
    text = "Withdrawal stuck for 4 days!! Support says wait.\nStill nothing. Rs 1.5 lakh pending"
    assert [text[s:e] for s, e in sentence_spans(text)] == [
        "Withdrawal stuck for 4 days!!", "Support says wait.", "Still nothing.",
        "Rs 1.5 lakh pending"]


def test_sentence_spans_without_punctuation_is_one_sentence():
    assert sentence_spans("no punctuation at all") == [(0, 21)]


def test_sentence_spans_count_code_points():  # CMP-13
    text = "🙏 बहुत अच्छा app. second part here"
    spans = sentence_spans(text)
    assert [text[s:e] for s, e in spans] == ["🙏 बहुत अच्छा app.", "second part here"]


# ------------------------------------------------------------------ candidates


def test_candidates_filter_length_profanity_and_redactions(cfg):
    ok = "The withdrawal screen shows pending for days with no date at all"
    members = [
        rv(1, ok),
        rv(2, "This is shit, the withdrawal never arrives and nobody answers me"),  # CMP-04
        rv(3, "Call me on [phone] about my withdrawal that never came through ok"),  # CMP-16
        rv(4, " ".join(["long"] * 61)),
    ]
    assert [r.id for r in select_candidates(members, cfg)] == ["r0001"]


def test_candidates_relax_when_nothing_is_eligible(cfg):  # CMP-01
    members = [rv(1, "Withdrawal is slow and late today"),   # 6 words: step 2 (5–80)
               rv(2, "Bad")]
    assert [r.id for r in select_candidates(members, cfg)] == ["r0001"]
    only_redacted = [rv(3, "My refund for order [number] never arrived in my bank account")]
    assert [r.id for r in select_candidates(only_redacted, cfg)] == ["r0003"]  # step 3
    mostly_pii = [rv(4, "[phone] [email] [phone] [email] call now")]
    assert select_candidates(mostly_pii, cfg) == []  # PII-22


def test_candidates_recent_first_mixed_ratings_and_capped(cfg):
    text = "The withdrawal screen shows pending for days with no date at all"
    members = [rv(i, text, rating=r, days_ago=d) for i, (r, d) in enumerate(
        [(1, 1), (1, 2), (1, 3), (5, 4), (3, 5), (1, 6), (5, 7), (2, 8), (1, 9), (1, 10)],
        start=1)]
    picked = select_candidates(members, cfg)
    assert len(picked) == 8
    assert [r.rating for r in picked[:3]] == [1, 3, 5]  # one per band first
    assert picked[0].id == "r0001"                      # most recent negative


def test_few_candidates_is_fine(cfg):  # CMP-02
    text = "The withdrawal screen shows pending for days with no date at all"
    assert len(select_candidates([rv(1, text), rv(2, text)], cfg)) == 2


# ------------------------------------------------------------------ quote picks


def contexts(cfg):
    _, cluster = clustered(cfg, corpus())
    return cluster, build_contexts(cluster, corpus(), cfg)


def test_build_quotes_slices_whole_sentences(cfg):
    cluster, ctx = contexts(cfg)
    c = ctx[0]
    r = c.candidates[0]
    quotes, errors = build_quotes(QuotePicks(picks=[
        QuotePick(theme_id=c.id, review_id=r.id, first_sentence=2, last_sentence=2)]), ctx[:1])
    assert errors == []
    q = quotes[0]
    assert q.text == r.canonical_text[q.span[0]:q.span[1]]
    assert q.text == sentence_spans_text(r.canonical_text)[1]
    assert q.source == r.source and q.rating == r.rating


def sentence_spans_text(text):
    return [text[s:e] for s, e in sentence_spans(text)]


def test_build_quotes_rejects_bad_picks(cfg):  # CMP-11, CMP-18
    cluster, ctx = contexts(cfg)
    a, b, c = ctx
    other_theme_review = b.candidates[0].id
    _, errors = build_quotes(QuotePicks(picks=[
        QuotePick(theme_id=a.id, review_id=other_theme_review, first_sentence=1,
                  last_sentence=1),
        QuotePick(theme_id=b.id, review_id=b.candidates[0].id, first_sentence=3,
                  last_sentence=1),
        QuotePick(theme_id="made_up", review_id="r0001", first_sentence=1, last_sentence=1),
        QuotePick(theme_id=c.id, review_id=c.candidates[0].id, first_sentence=1,
                  last_sentence=99),
    ]), ctx)
    text = " | ".join(errors)
    assert f"not a candidate for theme {a.id}" in text
    assert "sentences 3–1 do not exist" in text
    assert "made_up is not one of the top themes" in text
    assert "sentences 1–99 do not exist" in text


def test_quote_prompt_skips_named_people(cfg):  # PII-11, CMP-05
    prompt = load_prompt("quote_pick_v1", product="Groww", quote_min_words=8,
                         quote_max_words=60)
    assert "Skip any candidate review that names a person" in prompt
    assert "support agent" in prompt
    # The named candidate still reaches the model (code cannot detect names); the prompt
    # rule is what keeps it out. tests/test_compose_live.py checks the model's behaviour.
    reviews = corpus()
    reviews[0] = replace(reviews[0], canonical_text="Rahul from support was rude and closed "
                         "my withdrawal ticket without any help at all.")
    llm, cluster = clustered(cfg, reviews)
    compose_pulse(llm, cluster, reviews, cfg)
    pick_call = next(c for c in llm.calls if c["schema"] == "QuotePicks")
    assert "Rahul from support" in pick_call["prefix"]
    assert "names a person" in pick_call["system"]


# ------------------------------------------------------------------ the whole composer


def test_compose_first_attempt(cfg, tmp_path):
    reviews = corpus()
    llm, cluster = clustered(cfg, reviews)
    result = compose_pulse(llm, cluster, reviews, cfg)
    assert result.report.ok and len(result.attempts) == 1
    p = result.pulse
    assert [t.id for t in p.top_themes] == cluster.top
    assert len(p.quotes) == 3 and len(p.actions) == 3
    assert [a.theme_id for a in p.actions] == cluster.top
    assert p.review_count == 30 and p.sources == {"play_store": 30, "app_store": 0}
    write_call = next(c for c in llm.calls if c["schema"] == "PulseWriting")
    assert "words in total for the 3 summaries" in write_call["user"]
    assert "Quote shown: “" in write_call["user"]
    assert write_call["effort"] == "high"

    paths = write_compose_artifacts(result, cfg, tmp_path)
    assert paths["pulse_md"].read_text("utf-8").startswith("Groww Weekly Review Pulse")
    assert set(paths) == {"pulse_json", "pulse_md", "validation"}


def test_bad_quote_pick_retries_quotes_only(cfg):
    reviews = corpus()
    calls = {"n": 0}

    def pick(call):
        from tests.fake_llm import default_pick
        picks = default_pick(call)
        calls["n"] += 1
        if calls["n"] == 1:  # first answer: sentence range out of bounds
            picks.picks[0].last_sentence = 50
        return picks

    llm, cluster = clustered(cfg, reviews, pick=pick)
    result = compose_pulse(llm, cluster, reviews, cfg)
    assert len(result.attempts) == 2
    assert result.attempts[1]["redid"] == ["quotes"]
    retry = [c for c in llm.calls if c["name"] == "quote_pick_retry1"]
    assert "do not exist" in retry[0]["user"] and "Your previous picks were" in retry[0]["user"]
    assert sum(c["schema"] == "PulseWriting" for c in llm.calls) == 1


def test_bad_writing_retries_writing_only(cfg):
    reviews = corpus()
    calls = {"n": 0}

    def write(call):
        calls["n"] += 1
        w = default_write(call)
        if calls["n"] == 1:
            w.summaries[0].summary = "Nearly 45% of users complain"
        return w

    llm, cluster = clustered(cfg, reviews, write=write)
    result = compose_pulse(llm, cluster, reviews, cfg)
    assert len(result.attempts) == 2 and result.attempts[1]["redid"] == ["writing"]
    retry = next(c for c in llm.calls if c["name"] == "write_retry1")
    assert "states a statistic" in retry["user"]
    assert sum(c["schema"] == "QuotePicks" for c in llm.calls) == 1


def test_compose_gives_up_after_two_retries(cfg):  # VAL-07
    reviews = corpus()

    def write(call):
        return PulseWriting(summaries=[ThemeSummary(theme_id=t, summary="Things go wrong")
                                       for t in THEMES],
                            actions=[ActionDraft(theme_id=THEMES[0], action="Do x",
                                                 signal="y")])

    llm, cluster = clustered(cfg, reviews, write=write)
    with pytest.raises(ComposeError) as e:
        compose_pulse(llm, cluster, reviews, cfg)
    assert len(e.value.attempts) == 3
    assert all(not a["ok"] for a in e.value.attempts)
    assert "exactly 3 action ideas" in str(e.value)


def test_theme_without_quotable_reviews_fails_clearly(cfg):  # CMP-01, risk #3
    reviews = corpus()
    # Every support review becomes unquotable (profanity).
    reviews = [replace(r, canonical_text="shit " * 12) if labeller(r.id)[0] == "support"
               else r for r in reviews]
    llm, cluster = clustered(cfg, reviews)
    if "support" not in cluster.top:
        pytest.skip("support not in the top 3 for this fixture")
    with pytest.raises(ComposeError, match="theme support has no quotable review"):
        compose_pulse(llm, cluster, reviews, cfg)
