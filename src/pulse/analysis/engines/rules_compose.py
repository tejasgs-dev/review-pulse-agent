"""Default composer: quotes picked by rules, summaries and actions from templates, no model
(architecture §4.5, plan Phase 4).

- **Name filter (PII-11).** Skip any candidate that probably names a person: a title before a
  capitalized word, "my name is", "I am" + a capitalized word, a capitalized word before
  "from support", "agent"/"executive" + a capitalized word, or any capitalized word (at the
  start of a sentence too) that is neither a common word in this run's reviews (seen in
  lowercase at least twice) nor on the allowlist.
  It errs towards skipping; humans still read the first pulses.
- **Quote pick.** Candidates are scored by similarity to the theme centroid, theme keywords,
  a 1–2★ rating when the theme is mostly negative, and a 10–30 word length. The quote is the
  1–2 whole sentences with the most theme keywords; code slices `canonical_text`, so quotes
  are always verbatim (via `build_quotes`).
- **Templates.** Each theme catalogue entry has summary, action and signal templates. Actions
  and signals carry this week's figures and keywords (filled in by code), so they differ from
  week to week; summaries carry keywords only (the renderer prints the shares, CMP-22).
- **Fallbacks.** A failed check moves to the next option: the next candidate for a quote
  problem; shorter quotes and the short templates for too many words. At most 2 rounds.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from sklearn.feature_extraction.text import ENGLISH_STOP_WORDS

from pulse.analysis.catalogue import Catalogue, Figures, Templates, fill
from pulse.analysis.cluster import ClusterResult
from pulse.analysis.compose import (
    MAX_RETRIES,
    ActionDraft,
    ComposeError,
    ComposeResult,
    PulseWriting,
    QuotePick,
    QuotePicks,
    ThemeContext,
    ThemeSummary,
    assemble,
    build_contexts,
    build_quotes,
    sentence_spans,
    trend_word,
)
from pulse.analysis.validate import validate_pulse
from pulse.config import PulseConfig
from pulse.models import CleanReview
from pulse.render.markdown import format_share
from pulse.textutil import word_count

QUOTE_WORDS = (35, 25, 18)   # longest quote window per round
IDEAL_WORDS = (10, 30)
KEYWORD_WEIGHT = 0.15        # per theme keyword in the quote window (up to 3)

_CAP = r"([A-Z][a-z]{2,})"
_NAME_PATTERNS = [
    re.compile(r"\b(?:Mr|Mrs|Ms|Miss|Sir|Madam|Dr|Shri|Smt)\.?\s+" + _CAP),
    re.compile(r"\b(?:[Ii] am|[Ii]'m|[Ii]m|[Tt]his is)\s+" + _CAP),
    re.compile(_CAP + r"\s+from\s+(?:the\s+)?(?:support|customer care|customer service|"
               r"customer support|team|groww|help ?desk)", re.I),
    re.compile(r"\b(?:agent|executive|advisor|adviser|representative|associate|named|called)"
               r"\s+(?:named\s+|called\s+)?" + _CAP),
]
_MY_NAME = re.compile(r"\bmy name is\b", re.I)
# Any capitalized word, at the start of a sentence or in the middle of one.
_ANY_CAP = re.compile(r"\b" + _CAP + r"\b")
_TOKEN = re.compile(r"[A-Za-z]+")


# ---------------------------------------------------------------------------- names


class NameFilter:
    """Flags text that probably contains a person's name. Built once per run: a capitalized
    word is only suspicious when its lowercase form is not common in this run's reviews."""

    def __init__(self, corpus: Iterable[str], allowlist: Iterable[str]) -> None:
        lower = Counter(t for text in corpus for t in _TOKEN.findall(text) if t.islower())
        self.common = {w for w, n in lower.items() if n >= 2} | set(ENGLISH_STOP_WORDS)
        self.allow = {a.lower() for a in allowlist}

    def _is_name(self, word: str) -> bool:
        w = word.lower()
        return w not in self.common and w not in self.allow

    def names_person(self, text: str) -> bool:
        if _MY_NAME.search(text):
            return True
        for pattern in _NAME_PATTERNS:
            if any(self._is_name(m.group(1)) for m in pattern.finditer(text)):
                return True
        return any(self._is_name(m.group(1)) for m in _ANY_CAP.finditer(text))


# ---------------------------------------------------------------------------- quotes


def _stem(word: str) -> str:
    return word[:-1] if len(word) > 3 and word.endswith("s") and not word.endswith("ss") else word


def _tokens(text: str) -> set[str]:
    return {_stem(t.lower()) for t in _TOKEN.findall(text)}


def best_window(review: CleanReview, keyword_tokens: set[str], *, min_words: int,
                max_words: int) -> tuple[int, int, int] | None:
    """(first, last, keyword hits) for the 1–2 consecutive sentences with the most theme
    keywords and a length in [min_words, max_words]; sentence numbers start at 1."""
    text = review.canonical_text
    spans = sentence_spans(text)
    best = None
    for i in range(len(spans)):
        for j in (i, i + 1):
            if j >= len(spans):
                continue
            piece = text[spans[i][0]:spans[j][1]]
            n = word_count(piece)
            if not min_words <= n <= max_words:
                continue
            hits = len(_tokens(piece) & keyword_tokens)
            ideal = IDEAL_WORDS[0] <= n <= IDEAL_WORDS[1]
            key = (hits, ideal, -n, -i)
            if best is None or key > best[0]:
                best = (key, (i + 1, j + 1, hits))
    return best[1] if best else None


@dataclass
class ScoredCandidate:
    review: CleanReview
    first: int
    last: int
    score: float


