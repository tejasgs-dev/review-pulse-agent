"""runs/{week}/manifest.json: one record per run week, one section per stage (§6).

Phase 1 writes the `ingest` section. Later phases add theirs (config snapshot, model IDs,
token usage, timings) without overwriting other sections.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pulse.fileio import write_json


def manifest_path(runs_dir: Path, week: str) -> Path:
    return runs_dir / week / "manifest.json"


def read_manifest(runs_dir: Path, week: str) -> dict[str, Any]:
    path = manifest_path(runs_dir, week)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def update_manifest(runs_dir: Path, week: str, section: str, data: dict[str, Any]) -> Path:
    """Replace one section of the week's manifest, keeping the others."""
    manifest = read_manifest(runs_dir, week)
    manifest["week"] = week
    manifest[section] = data
    manifest["updated_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    path = manifest_path(runs_dir, week)
    write_json(path, manifest)
    return path
