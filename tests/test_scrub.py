"""PII scrubber tests (plan Phase 2). Every identifier below is synthetic."""

import json
import random
import time
from datetime import UTC, date, datetime

import pytest

from pulse.models import Review
from pulse.privacy.detectors import detect
from pulse.privacy.scan import find_pii
from pulse.privacy.scrub import redact, scrub, scrub_all, write_clean_reviews


def _r(text: str) -> str:
    return redact(text)[0]


# -- positive cases, one group per detector ---------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("mail me at test.user+groww@example.co.in please", "mail me at [email] please"),
    ("Contact ABC.Def@Mail.COM now", "Contact [email] now"),
    ("support@groww.in never replied.", "[email] never replied."),
])
def test_email(text, expected):
    assert _r(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("call 9876543210 now", "call [phone] now"),
    ("call +91 98765 43210 now", "call [phone] now"),
    ("call +91-9876543210", "call [phone]"),
    ("call 09876543210", "call [phone]"),
    ("call 987-654-3210 ok", "call [phone] ok"),
    ("call 0091 9876543210", "call [phone]"),
    ("US number +1 415 555 0100 here", "US number [phone] here"),
    ("UK +44 20 7946 0958", "UK [phone]"),
])
def test_phone(text, expected):
    assert _r(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("my PAN ABCPE1234F is blocked", "my PAN [id] is blocked"),
    ("pan abcpe1234f rejected", "pan [id] rejected"),
    ("Aadhaar 2345 6789 0123 linked", "Aadhaar [id] linked"),
    ("aadhaar 2345-6789-0123", "aadhaar [id]"),
    ("aadhaar 234567890123", "aadhaar [id]"),
    ("pay to test.user@okaxis or 98765@ybl", "pay to [id] or [id]"),
])
def test_ids(text, expected):
    assert _r(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("account 123456789012345 frozen", "account [number] frozen"),
    ("order ORD1234567890 stuck", "order ORD[number] stuck"),
    ("card 4111 1111 1111 1111 declined", "card [number] declined"),
    ("ref 12345678 pending", "ref [number] pending"),  # 8 digits: the configured minimum
    ("my ticket number 12345678 still open", "my ticket number [number] still open"),
    ("Complaint no: 4567890, no reply", "Complaint no: [number], no reply"),
    ("Ticket number is 12345678.", "Ticket number is [number]."),
    ("order id #55512 and UTR 99887766", "order id #[number] and UTR [number]"),
    ("order id 1234567 and ticket 12345 stuck", "order id [number] and ticket [number] stuck"),
    ("order no. 1234567 not executed", "order no. [number] not executed"),
    ("order #98765 rejected", "order #[number] rejected"),
    ("order number: 55512345 failed", "order number: [number] failed"),
])
def test_long_digit_runs(text, expected):
    assert _r(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("see https://example.com/help?id=42.", "see [link]."),
    ("visit www.example.com/x", "visit [link]"),
    ("go to groww.in/support now", "go to [link] now"),
    ("short link bit.ly/abc123", "short link [link]"),
    ("(http://example.org)", "([link])"),
    ("ping @groww_support on X", "ping [handle] on X"),
    ("thanks @Some.User", "thanks [handle]"),
])
def test_links_and_handles(text, expected):
    assert _r(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("mera number 9876543210 hai, call karo", "mera number [phone] hai, call karo"),
    ("UPI id test.user@okaxis pe paise bhejo", "UPI id [id] pe paise bhejo"),
    ("PAN abcpe1234f galat dikha raha hai", "PAN [id] galat dikha raha hai"),
    ("mail karo xyz.abc@gmail.com pe ya 98765 43210 pe", "mail karo [email] pe ya [phone] pe"),
    ("Withdrawal ₹5000 stuck, order 123456789012 ka refund nahi aaya",
     "Withdrawal ₹5000 stuck, order [number] ka refund nahi aaya"),
])
def test_hinglish_and_mixed_format(text, expected):
    assert _r(text) == expected


# -- negative cases: must NOT be redacted --------------------------------------------

