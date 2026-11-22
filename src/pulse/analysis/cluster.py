"""Theme clustering: Pass A (discovery) and Pass B (assignment), plus quality checks.

Architecture §4.4 and plan Phase 3 (S1–S6). The model proposes at most `max_themes` themes
and labels every review; code validates every answer, re-requests anything missing or
invalid, and does all counting and ranking (`rank.py`). Two reserved buckets,
`general_praise` and `other`, are never themes and never ranked.
"""

from __future__ import annotations

import hashlib
import math
import random
import re
from collections import Counter, defaultdict
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from pulse.analysis.llm import StructuredLlm, Usage
from pulse.analysis.rank import (
    GENERAL_PRAISE,
    OTHER,
    RESERVED,
    ThemeStats,
    rank_themes,
    theme_stats,
    top_themes,
)
from pulse.config import AnalysisConfig, PulseConfig
from pulse.fileio import write_json
from pulse.models import CleanReview

PROMPT_DIR = Path(__file__).parent / "prompts"
CLUSTER_PROMPT = "cluster_v1"
ASSIGN_PROMPT = "assign_v1"

MAX_RETRIES = 2              # re-requests after the first attempt, for Pass A and each batch
MAX_OTHER_EXAMPLES = 50      # `other` reviews shown to the re-discovery call
MIN_THEMES = 3               # the pulse needs 3 themes
MAX_LABEL_WORDS = 4
EXAMPLES_PER_THEME = 3

_THEME_ID = re.compile(r"^[a-z][a-z0-9_]{1,39}$")

SEED_DESCRIPTIONS = {
    "onboarding_kyc": "onboarding and KYC (account opening, verification, documents)",
    "payments": "payments and fund transfers (adding money, UPI, bank mandates)",
    "withdrawals": "withdrawals (moving money back to the bank, redemption payouts)",
    "performance_bugs": "app performance and bugs (crashes, lag, freezes, glitches)",
    "charges": "charges and brokerage (fees, hidden or unexpected charges)",
    "support": "customer support (response time, resolution, ticket handling)",
    "trading_features": "F&O and trading features (orders, charts, indicators, options)",
    "statements_tax": "statements and tax reports (P&L, contract notes, tax documents)",
}


# ---------------------------------------------------------------------------- schemas


class ThemeDef(BaseModel):
    id: str
    label: str
    description: str
    include: str
    exclude: str
    example_ids: list[str]


class Discovery(BaseModel):
    themes: list[ThemeDef]
    taxonomy_changes: list[str]


Sentiment = Literal["positive", "neutral", "negative", "mixed"]


class Assignment(BaseModel):
    review_id: str
    theme_id: str
    secondary_theme_id: str | None
    sentiment: Sentiment


class AssignmentBatch(BaseModel):
    assignments: list[Assignment]


class ClusterError(RuntimeError):
    """The model's answers could not be made valid within the retry limit."""


# ---------------------------------------------------------------------------- input


def load_prompt(name: str, **values: object) -> str:
    text = (PROMPT_DIR / f"{name}.md").read_text(encoding="utf-8")
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", str(value))
    leftover = re.search(r"\{\{\w+\}\}", text)
    if leftover:
        raise ValueError(f"prompt {name} has an unfilled placeholder {leftover.group(0)}")
    return text


def week_seed(week: str, salt: str = "") -> int:
    """A stable seed per ISO week, so a run can be repeated exactly."""
    return int(hashlib.sha256(f"{week}:{salt}".encode()).hexdigest()[:16], 16)


def seeded_shuffle(reviews: Sequence[CleanReview], week: str, salt: str = "") -> list[CleanReview]:
    """Shuffle in a fixed order for the week, independent of the input order."""
    items = sorted(reviews, key=lambda r: r.id)
    random.Random(week_seed(week, salt)).shuffle(items)
    return items


def _rating_band(rating: int) -> str:
    return "neg" if rating <= 2 else "mid" if rating == 3 else "pos"


def stratified_sample(reviews: Sequence[CleanReview], k: int, seed: int) -> list[CleanReview]:
    """k reviews, proportional across rating band (1–2★ / 3★ / 4–5★) × ISO week.

    Quotas use the largest-remainder method, so they add up to exactly k.
    """
    if k >= len(reviews):
        return list(reviews)
    groups: dict[tuple[str, int, int], list[CleanReview]] = defaultdict(list)
    for r in sorted(reviews, key=lambda r: r.id):
        iso = r.review_date.isocalendar()
        groups[(_rating_band(r.rating), iso.year, iso.week)].append(r)
    keys = sorted(groups)
    exact = {g: len(groups[g]) * k / len(reviews) for g in keys}
    quota = {g: math.floor(exact[g]) for g in keys}
    remaining = k - sum(quota.values())
    for g in sorted(keys, key=lambda g: (-(exact[g] - quota[g]), g))[:remaining]:
        quota[g] += 1
    rng = random.Random(seed)
    sample: list[CleanReview] = []
    for g in keys:
        sample.extend(rng.sample(groups[g], quota[g]))
    rng.shuffle(sample)
    return sample


