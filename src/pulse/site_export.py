"""`pulse export-site`: the week's published pulse as static JSON for the website (site/).

The site reads `site/pulses/index.json` (one entry per week, newest first) and
`site/pulses/{week}.json`. Both files are committed to a public repository, so the export is
built from an explicit allowlist of fields and never copies a run artifact as a whole:

- From `pulse.json`: week, window, counts per store, general-praise share, the top 3 themes'
  summaries, the 3 quotes (theme, text, rating, store) and the 3 action ideas.
- From `themes.json`: every theme's label, share, 1–2★ share, count and rank, and the "other"
  share.

Left out on purpose: review IDs, quote spans, theme keywords and descriptions, per-review
assignments, the email body, and everything in `data/` or `clean_reviews.jsonl`.

The three quotes are the only review text that may be committed (architecture §6, written
exception). So the export refuses a week whose pulse did not pass validation, and re-runs the
PII scanner on every text field as a last check.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from pulse.config import PROJECT_ROOT, PulseConfig
from pulse.fileio import write_json
from pulse.privacy.scan import find_pii
from pulse.render.markdown import _sentence, week_of
from pulse.weeks import parse_iso_week

SCHEMA_VERSION = 1
DEFAULT_SITE_PULSES = PROJECT_ROOT / "site" / "pulses"
INDEX_NAME = "index.json"
SOURCES = ("play_store", "app_store")
_WEEK = re.compile(r"^\d{4}-W\d{2}$")


class MissingPulseError(RuntimeError):
    """The week has no composed pulse (or its run artifacts are incomplete)."""


class ExportRefusedError(RuntimeError):
    """The pulse exists but must not be published to the site."""


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise MissingPulseError(f"{path.name} not found in {path.parent}") from e
    except json.JSONDecodeError as e:
        raise MissingPulseError(f"{path} is not valid JSON: {e}") from e


def _share(value: Any) -> float:
    # Full precision: the site rounds for display, and pre-rounding here could round twice
    # (0.94495 -> 0.945 -> "95%" where the Doc says "94%").
    return float(value)


def build_week(cfg: PulseConfig, week: str) -> dict[str, Any]:
    """The site JSON for one week, from `runs/{week}/`. Raises MissingPulseError or
    ExportRefusedError."""
    run_dir = cfg.paths.runs_path / week
    if not (run_dir / "pulse.json").exists():
        raise MissingPulseError(f"no composed pulse yet for {week}: run `pulse run --week {week}`")
    pulse = _read_json(run_dir / "pulse.json")
    themes_doc = _read_json(run_dir / "themes.json")
    validation = _read_json(run_dir / "validation.json")

    if validation.get("ok") is not True:
        raise ExportRefusedError(f"the {week} pulse did not pass validation; nothing exported")
    if pulse.get("week") != week or themes_doc.get("week") != week:
        raise ExportRefusedError(f"runs/{week} holds a pulse for a different week")

    themes = sorted(themes_doc["themes"], key=lambda t: t["volume_share"], reverse=True)
    labels = {t["id"]: t["label"] for t in themes}
    start, end = pulse["window"]

    out: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "week": week,
        "week_label": week_of(week),
        "window": {"start": str(start), "end": str(end)},
        "review_count": int(pulse["review_count"]),
        "sources": {s: int(pulse.get("sources", {}).get(s, 0)) for s in SOURCES},
        "filters": {"min_words": cfg.ingest.min_words, "language": "en"},
        "general_praise_share": _share(pulse["general_praise_share"]),
        "other_share": _share(themes_doc["buckets"]["other"]["share"]),
        "themes": [
            {
                "id": t["id"],
                "label": t["label"],
                "share": _share(t["volume_share"]),
                "negative_share": _share(t["negative_share"]),
                "review_count": int(t["count"]),
                "rank": int(t["rank"]),
                "top": bool(t["top"]),
            }
            for t in themes
        ],
        "top_themes": [
            {
                "id": t["id"],
                "label": t["label"],
                "share": _share(t["volume_share"]),
                "negative_share": _share(t["negative_share"]),
                "summary": _sentence(t["summary"]),
            }
            for t in pulse["top_themes"]
        ],
        "quotes": [
            {
                "theme_id": q["theme_id"],
                "theme_label": labels.get(q["theme_id"], q["theme_id"]),
                "text": q["text"],
                "rating": int(q["rating"]),
                "source": q["source"],
            }
            for q in pulse["quotes"]
        ],
        "actions": [
            {
                "theme_id": a["theme_id"],
                "theme_label": labels.get(a["theme_id"], a["theme_id"]),
                "action": _sentence(a["action"]),
                "signal": _sentence(a["signal"]),
            }
            for a in pulse["actions"]
        ],
    }
    _check(out, cfg)
    return out


def _check(out: dict[str, Any], cfg: PulseConfig) -> None:
    """Shape and privacy checks on what is about to be committed."""
    if len(out["quotes"]) != 3 or len(out["top_themes"]) != 3 or len(out["actions"]) != 3:
        raise ExportRefusedError("a site pulse needs exactly 3 top themes, 3 quotes and 3 actions")
    for q in out["quotes"]:
        if q["source"] not in SOURCES or not 1 <= q["rating"] <= 5:
            raise ExportRefusedError(f"quote has an unknown store or rating: {q['source']}")
    texts = [q["text"] for q in out["quotes"]]
    texts += [t["summary"] for t in out["top_themes"]] + [t["label"] for t in out["themes"]]
    texts += [a["action"] for a in out["actions"]] + [a["signal"] for a in out["actions"]]
    for text in texts:
        hits = find_pii(text, number_min_digits=cfg.privacy.number_min_digits,
                        reference_min_digits=cfg.privacy.reference_min_digits)
        if hits:
            kinds = ", ".join(sorted({h.kind for h in hits}))
            raise ExportRefusedError(f"possible personal data ({kinds}) in the pulse text; "
                                     "nothing exported")


def index_entry(data: dict[str, Any]) -> dict[str, Any]:
    top = data["top_themes"][0]
    return {
        "week": data["week"],
        "week_label": data["week_label"],
        "window": data["window"],
        "review_count": data["review_count"],
        "general_praise_share": data["general_praise_share"],
        "top_theme": {"label": top["label"], "share": top["share"]},
    }


def _read_index(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        weeks = json.loads(path.read_text(encoding="utf-8"))["weeks"]
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        raise ExportRefusedError(f"{path} is not a valid site index; fix or delete it") from e
    return [w for w in weeks if isinstance(w, dict) and _WEEK.match(str(w.get("week", "")))]


def export_week(cfg: PulseConfig, week: str, out_dir: Path = DEFAULT_SITE_PULSES) -> Path:
    """Write `{out_dir}/{week}.json` and add or replace the week in `{out_dir}/index.json`."""
    parse_iso_week(week)
    data = build_week(cfg, week)
    index_path = out_dir / INDEX_NAME
    weeks = [w for w in _read_index(index_path) if w["week"] != week]
    weeks.append(index_entry(data))
    weeks.sort(key=lambda w: w["week"], reverse=True)

    target = out_dir / f"{week}.json"
    write_json(target, data)
    write_json(index_path, {"schema_version": SCHEMA_VERSION, "latest": weeks[0]["week"],
                            "weeks": weeks})
    return target
