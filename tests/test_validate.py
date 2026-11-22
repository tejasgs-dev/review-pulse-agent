"""Contract tests: hand-built bad pulses must be rejected (§4.6, plan Phase 4)."""

import pytest

from pulse.analysis.validate import CHECKS, validate_pulse
from pulse.models import ActionIdea, Quote, Theme
from pulse.render.markdown import render_body
from pulse.textutil import word_count
from tests.pulse_fixture import ASSIGNMENTS, REVIEWS, mutate, valid_pulse


def check(pulse, cfg, **kw):
    return validate_pulse(pulse, reviews=REVIEWS, assignments=ASSIGNMENTS, cfg=cfg, **kw)


def failed(report):
    return {i.check for i in report.issues}


def test_valid_pulse_passes(cfg):
    report = check(valid_pulse(), cfg)
    assert report.ok, report.issues
    assert report.to_dict()["checks"] == dict.fromkeys(CHECKS, "pass")
    assert report.word_count == 186   # 180 + the theme label before each quote


# ------------------------------------------------------------- the plan's five bad pulses


def test_invented_quote_is_rejected(cfg):
    def invent(p):
        q = p.quotes[0]
        p.quotes[0] = Quote(review_id=q.review_id, theme_id=q.theme_id, span=q.span,
                            rating=q.rating, source=q.source,
                            text="Withdrawals are instant and the app is perfect.")
    report = check(mutate(invent), cfg)
    assert "quote_fidelity" in failed(report)
    assert report.issues[0].area == "quotes"


def test_slightly_edited_quote_is_rejected(cfg):
    def edit(p):
        p.quotes[1].text = p.quotes[1].text.replace("freezes", "freeze")
    assert "quote_fidelity" in failed(check(mutate(edit), cfg))


def _pad_to(pulse, words):
    """Lengthen the first summary until the body has exactly `words` words."""
    t = pulse.top_themes[0]
    extra = words - word_count(render_body(pulse))
    t.summary = t.summary + " and" * extra
    assert word_count(render_body(pulse)) == words


def test_251_words_is_rejected_and_250_passes(cfg):  # VAL-01
    at_limit = mutate(lambda p: _pad_to(p, 250))
    assert check(at_limit, cfg).ok
    over = mutate(lambda p: _pad_to(p, 251))
    report = check(over, cfg)
    assert failed(report) == {"word_count"}
    assert report.word_count == 251
    assert "Shorten it by at least 1 words" in report.issues[0].message
    assert report.issues[0].area == "writing"  # quotes use well under half the budget


def test_four_themes_is_rejected(cfg):
    def four(p):
        p.top_themes.append(Theme(id="charges", label="Hidden charges", description="d",
                                  volume_share=0.1, negative_share=0.8, summary="Fees surprise"))
    report = check(mutate(four), cfg)
    assert "theme_count" in failed(report)


def test_too_many_clustered_themes_is_rejected(cfg):
    report = check(mutate(lambda p: setattr(p, "clustered_theme_count", 6)), cfg)
    assert "theme_count" in failed(report)
    assert report.issues[0].area == "structure"


def test_email_address_in_body_is_rejected(cfg):
    def leak(p):
        p.actions[2].action = "Ask users to write to help.desk@example.com with screenshots"
    report = check(mutate(leak), cfg)
    assert "pii_scan" in failed(report)
    assert "email" in report.issues[0].message


def test_missing_action_is_rejected(cfg):
    report = check(mutate(lambda p: p.actions.pop()), cfg)
    assert {"action_count", "action_theme_link"} <= failed(report)


# ------------------------------------------------------------- other checks


def test_quote_from_another_theme_is_rejected(cfg):  # CMP-18
    def swap(p):
        p.quotes[0].theme_id, p.quotes[1].theme_id = "app_performance", "withdrawals"
    assert "quote_theme_link" in failed(check(mutate(swap), cfg))


def test_two_quotes_from_one_review_are_rejected(cfg):  # CMP-19
    def dup(p):
        r = REVIEWS["r0101"]
        p.quotes[1] = Quote.from_span(r, "app_performance", 0, len(r.canonical_text))
    report = check(mutate(dup), cfg)
    assert "quote_count" in failed(report)


def test_quote_too_short_is_rejected(cfg):
    def short(p):
        r = REVIEWS["r0303"]
        p.quotes[2] = Quote.from_span(r, "support", 0, len("Support closed my ticket"))
    assert "quote_length" in failed(check(mutate(short), cfg))


def test_one_action_per_theme(cfg):  # CMP-20
    def double(p):
        p.actions[2] = ActionIdea(theme_id="withdrawals", action="Add an SMS on credit",
                                  signal="fewer reviews")
    report = check(mutate(double), cfg)
    assert "action_theme_link" in failed(report)
    assert any("support needs exactly one action" in i.message for i in report.issues)


def test_action_needs_a_signal(cfg):
    report = check(mutate(lambda p: setattr(p.actions[0], "signal", " ")), cfg)
    assert "action_theme_link" in failed(report)


def test_action_for_unknown_theme(cfg):
    report = check(mutate(lambda p: setattr(p.actions[0], "theme_id", "kyc")), cfg)
    assert "action_theme_link" in failed(report)


def test_summary_with_a_statistic_is_rejected(cfg):  # CMP-22
    def stat(p):
        p.top_themes[0].summary = "About 40% of reviewers wait days for withdrawals"
    report = check(mutate(stat), cfg)
    assert failed(report) == {"no_stats_in_text"}


def test_empty_summary_is_rejected(cfg):  # VAL-10
    assert "summaries" in failed(check(mutate(
        lambda p: setattr(p.top_themes[1], "summary", "")), cfg))


@pytest.mark.parametrize("text", ["Shown as <verbatim quote 2> here", "See {doc_url} for more"])
def test_leftover_template_tokens_are_rejected(cfg, text):
    assert "placeholders" in failed(check(mutate(
        lambda p: setattr(p.actions[0], "action", text)), cfg))


def test_angle_brackets_in_text_are_not_placeholders(cfg):  # VAL-05
    def heart(p):
        p.top_themes[0].summary = "Users love the SIP flow <3 but wait on payouts"
    assert check(mutate(heart), cfg).ok


def test_doc_url_is_allowed_in_email_but_other_links_are_not(cfg):  # VAL-04
    url = "https://docs.google.com/document/d/abc123/edit"
    assert check(valid_pulse(), cfg, doc_url=url).ok
    leak = mutate(lambda p: setattr(p.actions[0], "action", "See https://example.com/help"))
    assert "pii_scan" in failed(check(leak, cfg, doc_url=url))


def test_empty_sections_are_rejected(cfg):
    report = check(mutate(lambda p: p.quotes.clear()), cfg)
    assert {"quote_count", "placeholders"} <= failed(report)


def test_every_issue_is_reported_at_once(cfg):  # VAL-08: full validation each time
    def many(p):
        p.actions.pop()
        p.top_themes[0].summary = "Roughly 50% wait"
        p.quotes[0].text = "invented"
    assert {"action_count", "no_stats_in_text", "quote_fidelity"} <= failed(
        check(mutate(many), cfg))
