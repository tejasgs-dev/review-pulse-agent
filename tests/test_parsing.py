import codecs
from datetime import UTC, datetime, timedelta, timezone

import pytest

from pulse.ingest.base import SourceError
from pulse.ingest.parsing import decode_bytes, parse_rating, parse_timestamp

TEXT = "Star Rating,Review Text\n5,बहुत अच्छा ₹500 ★\n"


@pytest.mark.parametrize("data", [
    codecs.BOM_UTF16_LE + TEXT.encode("utf-16-le"),
    codecs.BOM_UTF16_BE + TEXT.encode("utf-16-be"),
    TEXT.encode("utf-16-le"),  # no BOM
    TEXT.encode("utf-16-be"),
    codecs.BOM_UTF8 + TEXT.encode("utf-8"),
    TEXT.encode("utf-8"),
])
def test_decode_detects_encoding(data):  # ING-04
    assert decode_bytes(data) == TEXT


def test_decode_rejects_unknown_encoding():
    with pytest.raises(SourceError, match="encoding"):
        decode_bytes("caf\xe9 ok".encode("cp1252"))


@pytest.mark.parametrize("value,expected", [
    ("4", 4), ("4.0", 4), (" 5 ", 5), (3, 3), ("6", 6), ("0", 0),
    ("4.5", None), ("", None), (None, None), ("five", None), (True, None),
])
def test_parse_rating(value, expected):  # ING-07 (range is checked in normalize)
    assert parse_rating(value) == expected


IST = timezone(timedelta(hours=5, minutes=30))


@pytest.mark.parametrize("value,expected", [
    ("2026-11-10T18:30:00Z", datetime(2026, 11, 10, 18, 30, tzinfo=UTC)),
    ("2026-11-18T20:25:35-07:00", datetime(2026, 11, 19, 3, 25, 35, tzinfo=UTC)),
    ("2026-11-10 18:30:00", datetime(2026, 11, 10, 18, 30, tzinfo=UTC)),  # naive → UTC
    ("2026-11-10 18:30:00 UTC", datetime(2026, 11, 10, 18, 30, tzinfo=UTC)),
    ("2026-11-10", datetime(2026, 11, 10, tzinfo=UTC)),
    ("10 Nov 2026", datetime(2026, 11, 10, tzinfo=UTC)),
    ("Nov 10, 2026", datetime(2026, 11, 10, tzinfo=UTC)),
    ("1794335400000", datetime(2026, 11, 10, 18, 30, tzinfo=UTC)),  # epoch millis
    (1794335400, datetime(2026, 11, 10, 18, 30, tzinfo=UTC)),       # epoch seconds
])
def test_parse_timestamp(value, expected):  # ING-08
    assert parse_timestamp(value) == expected


@pytest.mark.parametrize("value", ["", None, "yesterday", "03/04/2026", "2026-13-45"])
def test_parse_timestamp_rejects_unparseable_or_ambiguous(value):
    assert parse_timestamp(value) is None


def test_parse_timestamp_is_always_aware():
    assert parse_timestamp("2026-11-10 10:00:00").tzinfo is not None
