"""Pulse composer: what both engines share (architecture §4.5, plan Phase 4).

- **Candidate selection (code).** Up to 8 reviews per top theme: the right length, no
  profanity, no redaction tokens, recent first, ratings mixed. Limits relax in steps if a
  theme has nothing eligible (edge cases CMP-01/02/04/16, PII-22).
- **Quotes by sentence range.** Each candidate is split into whole sentences; an engine picks
  a review and a first/last sentence, and `build_quotes` slices `canonical_text`, so a quote
  is always verbatim and starts and ends on a sentence boundary (CMP-10, CMP-12).
- **Assembly and artifacts.** Every number in the pulse is inserted by the renderer from the
  computed stats (CMP-22); `validate.py` checks the result.

The engines are `engines/rules_compose.py` (default: rules and templates) and
`engines/llm_compose.py` (optional: Claude), and `engines/hybrid_compose.py` (the default:
the LLM writes on top of the local ML results).
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from pulse.analysis.cluster import ClusterResult, ThemeDef
from pulse.analysis.rank import GENERAL_PRAISE, ThemeStats
from pulse.analysis.validate import ValidationReport
from pulse.config import PulseConfig
from pulse.fileio import atomic_write, write_json
from pulse.models import ActionIdea, CleanReview, Pulse, Quote, Theme
from pulse.render.markdown import email_subject, render_body, render_doc, render_email
from pulse.textutil import word_count

LLM_PROMPTS = {"quote_pick": "quote_pick_v1", "write": "write_v1"}
HYBRID_PROMPTS = {"write": "write_v2"}
MAX_RETRIES = 2
MAX_CANDIDATES = 8

_REDACTION = re.compile(r"\[(?:email|phone|id|number|link|handle)\]")
# Clear profanity only (CMP-04); the audience is internal and professional.
_PROFANITY = re.compile(r"\b(?:fuck\w*|f\*+k|shit\w*|bullshit|bastard\w*|bitch\w*|asshole\w*|"
                        r"motherf\w*|chutiya\w*|madarchod|bhenchod|behenchod)\b", re.I)
# A sentence ends at . ! ? (one or more) before whitespace or the end, or at a line break.
_SENTENCE_END = re.compile(r"[.!?]+(?=\s|$)|\n+")


class ComposeError(RuntimeError):
    """The composer could not produce a valid pulse within the retry limit."""

    def __init__(self, message: str, attempts: list[dict[str, Any]]) -> None:
        super().__init__(message)
        self.attempts = attempts


# ---------------------------------------------------------------------------- schemas


class QuotePick(BaseModel):
    theme_id: str
    review_id: str
    first_sentence: int
    last_sentence: int


class QuotePicks(BaseModel):
    picks: list[QuotePick]


class ThemeSummary(BaseModel):
    theme_id: str
    summary: str


class ActionDraft(BaseModel):
    theme_id: str
    action: str
    signal: str


class PulseWriting(BaseModel):
    summaries: list[ThemeSummary]
    actions: list[ActionDraft]


# ---------------------------------------------------------------------------- candidates


def sentence_spans(text: str) -> list[tuple[int, int]]:
    """(start, end) code-point offsets of each sentence, whitespace trimmed (CMP-13)."""
    spans, pos = [], 0
    for m in _SENTENCE_END.finditer(text):
        end = m.end() if not m.group().startswith("\n") else m.start()
        spans.append((pos, end))
        pos = m.end()
    spans.append((pos, len(text)))
    out = []
    for start, end in spans:
        seg = text[start:end]
        lead = len(seg) - len(seg.lstrip())
        trail = len(seg) - len(seg.rstrip())
        s, e = start + lead, end - trail
        if e > s and any(ch.isalnum() for ch in text[s:e]):
            out.append((s, e))
    return out or [(0, len(text))]


def _placeholder_ratio(text: str) -> float:
    tokens = text.split()
    return sum(bool(_REDACTION.search(t)) for t in tokens) / len(tokens) if tokens else 1.0


def _band(rating: int) -> int:
    return 0 if rating <= 2 else 1 if rating == 3 else 2


def select_candidates(members: Sequence[CleanReview], cfg: PulseConfig,
                      limit: int = MAX_CANDIDATES) -> list[CleanReview]:
    """Up to `limit` quotable reviews: recent first, ratings mixed (CMP-03: a preference)."""
    a = cfg.analysis
    steps = [  # (min words, max words, allow redaction tokens)
        (a.quote_min_words, a.quote_max_words, False),
        (5, 80, False),
        (1, None, True),
    ]
    for lo, hi, allow_redacted in steps:
        eligible = [
            r for r in members
            if lo <= word_count(r.canonical_text) <= (hi or 10**9)
            and not _PROFANITY.search(r.canonical_text)
            and (not _REDACTION.search(r.canonical_text) if not allow_redacted
                 else _placeholder_ratio(r.canonical_text) <= 0.3)
        ]
        if eligible:
            break
    else:
        return []
    eligible.sort(key=lambda r: (r.review_date, r.id), reverse=True)
    # Round-robin over rating bands, most negative band first, each in recency order.
    bands: dict[int, list[CleanReview]] = defaultdict(list)
    for r in eligible:
        bands[_band(r.rating)].append(r)
    picked: list[CleanReview] = []
    while len(picked) < limit and any(bands.values()):
        for b in (0, 1, 2):
            if bands[b] and len(picked) < limit:
                picked.append(bands[b].pop(0))
    return picked


# ---------------------------------------------------------------------------- context


@dataclass
class ThemeContext:
    definition: ThemeDef
    stats: ThemeStats
    members: list[CleanReview]
    candidates: list[CleanReview]

    @property
    def id(self) -> str:
        return self.definition.id


@dataclass
class ComposeResult:
    pulse: Pulse
    report: ValidationReport
    attempts: list[dict[str, Any]]
    usage: dict[str, Any] = field(default_factory=dict)
    engine: str = "local"
    # Who wrote the text: "llm" or "rules". With the hybrid engine, `fallback` says why the
    # rules wrote it instead, e.g. {"reason": "rate_limited", "error": "..."}.
    composer: str = "rules"
    fallback: dict[str, Any] | None = None


def build_contexts(cluster: ClusterResult, reviews: Sequence[CleanReview],
                   cfg: PulseConfig) -> list[ThemeContext]:
    defs = {t.id: t for t in cluster.themes}
    stats = {s.theme_id: s for s in cluster.ranked}
    by_theme: dict[str, list[CleanReview]] = defaultdict(list)
    for r in reviews:
        by_theme[cluster.assignments[r.id].theme_id].append(r)
    contexts = []
    for tid in cluster.top:
        members = sorted(by_theme[tid], key=lambda r: (r.review_date, r.id), reverse=True)
        candidates = select_candidates(members, cfg)
        if not candidates:
            raise ComposeError(f"theme {tid} has no quotable review", attempts=[])
        contexts.append(ThemeContext(defs[tid], stats[tid], members, candidates))
    return contexts


def trend_word(s: ThemeStats) -> str:
    """'rising', 'steady' or 'easing': the last 14 days compared with the window."""
    if s.recency_weight >= 1.2:
        return "rising"
    if s.recency_weight <= 0.8:
        return "easing"
    return "steady"


# ---------------------------------------------------------------------------- quotes


def build_quotes(picks: QuotePicks, contexts: Sequence[ThemeContext]) -> tuple[
        list[Quote], list[str]]:
    """Turn picks into verbatim quotes. Returns the quotes and any problems."""
    by_theme = {c.id: c for c in contexts}
    quotes, errors, seen = [], [], set()
    for p in picks.picks:
        ctx = by_theme.get(p.theme_id)
        if ctx is None:
            errors.append(f"theme {p.theme_id} is not one of the top themes")
            continue
        if p.theme_id in seen:
            errors.append(f"theme {p.theme_id} got more than one quote")
            continue
        review = next((r for r in ctx.candidates if r.id == p.review_id), None)
        if review is None:  # CMP-18
            errors.append(f"review {p.review_id} is not a candidate for theme {p.theme_id}")
            continue
        spans = sentence_spans(review.canonical_text)
        if not 1 <= p.first_sentence <= p.last_sentence <= len(spans):  # CMP-11
            errors.append(f"sentences {p.first_sentence}–{p.last_sentence} do not exist in "
                          f"review {p.review_id} (it has {len(spans)})")
            continue
        start, end = spans[p.first_sentence - 1][0], spans[p.last_sentence - 1][1]
        quotes.append(Quote.from_span(review, p.theme_id, start, end))
        seen.add(p.theme_id)
    for c in contexts:
        if c.id not in seen and not any(e.startswith(f"theme {c.id} ") for e in errors):
            errors.append(f"theme {c.id} has no quote")
    order = {c.id: i for i, c in enumerate(contexts)}
    quotes.sort(key=lambda q: order[q.theme_id])
    return quotes, errors


# ---------------------------------------------------------------------------- budget


def word_budget(pulse_without_text: Pulse, max_words: int) -> int:
    """Words left for summaries, actions and signals once headings, labels, stats and
    quotes are counted."""
    return max_words - word_count(render_body(pulse_without_text))


# ---------------------------------------------------------------------------- assembly


def assemble(cluster: ClusterResult, contexts: Sequence[ThemeContext],
             reviews: Sequence[CleanReview], quotes: Sequence[Quote],
             writing: PulseWriting | None) -> Pulse:
    summaries: dict[str, str] = {}
    for s in (writing.summaries if writing else []):
        summaries.setdefault(s.theme_id, s.summary.strip())
    themes = [Theme(id=c.id, label=c.definition.label, description=c.definition.description,
                    review_ids=[r.id for r in c.members], volume_share=c.stats.volume_share,
                    negative_share=c.stats.negative_share, avg_rating=c.stats.avg_rating,
                    score=c.stats.score, summary=summaries.get(c.id, ""))
              for c in contexts]
    order = {c.id: i for i, c in enumerate(contexts)}
    actions = sorted((ActionIdea(theme_id=x.theme_id, action=x.action.strip(),
                                 signal=x.signal.strip())
                      for x in (writing.actions if writing else [])),
                     key=lambda x: order.get(x.theme_id, len(order)))
    sources: dict[str, int] = {"play_store": 0, "app_store": 0}
    for r in reviews:
        sources[r.source] = sources.get(r.source, 0) + 1
    return Pulse(week=cluster.week, window=cluster.window, review_count=len(reviews),
                 sources=sources, top_themes=themes, quotes=list(quotes), actions=actions,
                 general_praise_share=cluster.buckets[GENERAL_PRAISE].volume_share,
                 clustered_theme_count=len(cluster.themes))


# ---------------------------------------------------------------------------- artifacts


def _jsonable(pulse: Pulse) -> dict[str, Any]:
    d = asdict(pulse)
    d["window"] = [x.isoformat() if isinstance(x, date) else x for x in pulse.window]
    for t in d["top_themes"]:
        t["review_count"] = len(t.pop("review_ids"))
    return d


def write_compose_artifacts(result: ComposeResult, cfg: PulseConfig, runs_dir: Path) -> dict[
        str, Path]:
    """pulse.json, pulse.md and validation.json for the week (§6)."""
    pulse = result.pulse
    week_dir = runs_dir / pulse.week
    doc = render_doc(pulse, product=cfg.product.name, min_words=cfg.ingest.min_words)
    paths = {"pulse_json": week_dir / "pulse.json", "pulse_md": week_dir / "pulse.md",
             "validation": week_dir / "validation.json"}
    write_json(paths["pulse_json"], {
        **_jsonable(pulse),
        "engine": result.engine,
        "composer": result.composer,
        **({"fallback": result.fallback["reason"]} if result.fallback else {}),
        **({"prompt_versions": LLM_PROMPTS} if result.engine == "llm" else {}),
        **({"prompt_versions": HYBRID_PROMPTS}
           if result.engine == "hybrid" and result.composer == "llm" else {}),
        "email": {"subject": email_subject(cfg.product.name, pulse.week),
                  "body": render_email(pulse, product=cfg.product.name,
                                       min_words=cfg.ingest.min_words)},
    })
    atomic_write(paths["pulse_md"], lambda tmp: tmp.write_text(doc, encoding="utf-8",
                                                               newline="\n"))
    write_json(paths["validation"], {**result.report.to_dict(), "attempts": result.attempts})
    return paths


def write_failure(runs_dir: Path, week: str, *, stage: str, error: str,
                  attempts: list[dict[str, Any]]) -> Path:
    """runs/{week}/failure.json: why the run stopped before publishing (VAL-07)."""
    path = runs_dir / week / "failure.json"
    write_json(path, {"week": week, "stage": stage, "error": error, "attempts": attempts,
                      "published": False})
    if attempts:
        write_json(runs_dir / week / "validation.json",
                   {**{k: v for k, v in attempts[-1].items() if k != "attempt"},
                    "attempts": attempts})
    return path
