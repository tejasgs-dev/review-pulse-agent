"""find_pii: the PII scanner, reused by the final output gate in Phase 4 (§4.3 layer 3)."""

from __future__ import annotations

from collections.abc import Iterable

from pulse.privacy.detectors import Hit, detect

__all__ = ["Hit", "find_pii"]


def find_pii(
    text: str,
    *,
    number_min_digits: int = 8,
    reference_min_digits: int = 5,
    allow: Iterable[str] = (),
) -> list[Hit]:
    """Every PII hit in `text`.

    `allow` lists exact strings that are expected in an artifact and are not PII, such as
    the pulse's own Google Doc URL in the email body (edge case VAL-04).
    """
    allowed = {a for a in allow if a}
    hits = detect(text, number_min_digits=number_min_digits,
                  reference_min_digits=reference_min_digits)
    return [h for h in hits if text[h.start:h.end] not in allowed]
