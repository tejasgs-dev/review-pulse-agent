"""CLI with the default local engine: no API key, no network (stub embedder from conftest)."""

import json
import re
from datetime import UTC, datetime

from typer.testing import CliRunner

from pulse.cli import ExitCode, app
from pulse.ingest.store import ReviewStore
from pulse.models import Review
from tests.local_corpus import corpus

runner = CliRunner()


def store_corpus(tmp_path):
    reviews = [Review(key=r.key, source=r.source, rating=r.rating, title=None,
                      text=r.canonical_text, review_date=r.review_date, language="en",
                      app_version=None,
                      updated_at=datetime(r.review_date.year, r.review_date.month,
                                          r.review_date.day, tzinfo=UTC))
               for r in corpus()]
    ReviewStore(tmp_path / "data" / "reviews.parquet").upsert(reviews)


def test_dry_run_with_local_engine(tmp_config, tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    store_corpus(tmp_path)
    result = runner.invoke(app, ["run", "--week", "2026-W46", "--dry-run", "--skip-ingest",
                                 "--config", str(tmp_config())])
    assert result.exit_code == ExitCode.OK, result.output
    assert "local engine" in result.output and "no API calls" in result.output
    week = tmp_path / "runs" / "2026-W46"
    md = (week / "pulse.md").read_text(encoding="utf-8")
    assert md.count("• “") == 3 and "Action Ideas" in md
    assert "general praise" in md.splitlines()[1]
    # Actions carry this week's figures.
    actions = md.split("Action Ideas", 1)[1]
    assert re.search(r"\d", actions)
    themes = json.loads((week / "themes.json").read_text(encoding="utf-8"))
    assert themes["engine"] == "local" and len(themes["themes"]) <= 5
    manifest = json.loads((week / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["cluster"]["engine"] == "local" and "usage" not in manifest["cluster"]
    assert manifest["compose"]["status"] == "ok"
    assert not (week / "llm").exists()          # nothing sent to any API


def test_engine_option_is_validated(tmp_config):
    result = runner.invoke(app, ["analyze", "--engine", "gpt", "--config", str(tmp_config())])
    assert result.exit_code != 0 and "must be local or llm" in result.output


def test_doctor_checks_the_local_engine(tmp_config, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    result = runner.invoke(app, ["doctor", "--config", str(tmp_config())])
    assert "analysis engine: local" in result.output
    assert "theme catalogue is valid" in result.output
    assert "ANTHROPIC_API_KEY" not in result.output      # not needed by the local engine
    assert "embedding model is not cached yet" in result.output  # tmp data dir is empty


def test_doctor_checks_the_api_key_for_the_llm_engine(tmp_config, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    path = tmp_config(lambda d: d["analysis"].__setitem__("engine", "llm"))
    result = runner.invoke(app, ["doctor", "--config", str(path)])
    assert "ANTHROPIC_API_KEY is set (LLM engine)" in result.output
