import json
import ssl
from datetime import UTC, date, datetime
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from pulse.ingest import play_public
from pulse.ingest.base import SourceError
from pulse.ingest.play_public import PlayStorePublicSource

SINCE, UNTIL = date(2026, 9, 14), date(2026, 11, 22)


def _ts(y, m, d, h=6):
    return int(datetime(y, m, d, h, tzinfo=UTC).timestamp())


def gp_item(rid, score=3, text="Withdrawal took five days", ts=None, user="Secret Name"):
    """One review in Google Play's positional format (see ElementSpecs.Review)."""
    item = [None] * 11
    item[0] = rid
    item[1] = [user, [None, None, None, [None, None, "https://img.example/avatar"]]]
    item[2] = score
    item[4] = text
    item[5] = [ts or _ts(2026, 11, 10), 0]
    item[6] = 7
    item[7] = [None, "Hi Secret Name, sorry about that", [ts or _ts(2026, 11, 10), 0]]
    item[10] = "5.12.0"
    return item


def gp_response(items, token=None) -> str:
    data = [items, None, [None, token] if token else None, None]
    envelope = [["wrb.fr", "oCPfdb", json.dumps(data), None, None, None, "generic"]]
    return ")]}'\n\n" + json.dumps(envelope)


def _source(handler, **kw):
    """handler(lang, token, n) -> Response; records (lang, token) per call."""
    calls: list[tuple[str, str | None]] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        lang = parse_qs(urlparse(str(request.url)).query)["hl"][0]
        body = request.content.decode()
        token = body.split("%5C%22")[1] if "%2Cnull%2C%5C%22" in body else None
        calls.append((lang, token))
        return handler(lang, token, len(calls))

    client = httpx.Client(transport=httpx.MockTransport(wrapped))
    kw.setdefault("langs", ["en"])
    src = PlayStorePublicSource("com.nextbillion.groww", user_agent="test-agent",
                                delay_seconds=0, client=client, sleep=lambda s: None, **kw)
    return src, calls


def _pages(pages: dict):
    """pages: {(lang, token): (items, next_token)}"""
    def handler(lang, token, n):
        items, nxt = pages.get((lang, token), ([], None))
        return httpx.Response(200, text=gp_response(items, nxt))
    return handler


def test_importing_restores_tls_verification():
    assert ssl._create_default_https_context is ssl.create_default_context
    assert play_public._ssl_default is ssl.create_default_context


def test_maps_fields_and_never_reads_identity():
    ts = _ts(2026, 11, 10, 18)
    src, _ = _source(_pages({("en", None): ([gp_item("g1", 1, "App froze at 9:15", ts)], None)}))
    [r] = src.fetch(SINCE, UNTIL).reviews
    assert (r.source_review_id, r.rating, r.text, r.app_version) == (
        "g1", 1, "App froze at 9:15", "5.12.0")
    assert r.submitted_at == datetime(2026, 11, 10, 18, tzinfo=UTC)  # not shifted by local tz
    dumped = json.dumps(r.to_json())
    assert "Secret Name" not in dumped and "avatar" not in dumped and "sorry" not in dumped


def test_pages_with_token_until_past_window_start():
    pages = {
        ("en", None): ([gp_item("a", ts=_ts(2026, 11, 20)), gp_item("b", ts=_ts(2026, 10, 1))],
                       "tok1"),
        ("en", "tok1"): ([gp_item("c", ts=_ts(2026, 9, 20)), gp_item("d", ts=_ts(2026, 9, 1))],
                         "tok2"),
        ("en", "tok2"): ([gp_item("e", ts=_ts(2026, 8, 1))], None),
    }
    src, calls = _source(_pages(pages))
    result = src.fetch(SINCE, UNTIL)
    assert [r.source_review_id for r in result] == ["a", "b", "c", "d"]
    assert calls == [("en", None), ("en", "tok1")]  # stopped once a page reached 1 Sep
    assert result.complete and result.covered_since == SINCE


