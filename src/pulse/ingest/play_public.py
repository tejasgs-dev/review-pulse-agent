"""PlayStorePublicSource: reviews from the public Play Store listing (§4.1, §13 #2).

Approved for this course project on 2026-11-21: public listing only, no login,
rate-limited, and only the reviews the review window needs.

It uses google-play-scraper's request format and response parser, but sends the
requests itself with httpx. The library's own `reviews()` helper is avoided because:
- it swallows every exception, so a block or an outage looks like "no more reviews";
- it sets no timeout (an unattended run could hang) and no User-Agent;
- importing it disables TLS certificate checks process-wide
  (`ssl._create_default_https_context = ssl._create_unverified_context`).
  The original default is restored right after import below.
The library's internals are used, so its version is pinned in pyproject.toml.
"""

from __future__ import annotations

import contextlib
import json
import ssl
import time
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

import httpx

from pulse.ingest.base import FetchResult, RawReview, SourceError
from pulse.ingest.parsing import clean_str, parse_rating
from pulse.weeks import IST

_ssl_default = ssl._create_default_https_context
try:
    from google_play_scraper import Sort
    from google_play_scraper.constants.element import ElementSpecs
    from google_play_scraper.constants.regex import Regex
    from google_play_scraper.constants.request import Formats
finally:
    ssl._create_default_https_context = _ssl_default  # undo the library's TLS downgrade

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
RATE_LIMIT_MARKER = "PlayGatewayError"
# The only fields ever extracted. userName, userImage and reply fields are never read.
_FIELDS = ("reviewId", "score", "content", "at", "appVersion", "reviewCreatedVersion")


