import codecs
import csv
import io
import json
from dataclasses import fields
from datetime import UTC, date, datetime

import pytest

from pulse.ingest.base import RawReview
from pulse.ingest.play_file import PlayStoreFileSource
from tests.conftest import PLAY_HEADER, play_row

SINCE, UNTIL = date(2026, 9, 14), date(2026, 11, 22)


def _csv(rows, header=PLAY_HEADER) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(header)
    w.writerows(rows)
    return buf.getvalue()


def _source(cfg, tmp_path, **kw):
    d = tmp_path / "imports" / "play"
    d.mkdir(parents=True, exist_ok=True)
    return d, PlayStoreFileSource(d, cfg.ingest.play_columns, **kw)


def test_reads_utf16_console_export(cfg, tmp_path):  # ING-04, ING-05
    d, src = _source(cfg, tmp_path)
    rows = [
        play_row(rid="a1", rating="1", submitted="2026-11-10T18:30:00Z",
                 text='Withdrawal stuck for 5 days,\nsupport says "wait"'),
        play_row(rid="a2", rating="4.0", submitted="2026-11-11T09:00:00Z", text="बहुत अच्छा ऐप"),
    ]
    (d / "reviews_202611.csv").write_bytes(codecs.BOM_UTF16_LE + _csv(rows).encode("utf-16-le"))

    result = src.fetch(SINCE, UNTIL)
    assert result.complete and not result.warnings
    first, second = result.reviews
    assert first.text == 'Withdrawal stuck for 5 days,\nsupport says "wait"'
    assert first.rating == 1
    assert first.submitted_at == datetime(2026, 11, 10, 18, 30, tzinfo=UTC)
    assert first.source_review_id == "https://play.google.com/review?id=a1"
    assert second.rating == 4 and second.text == "बहुत अच्छा ऐप"


def test_identity_and_reply_columns_are_never_read(cfg, tmp_path):  # §4.3 layer 1, ING-11
    d, src = _source(cfg, tmp_path)
    row = play_row(rid="a1", rating="2", submitted="2026-11-10", text="Slow app at market open",
                   device="SecretPhone X1", reply="Hi Priya, we are sorry")
    (d / "e.csv").write_text(_csv([row]), encoding="utf-8")
    review = src.fetch(SINCE, UNTIL).reviews[0]
    dumped = json.dumps(review.to_json())
    assert "SecretPhone" not in dumped and "Priya" not in dumped
    assert {f.name for f in fields(RawReview)}.isdisjoint({"author", "device", "reply", "name"})


def test_missing_required_column_skips_file_with_clear_message(cfg, tmp_path):  # ING-02
    d, src = _source(cfg, tmp_path)
    header = [h for h in PLAY_HEADER if h != "Star Rating"]
    (d / "bad.csv").write_text(_csv([], header=header), encoding="utf-8")
    result = src.fetch(SINCE, UNTIL)
    assert result.reviews == [] and not result.complete
    assert "rating → 'Star Rating'" in result.warnings[0]


def test_extra_unmapped_columns_are_ignored(cfg, tmp_path):  # ING-03
    d, src = _source(cfg, tmp_path)
    row = play_row(rid="a1", rating="5", submitted="2026-11-10", text="Great app") + ["extra"]
    (d / "e.csv").write_text(_csv([row], header=[*PLAY_HEADER, "New Column"]), encoding="utf-8")
    assert len(src.fetch(SINCE, UNTIL)) == 1


def test_few_malformed_rows_are_skipped_and_counted(cfg, tmp_path):  # ING-06
    d, src = _source(cfg, tmp_path)
    good = [play_row(rid=f"g{i}", rating="3", submitted="2026-11-10", text="Okay app overall")
            for i in range(19)]
    (d / "e.csv").write_text(_csv([*good, ["only", "three", "cells"]]), encoding="utf-8")
    result = src.fetch(SINCE, UNTIL)
    assert len(result) == 19
    assert result.skipped["malformed_row"] == 1


def test_mostly_malformed_file_is_skipped(cfg, tmp_path):
    d, src = _source(cfg, tmp_path)
    good = play_row(rid="g", rating="3", submitted="2026-11-10", text="Okay app overall")
    (d / "e.csv").write_text(_csv([good, ["x"], ["y"]]), encoding="utf-8")
    result = src.fetch(SINCE, UNTIL)
    assert result.reviews == [] and "malformed" in result.warnings[0]


def test_empty_or_missing_import_dir_warns(cfg, tmp_path):  # ING-01
    src = PlayStoreFileSource(tmp_path / "nope", cfg.ingest.play_columns)
    result = src.fetch(SINCE, UNTIL)
    assert result.reviews == [] and "no export files" in result.warnings[0]


def test_json_and_jsonl_exports(cfg, tmp_path):
    d, src = _source(cfg, tmp_path, max_malformed_ratio=0.5)  # 1 bad line of 4 is 25%
    rec = {"Review Link": "j1", "Star Rating": 5, "Review Text": None,  # rating-only is valid
           "Review Submit Date and Time": "2026-11-10T10:00:00Z"}
    (d / "a.json").write_text(json.dumps({"reviews": [rec]}), encoding="utf-8")
    rec2 = {**rec, "Review Link": "j2", "Review Text": "Good"}
    (d / "b.jsonl").write_text(json.dumps(rec2) + "\n{not json\n" + json.dumps(rec2 | {
        "Review Link": "j3"}) + "\n" + json.dumps({**rec2, "Review Link": "j4"}) + "\n",
        encoding="utf-8")
    result = src.fetch(SINCE, UNTIL)
    assert sorted(r.source_review_id for r in result) == ["j1", "j2", "j3", "j4"]
    assert result.skipped["malformed_row"] == 1
    assert next(r for r in result if r.source_review_id == "j1").text is None


@pytest.mark.parametrize("name", ["notes.txt", "export.xlsx"])
def test_unsupported_files_are_ignored(cfg, tmp_path, name):
    d, src = _source(cfg, tmp_path)
    (d / name).write_text("whatever", encoding="utf-8")
    assert "no export files" in src.fetch(SINCE, UNTIL).warnings[0]
