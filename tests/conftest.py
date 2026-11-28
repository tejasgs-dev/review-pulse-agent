"""Shared builders for synthetic test data. No real user data anywhere in tests/."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from pulse.config import DEFAULT_CONFIG_PATH, PulseConfig, load_config
from pulse.ingest.base import RawReview

PLAY_HEADER = [
    "Package Name", "App Version Code", "App Version Name", "Reviewer Language", "Device",
    "Review Submit Date and Time", "Review Submit Millis Since Epoch",
    "Review Last Update Date and Time", "Review Last Update Millis Since Epoch",
    "Star Rating", "Review Title", "Review Text", "Developer Reply Date and Time",
    "Developer Reply Millis Since Epoch", "Developer Reply Text", "Review Link",
]


def play_row(*, rid: str, rating: str, text: str, submitted: str, updated: str = "",
             device: str = "Pixel 7", reply: str = "") -> list[str]:
    """A row shaped like a Play Console review export."""
    return [
        "com.nextbillion.groww", "512", "5.12.0", "en", device, submitted, "", updated, "",
        rating, "", text, "", "", reply, f"https://play.google.com/review?id={rid}",
    ]


# The shared Doc in publishing tests (the fake server starts with it, as if made by hand).
TEST_DOC_ID = "TestDoc_0123456789abcdefghij"

# Default review text: English and longer than the 9-word minimum.
LONG_TEXT = "Works well for me daily and the mutual fund screens are easy to use"


def raw(
    *, rid: str | None = "r1", rating: int | None = 4, text: str | None = LONG_TEXT,
    submitted: datetime | None = datetime(2026, 11, 10, 6, 0, tzinfo=UTC),
    updated: datetime | None = None, source: str = "play_store", title: str | None = None,
) -> RawReview:
    return RawReview(source=source, source_review_id=rid, rating=rating, title=title, text=text,
                     submitted_at=submitted, updated_at=updated, app_version="5.12.0")


def appstore_entry(*, rid: str, rating: int, text: str, updated: str, title: str = "Title",
                   author: str = "Reviewer Person") -> dict:
    """An entry shaped like Apple's customer-reviews JSON feed."""
    return {
        "author": {"name": {"label": author}, "uri": {"label": f"https://example.com/{author}"}},
        "updated": {"label": updated},
        "im:rating": {"label": str(rating)},
        "im:version": {"label": "5.12.0"},
        "id": {"label": rid},
        "title": {"label": title},
        "content": {"label": text, "attributes": {"type": "text"}},
        "im:voteSum": {"label": "0"},
    }


def appstore_page(entries: list[dict] | dict | None) -> dict:
    feed: dict = {"author": {"name": {"label": "iTunes Store"}}, "id": {"label": "x"}}
    if entries is not None:
        feed["entry"] = entries
    return {"feed": feed}


def to_json_bytes(obj: object) -> bytes:
    return json.dumps(obj).encode()


@pytest.fixture
def cfg(tmp_config) -> PulseConfig:
    """The default config, with data/ and runs/ redirected into the test's tmp_path."""
    return load_config(tmp_config(), load_env_file=False)


@pytest.fixture
def tmp_config(tmp_path: Path):
    """Write a copy of the default config with data/runs pointing into tmp_path."""

    def make(mutate=None) -> Path:
        data = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
        data["paths"] = {"data_dir": str(tmp_path / "data"), "runs_dir": str(tmp_path / "runs")}
        if mutate:
            mutate(data)
        p = tmp_path / "pulse.yaml"
        p.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
        return p

    return make


@pytest.fixture(autouse=True)
def _not_on_github_actions(monkeypatch):
    """CI runs these tests on GitHub Actions: behave as a local run unless a test sets these,
    and never write to the real job summary."""
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)


@pytest.fixture(autouse=True)
def _no_real_llm(request, monkeypatch):
    """Offline tests must never reach the real Claude API (it costs money). Tests that need a
    model patch `build_llm` with a fake; `@pytest.mark.slow` tests are exempt."""
    if request.node.get_closest_marker("slow"):
        return

    def refuse(*args, **kwargs):
        raise RuntimeError("tests must not call a real model API; patch build_llm")

    monkeypatch.setattr("pulse.analysis.llm.build_llm", refuse)
    # Belt and braces: a GroqClient built without a fake client fails with kind "auth".
    monkeypatch.delenv("GROQ_API_KEY", raising=False)


@pytest.fixture(autouse=True)
def _no_real_embedder(request, monkeypatch):
    """Offline tests never download the embedding model: they get a hashing stub instead.
    `@pytest.mark.slow` tests use the real model."""
    if request.node.get_closest_marker("slow"):
        return
    from tests.stub_embedder import HashingEmbedder

    monkeypatch.setattr("pulse.analysis.engines.local_cluster.build_embedder",
                        lambda cfg: HashingEmbedder())


@pytest.fixture(autouse=True)
def _no_real_mcp(request, monkeypatch):
    """Offline tests never reach a real MCP server (it would write to a real Google Doc and
    send real email). Tests that publish use the `fake_mcp` fixture instead."""
    if request.node.get_closest_marker("slow"):
        return

    def refuse(*args, **kwargs):
        raise RuntimeError("tests must not reach a real MCP server; use the fake_mcp fixture")

    monkeypatch.setattr("pulse.publish.mcp_client.open_client", refuse)
    monkeypatch.setattr("pulse.publish.mcp_client.wait_until_healthy", refuse)


@pytest.fixture
def fake_mcp(monkeypatch):
    """An in-process fake of gworkspace-mcp wired in place of the real connection."""
    from contextlib import asynccontextmanager

    from mcp import Client

    from tests.fake_mcp import FakeWorkspace

    ws = FakeWorkspace()
    ws.docs[TEST_DOC_ID] = {"title": "ReviewPulse: Groww app reviews",
                            "content": "ReviewPulse: Groww app reviews\n"}
    ws.connections = 0

    @asynccontextmanager
    async def open_client(server):
        ws.connections += 1
        async with Client(ws.server()) as client:
            yield client

    monkeypatch.setattr("pulse.publish.mcp_client.open_client", open_client)
    monkeypatch.setattr("pulse.publish.mcp_client.wait_until_healthy", lambda server: 0.0)
    return ws


@pytest.fixture
def publish_config(tmp_config, monkeypatch):
    """A config ready to publish: a recipient, the Doc ID and an API key, with the fake
    server. Retries keep the default count but do not wait."""
    monkeypatch.setenv("PULSE_RECIPIENT", "me@example.com")
    monkeypatch.setenv("PULSE_MCP_API_KEY", "test-key-0123456789-0123456789-xyz")
    monkeypatch.setenv("PULSE_DOC_ID", TEST_DOC_ID)

    def make(mutate=None) -> Path:
        def both(d):
            d["mcp"]["retry_delay_seconds"] = 0
            if mutate:
                mutate(d)
        return tmp_config(both)

    return make
