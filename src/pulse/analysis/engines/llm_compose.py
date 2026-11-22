"""Optional LLM composer (`--engine llm`, architecture §4.5).

Claude picks one quote per theme by sentence number (it never writes quote text or counts
characters) and writes one summary and one action idea per theme, words only. On a validation
failure only the failing part (quotes, writing, or both) is asked for again, with the errors
and the previous answer (at most 2 retries, each validated in full: VAL-08).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from pulse.analysis.cluster import ClusterResult, load_prompt
from pulse.analysis.compose import (
    LLM_PROMPTS,
    MAX_RETRIES,
    ActionDraft,
    ComposeError,
    ComposeResult,
    PulseWriting,
    QuotePicks,
    ThemeContext,
    assemble,
    build_contexts,
    build_quotes,
    sentence_spans,
    trend_word,
    word_budget,
)
from pulse.analysis.llm import StructuredLlm
from pulse.analysis.rank import ThemeStats
from pulse.analysis.validate import validate_pulse
from pulse.config import PulseConfig
from pulse.models import CleanReview, Quote

QUOTE_PROMPT = LLM_PROMPTS["quote_pick"]
WRITE_PROMPT = LLM_PROMPTS["write"]
EVIDENCE_PER_THEME = 12
EVIDENCE_MAX_WORDS = 50
SUMMARY_WORDS, ACTION_WORDS, SIGNAL_WORDS = 14, 22, 12


def _trend_phrase(s: ThemeStats) -> str:
    word = trend_word(s)
    return {"rising": "rising in the last two weeks",
             "easing": "quieter in the last two weeks"}.get(word, word)


def _candidate_block(ctx: ThemeContext) -> str:
    lines = [f"## Theme `{ctx.id}`: {ctx.definition.label}", ctx.definition.description, ""]
    for r in ctx.candidates:
        lines.append(f"Candidate {r.id} ({r.rating}★, {r.review_date.isoformat()}):")
        for i, (s, e) in enumerate(sentence_spans(r.canonical_text), start=1):
            lines.append(f"  [{i}] {' '.join(r.canonical_text[s:e].split())}")
        lines.append("")
    return "\n".join(lines)


def pick_quotes(llm: StructuredLlm, contexts: Sequence[ThemeContext], cfg: PulseConfig, *,
                errors: Sequence[str] = (), previous: QuotePicks | None = None,
                attempt: int = 0) -> QuotePicks:
    a = cfg.analysis
    system = load_prompt(QUOTE_PROMPT, product=cfg.product.name,
                         quote_min_words=a.quote_min_words, quote_max_words=a.quote_max_words)
    prefix = "\n".join(_candidate_block(c) for c in contexts)
    user = f"Choose one quote for each of the {len(contexts)} themes above."
    if errors:
        user += ("\n\nYour previous picks were:\n" + (previous.model_dump_json() if previous
                                                         else "(none)")
                 + "\n\nThey had these problems; fix all of them:\n"
                 + "\n".join(f"- {e}" for e in errors))
    return llm.parse(name="quote_pick" + (f"_retry{attempt}" if attempt else ""),
                     system=system, prefix=prefix, user=user, schema=QuotePicks,
                     effort=cfg.llm.effort.compose)


def _evidence(ctx: ThemeContext) -> str:
    lines = []
    for r in ctx.members[:EVIDENCE_PER_THEME]:
        words = r.canonical_text.split()
        text = " ".join(words[:EVIDENCE_MAX_WORDS]) + (" …" if len(words) > EVIDENCE_MAX_WORDS
                                                       else "")
        lines.append(f"- ({r.rating}★) {text}")
    return "\n".join(lines)


def write_pulse(llm: StructuredLlm, contexts: Sequence[ThemeContext], quotes: Sequence[Quote],
                budget: int, cfg: PulseConfig, *, errors: Sequence[str] = (),
                previous: PulseWriting | None = None, attempt: int = 0) -> PulseWriting:
    a = cfg.analysis
    system = load_prompt(WRITE_PROMPT, product=cfg.product.name, top_themes=a.top_themes,
                         max_words=a.max_words, summary_words=SUMMARY_WORDS,
                         action_words=ACTION_WORDS, signal_words=SIGNAL_WORDS)
    quote_by_theme = {q.theme_id: q for q in quotes}
    parts = []
    for rank, c in enumerate(contexts, start=1):
        s = c.stats
        q = quote_by_theme.get(c.id)
        parts.append("\n".join([
            f"## {rank}. `{c.id}`: {c.definition.label}",
            f"Definition: {c.definition.description} Includes: {c.definition.include}",
            f"Context (do not repeat these numbers): {s.count} reviews, "
            f"{s.volume_share:.0%} of all reviews; {s.negative_share:.0%} rated 1–2★; "
            f"{_trend_phrase(s)}.",
            f"Quote shown: “{' '.join(q.text.split())}”" if q else "Quote shown: (none)",
            "Recent reviews:",
            _evidence(c),
        ]))
    user = ("\n\n".join(parts)
            + f"\n\nYou have about {budget} words in total for the {len(contexts)} summaries, "
            "actions and signals.")
    if errors:
        user += ("\n\nYour previous answer was:\n" + (previous.model_dump_json() if previous
                                                       else "(none)")
                 + "\n\nIt had these problems; fix all of them:\n"
                 + "\n".join(f"- {e}" for e in errors))
    return llm.parse(name="write" + (f"_retry{attempt}" if attempt else ""), system=system,
                     prefix=None, user=user, schema=PulseWriting, effort=cfg.llm.effort.compose)


def compose_pulse_llm(llm: StructuredLlm, cluster: ClusterResult, reviews: Sequence[CleanReview],
                  cfg: PulseConfig) -> ComposeResult:
    """Compose and validate. Raises ComposeError (with every attempt's issues) on failure."""
    contexts = build_contexts(cluster, reviews, cfg)
    by_id = {r.id: r for r in reviews}
    assignments = {rid: x.theme_id for rid, x in cluster.assignments.items()}

    picks = writing = None
    quotes: list[Quote] = []
    quote_errors: list[str] = []
    writing_errors: list[str] = []
    retry_quotes, retry_writing = True, True
    attempts: list[dict[str, Any]] = []
    for attempt in range(MAX_RETRIES + 1):
        if retry_quotes:
            picks = pick_quotes(llm, contexts, cfg, errors=quote_errors, previous=picks,
                                attempt=attempt)
            quotes, pick_errors = build_quotes(picks, contexts)
        else:
            pick_errors = []
        if retry_writing:
            empty = PulseWriting(summaries=[], actions=[
                ActionDraft(theme_id=c.id, action="", signal="") for c in contexts])
            skeleton = assemble(cluster, contexts, reviews, quotes, empty)
            budget = word_budget(skeleton, cfg.analysis.max_words)
            writing = write_pulse(llm, contexts, quotes, budget, cfg, errors=writing_errors,
                                  previous=writing, attempt=attempt)
        pulse = assemble(cluster, contexts, reviews, quotes, writing)
        report = validate_pulse(pulse, reviews=by_id, assignments=assignments, cfg=cfg)
        for e in pick_errors:
            report.add("quote_count", e, "quotes")
        attempts.append({"attempt": attempt + 1,
                         "redid": [p for p, on in (("quotes", retry_quotes),
                                                   ("writing", retry_writing)) if on],
                         **report.to_dict()})
        if report.ok:
            return ComposeResult(pulse=pulse, report=report, attempts=attempts, engine="llm")
        quote_errors = report.for_area("quotes")
        writing_errors = report.for_area("writing")
        retry_quotes, retry_writing = bool(quote_errors), bool(writing_errors)
        if not (retry_quotes or retry_writing):
            break  # only structure issues: the composer cannot fix them
    last = attempts[-1]["issues"]
    raise ComposeError(f"the pulse failed validation after {len(attempts)} attempts: "
                       + "; ".join(i["message"] for i in last), attempts=attempts)