def score_candidates(ctx: ThemeContext, cluster: ClusterResult, keyword_tokens: set[str],
                     names: NameFilter, *, min_words: int, max_words: int) -> list[ScoredCandidate]:
    prefer_negative = ctx.stats.negative_share >= 0.5
    out = []
    for r in ctx.candidates:
        if names.names_person(r.canonical_text):
            continue
        window = best_window(r, keyword_tokens, min_words=min_words, max_words=max_words)
        if window is None:
            continue
        first, last, hits = window
        spans = sentence_spans(r.canonical_text)
        n = word_count(r.canonical_text[spans[first - 1][0]:spans[last - 1][1]])
        score = (cluster.centroid_similarity.get(r.id, 0.0)
                 + KEYWORD_WEIGHT * min(hits, 3)
                 + (0.1 if prefer_negative and r.rating <= 2 else 0.0)
                 + (0.1 if IDEAL_WORDS[0] <= n <= IDEAL_WORDS[1] else 0.0))
        out.append(ScoredCandidate(r, first, last, round(score, 6)))
    out.sort(key=lambda c: (-c.score, c.review.id))
    return out


# ---------------------------------------------------------------------------- templates


def _templates(catalogue: Catalogue, theme_id: str) -> Templates:
    return next((e for e in catalogue.themes if e.id == theme_id), catalogue.generic)


def write_from_templates(contexts: Sequence[ThemeContext], cluster: ClusterResult,
                         catalogue: Catalogue, *, short: bool) -> PulseWriting:
    summaries, actions = [], []
    for c in contexts:
        t = _templates(catalogue, c.id)
        kws = cluster.keywords.get(c.id) or []
        s = c.stats
        figures = Figures(count=s.count, recent_count=s.recent_count,
                          share=format_share(s.volume_share),
                          negative_share=format_share(s.negative_share), trend=trend_word(s))
        summaries.append(ThemeSummary(theme_id=c.id, summary=fill(
            t.summary_short if short else t.summary, kws)))
        actions.append(ActionDraft(theme_id=c.id,
                                   action=fill(t.action_short if short else t.action, kws, figures),
                                   signal=fill(t.signal, kws, figures)))
    return PulseWriting(summaries=summaries, actions=actions)


# ---------------------------------------------------------------------------- the composer


def compose_pulse_rules(cluster: ClusterResult, reviews: Sequence[CleanReview],
                        cfg: PulseConfig, catalogue: Catalogue) -> ComposeResult:
    """Compose and validate with rules only. Raises ComposeError with every attempt."""
    a, local = cfg.analysis, cfg.analysis.local
    contexts = build_contexts(cluster, reviews, cfg)
    by_id = {r.id: r for r in reviews}
    assignments = {rid: x.theme_id for rid, x in cluster.assignments.items()}
    names = NameFilter((r.canonical_text for r in reviews),
                       local.name_allowlist if local else [])
    theme_tokens = {
        c.id: {_stem(t) for k in (cluster.quality.get("keywords", {}).get(c.id)
                                  or cluster.keywords.get(c.id) or []) for t in k.split()}
        for c in contexts
    }

    position = dict.fromkeys((c.id for c in contexts), 0)
    short = False
    attempts: list[dict[str, Any]] = []
    for attempt in range(MAX_RETRIES + 1):
        max_words = QUOTE_WORDS[min(attempt, len(QUOTE_WORDS) - 1)] if short else QUOTE_WORDS[0]
        picks, used, missing = [], set(), []
        for c in contexts:
            ranked = [s for s in score_candidates(c, cluster, theme_tokens[c.id], names,
                                                  min_words=a.quote_min_words,
                                                  max_words=max_words)
                      if s.review.id not in used]
            if not ranked:
                missing.append(c.id)
                continue
            chosen = ranked[min(position[c.id], len(ranked) - 1)]
            used.add(chosen.review.id)
            picks.append(QuotePick(theme_id=c.id, review_id=chosen.review.id,
                                   first_sentence=chosen.first, last_sentence=chosen.last))
        quotes, pick_errors = build_quotes(QuotePicks(picks=picks), contexts)
        writing = write_from_templates(contexts, cluster, catalogue, short=short)
        pulse = assemble(cluster, contexts, reviews, quotes, writing)
        report = validate_pulse(pulse, reviews=by_id, assignments=assignments, cfg=cfg)
        for tid in missing:
            report.add("quote_count", f"theme {tid} has no quotable review that names no one "
                       f"and fits {a.quote_min_words}–{max_words} words", "quotes")
        for e in pick_errors:
            if not any(e.startswith(f"theme {tid} ") for tid in missing):
                report.add("quote_count", e, "quotes")
        attempts.append({"attempt": attempt + 1, "short_templates": short,
                         "max_quote_words": max_words,
                         "quotes": {q.theme_id: q.review_id for q in quotes},
                         **report.to_dict()})
        if report.ok:
            return ComposeResult(pulse=pulse, report=report, attempts=attempts, engine="local")

        # Next option. Quote problems: the next candidate for the themes concerned.
        quote_issues = report.for_area("quotes")
        progressed = False
        for q in quotes:
            if any(q.review_id in m or f"theme {q.theme_id} " in m for m in quote_issues):
                position[q.theme_id] += 1
                progressed = True
        # Too many words: shorter quotes and the short templates.
        if any(i.check == "word_count" for i in report.issues):
            short = True
            progressed = True
        if missing and not short:
            short = True  # shorter windows may find a quotable sentence
            progressed = True
        if not progressed:
            break  # nothing the rules can change (e.g. a structure problem)
    last = attempts[-1]["issues"]
    raise ComposeError(f"the pulse failed validation after {len(attempts)} attempts: "
                       + "; ".join(i["message"] for i in last), attempts=attempts)
