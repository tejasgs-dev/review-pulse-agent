from datetime import UTC, date, datetime

import pytest

from pulse.ingest.language import detect_language
from pulse.ingest.normalize import in_window, normalize
from pulse.textutil import strip_emojis, word_count
from pulse.weeks import review_window
from tests.conftest import LONG_TEXT, raw

TODAY = date(2026, 11, 21)

EIGHT = "The withdrawal took five days to reach me"          # 8 words
NINE = "The withdrawal took five days to reach my account"   # 9 words


def _norm(*raws, **kw):
    return normalize(raws, today=TODAY, **kw)


def _counts(result, source="play_store"):
    return result.stats[source].counts


def test_maps_raw_to_review():
    [r] = _norm(raw(text=f"  {LONG_TEXT}  ", rating=2)).reviews
    assert r.text == LONG_TEXT and r.rating == 2 and r.language == "en"
    assert r.review_date == date(2026, 11, 10) and r.updated_at.tzinfo is not None


# -- the three cleaning rules ----------------------------------------------------

def test_emoji_only_review_is_dropped():
    result = _norm(raw(text="👍👍👍 🙏 ⭐⭐⭐⭐⭐"))
    assert result.reviews == []
    assert _counts(result)["dropped_no_text"] == 1


def test_exactly_8_words_is_dropped():
    assert word_count(EIGHT) == 8
    result = _norm(raw(text=EIGHT))
    assert result.reviews == [] and _counts(result)["dropped_too_short"] == 1


def test_exactly_9_words_is_kept():
    assert word_count(NINE) == 9
    [r] = _norm(raw(text=NINE)).reviews
    assert r.text == NINE


def test_hindi_review_is_dropped():
    hindi = "पैसे निकालने में बहुत समय लगता है और सपोर्ट टीम जवाब नहीं देती"
    result = _norm(raw(text=hindi))
    assert result.reviews == [] and _counts(result)["dropped_non_english"] == 1
    assert result.stats["play_store"].dropped_languages == {"hi": 1}


def test_hinglish_review_is_dropped():
    hinglish = "Paise withdraw nahi ho rahe hai teen din se support bhi jawab nahi deta"
    result = _norm(raw(text=hinglish))
    assert result.reviews == [] and _counts(result)["dropped_non_english"] == 1
    assert result.stats["play_store"].dropped_languages == {"hi-Latn": 1}


def test_english_with_emojis_is_kept_with_emojis_removed():
    text = "Great app 👍 for investing in mutual funds and stocks 🚀🚀 easily"
    [r] = _norm(raw(text=text)).reviews
    assert r.text == "Great app for investing in mutual funds and stocks easily"


def test_emojis_do_not_count_as_words():
    result = _norm(raw(text=f"{EIGHT} 🙏🙏🙏"))  # 8 words + emojis
    assert result.reviews == [] and _counts(result)["dropped_too_short"] == 1
    assert _counts(result)["emojis_removed"] == 1


def test_emoji_between_words_keeps_them_apart():
    assert strip_emojis("good👍app") == "good app"
    assert strip_emojis("Nice ❤️ app ✅ fast 👨‍👩‍👧 ok") == "Nice app fast ok"


@pytest.mark.parametrize("text,expected", [
    ("₹500 lost :) ★", "₹500 lost :)"),      # currency and text emoticons stay; ★ goes
    ("Rs. 1,00,000 — 9:15 IST", "Rs. 1,00,000 — 9:15 IST"),
    ("Line one 😡\nline two", "Line one\nline two"),
])
def test_strip_keeps_text_currency_and_emoticons(text, expected):
    assert strip_emojis(text) == expected


def test_min_words_is_configurable():
    assert len(_norm(raw(text=EIGHT), min_words=8).reviews) == 1


def test_rating_only_review_is_dropped():
    result = _norm(raw(text=None))
    assert result.reviews == [] and _counts(result)["dropped_no_text"] == 1


def test_rule_order_counts_each_review_once():
    result = _norm(
        raw(rid="1", text="🙏"), raw(rid="2", text="Good app"), raw(rid="3", text=NINE),
        raw(rid="4", text="App bahut slow hai market open pe hota hai har din yaar"),
    )
    st = result.stats["play_store"]
    assert (st.input, st.kept) == (4, 1)
    assert {k: st.counts[k] for k in ("dropped_no_text", "dropped_too_short",
                                      "dropped_non_english")} == {
        "dropped_no_text": 1, "dropped_too_short": 1, "dropped_non_english": 1}


