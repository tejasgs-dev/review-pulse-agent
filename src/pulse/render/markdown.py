"""Pulse → Doc text and email text (architecture §7, plan Phase 4).

Every number in the rendered pulse is inserted here, from the computed stats; the model
writes words only (edge case CMP-22). The source line always states the cleaning rules
from config (§4.2), so it cannot drift from what ingest actually did.

Layout (§7.1):

    {header}                                 ← not counted toward the word limit
    {source line}                            ← not counted
    (blank)
    Top Themes                               ← body: counted (headings included)
    1. {label} ({share} of reviews · {neg} 1–2★) — {summary}
    ...
    What Users Said
    • {label}: “{quote}” (★{rating}, {store})   ← the theme the quote illustrates
    ...
    Action Ideas
    1. {label}: {action} Watch for: {signal}
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date

from pulse.models import Pulse, Quote
from pulse.weeks import parse_iso_week

SOURCE_LABELS = {"play_store": "Play Store", "app_store": "App Store"}
LANGUAGE_LABELS = {"en": "English only"}

HEADINGS = ("Top Themes", "What Users Said", "Action Ideas")


def format_window(start: date, end: date) -> str:
    """'7 Sep – 15 Nov 2026'; both years when the window crosses a year (edge case RND-03)."""
    if start.year == end.year:
        return f"{start.day} {start:%b} – {end.day} {end:%b %Y}"
    return f"{start.day} {start:%b %Y} – {end.day} {end:%b %Y}"


def format_share(share: float) -> str:
    """'31%'; '<1%' for a small non-zero share instead of a misleading 0% (RND-01)."""
    if 0 < share < 0.005:
        return "<1%"
    return f"{round(share * 100)}%"


def filter_summary(*, min_words: int, language: str = "en") -> str:
    """'9+ words, English only': the cleaning rules every counted review passed."""
    return f"{min_words}+ words, {LANGUAGE_LABELS.get(language, f'{language} only')}"


def source_line(
    *,
    review_count: int,
    sources: Mapping[str, int],
    window: tuple[date, date],
    min_words: int,
    language: str = "en",
    general_praise_share: float | None = None,
) -> str:
    """e.g. 'Source: 1,348 reviews (Play Store 1,201 · App Store 147), 9+ words,
    English only, 7 Sep – 15 Nov 2026 · 19% general praise'.

    Every source is listed, including one that returned nothing, so a missing store
    is visible to readers (edge case RND-02). The general-praise share is added when it
    is above 0, so a mostly negative pulse still shows how many reviewers were happy.
    """
    per_source = " · ".join(
        f"{SOURCE_LABELS.get(name, name)} {sources.get(name, 0):,}"
        for name in (*SOURCE_LABELS, *(s for s in sources if s not in SOURCE_LABELS))
    )
    noun = "review" if review_count == 1 else "reviews"
    line = (
        f"Source: {review_count:,} {noun} ({per_source}), "
        f"{filter_summary(min_words=min_words, language=language)}, "
        f"{format_window(*window)}"
    )
    if general_praise_share:
        line += f" · {format_share(general_praise_share)} general praise"
    return line


def week_of(week: str) -> str:
    """'Week of 9 Nov 2026': the Monday of the ISO week (RND-04)."""
    monday, _ = parse_iso_week(week)
    return f"Week of {monday.day} {monday:%b %Y}"


def header(product: str, week: str) -> str:
    return f"{product} Weekly Review Pulse — {week_of(week)}"


def email_subject(product: str, week: str) -> str:
    """§7.2: 'Groww Review Pulse — Week of 9 Nov 2026'."""
    return f"{product} Review Pulse — {week_of(week)}"


def _one_line(text: str) -> str:
    """Collapse whitespace. Words are never changed (CMP-14)."""
    return " ".join(text.split())


def _sentence(text: str) -> str:
    text = _one_line(text)
    return text if not text or text[-1] in ".!?" else text + "."


def render_quote(q: Quote, label: str | None = None) -> str:
    """Typographic quote marks around the unchanged quote text (CMP-17), led by the label of
    the theme the quote illustrates, so readers can tell which theme each quote belongs to."""
    store = SOURCE_LABELS.get(q.source, q.source)
    meta = f"★{q.rating}, {store}" if store else f"★{q.rating}"
    lead = f"{label}: " if label else ""
    return f"• {lead}“{_one_line(q.text)}” ({meta})"


def render_body(pulse: Pulse) -> str:
    """The counted part of the pulse: the three sections."""
    labels = {t.id: t.label for t in pulse.top_themes}
    lines = [HEADINGS[0]]
    for i, t in enumerate(pulse.top_themes, start=1):
        stats = f"{format_share(t.volume_share)} of reviews · {format_share(t.negative_share)} 1–2★"
        lines.append(f"{i}. {t.label} ({stats}) — {_sentence(t.summary)}")
    lines += ["", HEADINGS[1]]
    lines += [render_quote(q, labels.get(q.theme_id, q.theme_id)) for q in pulse.quotes]
    lines += ["", HEADINGS[2]]
    for i, a in enumerate(pulse.actions, start=1):
        area = labels.get(a.theme_id, a.theme_id)
        lines.append(f"{i}. {area}: {_sentence(a.action)} Watch for: {_sentence(a.signal)}")
    return "\n".join(lines)


def render_preamble(pulse: Pulse, *, product: str, min_words: int) -> str:
    """Header and source line: shown, but not counted toward the word limit (§4.6)."""
    return "\n".join([
        header(product, pulse.week),
        source_line(review_count=pulse.review_count, sources=pulse.sources,
                    window=pulse.window, min_words=min_words,
                    general_praise_share=pulse.general_praise_share),
    ])


def render_doc(pulse: Pulse, *, product: str, min_words: int) -> str:
    """The Google Doc text, also saved as runs/{week}/pulse.md."""
    return (render_preamble(pulse, product=product, min_words=min_words) + "\n\n"
            + render_body(pulse) + "\n")


def render_email(pulse: Pulse, *, product: str, min_words: int,
                 doc_url: str | None = None) -> str:
    """§7.2: the same note in plain text, then the Doc link once it exists."""
    body = render_doc(pulse, product=product, min_words=min_words)
    if doc_url:
        body += f"\nFull pulse: {doc_url}\n"
    return body