def test_merges_languages_and_drops_cross_language_duplicates():
    old = _ts(2026, 9, 1)
    pages = {
        ("en", None): ([gp_item("a"), gp_item("shared"), gp_item("x", ts=old)], None),
        ("hi", None): ([gp_item("shared"), gp_item("h1", text="बहुत अच्छा"),
                        gp_item("y", ts=old)], None),
    }
    src, _ = _source(_pages(pages), langs=["en", "hi"])
    result = src.fetch(SINCE, UNTIL)
    assert sorted(r.source_review_id for r in result) == ["a", "h1", "shared", "x", "y"]
    assert result.skipped["duplicate_across_langs"] == 1
    assert result.covered_since == SINCE


def test_listing_that_ends_before_window_start_is_flagged():
    src, _ = _source(_pages({("en", None): ([gp_item("a")], None)}))
    result = src.fetch(SINCE, UNTIL)
    assert len(result) == 1 and result.covered_since is None
    assert "ran out of reviews before" in result.warnings[0]


def test_rate_limit_marker_is_retried():
    responses = iter([
        httpx.Response(200, text="...com.google.play.gateway.proto.PlayGatewayError..."),
        httpx.Response(200, text=gp_response([gp_item("a", ts=_ts(2026, 9, 1))])),
    ])
    src, calls = _source(lambda lang, token, n: next(responses))
    assert len(src.fetch(SINCE, UNTIL)) == 1 and len(calls) == 2


def test_unexpected_format_fails_after_retries():
    src, calls = _source(lambda *a: httpx.Response(200, text="<html>consent</html>"),
                         max_retries=2)
    with pytest.raises(SourceError, match="unexpected response format"):
        src.fetch(SINCE, UNTIL)
    assert len(calls) == 3


def test_app_not_found():
    src, calls = _source(lambda *a: httpx.Response(404))
    with pytest.raises(SourceError, match="not found"):
        src.fetch(SINCE, UNTIL)
    assert len(calls) == 1


def test_secondary_language_failure_is_partial():
    def handler(lang, token, n):
        if lang == "hi":
            return httpx.Response(503)
        return httpx.Response(200, text=gp_response([gp_item("a", ts=_ts(2026, 9, 1))]))

    src, _ = _source(handler, langs=["en", "hi"], max_retries=1)
    result = src.fetch(SINCE, UNTIL)
    assert len(result) == 1 and not result.complete and result.covered_since is None
    assert result.warnings[0].startswith("hl=hi: HTTP 503")


def test_mid_paging_failure_keeps_earlier_pages():
    def handler(lang, token, n):
        if token is None:
            return httpx.Response(200, text=gp_response([gp_item("a")], "tok1"))
        return httpx.Response(500)

    src, _ = _source(handler, max_retries=0)
    result = src.fetch(SINCE, UNTIL)
    assert len(result) == 1 and not result.complete


def test_max_reviews_cap_stops_and_flags():
    def handler(lang, token, n):
        return httpx.Response(200, text=gp_response(
            [gp_item(f"{n}-{i}") for i in range(100)], f"tok{n}"))

    src, calls = _source(handler, max_reviews=250)
    result = src.fetch(SINCE, UNTIL)
    assert len(result) == 300 and len(calls) == 3 and not result.complete
    assert "max_reviews=250" in result.warnings[0]


def test_sends_generic_user_agent_and_requested_country():
    seen = []

    def handler(request):
        seen.append((request.headers["user-agent"], str(request.url)))
        return httpx.Response(200, text=gp_response([gp_item("a", ts=_ts(2026, 9, 1))]))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    PlayStorePublicSource("com.nextbillion.groww", langs=["en"], user_agent="test-agent",
                          client=client, sleep=lambda s: None).fetch(SINCE, UNTIL)
    ua, url = seen[0]
    assert ua == "test-agent" and "gl=in" in url and "hl=en" in url


def test_disabled_source_refuses():
    with pytest.raises(SourceError, match="disabled"):
        PlayStorePublicSource("com.nextbillion.groww", enabled=False)