def format_review(r: CleanReview) -> str:
    text = " ".join(r.canonical_text.split())
    return f"{r.id} | {r.rating}★ | {r.review_date.isoformat()} | {text}"


def format_reviews(reviews: Sequence[CleanReview]) -> str:
    return "\n".join(format_review(r) for r in reviews)


def split_batches(items: Sequence[CleanReview], size: int) -> list[list[CleanReview]]:
    """Balanced batches of at most `size` (1,348 at 500 -> 450, 449, 449)."""
    if not items:
        return []
    n = math.ceil(len(items) / size)
    base, extra = divmod(len(items), n)
    out, pos = [], 0
    for i in range(n):
        step = base + (1 if i < extra else 0)
        out.append(list(items[pos:pos + step]))
        pos += step
    return out


def _seed_block(seed_ids: Sequence[str]) -> str:
    if not seed_ids:
        return "(no seed themes configured)"
    return "\n".join(f"- `{s}`: {SEED_DESCRIPTIONS.get(s, s.replace('_', ' '))}"
                     for s in seed_ids)


# ---------------------------------------------------------------------------- Pass A


def validate_discovery(d: Discovery, *, known_ids: set[str], max_themes: int) -> list[str]:
    """Problems with a proposed taxonomy, as messages the model can act on."""
    errors = []
    if not MIN_THEMES <= len(d.themes) <= max_themes:
        errors.append(f"return between {MIN_THEMES} and {max_themes} themes, "
                      f"not {len(d.themes)}")
    seen: set[str] = set()
    for t in d.themes:
        if not _THEME_ID.match(t.id):
            errors.append(f"theme id {t.id!r} must be short snake_case")
        if t.id in RESERVED:
            errors.append(f"{t.id!r} is a reserved bucket, not a theme")
        if t.id in seen:
            errors.append(f"theme id {t.id!r} is used twice")
        seen.add(t.id)
        if not t.label.strip() or len(t.label.split()) > MAX_LABEL_WORDS:
            errors.append(f"label of {t.id!r} must be 1–{MAX_LABEL_WORDS} words")
        for name in ("description", "include", "exclude"):
            if not getattr(t, name).strip():
                errors.append(f"{name} of {t.id!r} is empty")
        examples = set(t.example_ids)
        if len(examples) != EXAMPLES_PER_THEME:
            errors.append(f"{t.id!r} needs {EXAMPLES_PER_THEME} distinct example_ids")
        unknown = sorted(examples - known_ids)
        if unknown:
            errors.append(f"example_ids of {t.id!r} are not in the input: {unknown[:5]}")
    return errors


def discover(
    llm: StructuredLlm, reviews: Sequence[CleanReview], cfg: PulseConfig, *,
    total_reviews: int, revision: str | None = None,
) -> Discovery:
    """Pass A. `revision` adds instructions for a second discovery (too much `other`)."""
    a = cfg.analysis
    system = load_prompt(CLUSTER_PROMPT, product=cfg.product.name, max_themes=a.max_themes,
                         min_share_pct=round(a.min_theme_share * 100),
                         seed_themes=_seed_block(a.seed_themes))
    sampled = len(reviews) < total_reviews
    prefix = (
        f"Reviews ({len(reviews)}"
        + (f", a stratified sample of {total_reviews}" if sampled else "")
        + "), one per line as `ID | rating | date | text`, in shuffled order:\n\n"
        + format_reviews(reviews)
    )
    task = "Propose the theme taxonomy for these reviews."
    if revision:
        task = revision
    known = {r.id for r in reviews}
    errors: list[str] = []
    for attempt in range(MAX_RETRIES + 1):
        user = task
        if errors:
            user += ("\n\nYour previous answer had these problems; fix all of them:\n"
                     + "\n".join(f"- {e}" for e in errors))
        result = llm.parse(name="discover" if attempt == 0 else f"discover_retry{attempt}",
                           system=system, prefix=prefix, user=user, schema=Discovery,
                           effort=cfg.llm.effort.cluster)
        errors = validate_discovery(result, known_ids=known, max_themes=a.max_themes)
        if not errors:
            return result
    raise ClusterError("theme discovery failed validation after "
                       f"{MAX_RETRIES + 1} attempts: " + "; ".join(errors))


