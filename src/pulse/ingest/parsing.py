"""Field parsing shared by adapters: encodings, ratings, timestamps."""

from __future__ import annotations

import codecs
from datetime import UTC, datetime, tzinfo

from pulse.ingest.base import SourceError

_NAIVE_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y/%m/%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%d %b %Y",
    "%d %B %Y",
    "%b %d, %Y",
    "%B %d, %Y",
    "%d-%b-%Y",
)


def decode_bytes(data: bytes) -> str:
    """Decode an export file. Play Console CSVs are usually UTF-16 LE with a BOM (ING-04)."""
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return data.decode("utf-16")
    if data.startswith(codecs.BOM_UTF8):
        return data[len(codecs.BOM_UTF8):].decode("utf-8")
    sample = data[:4096]
    if sample and sample.count(0) > len(sample) // 4:
        # UTF-16 without a BOM: for ASCII-heavy text, NULs sit at odd (LE) or even (BE) offsets.
        odd_nuls = sample[1::2].count(0)
        even_nuls = sample[0::2].count(0)
        return data.decode("utf-16-le" if odd_nuls >= even_nuls else "utf-16-be")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise SourceError(
            "could not detect the file encoding (expected UTF-8 or UTF-16); "
            "re-save the export as UTF-8"
        ) from e


def parse_rating(value: object) -> int | None:
    """'4', '4.0', 4 → 4. Anything that is not a whole number → None. Not range-checked."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        f = float(str(value).strip())
    except ValueError:
        return None
    return int(f) if f.is_integer() else None


def parse_timestamp(value: object, naive_tz: tzinfo = UTC) -> datetime | None:
    """Parse ISO 8601, epoch seconds/millis, or a few common export formats.

    Returns a timezone-aware datetime. Values without a zone are read as `naive_tz`.
    Ambiguous day/month formats such as 03/04/2026 are deliberately not supported.
    """
    if value is None:
        return None
    if isinstance(value, int | float) and not isinstance(value, bool):
        return _from_epoch(float(value))
    s = str(value).strip()
    if not s:
        return None
    if s.isdigit() and len(s) in (10, 13):
        return _from_epoch(float(s))
    if s.upper().endswith(" UTC"):
        s = s[:-4] + "+00:00"
    dt: datetime | None = None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        for fmt in _NAIVE_FORMATS:
            try:
                dt = datetime.strptime(s, fmt)
                break
            except ValueError:
                continue
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=naive_tz)


def _from_epoch(v: float) -> datetime | None:
    if v > 1e11:  # milliseconds
        v /= 1000
    try:
        return datetime.fromtimestamp(v, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def clean_str(value: object) -> str | None:
    """Strip a cell value; empty or missing becomes None."""
    if value is None:
        return None
    s = str(value).strip()
    return s or None
