from dataclasses import FrozenInstanceError, fields
from datetime import date

import pytest

from pulse.models import CleanReview, Quote, Review, review_key


def _clean(text="Withdrawal took five days to reach my bank account.", rating=1):
    return CleanReview(id="r0001", key="k", source="play_store", rating=rating,
                       review_date=date(2026, 11, 10), canonical_text=text)


def test_review_key_is_stable_and_source_specific():
    assert review_key("play_store", "abc") == review_key("play_store", "abc")
    assert review_key("play_store", "abc") != review_key("app_store", "abc")
    assert len(review_key("play_store", "abc")) == 64


def test_review_has_no_identity_fields():
    names = {f.name for f in fields(Review)}
    assert not names & {"author", "reviewer", "name", "avatar", "device", "source_review_id"}


@pytest.mark.parametrize("rating", [0, 6, 4.0, True])
def test_rating_must_be_int_1_to_5(rating):
    with pytest.raises(ValueError):
        _clean(rating=rating)


def test_reviews_are_immutable():
    with pytest.raises(FrozenInstanceError):
        _clean().canonical_text = "changed"


def test_quote_from_span_slices_canonical_text():
    r = _clean()
    q = Quote.from_span(r, "t1", 0, 15)
    assert q.text == "Withdrawal took"
    assert q.text in r.canonical_text
    assert q.rating == 1


@pytest.mark.parametrize("start,end", [(5, 5), (10, 2), (-1, 4), (0, 10_000)])
def test_quote_rejects_bad_spans(start, end):  # CMP-11
    with pytest.raises(ValueError, match="span"):
        Quote.from_span(_clean(), "t1", start, end)


def test_quote_span_handles_emoji():  # CMP-13
    r = _clean(text="Paise kab aayenge 🙏 still waiting")
    i = r.canonical_text.index("still")
    assert Quote.from_span(r, "t1", i, len(r.canonical_text)).text == "still waiting"
