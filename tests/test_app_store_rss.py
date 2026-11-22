import json
import re
from collections import Counter
from datetime import date

import httpx
import pytest

from pulse.ingest.app_store_rss import FIRST_PAGE_PATHS, PAGE_PATHS, AppStoreRssSource
from pulse.ingest.base import SourceError
from tests.conftest import appstore_entry, appstore_page

SINCE, UNTIL = date(2026, 9, 14), date(2026, 11, 22)
VARIANTS_P1 = len(FIRST_PAGE_PATHS) + len(PAGE_PATHS)
VARIANTS = len(PAGE_PATHS)


def _page_of(request: httpx.Request) -> int:
    m = re.search(r"page=(\d+)", str(request.url))
    return int(m.group(1)) if m else 1


def _source(handler, **kw):
    """handler(request, page, n_calls_so_far) -> Response. Records the page of every call."""
    calls: list[int] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        calls.append(_page_of(request))
        return handler(request, calls[-1], len(calls))

    client = httpx.Client(transport=httpx.MockTransport(wrapped))
    src = AppStoreRssSource("1404871703", "in", user_agent="test-agent", delay_seconds=0,
                            client=client, sleep=lambda s: None, **kw)
    return src, calls


def _by_page(pages: dict):
    return lambda req, page, n: httpx.Response(200, json=appstore_page(pages.get(page)))


def _entries(n, day="2026-11-10", start=0):
    return [appstore_entry(rid=str(start + i), rating=1 + i % 5, text=f"Review number {i} here",
                           updated=f"{day}T10:00:00-07:00") for i in range(n)]


def test_page_url_forms():
    src = AppStoreRssSource("1404871703", "in", user_agent="x")
    p1, p2 = src.page_urls(1), src.page_urls(2)
    assert p1[0] == ("https://itunes.apple.com/in/rss/customerreviews/"
                     "id=1404871703/sortby=mostrecent/json")
    assert len(p1) == VARIANTS_P1 and len(p2) == VARIANTS
    assert all("page=2" in u and "id=1404871703" in u for u in p2)


def test_scans_to_max_pages_and_trailing_empties_are_the_end():  # ING-22
    src, calls = _source(_by_page({1: _entries(3), 2: _entries(2, start=3)}))
    result = src.fetch(SINCE, UNTIL)
    assert len(result) == 5 and result.complete and not result.warnings
    # Every form of each empty page is tried, and nothing is retried after the last data page.
    assert Counter(calls) == {1: 1, 2: 1, **{p: VARIANTS for p in range(3, 11)}}


def test_interior_empty_page_is_retried_and_filled():
    """Observed live: page 7 empty in every URL form while page 8 had data."""
    tries = Counter()

    def handler(req, page, n):
        tries[page] += 1
        if page == 2 and tries[2] <= VARIANTS:  # empty on the first pass only
            return httpx.Response(200, json=appstore_page(None))
        data = {1: _entries(2), 2: _entries(2, start=10), 3: _entries(2, start=20)}
        return httpx.Response(200, json=appstore_page(data.get(page)))

    src, _ = _source(handler)
    result = src.fetch(SINCE, UNTIL)
    assert sorted(r.source_review_id for r in result) == ["0", "1", "10", "11", "20", "21"]
    assert result.complete


def test_interior_gap_that_stays_empty_marks_source_partial():
    src, _ = _source(_by_page({1: _entries(2), 3: _entries(2, start=20)}))
    result = src.fetch(SINCE, UNTIL)
    assert len(result) == 4 and not result.complete
    assert "pages [2] stayed empty" in result.warnings[0]


def test_tries_other_url_forms_when_feed_is_flaky():
    """Each URL form is randomly served an empty feed; another form has the data."""
    def handler(req, page, n):
        flaky_empty = "sortby=mostrecent/json" in str(req.url) and page in (1, 2)
        if flaky_empty or page > 3:
            return httpx.Response(200, json=appstore_page(None))
        return httpx.Response(200, json=appstore_page(_entries(2, start=10 * page)))

    src, calls = _source(handler)
    result = src.fetch(SINCE, UNTIL)
    assert sorted(r.source_review_id for r in result) == ["10", "11", "20", "21", "30", "31"]


