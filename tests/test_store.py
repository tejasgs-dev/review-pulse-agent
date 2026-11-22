import json
from dataclasses import replace
from datetime import UTC, date, datetime

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from pulse.ingest.normalize import normalize
from pulse.ingest.store import (
    REVIEW_SCHEMA,
    ReviewStore,
    StoreSchemaError,
    load_raw,
    new_run_id,
    save_raw,
)
from tests.conftest import LONG_TEXT, raw

IDENTITY_FIELDS = {"author", "reviewer", "name", "avatar", "device", "user", "email",
                   "source_review_id", "review_link", "reply"}


def _clean(*raws):
    return normalize(raws, today=date(2026, 11, 21))


def _reviews(*raws):
    return _clean(*raws).reviews


def test_schema_has_no_identity_fields(tmp_path):  # P1 exit criterion
    store = ReviewStore(tmp_path / "reviews.parquet")
    store.upsert(_reviews(raw(rid="a")))
    names = set(pq.read_schema(store.path).names)
    assert names == set(REVIEW_SCHEMA.names)
    assert names.isdisjoint(IDENTITY_FIELDS)


def test_upsert_is_idempotent(tmp_path):  # NRM-13
    store = ReviewStore(tmp_path / "reviews.parquet")
    first = store.upsert(_reviews(raw(rid="a"), raw(rid="b"), raw(rid="c")))
    assert (first.new, first.total) == (3, 3)
    mtime = store.path.stat().st_mtime_ns
    again = store.upsert(_reviews(raw(rid="a"), raw(rid="b"), raw(rid="c")))
    assert (again.new, again.updated, again.unchanged) == (0, 0, 3)
    assert store.path.stat().st_mtime_ns == mtime  # nothing to write, file untouched


def test_upsert_replaces_older_version(tmp_path):  # ING-10
    store = ReviewStore(tmp_path / "reviews.parquet")
    store.upsert(_reviews(raw(rid="a", text=f"Great app so far. {LONG_TEXT}", rating=5)))
    stats = store.upsert(_reviews(raw(rid="a", text=f"Now it crashes daily. {LONG_TEXT}",
                                      rating=1, updated=datetime(2026, 11, 15, tzinfo=UTC))))
    assert stats.updated == 1 and stats.total == 1
    [r] = store.load().values()
    assert r.rating == 1 and r.text.startswith("Now it crashes daily")


def test_older_version_never_overwrites_newer(tmp_path):
    store = ReviewStore(tmp_path / "reviews.parquet")
    store.upsert(_reviews(raw(rid="a", text=f"Newer text. {LONG_TEXT}", rating=1,
                              updated=datetime(2026, 11, 15, tzinfo=UTC))))
    stats = store.upsert(_reviews(raw(rid="a", text=f"Older text. {LONG_TEXT}", rating=5)))
    assert stats.unchanged == 1
    assert next(iter(store.load().values())).rating == 1


def test_same_version_with_new_derived_fields_is_refreshed(tmp_path):
    store = ReviewStore(tmp_path / "reviews.parquet")
    [r] = _reviews(raw(rid="a", text=f"Best app 🚀 ever. {LONG_TEXT}"))
    store.upsert([replace(r, text=f"Best app 🚀 ever. {LONG_TEXT}")])  # before emoji stripping
    assert store.upsert([r]).updated == 1
    assert "🚀" not in store.load()[r.key].text


def test_rejected_newer_version_removes_stored_review(tmp_path):
    store = ReviewStore(tmp_path / "reviews.parquet")
    store.upsert(_reviews(raw(rid="a"), raw(rid="b")))
    edit = _clean(raw(rid="a", text="Now bad", updated=datetime(2026, 11, 15, tzinfo=UTC)))
    stats = store.upsert(edit.reviews, edit.rejected)
    assert (stats.removed, stats.total) == (1, 1)


def test_rejected_older_version_does_not_remove_newer(tmp_path):
    store = ReviewStore(tmp_path / "reviews.parquet")
    store.upsert(_reviews(raw(rid="a", updated=datetime(2026, 11, 15, tzinfo=UTC))))
    stale = _clean(raw(rid="a", text="Bad"))  # an older, short version
    assert store.upsert(stale.reviews, stale.rejected).removed == 0


def test_old_schema_is_refused_with_rebuild_instructions(tmp_path):
    path = tmp_path / "reviews.parquet"
    old = pa.schema([*REVIEW_SCHEMA, ("quotable", pa.bool_())])
    pq.write_table(pa.Table.from_pylist([], schema=old), path)
    with pytest.raises(StoreSchemaError, match="pulse ingest --full"):
        ReviewStore(path).load()


def test_round_trip_preserves_values(tmp_path):
    store = ReviewStore(tmp_path / "reviews.parquet")
    [orig] = _reviews(raw(rid="a", text=f"{LONG_TEXT} and I lost ₹500", title="T"))
    store.upsert([orig])
    assert store.load()[orig.key] == orig


def test_no_temp_files_left_behind(tmp_path):  # ORC-06
    store = ReviewStore(tmp_path / "reviews.parquet")
    store.upsert(_reviews(raw(rid="a")))
    assert [p.name for p in tmp_path.iterdir()] == ["reviews.parquet"]


def test_raw_round_trip_without_identity(tmp_path):  # PII-24
    raws = [raw(rid="a", text="Line one\nline two ₹500"), raw(rid="b", text=None, rating=None)]
    path = save_raw(raws, tmp_path, "play_store", "run1")
    assert path == tmp_path / "raw" / "play_store" / "run1.jsonl"
    assert load_raw(path) == raws
    keys = set(json.loads(path.read_text(encoding="utf-8").splitlines()[0]))
    assert keys.isdisjoint({"author", "name", "avatar", "device"})


def test_run_ids_are_unique_within_a_second():  # ING-30
    now = datetime(2026, 11, 21, 10, 0, tzinfo=UTC)
    ids = {new_run_id(now) for _ in range(50)}
    assert len(ids) == 50 and all(i.startswith("20261121T100000Z-") for i in ids)
