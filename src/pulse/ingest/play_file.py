"""PlayStoreFileSource: reads review exports dropped in data/imports/play/ (§4.1, §13 #2)."""

from __future__ import annotations

import csv
import io
import json
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

from pulse.config import PLAY_FIELDS_REQUIRED
from pulse.ingest.base import FetchResult, RawReview, SourceError
from pulse.ingest.parsing import clean_str, decode_bytes, parse_rating, parse_timestamp

SUPPORTED_SUFFIXES = (".csv", ".json", ".jsonl")


class PlayStoreFileSource:
    name = "play_store"

    def __init__(
        self, import_dir: Path, columns: dict[str, str], *, max_malformed_ratio: float = 0.10
    ) -> None:
        self.import_dir = import_dir
        self.columns = columns
        self.max_malformed_ratio = max_malformed_ratio

    def fetch(self, since: date, until: date) -> FetchResult:
        # The whole export is read; windowing happens in normalize.py.
        result = FetchResult(source="play_store")
        files = (
            sorted(p for p in self.import_dir.iterdir()
                   if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES)
            if self.import_dir.is_dir() else []
        )
        if not files:  # ING-01
            result.warnings.append(f"no export files in {self.import_dir}")
            return result
        for path in files:
            try:
                reviews, skipped = self._read_file(path)
            except SourceError as e:
                result.warnings.append(f"{path.name}: skipped ({e})")
                result.skipped["file_failed"] += 1
                result.complete = False
                continue
            result.reviews.extend(reviews)
            result.skipped.update(skipped)
        return result

    # -- file formats -------------------------------------------------------

    def _read_file(self, path: Path) -> tuple[list[RawReview], Counter[str]]:
        text = decode_bytes(path.read_bytes())
        suffix = path.suffix.lower()
        if suffix == ".csv":
            records, malformed = self._csv_records(text)
        elif suffix == ".jsonl":
            records, malformed = _jsonl_records(text)
        else:
            records, malformed = _json_records(text)

        required = [self.columns[f] for f in sorted(PLAY_FIELDS_REQUIRED)]
        good: list[dict[str, Any]] = []
        for rec in records:
            if any(col not in rec for col in required):
                malformed += 1  # a JSON record missing a required key (null is allowed)
            else:
                good.append(rec)

        total = len(good) + malformed
        if total and malformed / total > self.max_malformed_ratio:  # ING-06
            raise SourceError(
                f"{malformed} of {total} rows are malformed "
                f"(limit {self.max_malformed_ratio:.0%})"
            )
        skipped: Counter[str] = Counter()
        if malformed:
            skipped["malformed_row"] += malformed
        return [self._map(rec) for rec in good], skipped

    def _csv_records(self, text: str) -> tuple[list[dict[str, Any]], int]:
        reader = csv.reader(io.StringIO(text, newline=""))
        try:
            header = [h.strip().lstrip("﻿") for h in next(reader)]
        except StopIteration as e:
            raise SourceError("file is empty") from e
        except csv.Error as e:
            raise SourceError(f"unreadable CSV header: {e}") from e
        self._check_header(header)

        records: list[dict[str, Any]] = []
        malformed = 0
        while True:
            try:
                row = next(reader)
            except StopIteration:
                break
            except csv.Error:
                malformed += 1
                continue
            if not any(cell.strip() for cell in row):
                continue  # blank line
            if len(row) != len(header):
                malformed += 1
                continue
            records.append(dict(zip(header, row, strict=True)))
        return records, malformed

    def _check_header(self, header: list[str]) -> None:  # ING-02
        missing = [
            f"{field} → '{self.columns[field]}'"
            for field in sorted(PLAY_FIELDS_REQUIRED)
            if self.columns[field] not in header
        ]
        if missing:
            raise SourceError(
                "missing required columns: " + ", ".join(missing)
                + ". Fix ingest.play_columns in config/pulse.yaml to match the export."
            )

    # -- mapping --------------------------------------------------------------

    def _get(self, rec: dict[str, Any], field: str) -> Any:
        col = self.columns.get(field)
        return rec.get(col) if col else None

    def _map(self, rec: dict[str, Any]) -> RawReview:
        # Only mapped fields are read, so author, device, and reply columns never leave the file.
        return RawReview(
            source="play_store",
            source_review_id=clean_str(self._get(rec, "review_id")),
            rating=parse_rating(self._get(rec, "rating")),
            title=clean_str(self._get(rec, "title")),
            text=clean_str(self._get(rec, "text")),
            submitted_at=parse_timestamp(self._get(rec, "date")),
            updated_at=parse_timestamp(self._get(rec, "updated")),
            app_version=clean_str(self._get(rec, "app_version")),
        )


def _json_records(text: str) -> tuple[list[dict[str, Any]], int]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise SourceError(f"invalid JSON: {e}") from e
    if isinstance(data, dict):
        data = data.get("reviews")
    if not isinstance(data, list):
        raise SourceError("JSON must be a list of reviews or an object with a 'reviews' list")
    good = [r for r in data if isinstance(r, dict)]
    return good, len(data) - len(good)


def _jsonl_records(text: str) -> tuple[list[dict[str, Any]], int]:
    records: list[dict[str, Any]] = []
    malformed = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            malformed += 1
            continue
        if isinstance(rec, dict):
            records.append(rec)
        else:
            malformed += 1
    return records, malformed