def test_user_agent_is_generic():
    seen = set()

    def handler(req, page, n):
        seen.add(req.headers["user-agent"])
        return httpx.Response(200, json=appstore_page(None))

    _source(handler)[0].fetch(SINCE, UNTIL)
    assert seen == {"test-agent"}


def test_single_entry_object_and_metadata_entry():  # ING-21, ING-20
    single = appstore_entry(rid="9", rating=2, text="Only one", updated="2026-11-10T10:00:00Z")
    metadata = {"im:name": {"label": "Groww"}, "id": {"label": "app"}}
    src, _ = _source(_by_page({1: [metadata, *_entries(1)], 2: single}))
    result = src.fetch(SINCE, UNTIL)
    assert [r.source_review_id for r in result] == ["0", "9"]
    assert result.skipped["not_a_review"] == 1


def test_author_is_never_read():
    src, _ = _source(_by_page({1: _entries(2)}))
    dumped = json.dumps([r.to_json() for r in src.fetch(SINCE, UNTIL)])
    assert "Reviewer Person" not in dumped and "example.com" not in dumped


def test_stops_once_a_whole_page_is_older_than_window():  # ING-25
    mixed = _entries(2, day="2026-09-01") + _entries(1, day="2026-10-01", start=10)
    pages = {1: _entries(3), 2: mixed, 3: _entries(3, day="2026-08-01", start=20),
             4: _entries(3, day="2026-07-01", start=30)}
    src, calls = _source(_by_page(pages))
    result = src.fetch(SINCE, UNTIL)
    assert sorted(set(calls)) == [1, 2, 3]  # page 2 still had an in-window review; 3 did not
    assert len(result) == 9  # the adapter only maps; normalize drops the old ones


def test_retries_transient_errors_then_succeeds():  # ING-23
    html = httpx.Response(200, text="<html>oops</html>", headers={"content-type": "text/html"})
    first = [httpx.Response(429), html]

    def handler(req, page, n):
        if n <= len(first):
            return first[n - 1]
        return httpx.Response(200, json=appstore_page(_entries(1) if page == 1 else None))

    src, calls = _source(handler)
    assert len(src.fetch(SINCE, UNTIL)) == 1
    assert calls[:3] == [1, 1, 1]


def test_first_page_failure_fails_the_source():
    src, calls = _source(lambda req, page, n: httpx.Response(503), max_retries=2)
    with pytest.raises(SourceError, match="HTTP 503 after 3 attempts"):
        src.fetch(SINCE, UNTIL)
    assert len(calls) == 3  # an outage does not fan out across URL forms


def test_non_retryable_status_fails_fast():
    src, calls = _source(lambda req, page, n: httpx.Response(404))
    with pytest.raises(SourceError, match="HTTP 404"):
        src.fetch(SINCE, UNTIL)
    assert len(calls) == 1


def test_later_page_failure_keeps_earlier_reviews():
    def handler(req, page, n):
        if page == 1:
            return httpx.Response(200, json=appstore_page(_entries(4)))
        return httpx.Response(500)

    src, _ = _source(handler, max_retries=1)
    result = src.fetch(SINCE, UNTIL)
    assert len(result) == 4 and not result.complete
    assert "page 2 failed" in result.warnings[0]


def test_network_error_is_retried():
    def handler(req, page, n):
        if n == 1:
            raise httpx.ConnectError("boom", request=req)
        return httpx.Response(200, json=appstore_page(None))

    src, calls = _source(handler)
    result = src.fetch(SINCE, UNTIL)
    assert "0 reviews" in result.warnings[-1]  # ING-24
    assert len(calls) == 1 + VARIANTS_P1 + 9 * VARIANTS


def test_respects_max_pages():
    src, calls = _source(lambda req, page, n: httpx.Response(
        200, json=appstore_page(_entries(1, start=page))), max_pages=2)
    assert len(src.fetch(SINCE, UNTIL)) == 2 and sorted(set(calls)) == [1, 2]
