from datetime import UTC, date, datetime

import pytest

from pulse.weeks import last_completed_week, parse_iso_week, review_window


def test_parse_week():
    assert parse_iso_week("2026-W47") == (date(2026, 11, 16), date(2026, 11, 22))


def test_week_53_exists_in_2026():  # ORC-04, RND-04
    assert parse_iso_week("2026-W53") == (date(2026, 12, 28), date(2027, 1, 3))


def test_2027_w01_starts_in_january():
    assert parse_iso_week("2027-W01")[0] == date(2027, 1, 4)


@pytest.mark.parametrize("bad", ["2026-W54", "2027-W53", "2026-47", "2026W47", "W47", ""])
def test_invalid_weeks_rejected(bad):
    with pytest.raises(ValueError):
        parse_iso_week(bad)


def test_last_completed_week_uses_ist_not_utc():  # ORC-03
    # Sunday 19:30 UTC = Monday 01:00 IST, so the week that just ended is W47.
    now = datetime(2026, 11, 22, 19, 30, tzinfo=UTC)
    assert last_completed_week(now) == "2026-W47"
    # 18:00 UTC = 23:30 IST, still Sunday, so W47 has not ended yet.
    assert last_completed_week(datetime(2026, 11, 22, 18, 0, tzinfo=UTC)) == "2026-W46"


def test_window_is_inclusive_and_n_weeks_long():  # NRM-02
    start, end = review_window("2026-W47", 10)
    assert end == date(2026, 11, 22)
    assert start == date(2026, 9, 14)
    assert (end - start).days + 1 == 70


def test_window_rejects_out_of_range_weeks_back():
    with pytest.raises(ValueError):
        review_window("2026-W47", 13)
