"""ISO week handling (architecture §4.9; edge cases ORC-03, ORC-04, NRM-04)."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
_WEEK = re.compile(r"^(\d{4})-W(\d{2})$")


def parse_iso_week(week: str) -> tuple[date, date]:
    """Return (Monday, Sunday) for a week like '2026-W47'. Rejects weeks that do not exist."""
    m = _WEEK.match(week.strip())
    if not m:
        raise ValueError(f"week must look like 2026-W47, got {week!r}")
    year, num = int(m.group(1)), int(m.group(2))
    try:
        monday = date.fromisocalendar(year, num, 1)
    except ValueError as e:
        raise ValueError(f"{week} does not exist (ISO year {year} has no week {num})") from e
    return monday, monday + timedelta(days=6)


def format_iso_week(d: date) -> str:
    year, num, _ = d.isocalendar()
    return f"{year}-W{num:02d}"


def last_completed_week(now: datetime | None = None, tz: ZoneInfo = IST) -> str:
    """The ISO week that most recently ended, judged in IST, not the machine's time zone."""
    local = (now or datetime.now(tz)).astimezone(tz)
    return format_iso_week(local.date() - timedelta(days=7))


def review_window(week: str, weeks_back: int) -> tuple[date, date]:
    """Inclusive window ending on the Sunday of `week` and spanning `weeks_back` weeks."""
    if not 8 <= weeks_back <= 12:
        raise ValueError("weeks_back must be 8–12")
    _, sunday = parse_iso_week(week)
    return sunday - timedelta(weeks=weeks_back) + timedelta(days=1), sunday