# ---------------------------------------------------------------------------- Pass B


def theme_definitions(themes: Sequence[ThemeDef]) -> str:
    lines = ["Theme definitions (fixed for every batch):", ""]
    for t in themes:
        lines += [f"### `{t.id}`: {t.label}", t.description,
                  f"- Include: {t.include}", f"- Exclude: {t.exclude}", ""]
    lines += ["Reserved labels: `general_praise` (positive, nothing specific to act on) and "
              "`other` (specific, but fits no theme)."]
    return "\n".join(lines)


@dataclass
class BatchCheck:
    accepted: dict[str, Assignment]
    missing: list[str]
    problems: Counter[str]


def check_batch(batch_ids: Sequence[str], result: AssignmentBatch,
                theme_ids: set[str]) -> BatchCheck:
    """Accept valid labels; list the IDs that must be asked for again."""
    wanted = set(batch_ids)
    problems: Counter[str] = Counter()
    by_id: dict[str, list[Assignment]] = defaultdict(list)
    for a in result.assignments:
        if a.review_id not in wanted:
            problems["outside_batch"] += 1
            continue
        by_id[a.review_id].append(a)
    accepted: dict[str, Assignment] = {}
    for rid, items in by_id.items():
        if len({i.theme_id for i in items}) > 1:
            problems["conflicting_duplicate"] += 1
            continue
        if len(items) > 1:
            problems["duplicate"] += 1
        a = items[0]
        if a.theme_id not in theme_ids and a.theme_id not in RESERVED:
            problems["unknown_theme"] += 1
            continue
        sec = a.secondary_theme_id
        if sec is not None and (sec not in theme_ids or sec == a.theme_id):
            problems["bad_secondary"] += 1
            a = a.model_copy(update={"secondary_theme_id": None})
        accepted[rid] = a
    missing = [rid for rid in batch_ids if rid not in accepted]
    problems["missing"] += len(missing)
    return BatchCheck(accepted=accepted, missing=missing, problems=+problems)


@dataclass
class AssignOutcome:
    assignments: dict[str, Assignment]
    problems: Counter[str] = field(default_factory=Counter)
    retries: int = 0


def assign(
    llm: StructuredLlm, reviews: Sequence[CleanReview], themes: Sequence[ThemeDef],
    cfg: PulseConfig, *, name: str = "assign",
) -> AssignOutcome:
    """Pass B: label every review, in balanced batches run concurrently."""
    a = cfg.analysis
    system = load_prompt(ASSIGN_PROMPT, product=cfg.product.name)
    prefix = theme_definitions(themes)
    theme_ids = {t.id for t in themes}
    batches = split_batches(reviews, a.assign_batch_size)

    def run_batch(index: int, batch: list[CleanReview]) -> AssignOutcome:
        outcome = AssignOutcome(assignments={})
        pending = batch
        for attempt in range(MAX_RETRIES + 1):
            note = "" if attempt == 0 else (
                "These reviews were missing or had an invalid label in your previous answer. ")
            user = (f"{note}Label each of these {len(pending)} reviews "
                    "(`ID | rating | date | text`):\n\n" + format_reviews(pending))
            call = f"{name}_b{index + 1}" + (f"_retry{attempt}" if attempt else "")
            result = llm.parse(name=call, system=system, prefix=prefix, user=user,
                               schema=AssignmentBatch, effort=cfg.llm.effort.assign)
            check = check_batch([r.id for r in pending], result, theme_ids)
            outcome.assignments.update(check.accepted)
            outcome.problems.update(check.problems)
            if not check.missing:
                return outcome
            missing = set(check.missing)
            pending = [r for r in pending if r.id in missing]
            if attempt < MAX_RETRIES:
                outcome.retries += 1
        raise ClusterError(f"{name}: {len(pending)} reviews in batch {index + 1} are still "
                           f"unlabelled after {MAX_RETRIES} re-requests")

    total = AssignOutcome(assignments={})
    with ThreadPoolExecutor(max_workers=max(1, min(a.assign_concurrency, len(batches)))) as ex:
        for part in ex.map(lambda ib: run_batch(*ib), enumerate(batches)):
            total.assignments.update(part.assignments)
            total.problems.update(part.problems)
            total.retries += part.retries
    unlabelled = [r.id for r in reviews if r.id not in total.assignments]
    if unlabelled:  # cannot happen if every batch returned; kept as a hard guarantee
        raise ClusterError(f"{name}: {len(unlabelled)} reviews have no label")
    return total