@pytest.mark.parametrize("text", [
    "5 star app, 4 star now",
    "lost ₹500 and then ₹1,00,000 in F&O",
    "Rs 1,00,000 and Rs. 25000 and INR 75000",
    "₹100000000 profit",                      # currency-led amount, even if long
    "since 2026, crashed at 9:15 on 12/11/2026",
    "up 12.5% today, 50k invested, 3 days late",
    "NIFTY50 and TATAMOTORS and SBIN and BANKNIFTY options",
    "3 days, 5 times, 1234 rs, 12 lakh, top 100 stocks",  # short numbers
    "order of 50000 rupees placed",           # an amount, not right after the keyword
    "id 5 and number 12",                     # filler words alone are not references
    "version 5.12.0 is slow",
    "worst app.In this app nothing works",    # missing space is not a domain
    "crashed @ 9:15 again",
    "I use it for SIP @ 9am",
    "rated 1-2 stars by many",
])
def test_not_pii(text):
    assert find_pii(text) == []
    assert _r(text) == text


# -- trade amounts and share counts are not IDs ------------------------------------------

@pytest.mark.parametrize("text", [
    "order of 50000 shares",
    "order 50000 shares placed",
    "buy order 25000 qty placed",
    "order for 25000 qty",
    "order is 100000 units",
    "sell order of 12500000 shares failed",   # 8 digits, but a share count
    "placed an order 75000/- worth",
    "order 500000 rs stuck",
    "order of ₹250000 executed late",
    "limit order 120000 rupees",
    "bought 15000000 shares of a penny stock", # 8 digits followed by "shares"
    "my order 20000 lots",
    "order worth 5 lakh, 25000000 value",
    "invested 10000000 rs in SIP",
])
def test_trade_amounts_and_share_counts_are_kept(text):
    assert find_pii(text) == []
    assert _r(text) == text


@pytest.mark.parametrize("text,expected", [
    ("order id 55512345 for 50000 shares", "order id [number] for 50000 shares"),
    ("order no 1234567 of 2500 qty", "order no [number] of 2500 qty"),
    ("ticket 12345678 about my 50000 shares", "ticket [number] about my 50000 shares"),
])
def test_order_ids_next_to_quantities(text, expected):
    assert _r(text) == expected


# -- overlaps and mechanics -------------------------------------------------------------

def test_email_inside_url_is_one_link():
    assert _r("https://example.com/u/test.user@example.com") == "[link]"


def test_email_is_not_also_a_domain_link():
    assert _r("write to a.b@example.com") == "write to [email]"


def test_phone_shaped_upi_id_is_one_id():
    assert _r("9876543210@ybl") == "[id]"


def test_hits_do_not_overlap_and_are_sorted():
    text = "x test.user@example.com 9876543210 https://example.org @someone"
    hits = detect(text)
    assert hits == sorted(hits)
    assert all(a.end <= b.start for a, b in zip(hits, hits[1:], strict=False))
    assert [h.kind for h in hits] == ["email", "phone", "url", "handle"]


def test_redaction_counts_by_kind():
    _, counts = redact("a@example.com, b@example.com, 9876543210, ABCPE1234F")
    assert counts == {"email": 2, "phone": 1, "pan": 1}


def test_placeholders_are_not_detected():
    assert find_pii("[email] [phone] [id] [number] [link] [handle]") == []


def test_redact_is_idempotent():
    once = _r("mail test.user@example.com or call 9876543210 at https://x.example.com")
    assert _r(once) == once


def test_number_threshold_is_configurable():
    assert redact("value 1234567", number_min_digits=7)[0] == "value [number]"
    assert redact("value 1234567", number_min_digits=8)[0] == "value 1234567"
    assert redact("ticket 1234", reference_min_digits=4)[0] == "ticket [number]"
    assert redact("ticket 1234", reference_min_digits=5)[0] == "ticket 1234"


def test_allow_list_skips_expected_strings():  # VAL-04: the pulse's own Doc link
    doc = "https://docs.google.com/document/d/abc123/edit"
    assert find_pii(f"Full pulse: {doc}", allow=[doc]) == []
    assert find_pii(f"Full pulse: {doc} and https://other.example.com", allow=[doc])


