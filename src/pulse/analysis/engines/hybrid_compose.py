"""Hybrid composer (`--engine hybrid`, the default): ML decides, the LLM writes.

Division of work:

- **The ML (unchanged).** `local_cluster.py` groups the reviews, matches themes against
  `config/themes.yaml`, counts them and computes each theme's share and 14-day trend. Those
  results are final.
- **The LLM (this module).** For each top theme it gets the ML results (label, top keywords,
  counts, trend) and a small sample of scrubbed, quotable reviews (`analysis.hybrid.
  sample_per_theme`), never the whole corpus. In one structured call it picks the quote (by
  sentence number, so code copies it verbatim), and writes the summary, the action idea and
  the signal from what those reviews describe.
- **Numbers stay with the ML.** The LLM may not type a digit. Actions and signals refer to
  figures only through the placeholders `{recent_count}` and `{trend}`, which code fills from
  the ML stats; every signal must use `{recent_count}`, the 14-day figure. The validator's
  `numbers_match_ml` check rejects any number that is not the ML's.
- **Safety in code.** The sample is filtered by the rules engine's name filter (PII-11) and
  quote-fit check before the LLM sees it, so the model can only quote reviews that passed.
- **Rewrite loop.** A failed check (or output that misses the schema) is sent back with the
  previous answer, at most `MAX_RETRIES` (2) times. If it still fails, ComposeError; on a model
  failure, LlmError. The CLI then falls back to `rules_compose` so the week still goes out.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel

from pulse.analysis.catalogue import Catalogue
from pulse.analysis.cluster import ClusterResult, load_prompt
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
    select_candidates,
    sentence_spans,
    trend_word,
    word_budget,
)
from pulse.analysis.engines.rules_compose import NameFilter, QuoteFit, _stem
from pulse.analysis.llm import LlmError, StructuredLlm
from pulse.analysis.validate import validate_pulse
from pulse.config import PulseConfig
from pulse.models import CleanReview
from pulse.textutil import word_count

PROMPT = "write_v2"
SUMMARY_WORDS, ACTION_WORDS, SIGNAL_WORDS = 14, 26, 16
SAMPLE_MAX_WORDS = 80          # longer sample reviews are cut at a sentence boundary
TYPICAL_QUOTE_WORDS = 25       # budget estimate before any quote is picked

PLACEHOLDERS = ("recent_count", "trend")
_PLACEHOLDER = re.compile(r"\{([^{}]*)\}")
_DIGIT = re.compile(r"\d")
# In an action or signal, a figure written as words: an invented target such as "within one
# business day" or "two hours". "Two weeks" is allowed: it is the 14-day window that every
# signal is measured on.
_NUMBER_WORD_TARGET = re.compile(
    r"\b(?:a|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|"
    r"thirty|forty|fifty|hundred|thousand|lakh|crore|half|a few|several|dozen)\s+"
    r"(?:business\s+|working\s+|calendar\s+)?"
    r"(?:seconds?|minutes?|mins?|hours?|hrs?|days?|months?|years?|percent|per\s*cent|"
    r"rupees?|rs|inr|lakhs?|crores?)\b", re.I)


# ---------------------------------------------------------------------------- schema


class QuoteRef(BaseModel):
    review_id: str
    first_sentence: int
    last_sentence: int


class HybridTheme(BaseModel):
    theme_id: str
    quote: QuoteRef
    summary: str
    action: str
    signal: str


class HybridWriting(BaseModel):
    themes: list[HybridTheme]


# ---------------------------------------------------------------------------- the ML facts


def figures(ctx: ThemeContext) -> dict[str, str]:
    """The values the placeholders stand for, formatted as the pulse prints them."""
    return {"recent_count": f"{ctx.stats.recent_count:,}", "trend": trend_word(ctx.stats)}


def ml_numbers(contexts: Sequence[ThemeContext]) -> dict[str, set[str]]:
    """Per theme, the only numbers an action or a signal may contain."""
    return {c.id: {figures(c)["recent_count"]} for c in contexts}


def safe_samples(contexts: Sequence[ThemeContext], cluster: ClusterResult,
                 reviews: Sequence[CleanReview], cfg: PulseConfig,
                 catalogue: Catalogue) -> dict[str, dict[str, int]]:
    """Replace each theme's candidates with a sample the LLM may see and quote: reviews that
    fit the theme (QuoteFit) and name no one (NameFilter), recent first, ratings mixed."""
    local = cfg.analysis.local
    names = NameFilter((r.canonical_text for r in reviews), local.name_allowlist if local else [])
    fit = QuoteFit(cluster, catalogue, local.quote_off_topic_words if local else [])
    counts = {}
    for c in contexts:
        tokens = {_stem(t) for k in (cluster.quality.get("keywords", {}).get(c.id)
                                     or cluster.keywords.get(c.id) or []) for t in k.split()}
        safe = [r for r in c.members if fit.reason(r, c.id, tokens) is None
                and not names.names_person(r.canonical_text)]
        c.candidates = select_candidates(safe, cfg, limit=cfg.analysis.hybrid.sample_per_theme)
        counts[c.id] = {"members": len(c.members), "safe": len(safe),
                        "sent": len(c.candidates)}
        if not c.candidates:
            raise ComposeError(f"theme {c.id} has no quotable review that fits the theme and "
                               "names no one", attempts=[])
    return counts


def _sample_lines(review: CleanReview) -> list[str]:
    lines, words = [], 0
    for i, (s, e) in enumerate(sentence_spans(review.canonical_text), start=1):
        sentence = " ".join(review.canonical_text[s:e].split())
        if words and words + word_count(sentence) > SAMPLE_MAX_WORDS:
            break
        lines.append(f"  [{i}] {sentence}")
        words += word_count(sentence)
    return lines


def theme_block(rank: int, ctx: ThemeContext, cluster: ClusterResult, cfg: PulseConfig) -> str:
    s, f = ctx.stats, figures(ctx)
    keywords = (cluster.keywords.get(ctx.id) or [])[:cfg.analysis.hybrid.keywords_per_theme]
    lines = [
        f"## {rank}. Theme `{ctx.id}`: {ctx.definition.label}",
        f"ML results (final; do not type these numbers): {s.count:,} reviews in the window; "
        f"{{recent_count}} = {f['recent_count']} in the last two weeks; "
        f"{{trend}} = {f['trend']}.",
        f"Top keywords: {', '.join(keywords) or 'none'}",
        "Sample reviews:",
    ]
    for r in ctx.candidates:
        lines.append(f"Review {r.id} ({r.rating}★):")
        lines.extend(_sample_lines(r))
    return "\n".join(lines)


# ---------------------------------------------------------------------------- checks + fill


def raw_number_issues(writing: HybridWriting, contexts: Sequence[ThemeContext]) -> list[str]:
    """Checks on the model's own text, before placeholders are filled: no digit typed by the
    model, only known placeholders, none in summaries, `{recent_count}` in every signal."""
    issues = []
    expected = [c.id for c in contexts]
    got = [t.theme_id for t in writing.themes]
    for tid in expected:
        if got.count(tid) != 1:
            issues.append(f"return exactly one entry for theme {tid} (got {got.count(tid)})")
    for tid in got:
        if tid not in expected:
            issues.append(f"theme {tid} is not one of the top themes; use the IDs given")
    for t in writing.themes:
        for field in ("summary", "action", "signal"):
            text = getattr(t, field)
            if _DIGIT.search(_PLACEHOLDER.sub("", text)):
                issues.append(f"the {field} of {t.theme_id} contains a digit; never type "
                              "numbers, use {recent_count} or {trend}")
            if field != "summary" and (m := _NUMBER_WORD_TARGET.search(text)):
                issues.append(f"the {field} of {t.theme_id} invents a figure "
                              f"(\"{m.group(0)}\"); no deadlines, time limits or amounts")
            for name in _PLACEHOLDER.findall(text):
                if field == "summary":
                    issues.append(f"the summary of {t.theme_id} uses {{{name}}}; summaries use "
                                  "no placeholders")
                elif name not in PLACEHOLDERS:
                    issues.append(f"the {field} of {t.theme_id} uses an unknown placeholder "
                                  f"{{{name}}}; only {{recent_count}} and {{trend}} exist")
        if "{recent_count}" not in t.signal:
            issues.append(f"the signal of {t.theme_id} must contain {{recent_count}} (the "
                          "last-two-weeks figure)")
    return issues


def fill_numbers(text: str, values: dict[str, str]) -> str:
    """Fill the known placeholders from the ML values; anything else is left as written (and
    reported by `raw_number_issues`)."""
    return _PLACEHOLDER.sub(lambda m: values.get(m.group(1), m.group(0)), text)


def to_parts(writing: HybridWriting, contexts: Sequence[ThemeContext]) -> tuple[
        QuotePicks, PulseWriting]:
    values = {c.id: figures(c) for c in contexts}
    picks, summaries, actions = [], [], []
    for t in writing.themes:
        v = values.get(t.theme_id, {})
        picks.append(QuotePick(theme_id=t.theme_id, review_id=t.quote.review_id,
                               first_sentence=t.quote.first_sentence,
                               last_sentence=t.quote.last_sentence))
        summaries.append(ThemeSummary(theme_id=t.theme_id, summary=t.summary))
        actions.append(ActionDraft(theme_id=t.theme_id, action=fill_numbers(t.action, v),
                                   signal=fill_numbers(t.signal, v)))
    return QuotePicks(picks=picks), PulseWriting(summaries=summaries, actions=actions)


# ---------------------------------------------------------------------------- the call


def write_hybrid(llm: StructuredLlm, contexts: Sequence[ThemeContext], cluster: ClusterResult,
                 cfg: PulseConfig, budget: int, *, errors: Sequence[str] = (),
                 previous: HybridWriting | None = None, attempt: int = 0) -> HybridWriting:
    a = cfg.analysis
    system = load_prompt(PROMPT, product=cfg.product.name, top_themes=a.top_themes,
                         quote_min_words=a.quote_min_words, quote_max_words=a.quote_max_words,
                         summary_words=SUMMARY_WORDS, action_words=ACTION_WORDS,
                         signal_words=SIGNAL_WORDS, max_words=a.max_words)
    user = ("\n\n".join(theme_block(i, c, cluster, cfg) for i, c in enumerate(contexts, 1))
            + f"\n\nYou have about {budget} words in total for the {len(contexts)} summaries, "
            "actions and signals.")
    if errors:
        user += ("\n\nYour previous answer was:\n"
                 + (previous.model_dump_json() if previous else "(not valid JSON)")
                 + "\n\nIt had these problems; fix all of them:\n"
                 + "\n".join(f"- {e}" for e in errors))
    return llm.parse(name="hybrid_write" + (f"_retry{attempt}" if attempt else ""),
                     system=system, prefix=None, user=user, schema=HybridWriting,
                     effort=cfg.llm.stage_effort("compose"))


def compose_pulse_hybrid(llm: StructuredLlm, cluster: ClusterResult,
                         reviews: Sequence[CleanReview], cfg: PulseConfig,
                         catalogue: Catalogue) -> ComposeResult:
    """Compose with the LLM on top of the ML results; validate every attempt. Raises
    ComposeError (still failing after the rewrites) or LlmError (the model failed)."""
    contexts = build_contexts(cluster, reviews, cfg)
    sample_counts = safe_samples(contexts, cluster, reviews, cfg, catalogue)
    by_id = {r.id: r for r in reviews}
    assignments = {rid: x.theme_id for rid, x in cluster.assignments.items()}
    allowed_numbers = ml_numbers(contexts)

    empty = PulseWriting(summaries=[], actions=[
        ActionDraft(theme_id=c.id, action="", signal="") for c in contexts])
    budget = (word_budget(assemble(cluster, contexts, reviews, [], empty), cfg.analysis.max_words)
              - TYPICAL_QUOTE_WORDS * len(contexts))

    writing: HybridWriting | None = None
    errors: list[str] = []
    attempts: list[dict[str, Any]] = []
    for attempt in range(MAX_RETRIES + 1):
        try:
            writing = write_hybrid(llm, contexts, cluster, cfg, budget, errors=errors,
                                   previous=writing, attempt=attempt)
        except LlmError as e:
            # Output that missed the schema is retried like a failed check; any other model
            # failure (rate limit, auth, network) ends the attempt and the caller falls back.
            if e.kind not in ("schema", "max_tokens") or attempt == MAX_RETRIES:
                e.attempts = attempts           # type: ignore[attr-defined]
                raise
            errors = [f"your previous answer was not valid JSON for the schema ({e}); answer "
                      "again with every field"]
            attempts.append({"attempt": attempt + 1, "ok": False, "llm_error": e.kind,
                             "issues": [{"check": "schema", "area": "both",
                                         "message": str(e)}]})
            writing = None
            continue
        raw_issues = raw_number_issues(writing, contexts)
        picks, parts = to_parts(writing, contexts)
        quotes, pick_errors = build_quotes(picks, contexts)
        pulse = assemble(cluster, contexts, reviews, quotes, parts)
        report = validate_pulse(pulse, reviews=by_id, assignments=assignments, cfg=cfg,
                                ml_numbers=allowed_numbers)
        for e in raw_issues:
            report.add("numbers_match_ml", e, "writing")
        for e in pick_errors:
            report.add("quote_count", e, "quotes")
        attempts.append({"attempt": attempt + 1, "samples": sample_counts,
                         "quotes": {q.theme_id: q.review_id for q in quotes},
                         **report.to_dict()})
        if report.ok:
            return ComposeResult(pulse=pulse, report=report, attempts=attempts,
                                 engine="hybrid", composer="llm")
        errors = [i.message for i in report.issues]
        if len(quotes) == len(contexts):   # the next budget counts the quotes really picked
            budget = word_budget(assemble(cluster, contexts, reviews, quotes, empty),
                                 cfg.analysis.max_words)
    last = attempts[-1]["issues"]
    raise ComposeError(f"the hybrid pulse failed validation after {len(attempts)} attempts: "
                       + "; ".join(i["message"] for i in last), attempts=attempts)