# ---------------------------------------------------------------------------- the stage


@dataclass
class ClusterResult:
    week: str
    window: tuple[date, date]
    themes: list[ThemeDef]
    taxonomy_changes: list[str]
    assignments: dict[str, Assignment]
    ranked: list[ThemeStats]              # clustered themes, best first
    buckets: dict[str, ThemeStats]        # general_praise, other
    top: list[str]                        # top theme IDs, best first
    discovery_input: int
    review_count: int
    rediscovered: bool
    warnings: list[dict[str, str]]
    quality: dict[str, Any]
    usage: Usage
    model: str


def _share(assignments: dict[str, Assignment], label: str) -> float:
    return (sum(1 for a in assignments.values() if a.theme_id == label) / len(assignments)
            if assignments else 0.0)


def run_clustering(
    llm: StructuredLlm, reviews: Sequence[CleanReview], cfg: PulseConfig, *,
    week: str, window: tuple[date, date],
) -> ClusterResult:
    a: AnalysisConfig = cfg.analysis
    if not reviews:
        raise ClusterError("no reviews to cluster")
    ordered = seeded_shuffle(reviews, week)
    if len(ordered) > a.max_discovery_reviews:
        discovery_input = stratified_sample(ordered, a.max_discovery_reviews,
                                            week_seed(week, "sample"))
    else:
        discovery_input = ordered

    discovery = discover(llm, discovery_input, cfg, total_reviews=len(ordered))
    outcome = assign(llm, ordered, discovery.themes, cfg)
    problems, retries = Counter(outcome.problems), outcome.retries

    rediscovered = False
    other_share = _share(outcome.assignments, OTHER)
    if other_share > a.max_other_share:
        rediscovered = True
        others = [r for r in ordered if outcome.assignments[r.id].theme_id == OTHER]
        revision = (
            f"A first taxonomy left {other_share:.0%} of the reviews in `other`, above the "
            f"{a.max_other_share:.0%} limit. It was:\n\n" + theme_definitions(discovery.themes)
            + f"\n\nHere are {min(len(others), MAX_OTHER_EXAMPLES)} of the reviews that went "
            "to `other`:\n\n" + format_reviews(others[:MAX_OTHER_EXAMPLES])
            + f"\n\nPropose a revised taxonomy (still at most {a.max_themes} themes) that "
            "covers more of the specific feedback. Note in taxonomy_changes what changed "
            "from the first taxonomy."
        )
        discovery = discover(llm, discovery_input, cfg, total_reviews=len(ordered),
                             revision=revision)
        outcome = assign(llm, ordered, discovery.themes, cfg, name="reassign")
        problems.update(outcome.problems)
        retries += outcome.retries

    assignments = outcome.assignments
    primary = {rid: x.theme_id for rid, x in assignments.items()}
    secondary = {rid: x.secondary_theme_id for rid, x in assignments.items()}
    ids = [t.id for t in discovery.themes]
    stats = theme_stats([*ids, *RESERVED], reviews, primary, window_end=window[1],
                        recency_days=a.recency_days, secondary=secondary)
    ranked = rank_themes([s for s in stats if s.theme_id in ids], top_n=a.top_themes)
    buckets = {s.theme_id: s for s in stats if s.theme_id in RESERVED}

    warnings: list[dict[str, str]] = []
    if buckets[OTHER].volume_share > a.max_other_share:
        warnings.append({"code": "other_share_high", "message":
                         f"`other` holds {buckets[OTHER].volume_share:.0%} of reviews "
                         f"(limit {a.max_other_share:.0%}) even after re-discovery"})
    for s in ranked:
        if s.volume_share < a.min_theme_share:
            warnings.append({"code": "small_theme", "message":
                             f"{s.theme_id} holds {s.volume_share:.1%} of reviews "
                             f"(under {a.min_theme_share:.0%})"})

    quality: dict[str, Any] = {
        "assignment_problems": dict(sorted(problems.items())),
        "retries": retries,
        "sentiment_mismatch": _sentiment_mismatch(reviews, assignments),
        "sentiment": dict(Counter(x.sentiment for x in assignments.values()).most_common()),
    }
    probe = _consistency_probe(llm, ordered, discovery.themes, assignments, cfg, week)
    quality["consistency"] = probe
    if probe.get("agreement") is not None and probe["agreement"] < a.min_agreement:
        warnings.append({"code": "low_agreement", "message":
                         f"consistency probe agreement {probe['agreement']:.0%} is under "
                         f"{a.min_agreement:.0%}; two themes probably overlap"})

    return ClusterResult(
        week=week, window=window, themes=list(discovery.themes),
        taxonomy_changes=list(discovery.taxonomy_changes), assignments=assignments,
        ranked=ranked, buckets=buckets,
        top=[s.theme_id for s in top_themes(ranked, a.top_themes)],
        discovery_input=len(discovery_input), review_count=len(reviews),
        rediscovered=rediscovered, warnings=warnings, quality=quality, usage=llm.usage,
        model=cfg.llm.model,
    )


