"""Hard checks on a composed pulse (architecture §4.6, plan Phase 4). Pure code, no model.

Each failed check produces an `Issue` with an `area` that tells the composer what to redo:
`quotes` (pick quotes again), `writing` (rewrite summaries and actions), `both`, or
`structure` (a code or clustering problem the composer cannot fix).

The word count uses `textutil.word_count` (whitespace tokens with at least one letter or
digit, edge case VAL-02) on the rendered body: the three sections including their headings,
excluding the header and source line (VAL-03).
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Literal

from pulse.config import PulseConfig
from pulse.models import CleanReview, Pulse
from pulse.privacy.scan import find_pii
from pulse.render.markdown import HEADINGS, render_body, render_doc, render_email
from pulse.textutil import word_count

Area = Literal["quotes", "writing", "both", "structure"]

CHECKS = (
    "theme_count", "quote_count", "quote_fidelity", "quote_theme_link", "quote_length",
    "action_count", "action_theme_link", "summaries", "no_stats_in_text", "word_count",
    "pii_scan", "placeholders",
)

# Only the real template tokens, never any angle bracket: a quote may say "<3" (VAL-05).
_PLACEHOLDER = re.compile(r"<verbatim quote \d+>|\{(?:doc_url|date|iso_week|week|summary|"
                          r"action|signal|label)\}|\bTODO\b")
# A statistic written by the model; numbers are inserted by the renderer (CMP-22).
_STAT = re.compile(r"\d+(?:\.\d+)?\s*%|\b\d+\s*(?:out of|in)\s*\d+\b")


@dataclass(frozen=True)
class Issue:
    check: str
    message: str
    area: Area


@dataclass
class ValidationReport:
    issues: list[Issue] = field(default_factory=list)
    word_count: int = 0

    @property
    def ok(self) -> bool:
        return not self.issues

    def add(self, check: str, message: str, area: Area) -> None:
        self.issues.append(Issue(check, message, area))

    def for_area(self, area: str) -> list[str]:
        return [i.message for i in self.issues if i.area in (area, "both")]

    def to_dict(self) -> dict:
        failed = Counter(i.check for i in self.issues)
        return {
            "ok": self.ok,
            "word_count": self.word_count,
            "checks": {c: ("fail" if failed[c] else "pass") for c in CHECKS},
            "issues": [{"check": i.check, "area": i.area, "message": i.message}
                       for i in self.issues],
        }


def validate_pulse(
    pulse: Pulse,
    *,
    reviews: Mapping[str, CleanReview],
    assignments: Mapping[str, str],
    cfg: PulseConfig,
    doc_url: str | None = None,
) -> ValidationReport:
    """Run every check. `assignments` maps review ID -> primary theme ID."""
    a = cfg.analysis
    r = ValidationReport()
    top_ids = [t.id for t in pulse.top_themes]

    # Theme count: clustering found 5 or fewer; the pulse shows exactly 3, all distinct.
    if pulse.clustered_theme_count > a.max_themes:
        r.add("theme_count", f"clustering returned {pulse.clustered_theme_count} themes; "
              f"the limit is {a.max_themes}", "structure")
    if len(top_ids) != a.top_themes or len(set(top_ids)) != len(top_ids):
        r.add("theme_count", f"the pulse must show exactly {a.top_themes} distinct themes, "
              f"not {len(top_ids)}", "structure")

    # Quotes: exactly 3, one per top theme, from 3 different reviews (CMP-19).
    if len(pulse.quotes) != a.quotes:
        r.add("quote_count", f"exactly {a.quotes} quotes are needed, not {len(pulse.quotes)}",
              "quotes")
    quote_themes = Counter(q.theme_id for q in pulse.quotes)
    for tid in top_ids:
        if quote_themes[tid] != 1:
            r.add("quote_count", f"theme {tid} needs exactly one quote, has {quote_themes[tid]}",
                  "quotes")
    if len({q.review_id for q in pulse.quotes}) != len(pulse.quotes):
        r.add("quote_count", "two quotes come from the same review", "quotes")

    for q in pulse.quotes:
        review = reviews.get(q.review_id)
        start, end = q.span
        # Fidelity: the text is exactly the slice of the review's canonical text.
        if review is None:
            r.add("quote_fidelity", f"quote cites unknown review {q.review_id}", "quotes")
        elif (not q.text.strip() or q.text not in review.canonical_text
              or review.canonical_text[start:end] != q.text):
            r.add("quote_fidelity", f"quote from {q.review_id} is not verbatim review text",
                  "quotes")
        # Link: the review belongs to the theme the quote is shown under.
        if assignments.get(q.review_id) != q.theme_id or q.theme_id not in top_ids:
            r.add("quote_theme_link", f"review {q.review_id} is not assigned to theme "
                  f"{q.theme_id}", "quotes")
        n = word_count(q.text)
        if not a.quote_min_words <= n <= a.quote_max_words:
            r.add("quote_length", f"quote from {q.review_id} has {n} words; use "
                  f"{a.quote_min_words}–{a.quote_max_words}", "quotes")

    # Actions: exactly 3, one per top theme (CMP-20), with an action and a signal.
    if len(pulse.actions) != a.actions:
        r.add("action_count", f"exactly {a.actions} action ideas are needed, not "
              f"{len(pulse.actions)}", "writing")
    action_themes = Counter(x.theme_id for x in pulse.actions)
    for x in pulse.actions:
        if x.theme_id not in top_ids:
            r.add("action_theme_link", f"action references {x.theme_id}, which is not a "
                  "top theme", "writing")
        if not x.action.strip() or not x.signal.strip():
            r.add("action_theme_link", f"action for {x.theme_id} needs both an action and a "
                  "signal", "writing")
    for tid in top_ids:
        if action_themes[tid] != 1:
            r.add("action_theme_link", f"theme {tid} needs exactly one action, has "
                  f"{action_themes[tid]}", "writing")

    # Summaries: present, and no statistics (the renderer inserts the numbers).
    for t in pulse.top_themes:
        if not t.summary.strip():
            r.add("summaries", f"theme {t.id} has no summary", "writing")
        elif _STAT.search(t.summary):
            r.add("no_stats_in_text", f"summary of {t.id} states a statistic; write words "
                  "only, the numbers are added automatically", "writing")

    # Word count on the rendered body.
    body = render_body(pulse)
    r.word_count = word_count(body)
    if r.word_count > a.max_words:
        quote_words = sum(word_count(q.text) for q in pulse.quotes)
        area: Area = "both" if quote_words > a.max_words // 2 else "writing"
        r.add("word_count", f"the pulse body has {r.word_count} words; the limit is "
              f"{a.max_words} (quotes use {quote_words}). Shorten it by at least "
              f"{r.word_count - a.max_words} words", area)

    # PII scan on the Doc body and the email body. The Doc link and the recipient are the
    # only allowed exceptions (VAL-04).
    doc = render_doc(pulse, product=cfg.product.name, min_words=cfg.ingest.min_words)
    email = render_email(pulse, product=cfg.product.name, min_words=cfg.ingest.min_words,
                         doc_url=doc_url)
    allow = [doc_url or "", cfg.publish.recipient or ""]
    kinds = _pii_kinds((doc, email), cfg, allow)
    if kinds:
        r.add("pii_scan", "the rendered pulse contains identifiers ("
              + ", ".join(sorted(kinds)) + "); remove them", "both")

    # No template leftovers and no empty sections.
    if _PLACEHOLDER.search(doc):
        r.add("placeholders", "the pulse contains a leftover template token", "writing")
    for heading, items in zip(HEADINGS, (pulse.top_themes, pulse.quotes, pulse.actions),
                              strict=True):
        if not items:
            r.add("placeholders", f"section '{heading}' is empty", "writing")
    return r


def _pii_kinds(texts: Iterable[str], cfg: PulseConfig, allow: list[str]) -> set[str]:
    kinds: set[str] = set()
    for text in texts:
        kinds.update(h.kind for h in find_pii(
            text, number_min_digits=cfg.privacy.number_min_digits,
            reference_min_digits=cfg.privacy.reference_min_digits, allow=allow))
    return kinds