def test_stats_are_per_source():
    result = _norm(raw(rid="p", text=EIGHT), raw(rid="a", source="app_store", text=NINE))
    assert result.stats["play_store"].counts["dropped_too_short"] == 1
    assert result.stats["app_store"].kept == 1


def test_rejected_keys_are_reported_for_store_cleanup():
    from pulse.models import review_key

    result = _norm(raw(rid="x", text=EIGHT))
    assert list(result.rejected) == [review_key("play_store", "x")]


# -- validity and dedupe -----------------------------------------------------------

def test_ist_date_not_utc_date():  # ING-08
    late_sunday_utc = datetime(2026, 11, 22, 19, 0, tzinfo=UTC)  # Monday 00:30 IST
    [r] = normalize([raw(submitted=late_sunday_utc)], today=date(2026, 11, 23)).reviews
    assert r.review_date == date(2026, 11, 23)


@pytest.mark.parametrize("rating", [0, 6, None, -1])
def test_bad_ratings_dropped(rating):  # ING-07
    result = _norm(raw(rating=rating))
    assert result.reviews == [] and _counts(result)["dropped_bad_rating"] == 1


def test_missing_date_dropped():
    result = _norm(raw(submitted=None))
    assert result.reviews == [] and _counts(result)["dropped_bad_date"] == 1


def test_future_dates_dropped():  # NRM-03
    result = _norm(raw(submitted=datetime(2026, 12, 1, tzinfo=UTC)))
    assert result.reviews == [] and _counts(result)["dropped_future_date"] == 1


def test_same_review_twice_is_deduped():  # ING-09, ING-27
    result = _norm(raw(rid="x"), raw(rid="x"))
    assert len(result.reviews) == 1 and _counts(result)["duplicate"] == 1


@pytest.mark.parametrize("order", [0, 1])
def test_edited_review_newest_version_wins(order):  # ING-10
    old = raw(rid="x", text=f"Great app so far. {LONG_TEXT}", rating=5)
    new = raw(rid="x", text=f"Withdrawals now fail every time. {LONG_TEXT}", rating=1,
              updated=datetime(2026, 11, 15, tzinfo=UTC))
    [r] = _norm(*((old, new) if order == 0 else (new, old))).reviews
    assert r.rating == 1 and r.text.startswith("Withdrawals")
    assert r.review_date == date(2026, 11, 15)


def test_edit_that_shortens_a_review_rejects_it():
    old = raw(rid="x", text=LONG_TEXT)
    new = raw(rid="x", text="Bad now", updated=datetime(2026, 11, 15, tzinfo=UTC))
    result = _norm(old, new)
    assert result.reviews == [] and len(result.rejected) == 1


def test_identical_text_from_different_users_is_kept():  # NRM-05
    result = _norm(raw(rid="u1"), raw(rid="u2"))
    assert len(result.reviews) == 2 and _counts(result)["duplicate"] == 0


def test_rows_without_ids_dedupe_by_content():
    a = f"Nice  APP {LONG_TEXT}"
    result = _norm(raw(rid=None, text=a), raw(rid=None, text=a.lower()))
    assert len(result.reviews) == 1  # NRM-06: whitespace/case-insensitive hash


def test_word_count_is_unicode_aware():  # NRM-08
    assert word_count("बहुत अच्छा ऐप है") == 4
    assert word_count("Great app 🙏 !!") == 2  # emoji-only tokens are never words
    assert word_count(strip_emojis("Great app 🙏 !!")) == 2


@pytest.mark.parametrize("text,lang", [
    ("The withdrawal took five days to reach my bank account", "en"),
    ("पैसे निकालने में बहुत समय लगता है और सपोर्ट जवाब नहीं देता", "hi"),
    ("ok ok", "und"),
    ("Best app ever for trading", "en"),
    ("Very user friendly", "en"),
    ("ek dikkat hai ap se guzarish hai ki ap ek update karo", "hi-Latn"),
    ("app bahut slow hai market open pe", "hi-Latn"),
    ("TERRIBLE BROKERAGE DEDUCTED FROM MY SMALL PROFIT TODAY, UNREASONABLE CHARGES", "en"),
])
def test_language_detection(text, lang):  # NRM-09
    assert detect_language(text) == lang


def test_window_is_inclusive_at_both_ends():  # NRM-02
    start, end = review_window("2026-W47", 10)
    days = [start, end, date(2026, 9, 13), date(2026, 11, 23)]
    result = normalize(
        [raw(rid=str(i), submitted=datetime(d.year, d.month, d.day, 6, tzinfo=UTC))
         for i, d in enumerate(days)],
        today=date(2026, 11, 30),
    )
    assert sorted(r.review_date for r in in_window(result.reviews, start, end)) == [start, end]