def _sentiment_mismatch(reviews: Sequence[CleanReview],
                        assignments: dict[str, Assignment]) -> dict[str, int]:
    """Rating vs. text disagreements. Diagnostic only; ranking uses the rating."""
    high_negative = low_positive = 0
    for r in reviews:
        s = assignments[r.id].sentiment
        high_negative += r.rating >= 4 and s == "negative"
        low_positive += r.rating <= 2 and s == "positive"
    return {"rated_4_5_but_negative": high_negative, "rated_1_2_but_positive": low_positive}


def _consistency_probe(llm: StructuredLlm, ordered: Sequence[CleanReview],
                       themes: Sequence[ThemeDef], assignments: dict[str, Assignment],
                       cfg: PulseConfig, week: str) -> dict[str, Any]:
    """Label a random sample a second time, in a different order, and measure agreement."""
    k = min(cfg.analysis.consistency_sample, len(ordered))
    if k == 0:
        return {"sample": 0, "agreement": None}
    rng = random.Random(week_seed(week, "probe"))
    sample = rng.sample(sorted(ordered, key=lambda r: r.id), k)
    again = assign(llm, sample, themes, cfg, name="probe").assignments
    differ = sorted(r.id for r in sample if again[r.id].theme_id != assignments[r.id].theme_id)
    return {"sample": k, "agreement": round(1 - len(differ) / k, 4), "disagreements": differ}


# ---------------------------------------------------------------------------- artifacts


def themes_path(runs_dir: Path, week: str) -> Path:
    return runs_dir / week / "themes.json"


def themes_payload(result: ClusterResult) -> dict[str, Any]:
    defs = {t.id: t for t in result.themes}
    themes = []
    for s in result.ranked:
        t = defs[s.theme_id]
        themes.append({"id": t.id, "label": t.label, "description": t.description,
                       "include": t.include, "exclude": t.exclude,
                       "example_ids": t.example_ids, "top": t.id in result.top,
                       **{k: v for k, v in s.to_dict().items() if k != "theme_id"}})
    return {
        "week": result.week,
        "window": {"start": result.window[0].isoformat(), "end": result.window[1].isoformat()},
        "model": result.model,
        "prompt_versions": {"cluster": CLUSTER_PROMPT, "assign": ASSIGN_PROMPT},
        "review_count": result.review_count,
        "discovery": {"input_reviews": result.discovery_input,
                      "sampled": result.discovery_input < result.review_count,
                      "rediscovered": result.rediscovered},
        "top_themes": result.top,
        "themes": themes,
        "buckets": {name: {"count": s.count, "share": round(s.volume_share, 4),
                           "negative_share": round(s.negative_share, 4)}
                    for name, s in result.buckets.items()},
        "taxonomy_changes": result.taxonomy_changes,
        "quality": result.quality,
        "warnings": result.warnings,
        "assignments": [
            {"review_id": rid, "theme_id": x.theme_id,
             "secondary_theme_id": x.secondary_theme_id, "sentiment": x.sentiment}
            for rid, x in sorted(result.assignments.items())
        ],
    }


def write_themes(result: ClusterResult, runs_dir: Path) -> Path:
    path = themes_path(runs_dir, result.week)
    write_json(path, themes_payload(result))
    return path


def manifest_section(result: ClusterResult) -> dict[str, Any]:
    return {
        "status": "ok",
        "model": result.model,
        "prompt_versions": {"cluster": CLUSTER_PROMPT, "assign": ASSIGN_PROMPT},
        "reviews": result.review_count,
        "discovery_input": result.discovery_input,
        "rediscovered": result.rediscovered,
        "themes": [t.id for t in result.themes],
        "top_themes": result.top,
        "general_praise_share": round(result.buckets[GENERAL_PRAISE].volume_share, 4),
        "other_share": round(result.buckets[OTHER].volume_share, 4),
        "consistency_agreement": result.quality["consistency"].get("agreement"),
        "retries": result.quality["retries"],
        "warnings": [w["code"] for w in result.warnings],
        "usage": result.usage.to_dict(),
    }
