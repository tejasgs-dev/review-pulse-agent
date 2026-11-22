from typer.testing import CliRunner

from pulse.cli import ExitCode, app

runner = CliRunner()


def test_help_lists_commands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("run", "ingest", "analyze", "publish", "doctor"):
        assert cmd in result.output


def test_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "0.1.0" in result.output


def test_publish_is_not_implemented_yet(tmp_config):
    result = runner.invoke(app, ["publish", "--week", "2026-W47", "--config", str(tmp_config())])
    assert result.exit_code == ExitCode.NOT_IMPLEMENTED


def test_run_dry_run_end_to_end(tmp_config, tmp_path, monkeypatch):
    """Stages 3–6 with the model faked: writes pulse.md, pulse.json, validation.json."""
    import json

    path = tmp_config(lambda d: d["analysis"].__setitem__("min_reviews", 2))
    _store_three_reviews(tmp_path)
    _fake_llm(monkeypatch)
    result = runner.invoke(app, ["run", "--week", "2026-W47", "--dry-run", "--skip-ingest",
                                 "--engine", "llm", "--config", str(path)])
    assert result.exit_code == ExitCode.OK, result.output
    assert "Dry run: nothing was published." in result.output
    week = tmp_path / "runs" / "2026-W47"
    md = (week / "pulse.md").read_text(encoding="utf-8")
    assert md.startswith("Groww Weekly Review Pulse — Week of 16 Nov 2026\nSource: 3 reviews")
    assert "Top Themes" in md and "What Users Said" in md and "Action Ideas" in md
    assert sum(x.startswith("• ") and ": “" in x for x in md.splitlines()) == 3
    validation = json.loads((week / "validation.json").read_text(encoding="utf-8"))
    assert validation["ok"] and set(validation["checks"].values()) == {"pass"}
    pulse = json.loads((week / "pulse.json").read_text(encoding="utf-8"))
    assert pulse["email"]["subject"] == "Groww Review Pulse — Week of 16 Nov 2026"
    manifest = json.loads((week / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["compose"]["status"] == "ok"
    assert not (week / "failure.json").exists()


def test_run_without_dry_run_stops_before_publishing(tmp_config, tmp_path, monkeypatch):
    path = tmp_config(lambda d: d["analysis"].__setitem__("min_reviews", 2))
    _store_three_reviews(tmp_path)
    _fake_llm(monkeypatch)
    result = runner.invoke(app, ["run", "--week", "2026-W47", "--skip-ingest",
                                 "--engine", "llm", "--config", str(path)])
    assert result.exit_code == ExitCode.NOT_IMPLEMENTED
    assert (tmp_path / "runs" / "2026-W47" / "pulse.md").exists()


def test_run_writes_failure_json_when_validation_keeps_failing(tmp_config, tmp_path,
                                                                monkeypatch):
    import json

    from pulse.analysis.compose import ActionDraft, PulseWriting, ThemeSummary

    path = tmp_config(lambda d: d["analysis"].__setitem__("min_reviews", 2))
    _store_three_reviews(tmp_path)
    fake = _fake_llm(monkeypatch)
    # Always one action short: every attempt fails the action check.
    fake.write = lambda call: PulseWriting(
        summaries=[ThemeSummary(theme_id=t, summary="Refunds are slow")
                   for t in ("a_theme", "b_theme", "c_theme")],
        actions=[ActionDraft(theme_id="a_theme", action="Show refund status", signal="Fewer"),
                 ActionDraft(theme_id="b_theme", action="Show refund status", signal="Fewer")])
    result = runner.invoke(app, ["run", "--week", "2026-W47", "--dry-run", "--skip-ingest",
                                 "--engine", "llm", "--config", str(path)])
    assert result.exit_code == ExitCode.VALIDATION_FAILED
    week = tmp_path / "runs" / "2026-W47"
    failure = json.loads((week / "failure.json").read_text(encoding="utf-8"))
    assert failure["published"] is False and len(failure["attempts"]) == 3
    assert not (week / "pulse.md").exists()
    assert sum(c["schema"] == "PulseWriting" for c in fake.calls) == 3
    assert sum(c["schema"] == "QuotePicks" for c in fake.calls) == 1  # quotes were fine


def test_invalid_week_rejected():
    result = runner.invoke(app, ["analyze", "--week", "2026-W54"])
    assert result.exit_code != 0
    assert "does not exist" in result.output


def test_invalid_source_rejected():
    result = runner.invoke(app, ["ingest", "--source", "twitter"])
    assert result.exit_code != 0


def test_weeks_back_range_enforced():
    result = runner.invoke(app, ["run", "--weeks-back", "13"])
    assert result.exit_code != 0


def test_doctor_reports_missing_setup(monkeypatch):
    for var in ("ANTHROPIC_API_KEY", "PULSE_RECIPIENT", "PULSE_DOCS_FOLDER_ID"):
        monkeypatch.setenv(var, "")
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == ExitCode.DOCTOR_FAILED
    assert "MCP server 'docs' is configured" in result.output


def _store_three_reviews(tmp_path):
    from datetime import UTC, date, datetime

    from pulse.ingest.store import ReviewStore
    from pulse.models import Review

    reviews = [Review(key=f"k{i}", source="play_store", rating=1, title=None,
                      text=f"Refund {i} never reached me, call 98765 4321{i} please help",
                      review_date=date(2026, 11, 10), language="en", app_version=None,
                      updated_at=datetime(2026, 11, 10, tzinfo=UTC)) for i in range(3)]
    ReviewStore(tmp_path / "data" / "reviews.parquet").upsert(reviews)


def _fake_llm(monkeypatch):
    """One theme per review (r0001 -> a, r0002 -> b, r0003 -> c)."""
    from pulse.analysis import llm as llm_mod
    from pulse.analysis.engines.llm_cluster import Discovery
    from tests.fake_llm import FakeLlm, theme

    ids = ["r0001", "r0002", "r0003"]
    disc = Discovery(themes=[theme(t, ids) for t in ("a_theme", "b_theme", "c_theme")],
                     taxonomy_changes=[])
    fake = FakeLlm([disc], lambda rid: (["a_theme", "b_theme", "c_theme"][int(rid[1:]) - 1],
                                        None, "negative"))
    monkeypatch.setattr(llm_mod, "build_llm", lambda cfg, log_dir=None: fake)
    return fake


def test_analyze_scrubs_clusters_and_writes_themes(tmp_config, tmp_path, monkeypatch):
    import json

    path = tmp_config(lambda d: d["analysis"].__setitem__("min_reviews", 2))
    _store_three_reviews(tmp_path)
    fake = _fake_llm(monkeypatch)

    result = runner.invoke(app, ["analyze", "--week", "2026-W47", "--engine", "llm",
                                 "--config", str(path)])
    assert result.exit_code == ExitCode.OK, result.output
    assert "3 reviews had identifiers removed (phone 3)" in result.output
    assert "general praise 0.0%" in result.output
    themes = json.loads((tmp_path / "runs" / "2026-W47" / "themes.json").read_text("utf-8"))
    assert len(themes["assignments"]) == 3 and len(themes["top_themes"]) == 3
    # The model only ever sees scrubbed text.
    sent = " ".join((c["prefix"] or "") + c["user"] for c in fake.calls)
    assert "[phone]" in sent and "98765" not in sent
    manifest = json.loads((tmp_path / "runs" / "2026-W47" / "manifest.json").read_text("utf-8"))
    assert manifest["cluster"]["status"] == "ok"
    assert manifest["cluster"]["top_themes"] == themes["top_themes"]


def test_analyze_reports_llm_failure(tmp_config, tmp_path, monkeypatch):
    import json

    from pulse.analysis import llm as llm_mod

    class Refusing:
        usage = llm_mod.Usage()

        def parse(self, **kw):
            raise llm_mod.LlmRefusal("discover: declined", category="cyber")

    path = tmp_config(lambda d: d["analysis"].__setitem__("min_reviews", 2))
    _store_three_reviews(tmp_path)
    monkeypatch.setattr(llm_mod, "build_llm", lambda cfg, log_dir=None: Refusing())
    result = runner.invoke(app, ["analyze", "--week", "2026-W47", "--engine", "llm",
                                 "--config", str(path)])
    assert result.exit_code == ExitCode.LLM_FAILED
    manifest = json.loads((tmp_path / "runs" / "2026-W47" / "manifest.json").read_text("utf-8"))
    assert manifest["cluster"]["status"] == "failed"
    assert manifest["cluster"]["refusal_category"] == "cyber"


def test_analyze_scrubs_window_and_writes_clean_reviews(tmp_config, tmp_path, monkeypatch):
    import json

    path = tmp_config(lambda d: d["analysis"].__setitem__("min_reviews", 2))
    _store_three_reviews(tmp_path)
    _fake_llm(monkeypatch)

    result = runner.invoke(app, ["analyze", "--week", "2026-W47", "--engine", "llm",
                                 "--config", str(path)])
    assert result.exit_code == ExitCode.OK
    assert "3 reviews had identifiers removed (phone 3)" in result.output
    out = tmp_path / "runs" / "2026-W47" / "clean_reviews.jsonl"
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert [r["id"] for r in rows] == ["r0001", "r0002", "r0003"]
    assert all("[phone]" in r["canonical_text"] and "98765" not in r["canonical_text"]
               for r in rows)
    manifest = json.loads((out.parent / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["scrub"] == {"reviews": 3, "reviews_with_pii": 3, "redactions": {"phone": 3}}


def test_analyze_stops_below_min_reviews(tmp_config):
    result = runner.invoke(app, ["analyze", "--week", "2026-W47", "--config", str(tmp_config())])
    assert result.exit_code == ExitCode.TOO_FEW_REVIEWS
