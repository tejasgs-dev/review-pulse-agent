"""Theme clustering: types and artifacts shared by both engines (architecture §4.4).

The engines live in `analysis/engines/`: `local_cluster` (default: sentence embeddings +
k-means) and `llm_cluster` (optional: two passes with Claude). Both return a `ClusterResult`,
so ranking (`rank.py`), composing, validation and rendering do not depend on the engine. Two
reserved buckets, `general_praise` and `other`, are never themes and never ranked.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from pulse.analysis.llm import Usage
from pulse.analysis.rank import GENERAL_PRAISE, OTHER, ThemeStats
from pulse.fileio import write_json

PROMPT_DIR = Path(__file__).parent / "prompts"
LLM_PROMPTS = {"cluster": "cluster_v1", "assign": "assign_v1"}


class ThemeDef(BaseModel):
    id: str
    label: str
    description: str
    include: str
    exclude: str
    example_ids: list[str]


@dataclass(frozen=True)
class Label:
    """One review's place in the clustering: a theme ID, `general_praise` or `other`."""

    review_id: str
    theme_id: str
    secondary_theme_id: str | None = None
    sentiment: str | None = None      # LLM engine only; the local engine has no sentiment


class ClusterError(RuntimeError):
    """The model's answers could not be made valid within the retry limit."""


def load_prompt(name: str, **values: object) -> str:
    text = (PROMPT_DIR / f"{name}.md").read_text(encoding="utf-8")
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", str(value))
    leftover = re.search(r"\{\{\w+\}\}", text)
    if leftover:
        raise ValueError(f"prompt {name} has an unfilled placeholder {leftover.group(0)}")
    return text


@dataclass
class ClusterResult:
    week: str
    window: tuple[date, date]
    themes: list[ThemeDef]
    taxonomy_changes: list[str]
    assignments: dict[str, Label]
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
    engine: str = "llm"
    # Local engine extras: top keywords per theme, and each theme review's cosine
    # similarity to its theme centroid (used by the rules composer).
    keywords: dict[str, list[str]] = field(default_factory=dict)
    centroid_similarity: dict[str, float] = field(default_factory=dict)


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
        "engine": result.engine,
        "model": result.model,
        **({"prompt_versions": LLM_PROMPTS} if result.engine == "llm" else {}),
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
        "engine": result.engine,
        "model": result.model,
        **({"prompt_versions": LLM_PROMPTS} if result.engine == "llm" else {}),
        "reviews": result.review_count,
        "discovery_input": result.discovery_input,
        "rediscovered": result.rediscovered,
        "themes": [t.id for t in result.themes],
        "top_themes": result.top,
        "general_praise_share": round(result.buckets[GENERAL_PRAISE].volume_share, 4),
        "other_share": round(result.buckets[OTHER].volume_share, 4),
        "consistency_agreement": result.quality.get("consistency", {}).get("agreement"),
        "stability_median_ari": result.quality.get("stability", {}).get("median_ari"),
        "retries": result.quality.get("retries", 0),
        "warnings": [w["code"] for w in result.warnings],
        **({"usage": result.usage.to_dict()} if result.engine == "llm" else {}),
    }
