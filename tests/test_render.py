from dataclasses import replace
from datetime import date
from pathlib import Path

from pulse.models import Quote
from pulse.render.markdown import (
    email_subject,
    filter_summary,
    format_share,
    format_window,
    header,
    render_doc,
    render_email,
    render_quote,
    source_line,
    week_of,
)
from tests.pulse_fixture import valid_pulse


def test_source_line_states_filters():
    line = source_line(review_count=1348, sources={"play_store": 1201, "app_store": 147},
                       window=(date(2026, 9, 7), date(2026, 11, 15)), min_words=9)
    assert line == ("Source: 1,348 reviews (Play Store 1,201 · App Store 147), "
                    "9+ words, English only, 7 Sep – 15 Nov 2026")


def test_filter_summary_follows_config():
    assert filter_summary(min_words=12) == "12+ words, English only"


def test_missing_source_is_shown_as_zero():  # RND-02
    line = source_line(review_count=80, sources={"app_store": 80},
                       window=(date(2026, 9, 7), date(2026, 11, 15)), min_words=9)
    assert "(Play Store 0 · App Store 80)" in line


def test_window_crossing_a_year_shows_both_years():  # RND-03
    assert format_window(date(2026, 12, 28), date(2027, 3, 7)) == "28 Dec 2026 – 7 Mar 2027"
    assert format_window(date(2026, 9, 7), date(2026, 11, 15)) == "7 Sep – 15 Nov 2026"


def test_singular_review():
    line = source_line(review_count=1, sources={"play_store": 1},
                       window=(date(2026, 9, 7), date(2026, 11, 15)), min_words=9)
    assert line.startswith("Source: 1 review (")


# ---------------------------------------------------------------- Phase 4

GOLDEN = Path(__file__).parent / "fixtures" / "golden_pulse.md"


def test_golden_pulse():
    assert render_doc(valid_pulse(), product="Groww", min_words=9) == GOLDEN.read_text("utf-8")


def test_source_line_adds_general_praise_share():
    kw = dict(review_count=10, sources={"play_store": 10},
              window=(date(2026, 9, 7), date(2026, 11, 15)), min_words=9)
    assert source_line(**kw, general_praise_share=0.188).endswith(" · 19% general praise")
    assert source_line(**kw, general_praise_share=0.002).endswith(" · <1% general praise")
    assert "general praise" not in source_line(**kw, general_praise_share=0.0)
    assert "general praise" not in source_line(**kw)


def test_format_share_never_shows_zero_for_nonzero():  # RND-01
    assert format_share(0.0) == "0%"
    assert format_share(0.004) == "<1%"
    assert format_share(0.005) == "0%" or format_share(0.005) == "1%"
    assert format_share(0.314) == "31%"


def test_week_of_uses_iso_monday():  # RND-04
    assert week_of("2026-W46") == "Week of 9 Nov 2026"
    assert week_of("2026-W53") == "Week of 28 Dec 2026"
    assert week_of("2027-W01") == "Week of 4 Jan 2027"
    title = "ReviewPulse: Groww app reviews — Week of 9 Nov 2026"
    assert header("Groww", "2026-W46") == title
    assert email_subject("Groww", "2026-W46") == title


def test_quote_keeps_inner_text_and_collapses_whitespace():  # CMP-14, CMP-17
    q = Quote(review_id="r1", theme_id="t", span=(0, 10), rating=2, source="app_store",
              text='It said "pending"\n  for <3 days')
    assert render_quote(q) == '• “It said "pending" for <3 days” (★2, App Store)'


def test_email_appends_doc_link():
    p = valid_pulse()
    body = render_email(p, product="Groww", min_words=9, doc_url="https://docs.google.com/x")
    assert body.endswith("\nFull pulse: https://docs.google.com/x\n")
    assert body.startswith(render_doc(p, product="Groww", min_words=9))


def test_each_quote_is_labelled_with_its_own_theme():
    pulse = valid_pulse()
    labels = {t.id: t.label for t in pulse.top_themes}
    # Shuffle the quote order: the label must follow the quote's theme, not its position.
    pulse = replace(pulse, quotes=list(reversed(pulse.quotes)))
    lines = [line for line in render_doc(pulse, product="Groww", min_words=9).splitlines()
             if line.startswith("• ")]
    assert len(lines) == len(pulse.quotes) == 3
    for line, q in zip(lines, pulse.quotes, strict=True):
        assert line.startswith(f"• {labels[q.theme_id]}: “{q.text}”"), line