class PlayStorePublicSource:
    name = "play_store"

    def __init__(
        self,
        package: str,
        *,
        enabled: bool = True,
        langs: tuple[str, ...] | list[str] = ("en",),
        country: str = "in",
        user_agent: str = "review-pulse",
        delay_seconds: float = 1.5,
        batch_size: int = 200,
        max_reviews: int = 40_000,
        max_retries: int = 3,
        timeout_seconds: float = 30.0,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not enabled:
            raise SourceError(
                "the public Play Store fetcher is disabled (ingest.play_public.enabled)"
            )
        if not langs:
            raise SourceError("ingest.play_public.langs is empty")
        self.package = package
        self.langs = tuple(langs)
        self.country = country
        self.user_agent = user_agent
        self.delay = delay_seconds
        self.batch_size = batch_size
        self.max_reviews = max_reviews
        self.max_retries = max_retries
        self.timeout = timeout_seconds
        self._client = client
        self._sleep = sleep

    def fetch(self, since: date, until: date) -> FetchResult:
        """Newest-first pages for each language until a page reaches back past `since`."""
        result = FetchResult(source="play_store")
        seen: set[str] = set()
        reached_all = True
        owned = self._client is None
        client = self._client or httpx.Client(follow_redirects=True)
        with client if owned else contextlib.nullcontext(client):
            for i, lang in enumerate(self.langs):
                if i:
                    self._sleep(self.delay)
                before = len(result)
                try:
                    reached = self._fetch_lang(client, lang, since, result, seen)
                except SourceError as e:
                    if i == 0 and len(result) == before:
                        raise  # the primary language failed outright: the source failed
                    result.warnings.append(
                        f"hl={lang}: {e}; kept {len(result) - before} reviews from it"
                    )
                    result.complete = False
                    reached_all = False
                    continue
                except _CapReached:
                    result.warnings.append(
                        f"stopped at max_reviews={self.max_reviews} before reaching {since}; "
                        "older reviews in the window are missing"
                    )
                    result.complete = False
                    reached_all = False
                    break
                if not reached:
                    reached_all = False
                    result.warnings.append(
                        f"hl={lang}: the listing ran out of reviews before {since}"
                    )
        if not result.reviews:
            result.warnings.append(
                f"0 reviews from the Play Store listing for {self.package}; "
                "check the package ID, or whether Google Play is blocking requests"
            )
        result.covered_since = since if (reached_all and result.complete) else None
        return result

    # -- paging ---------------------------------------------------------------

    def _fetch_lang(
        self, client: httpx.Client, lang: str, since: date, result: FetchResult, seen: set[str]
    ) -> bool:
        """Page one language. Returns True once a page reaches back past `since`."""
        token: str | None = None
        first = True
        while True:
            if not first:
                self._sleep(self.delay)
            first = False
            items, token = self._request(client, lang, token)
            batch = [r for item in items if (r := _map(item)) is not None]
            for r in batch:
                if r.source_review_id in seen:  # the same review listed under two hl= values
                    result.skipped["duplicate_across_langs"] += 1
                    continue
                if r.source_review_id:
                    seen.add(r.source_review_id)
                result.reviews.append(r)
            dates = [r.submitted_at for r in batch if r.submitted_at]
            if dates and min(dates).astimezone(IST).date() < since:
                return True
            if not items or token is None:
                return False
            if len(result) >= self.max_reviews:
                raise _CapReached

    def _request(
        self, client: httpx.Client, lang: str, token: str | None
    ) -> tuple[list[Any], str | None]:
        url = Formats.Reviews.build(lang=lang, country=self.country)
        body = Formats.Reviews.build_body(
            self.package, Sort.NEWEST.value, self.batch_size, "null", "null", token
        )
        headers = {
            "content-type": "application/x-www-form-urlencoded",
            "User-Agent": self.user_agent,
        }
        error = "unknown error"
        for attempt in range(self.max_retries + 1):
            try:
                resp = client.post(url, content=body, headers=headers, timeout=self.timeout)
            except httpx.TransportError as e:
                error = f"network error ({type(e).__name__})"
            else:
                if resp.status_code in RETRYABLE_STATUS:
                    error = f"HTTP {resp.status_code}"
                elif resp.status_code == 404:
                    raise SourceError(f"app {self.package} not found on Google Play (HTTP 404)")
                elif resp.status_code >= 400:
                    raise SourceError(f"HTTP {resp.status_code}")
                elif RATE_LIMIT_MARKER in resp.text:
                    error = "rate limited by Google Play"
                else:
                    try:
                        return _parse(resp.text)
                    except ValueError as e:
                        error = f"unexpected response format ({e})"
            if attempt < self.max_retries:
                self._sleep(max(self.delay, 1.0) * 2 ** (attempt + 1))
        raise SourceError(f"{error} after {self.max_retries + 1} attempts")


class _CapReached(Exception):
    pass


def _parse(text: str) -> tuple[list[Any], str | None]:
    """Split a batchexecute response into (review items, next-page token)."""
    found = Regex.REVIEWS.findall(text)
    if not found:
        raise ValueError("no review payload in response")
    try:
        envelope = json.loads(found[0])
        payload = envelope[0][2]
        if payload is None:  # nothing (more) to return
            return [], None
        data = json.loads(payload)
    except (json.JSONDecodeError, IndexError, TypeError) as e:
        raise ValueError(type(e).__name__) from e
    items = data[0] if data and isinstance(data[0], list) else []
    try:
        token = data[-2][-1]
    except (IndexError, TypeError):
        token = None
    return items, token if isinstance(token, str) else None


def _field(item: Any, name: str) -> Any:
    try:
        return ElementSpecs.Review[name].extract_content(item)
    except Exception:
        return None


def _map(item: Any) -> RawReview | None:
    if not isinstance(item, list):
        return None
    values = {name: _field(item, name) for name in _FIELDS}
    at = values["at"]
    # The library builds `at` with datetime.fromtimestamp(), a naive *local* time.
    # astimezone() reads a naive datetime as local time, which inverts that exactly.
    submitted = at.astimezone(UTC) if isinstance(at, datetime) else None
    return RawReview(
        source="play_store",
        source_review_id=clean_str(values["reviewId"]),
        rating=parse_rating(values["score"]),
        title=None,  # Play reviews have no title
        text=clean_str(values["content"]),
        submitted_at=submitted,
        updated_at=None,
        app_version=clean_str(values["appVersion"] or values["reviewCreatedVersion"]),
    )
