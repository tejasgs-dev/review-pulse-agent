"""Local storage: raw fetch results (JSONL), ingest coverage, and the review store (parquet)."""

from __future__ import annotations

import json
import secrets
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from pulse.fileio import atomic_write, write_json, write_jsonl
from pulse.ingest.base import RawReview
from pulse.models import Review

# The store's columns, exactly. No reviewer identity field can be stored (P1 exit criteria).
REVIEW_SCHEMA = pa.schema([
    ("key", pa.string()),
    ("source", pa.string()),
    ("rating", pa.int8()),
    ("title", pa.string()),
    ("text", pa.string()),
    ("review_date", pa.date32()),
    ("language", pa.string()),
    ("app_version", pa.string()),
    ("updated_at", pa.timestamp("us", tz="UTC")),
])


class StoreSchemaError(RuntimeError):
    """reviews.parquet was written by an older version with different columns."""


def new_run_id(now: datetime | None = None) -> str:
    """Timestamp plus a random suffix, so two runs in the same second never collide (ING-30)."""
    now = now or datetime.now(UTC)
    return f"{now.astimezone(UTC):%Y%m%dT%H%M%SZ}-{secrets.token_hex(3)}"


def save_raw(reviews: Iterable[RawReview], data_dir: Path, source: str, run_id: str) -> Path:
    """Save one source's mapped results to data/raw/{source}/{run_id}.jsonl for replay.

    These are RawReview records, not untouched HTTP responses, so reviewer names and
    avatars are never written to disk (resolves edge case PII-24).
    """
    path = data_dir / "raw" / source / f"{run_id}.jsonl"
    write_jsonl(path, [r.to_json() for r in reviews])
    return path


def load_raw(path: Path) -> list[RawReview]:
    with path.open(encoding="utf-8") as f:
        return [RawReview.from_json(json.loads(line)) for line in f if line.strip()]


@dataclass(frozen=True)
class Coverage:
    """A date range a source has been fully fetched for, with no known gaps."""

    since: date
    until: date


class IngestState:
    """data/ingest_state.json: per-source coverage, so weekly runs fetch only new reviews.

    Coverage is recorded only after a *complete* fetch. A partial or failed run leaves it
    unchanged, so the next run re-fetches from the start of the missing range.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, Coverage]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return {
                src: Coverage(date.fromisoformat(v["since"]), date.fromisoformat(v["until"]))
                for src, v in raw.items()
            }
        except (ValueError, KeyError, TypeError, AttributeError):
            return {}  # a corrupt state file only costs a full re-fetch

    def save(self, coverage: dict[str, Coverage]) -> None:
        write_json(self.path, {s: {"since": c.since.isoformat(), "until": c.until.isoformat()}
                               for s, c in sorted(coverage.items())})


@dataclass
class UpsertStats:
    new: int = 0
    updated: int = 0
    unchanged: int = 0
    removed: int = 0   # stored reviews whose newest version now fails a cleaning rule
    total: int = 0


class ReviewStore:
    """data/reviews.parquet: every cleaned review ever ingested, one row per key."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, Review]:
        if not self.path.exists():
            return {}
        names = pq.read_schema(self.path).names
        if names != REVIEW_SCHEMA.names:
            raise StoreSchemaError(
                f"{self.path} has columns {names}, expected {REVIEW_SCHEMA.names}. It was "
                "written by an older version. Move it (and ingest_state.json) aside, then "
                "run `pulse ingest --full` to rebuild it."
            )
        rows = pq.read_table(self.path, schema=REVIEW_SCHEMA).to_pylist()
        return {row["key"]: Review(**row) for row in rows}

    def upsert(
        self, reviews: Iterable[Review], rejected: Mapping[str, datetime] | None = None
    ) -> UpsertStats:
        """Add new reviews, replace older versions, and remove rejected ones.

        `rejected` maps a key to the time of a newer version that failed a cleaning rule
        (for example, an edit that shortened the review). A stored copy that is not newer
        than that version is removed.
        """
        existing = self.load()
        stats = UpsertStats()
        for key, version_time in (rejected or {}).items():
            prev = existing.get(key)
            if prev is not None and prev.updated_at <= version_time:
                del existing[key]
                stats.removed += 1
        for r in reviews:
            prev = existing.get(r.key)
            if prev is None:
                stats.new += 1
            elif r.updated_at > prev.updated_at or (r.updated_at == prev.updated_at and r != prev):
                # Same version, different derived fields (e.g. improved text cleaning).
                stats.updated += 1
            else:
                stats.unchanged += 1
                continue
            existing[r.key] = r
        stats.total = len(existing)
        if stats.new or stats.updated or stats.removed or not self.path.exists():
            self._write(existing.values())
        return stats

    def _write(self, reviews: Iterable[Review]) -> None:
        rows = sorted((asdict(r) for r in reviews), key=lambda d: (d["review_date"], d["key"]))
        table = pa.Table.from_pylist(rows, schema=REVIEW_SCHEMA)
        atomic_write(self.path, lambda tmp: pq.write_table(table, tmp))
