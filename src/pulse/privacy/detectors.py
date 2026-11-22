"""Regex PII detectors (architecture §4.3, implementation plan Phase 2).

Each detector returns spans with a kind and a placeholder:

    email                         → [email]
    phone (Indian and international) → [phone]
    PAN, Aadhaar-like, UPI ID     → [id]
    long digit runs (config), and shorter reference numbers right after a word
    such as "ticket", "complaint", "order id", "UTR" (config) → [number];
    amounts and quantities ("₹5000", "50000 shares", "75000/-") are never numbers
    URL                           → [link]
    @handle                       → [handle]

Overlapping matches are resolved longest-first, then by a fixed priority, so each
character is redacted at most once (an email inside a URL becomes one [link]).
Patterns avoid nested quantifiers so they run in linear time on long input.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

PLACEHOLDER = {
    "email": "[email]",
    "phone": "[phone]",
    "pan": "[id]",
    "aadhaar": "[id]",
    "upi": "[id]",
    "number": "[number]",
    "url": "[link]",
    "handle": "[handle]",
}
# Tie-break when two matches are equally long: lower wins.
_PRIORITY = {k: i for i, k in enumerate(
    ("email", "upi", "url", "phone", "aadhaar", "pan", "number", "handle"))}


@dataclass(frozen=True, order=True)
class Hit:
    start: int
    end: int
    kind: str

    @property
    def placeholder(self) -> str:
        return PLACEHOLDER[self.kind]


_EMAIL = re.compile(
    r"(?<![\w.%+-])[A-Za-z0-9][A-Za-z0-9._%+-]{0,63}"
    r"@(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.){1,8}[A-Za-z]{2,24}(?![\w-])"
)
# UPI IDs look like name@bank: an @ followed by a bank handle with no dot after it.
_UPI = re.compile(r"(?<![\w.%+-])[A-Za-z0-9][A-Za-z0-9._-]{1,63}@[A-Za-z]{2,32}(?![\w.@-])")
_HANDLE = re.compile(r"(?<![\w@.])@[A-Za-z_][A-Za-z0-9_.]{0,28}[A-Za-z0-9_](?![\w@])")

_URL_SCHEME = re.compile(r"(?i)\b(?:https?://|www\.)[^\s<>\"'`]+")
# Bare domains: lowercase only, so a missing space after a full stop ("app.In this")
# is not mistaken for a domain.
_URL_BARE = re.compile(
    r"(?<![@\w.-])(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.){1,4}"
    r"(?:com|in|net|org|io|co|app|xyz|info|biz|ai|ly|gl|gov|edu)"
    r"(?:/[^\s<>\"'`]*)?(?![\w-])"
)
_URL_TRAILING = ".,;:!?)]}'\""

# Indian mobile: 10 digits starting 6–9, unbroken or in the usual 5-5 / 3-3-4 groups,
# with an optional +91 / 0091 / 91 / 0 prefix.
_IN_MOBILE = r"(?:[6-9]\d{9}|[6-9]\d{4}[\s-]\d{5}|[6-9]\d{2}[\s-]\d{3}[\s-]\d{4})"
_PHONE_IN = re.compile(rf"(?<![\w+])(?:(?:\+|00)?91[\s-]?|0)?{_IN_MOBILE}(?!\w)")
_PHONE_INTL = re.compile(r"(?<![\w+])\+(?!91)\d{1,3}[\s-]?\(?\d{1,4}\)?(?:[\s-]?\d){5,10}(?!\w)")

# PAN: 5 letters, 4 digits, 1 letter; the 4th letter is the holder type (P, C, H, F, ...).
_PAN = re.compile(r"(?i)(?<![A-Za-z0-9])[A-Z]{3}[ABCFGHJLPT][A-Z]\d{4}[A-Z](?![A-Za-z0-9])")
# Aadhaar-like: 12 digits starting 2–9, optionally in 4-4-4 groups with one separator style.
_AADHAAR = re.compile(r"(?<!\w)[2-9]\d{3}([\s-]?)\d{4}\1\d{4}(?!\w)")
# Card-style groups of four (1234 5678 9012 3456) count as one long number.
_GROUPED_DIGITS = re.compile(r"(?<!\d)\d{4}(?:[\s-]\d{4}){2,}(?!\d)")
_CURRENCY_BEFORE = re.compile(r"(?i)(?:₹|\brs\.?|\binr)\s*$")
# Reference numbers: a keyword, optional filler ("no", "number", "id", "is", ":", "#"),
# then the digits. Real Groww ticket numbers are 8 digits; some references are shorter.
_REF_KEYWORD = (r"(?:ticket|complaint|complain|ref|reference|account|a/c|acct|txn|"
                r"transaction|utr|case|request|req|srn|application|client\s+id|customer\s+id)")
_REF_FILLER = r"(?:\s*(?:(?:no|number|num|id|is|was)\b|[#:.\-]))*"
# In a trading app "order" usually precedes a quantity ("order 50000 shares"), so it only
# counts as a reference with an explicit ID marker: "order id", "order no", "order #".
_ORDER_REF = (r"\border(?:\s*(?:(?:is|was)\b|[:.\-]))*"
              r"\s*(?:(?:id|no|number|num)\b|#)" + _REF_FILLER)
# A number followed by one of these is a quantity or an amount, never an identifier.
_QUANTITY_AFTER = re.compile(
    r"(?i)\s*(?:/-|₹|(?:shares?|qty|quantity|units?|lots?|rs|rupees?|inr|k|lakhs?|lacs?|"
    r"crores?|cr|worth|value|amount|times)\b)"
)


@lru_cache(maxsize=8)
def _digit_run(min_digits: int) -> re.Pattern[str]:
    return re.compile(rf"(?<!\d)\d{{{min_digits},}}(?!\d)")


@lru_cache(maxsize=8)
def _reference(min_digits: int) -> re.Pattern[str]:
    return re.compile(
        rf"(?i)(?:\b{_REF_KEYWORD}{_REF_FILLER}|{_ORDER_REF})\s*(\d{{{min_digits},}})(?!\d)"
    )


def _is_quantity(text: str, start: int, end: int) -> bool:
    """True for an amount or a quantity: '₹100000000', '50000 shares', '75000/-'."""
    return bool(_CURRENCY_BEFORE.search(text, max(0, start - 6), start)
                or _QUANTITY_AFTER.match(text, end))


def _url_hits(text: str) -> list[Hit]:
    hits = []
    for pattern in (_URL_SCHEME, _URL_BARE):
        for m in pattern.finditer(text):
            end = m.end()
            while end > m.start() and text[end - 1] in _URL_TRAILING:
                end -= 1  # keep sentence punctuation outside the link
            hits.append(Hit(m.start(), end, "url"))
    return hits


def _number_hits(text: str, min_digits: int, reference_min_digits: int) -> list[Hit]:
    spans = [m.span(1) for m in _reference(reference_min_digits).finditer(text)]  # digits only
    for pattern in (_digit_run(min_digits), _GROUPED_DIGITS):
        spans.extend(m.span() for m in pattern.finditer(text))
    return [Hit(s, e, "number") for s, e in spans if not _is_quantity(text, s, e)]


def detect(
    text: str, *, number_min_digits: int = 8, reference_min_digits: int = 5
) -> list[Hit]:
    """All PII spans in `text`, non-overlapping and sorted by position."""
    if not text:
        return []
    candidates: list[Hit] = []
    for kind, pattern in (("email", _EMAIL), ("upi", _UPI), ("handle", _HANDLE),
                          ("phone", _PHONE_IN), ("phone", _PHONE_INTL),
                          ("pan", _PAN), ("aadhaar", _AADHAAR)):
        candidates.extend(Hit(m.start(), m.end(), kind) for m in pattern.finditer(text))
    candidates.extend(_url_hits(text))
    candidates.extend(_number_hits(text, number_min_digits, reference_min_digits))

    # Longest first, then priority; keep each hit only if it overlaps nothing kept so far.
    chosen: list[Hit] = []
    for hit in sorted(candidates, key=lambda h: (-(h.end - h.start), _PRIORITY[h.kind], h.start)):
        if hit.end > hit.start and all(hit.end <= c.start or hit.start >= c.end for c in chosen):
            chosen.append(hit)
    return sorted(chosen)
