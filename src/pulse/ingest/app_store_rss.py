"""AppStoreRssSource: Apple's public customer-reviews JSON feed (§4.1).

Observed feed behavior (2026-11-21, country=in): the path segments (`page=`, `id=`,
`sortby=`) can appear in any order, and each URL form is independently and
intermittently served an *empty* feed (HTTP 200, no `entry`), with which form works
changing from minute to minute. Sometimes every form of a page is empty while a
later page still has data. So the adapter:
- tries several equivalent URL forms per page until one has entries,
- keeps paging past empty pages (up to max_pages) instead of stopping at the first,
- re-tries interior empty pages once, and reports the source as partial if a gap remains.
Paging stops early once a whole page is older than the window.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable
from datetime import date
from typing import Any

import httpx

from pulse.ingest.base import FetchResult, RawReview, SourceError
from pulse.ingest.parsing import clean_str, parse_rating, parse_timestamp
from pulse.weeks import IST

BASE_URL = "https://itunes.apple.com/{country}/rss/customerreviews/"
# Equivalent path orders, tried in turn while a page comes back empty.
PAGE_PATHS = (
    "page={page}/id={app_id}/sortby=mostrecent/json",
    "id={app_id}/page={page}/sortby=mostrecent/json",
    "page={page}/sortby=mostrecent/id={app_id}/json",
    "sortby=mostrecent/page={page}/id={app_id}/json",
)
FIRST_PAGE_PATHS = (  # page 1 also answers without a page= segment
    "id={app_id}/sortby=mostrecent/json",
    "sortby=mostrecent/id={app_id}/json",
)
RETRYABLE_STATUS = frozenset({403, 429, 500, 502, 503, 504})


class AppStoreRssSource:
    name = "app_store"

    def __init__(
        self,
        app_id: str,
        country: str,
        *,
        user_agent: str,
        delay_seconds: float = 1.5,
        max_pages: int = 10,
        max_retries: int = 3,
        timeout_seconds: float = 20.0,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.app_id = app_id
        self.country = country
        self.user_agent = user_agent
        self.delay = delay_seconds
        self.max_pages = max_pages
        self.max_retries = max_retries
        self.timeout = timeout_seconds
        self._client = client
        self._sleep = sleep

    def page_urls(self, page: int) -> list[str]:
        paths = (FIRST_PAGE_PATHS + PAGE_PATHS) if page == 1 else PAGE_PATHS
        base = BASE_URL.format(country=self.country)
        return [base + p.format(app_id=self.app_id, page=page) for p in paths]

    def _fetch_page(self, client: httpx.Client, page: int) -> list[dict[str, Any]]:
        """Entries for one page, trying each URL form until one is non-empty.

        An HTTP or network failure (after retries) raises at once: that is an outage,
        not cache variance, so trying other URL forms would only add load.
        """
        for i, url in enumerate(self.page_urls(page)):
            if i:
                self._sleep(self.delay)
            entries = _entries(self._get_json(client, url, page))
            if entries:
                return entries
        return []

    def fetch(self, since: date, until: date) -> FetchResult:
        result = FetchResult(source="app_store")
        owned = self._client is None
        client = self._client or httpx.Client(follow_redirects=True)
        empty_pages: list[int] = []
        last_data_page = 0
        reached_since = False
        with client if owned else contextlib.nullcontext(client):
            for page in range(1, self.max_pages + 1):
                if page > 1:
                    self._sleep(self.delay)
                try:
                    entries = self._fetch_page(client, page)
                except SourceError as e:
                    if page == 1:
                        raise
                    result.warnings.append(
                        f"page {page} failed ({e}); kept {len(result)} reviews from earlier pages"
                    )
                    result.complete = False
                    break
                if not entries:
                    # An empty page is not proof of the end: a later page can still have
                    # data (observed: page 7 empty in every form, page 8 not). Keep going.
                    empty_pages.append(page)
                    continue
                last_data_page = page
                if self._add_page(entries, result, since):
                    reached_since = True
                    break

            # Interior gaps (empty pages followed by data) get one more try.
            missing = []
            for page in (p for p in empty_pages if p < last_data_page):
                self._sleep(self.delay)
                entries = self._fetch_page(client, page)
                if entries:
                    self._add_page(entries, result, since)
                else:
                    missing.append(page)
            if missing:
                result.warnings.append(
                    f"feed pages {missing} stayed empty although later pages had data; "
                    f"up to {50 * len(missing)} reviews may be missing"
                )
                result.complete = False
        result.covered_since = since if (reached_since and result.complete) else None

        if not result.reviews:  # ING-24
            result.warnings.append(
                f"0 reviews from the App Store feed; check app_store_id={self.app_id} "
                f"and country={self.country}"
            )
        return result

    @staticmethod
    def _add_page(entries: list[dict[str, Any]], result: FetchResult, since: date) -> bool:
        """Map a page into `result`. Returns True when paging can stop.

        ING-25: the feed is only roughly date-ordered, so stop only once the newest
        review on a whole page is older than the window.
        """
        page_reviews = [r for e in entries if (r := _map(e, result)) is not None]
        result.reviews.extend(page_reviews)
        dates = [r.submitted_at for r in page_reviews if r.submitted_at]
        return bool(dates) and max(dates).astimezone(IST).date() < since

    def _get_json(self, client: httpx.Client, url: str, page: int) -> dict[str, Any]:
        headers = {"User-Agent": self.user_agent, "Accept": "application/json"}
        error = "unknown error"
        for attempt in range(self.max_retries + 1):
            try:
                resp = client.get(url, headers=headers, timeout=self.timeout)
            except httpx.TransportError as e:
                error = f"network error ({type(e).__name__})"
            else:
                if resp.status_code in RETRYABLE_STATUS:
                    error = f"HTTP {resp.status_code}"
                elif resp.status_code >= 400:
                    raise SourceError(f"HTTP {resp.status_code} for page {page}")
                else:
                    try:
                        payload = resp.json()
                    except ValueError:  # ING-23: an HTML error page, not JSON
                        ctype = resp.headers.get("content-type", "unknown type")
                        error = f"response was not JSON ({ctype})"
                    else:
                        if isinstance(payload, dict) and isinstance(payload.get("feed"), dict):
                            return payload
                        error = "JSON had no 'feed' object"
            if attempt < self.max_retries:
                self._sleep(max(self.delay, 1.0) * 2**attempt)
        raise SourceError(f"{error} after {self.max_retries + 1} attempts")


def _entries(payload: dict[str, Any]) -> list[dict[str, Any]]:
    entry = payload["feed"].get("entry")
    if entry is None:
        return []
    if isinstance(entry, dict):  # ING-21: a page with one entry is an object, not a list
        return [entry]
    return [e for e in entry if isinstance(e, dict)]


def _label(entry: dict[str, Any], key: str) -> str | None:
    v = entry.get(key)
    return clean_str(v.get("label")) if isinstance(v, dict) else None


def _map(entry: dict[str, Any], result: FetchResult) -> RawReview | None:
    if "im:rating" not in entry:  # ING-20: app metadata entry, not a review
        result.skipped["not_a_review"] += 1
        return None
    # The author block is never read.
    return RawReview(
        source="app_store",
        source_review_id=_label(entry, "id"),
        rating=parse_rating(_label(entry, "im:rating")),
        title=_label(entry, "title"),
        text=_label(entry, "content"),
        submitted_at=parse_timestamp(_label(entry, "updated")),
        updated_at=None,
        app_version=_label(entry, "im:version"),
    )