# -- property test ------------------------------------------------------------------------

_FRAGMENTS = [
    "withdrawal stuck", "call", "9876543210", "+91 98765 43210", "test.user@example.com",
    "name@okaxis", "ABCPE1234F", "2345 6789 0123", "123456789012345", "https://example.com/a",
    "groww.in/help", "@someone", "₹500", "5 star", "2026", "9:15", "bahut slow hai", "@", ".",
    "-", "+", "0", "91", "@ybl", "www.", ".com", "4111 1111 1111 1111", "[email]", "x@y", "ORD99",
]


def test_property_nothing_detectable_survives_scrubbing():
    rng = random.Random(0)
    for _ in range(3000):
        seps = ["", " ", " ", ", ", "-", "\n"]
        text = "".join(rng.choice(_FRAGMENTS) + rng.choice(seps)
                       for _ in range(rng.randint(1, 8)))
        scrubbed = _r(text)
        assert find_pii(scrubbed) == [], (text, scrubbed)


def test_scrub_review_property(review_factory):
    clean = scrub(review_factory("mail test.user@example.com, call 9876543210"), "r0001")
    assert find_pii(clean.canonical_text) == []


# -- performance: no catastrophic backtracking (PII-23) --------------------------------------

@pytest.mark.parametrize("text", [
    "a@" * 25_000, "a." * 25_000 + "com", "9" * 50_000, "@a" * 25_000,
    ("1234 " * 10_000), ("x" * 49_000) + "@example.com",
])
def test_detectors_are_fast_on_hostile_input(text):
    start = time.perf_counter()
    redact(text)
    assert time.perf_counter() - start < 2.0


# -- Review → CleanReview ----------------------------------------------------------------------

@pytest.fixture
def review_factory():
    def make(text, key="k1", day=10, source="play_store", rating=2):
        return Review(key=key, source=source, rating=rating, title=None, text=text,
                      review_date=date(2026, 11, day), language="en", app_version="5.12.0",
                      updated_at=datetime(2026, 11, day, tzinfo=UTC))
    return make


def test_scrub_maps_fields(review_factory):
    r = review_factory("Refund to 9876543210 never came back to my account")
    clean = scrub(r, "r0042")
    assert (clean.id, clean.key, clean.source, clean.rating, clean.review_date) == (
        "r0042", r.key, r.source, r.rating, r.review_date)
    assert clean.canonical_text == "Refund to [phone] never came back to my account"


def test_scrub_all_assigns_stable_ids_in_date_order(review_factory):
    reviews = [review_factory("later review text", key="b", day=12),
               review_factory("earlier review text", key="a", day=10),
               review_factory("same day other key", key="c", day=10)]
    result = scrub_all(reviews)
    assert [(c.id, c.key) for c in result.reviews] == [("r0001", "a"), ("r0002", "c"),
                                                       ("r0003", "b")]
    assert scrub_all(list(reversed(reviews))).reviews == result.reviews


def test_scrub_all_counts(review_factory):
    result = scrub_all([review_factory("no pii here", key="a"),
                        review_factory("a@example.com and b@example.com", key="b")])
    assert result.reviews_with_pii == 1 and result.hits == {"email": 2}


def test_id_width_grows_past_9999(review_factory):
    reviews = [review_factory("text", key=f"k{i:05d}") for i in range(10_001)]
    ids = [c.id for c in scrub_all(reviews).reviews]
    assert ids[0] == "r00001" and ids[-1] == "r10001"


def test_write_clean_reviews(tmp_path, review_factory):
    result = scrub_all([review_factory("call 9876543210 about the stuck withdrawal")])
    path = write_clean_reviews(result.reviews, tmp_path / "runs", "2026-W46")
    assert path == tmp_path / "runs" / "2026-W46" / "clean_reviews.jsonl"
    [row] = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert set(row) == {"id", "key", "source", "rating", "review_date", "canonical_text"}
    assert "9876543210" not in path.read_text(encoding="utf-8")
    assert row["canonical_text"] == "call [phone] about the stuck withdrawal"
