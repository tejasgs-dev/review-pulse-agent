"""Phase 6: unattended runs. One test (or more) per failure row of architecture §10 that the
CLI handles: a source failing, retries with backoff, `--email-only`, exit codes and the run
summary, and the embedding model download."""

from __future__ import annotations

import json
from datetime import UTC, datetime, time

import pytest
from typer.testing import CliRunner

from pulse.cli import ExitCode, app
from pulse.config import load_config
from pulse.publish import mcp_client as mc
from pulse.publish import runner as pub
from pulse.publish.runner import PublishError, publish_path, publish_week
from pulse.render.markdown import render_doc_entry, source_line
from pulse.summary import EXIT_MEANINGS, RunSummary
from tests.conftest import TEST_DOC_ID, raw
from tests.local_corpus import corpus
from tests.test_ingest_runner import FakeSource

WEEK = "2026-W46"
MARKER = "(2026-W46)"
NOTE = "Play Store unavailable this week"
runner = CliRunner()


def app_store_reviews(n: int | None = None):
    """The synthetic local-engine corpus, as App Store reviews inside the W46 window."""
    rows = corpus()[:n] if n else corpus()
    return [raw(rid=f"a{i}", source="app_store", rating=r.rating, text=r.canonical_text,
                submitted=datetime.combine(r.review_date, time(6), tzinfo=UTC))
            for i, r in enumerate(rows)]


def fake_sources(monkeypatch, *, play_error=None, app_error=None, app_reviews=None):
    play = FakeSource("play_store", [], error=play_error)
    app_store = FakeSource("app_store", app_store_reviews() if app_reviews is None
                           else app_reviews, error=app_error)
    monkeypatch.setattr("pulse.ingest.runner.build_sources",
                        lambda cfg, data_dir: {"play_store": play, "app_store": app_store})


def out(result) -> str:
    return " ".join(result.output.split())


# ------------------------------------------------------------- §10 row 1: a source fails


def test_source_line_names_an_unavailable_source():
    from datetime import date
    line = source_line(review_count=120, sources={"play_store": 0, "app_store": 120},
                       window=(date(2026, 9, 7), date(2026, 11, 15)), min_words=9,
                       general_praise_share=0.2, unavailable=["play_store"])
    assert line.endswith(f"· 20% general praise · {NOTE}")
    assert "unavailable" not in source_line(
        review_count=1, sources={"play_store": 1}, window=(date(2026, 9, 7), date(2026, 11, 15)),
        min_words=9)


def test_play_store_down_still_runs_with_the_note(tmp_config, tmp_path, monkeypatch):
    fake_sources(monkeypatch, play_error="HTTP 403 (blocked)")
    result = runner.invoke(app, ["run", "--week", WEEK, "--dry-run",
                                 "--config", str(tmp_config())])
    assert result.exit_code == ExitCode.OK, result.output
    week = tmp_path / "runs" / WEEK
    source = (week / "pulse.md").read_text(encoding="utf-8").splitlines()[1]
    assert "Play Store 0" in source and source.endswith(NOTE)
    pulse = json.loads((week / "pulse.json").read_text(encoding="utf-8"))
    assert pulse["unavailable_sources"] == ["play_store"]
    assert NOTE in pulse["email"]["body"]
    manifest = json.loads((week / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["ingest"]["sources"]["play_store"]["status"] == "failed"
    summary = out(result).split("── ReviewPulse · pulse run")[1]
    assert "Result OK (exit 0: success)" in summary
    assert "Play Store failed, 0 in the window (HTTP 403 (blocked))" in summary
    assert f"Note {NOTE}" in summary and "Publish dry run" in summary


def test_play_store_down_still_publishes_with_the_note(publish_config, tmp_path, monkeypatch,
                                                       fake_mcp):
    fake_sources(monkeypatch, play_error="HTTP 403 (blocked)")
    result = runner.invoke(app, ["run", "--week", WEEK, "--config", str(publish_config())])
    assert result.exit_code == ExitCode.OK, result.output
    assert NOTE in fake_mcp.docs[TEST_DOC_ID]["content"]
    (mail,) = fake_mcp.sent
    assert NOTE in mail["body"]
    assert "Publish sent (Doc entry appended, email sent)" in out(result)


def test_play_store_down_and_too_few_left_stops_with_7(tmp_config, tmp_path, monkeypatch):
    fake_sources(monkeypatch, play_error="HTTP 403", app_reviews=app_store_reviews(10))
    result = runner.invoke(app, ["run", "--week", WEEK, "--dry-run",
                                 "--config", str(tmp_config())])
    assert result.exit_code == ExitCode.TOO_FEW_REVIEWS
    summary = out(result).split("── ReviewPulse · pulse run")[1]
    assert "exit 7: too few reviews" in summary
    assert "Reviews analysed 10 (minimum 50)" in summary
    assert f"Stopped at analyze: {NOTE} and only 10 reviews are left" in summary
    assert not (tmp_path / "runs" / WEEK / "pulse.md").exists()


def test_every_source_down_stops_at_ingest_with_5(tmp_config, tmp_path, monkeypatch):
    fake_sources(monkeypatch, play_error="HTTP 403", app_error="HTTP 503")
    result = runner.invoke(app, ["run", "--week", WEEK, "--dry-run",
                                 "--config", str(tmp_config())])
    assert result.exit_code == ExitCode.INGEST_FAILED
    assert "Stopped at ingest: every review source failed" in out(result)
    assert not (tmp_path / "runs" / WEEK / "clean_reviews.jsonl").exists()   # no analysis


def test_two_identical_runs_publish_once(publish_config, monkeypatch, fake_mcp):
    fake_sources(monkeypatch)
    path = str(publish_config())
    first = runner.invoke(app, ["run", "--week", WEEK, "--config", path])
    second = runner.invoke(app, ["run", "--week", WEEK, "--config", path])
    assert first.exit_code == second.exit_code == ExitCode.OK, second.output
    assert fake_mcp.docs[TEST_DOC_ID]["content"].count(MARKER) == 1
    assert len(fake_mcp.sent) == 1
    assert "Publish skipped (already published" in out(second)


# ------------------------------------------------------------- MCP retries with backoff


@pytest.fixture
def pcfg(publish_config):
    return load_config(publish_config(), load_env_file=False)


def write_week(cfg, doc="ReviewPulse: Groww app reviews — Week of 9 Nov 2026\nSource: 3 reviews\n"):
    wd = cfg.paths.runs_path / WEEK
    wd.mkdir(parents=True, exist_ok=True)
    (wd / "pulse.md").write_text(doc, encoding="utf-8")
    (wd / "pulse.json").write_text(json.dumps({"week": WEEK, "email": {
        "subject": "ReviewPulse: Groww app reviews — Week of 9 Nov 2026", "body": doc}}),
        encoding="utf-8")
    (wd / "validation.json").write_text(json.dumps({"ok": True}), encoding="utf-8")


def fail_first(monkeypatch, name, times, error, *, after=False):
    """Make runner.<name> raise `error` the first `times` calls (after the real call when
    `after`, as when a reply is lost but the tool did its work)."""
    real = getattr(pub, name)
    left = {"n": times}

    async def wrapped(*args, **kwargs):
        if left["n"] and not after:
            left["n"] -= 1
            raise error
        result = await real(*args, **kwargs)
        if left["n"]:
            left["n"] -= 1
            raise error
        return result

    monkeypatch.setattr(pub, name, wrapped)


def test_a_timed_out_check_is_retried_with_backoff(pcfg, fake_mcp, monkeypatch):
    cfg = pcfg.model_copy(update={"mcp": pcfg.mcp.model_copy(
        update={"retry_delay_seconds": 10})})
    write_week(cfg)
    fail_first(monkeypatch, "week_in_doc", 2, mc.McpCallError("doc_contains_text timed out"))
    waits = []
    record = publish_week(cfg, WEEK, sleep=waits.append)
    assert waits == [10, 20]                                   # exponential backoff
    assert record["status"] == "sent" and len(record["retries"]) == 2
    assert {r["step"] for r in record["retries"]} == {"check"}
    assert fake_mcp.docs[TEST_DOC_ID]["content"].count(MARKER) == 1 and len(fake_mcp.sent) == 1


def test_retries_give_up_with_the_last_error(pcfg, fake_mcp, monkeypatch):
    write_week(pcfg)
    fail_first(monkeypatch, "week_in_doc", 9, mc.McpCallError("doc_contains_text timed out"))
    with pytest.raises(PublishError, match="timed out") as e:
        publish_week(pcfg, WEEK)
    assert e.value.step == "check" and len(e.value.record["retries"]) == pcfg.mcp.retries
    assert fake_mcp.sent == [] and MARKER not in fake_mcp.docs[TEST_DOC_ID]["content"]


def test_an_append_that_landed_before_its_timeout_is_not_appended_twice(pcfg, fake_mcp,
                                                                         monkeypatch):
    write_week(pcfg)
    fail_first(monkeypatch, "append_entry", 1, mc.McpCallError("append_to_doc timed out"),
               after=True)
    record = publish_week(pcfg, WEEK)
    assert fake_mcp.tool_names() == ["doc_contains_text", "append_to_doc",
                                     "doc_contains_text", "send_email"]
    assert fake_mcp.docs[TEST_DOC_ID]["content"].count(MARKER) == 1 and len(fake_mcp.sent) == 1
    assert record["status"] == "sent" and record["doc"]["confirmed_by"] == "doc_contains_text"


def test_a_lost_connection_is_retried(pcfg, fake_mcp, monkeypatch):
    write_week(pcfg)
    real = mc.open_client
    calls = {"n": 0}

    def flaky(server):
        calls["n"] += 1
        if calls["n"] == 1:
            raise mc.McpConnectError("cannot connect to the MCP server: ConnectError")
        return real(server)

    monkeypatch.setattr(mc, "open_client", flaky)
    assert publish_week(pcfg, WEEK)["status"] == "sent"
    assert calls["n"] == 2 and len(fake_mcp.sent) == 1


def test_a_send_that_timed_out_is_never_retried(pcfg, fake_mcp, monkeypatch):
    write_week(pcfg)
    fail_first(monkeypatch, "send_pulse", 1, mc.McpCallError("send_email timed out"),
               after=True)
    with pytest.raises(PublishError) as e:
        publish_week(pcfg, WEEK)
    assert e.value.step == "send" and e.value.record["status"] == "partial"
    assert len(fake_mcp.sent) == 1                    # it did go out: sending again would dup


def test_a_tool_error_is_never_retried(pcfg, fake_mcp):
    write_week(pcfg)
    fake_mcp.fail["doc_contains_text"] = "Google API error 500: backend"
    with pytest.raises(PublishError):
        publish_week(pcfg, WEEK)
    assert fake_mcp.tool_names() == ["doc_contains_text"]


@pytest.mark.parametrize("error, step, forced, email_only, expected", [
    (mc.McpConnectError("x"), "connect", False, False, True),
    (mc.McpCallError("timeout"), "check", True, False, True),
    (mc.McpCallError("timeout"), "append", False, False, True),
    (mc.McpCallError("timeout"), "append", True, False, False),      # forced: never twice
    (mc.McpCallError("timeout"), "email_setup", False, False, True),
    (mc.McpCallError("timeout"), "email_setup", True, False, False),
    (mc.McpCallError("timeout"), "send", False, False, False),       # may have been sent
    (mc.McpToolError("t", "error"), "check", False, False, False),   # the server must be fixed
    (mc.McpSetupError("rejected the API key"), "connect", False, False, False),
])
def test_what_is_retried(error, step, forced, email_only, expected):
    record = {"forced": forced, "email_only": email_only}
    assert pub._can_retry(error, step, record) is expected


def test_a_rejected_key_is_not_transient():
    assert not mc.is_transient(mc.McpSetupError("the server rejected the API key"))
    assert mc.is_transient(mc.McpConnectError("did not answer within 60s"))


# ------------------------------------------------------------- --email-only


def test_email_only_sends_just_the_email_when_the_entry_is_there(pcfg, fake_mcp):
    write_week(pcfg)
    fake_mcp.fail["send_email"] = "Google API error 500: backend"
    with pytest.raises(PublishError):
        publish_week(pcfg, WEEK)
    publish_path(pcfg.paths.runs_path, WEEK).unlink()     # as on Actions: nothing is kept
    del fake_mcp.fail["send_email"]
    fake_mcp.calls.clear()
    record = publish_week(pcfg, WEEK, email_only=True)
    assert fake_mcp.tool_names() == ["doc_contains_text", "send_email"]
    assert fake_mcp.docs[TEST_DOC_ID]["content"].count(MARKER) == 1 and len(fake_mcp.sent) == 1
    assert record["status"] == "sent" and record["email_only"] is True


def test_email_only_refuses_when_the_doc_has_no_entry(pcfg, fake_mcp):
    write_week(pcfg)
    with pytest.raises(PublishError, match="has no entry") as e:
        publish_week(pcfg, WEEK, email_only=True)
    assert "without --email-only" in e.value.hint
    assert fake_mcp.tool_names() == ["doc_contains_text"] and fake_mcp.sent == []


def test_cli_email_only_and_its_hints(publish_config, fake_mcp):
    path = str(publish_config())
    write_week(load_config(path, load_env_file=False))
    both = runner.invoke(app, ["publish", "--week", WEEK, "--force", "--email-only",
                               "--config", path])
    assert both.exit_code == 2 and "--force or --email-only" in out(both)
    assert fake_mcp.calls == []
    missing = runner.invoke(app, ["publish", "--week", WEEK, "--email-only", "--config", path])
    assert missing.exit_code == ExitCode.PUBLISH_FAILED
    assert "(without --email-only) to append the entry" in out(missing)
    fake_mcp.fail["send_email"] = "Google API error 500: backend"
    failed = runner.invoke(app, ["publish", "--week", WEEK, "--config", path])
    assert failed.exit_code == ExitCode.PUBLISH_FAILED
    assert f"run `pulse publish --week {WEEK} --email-only`" in out(failed)
    assert "Publish partial" in out(failed)
    del fake_mcp.fail["send_email"]
    ok = runner.invoke(app, ["publish", "--week", WEEK, "--email-only", "--config", path])
    assert ok.exit_code == ExitCode.OK, ok.output
    assert "Publish sent (email only)" in out(ok)
    assert fake_mcp.docs[TEST_DOC_ID]["content"].count(MARKER) == 1 and len(fake_mcp.sent) == 1


def test_a_timed_out_send_hint_points_to_email_only(pcfg, fake_mcp):
    from pulse.cli import _publish_hint

    hint = _publish_hint(PublishError("send_email timed out", step="send"), WEEK)
    assert f"`pulse publish --week {WEEK} --email-only`" in hint and "appended" in hint


# ------------------------------------------------------------- exit codes and the summary


def test_every_exit_code_has_a_meaning():
    assert {int(c) for c in ExitCode} == set(EXIT_MEANINGS)


def test_an_unexpected_error_exits_1_with_a_summary(tmp_config, monkeypatch):
    fake_sources(monkeypatch)

    def boom(*a, **k):
        raise ValueError("something broke")

    monkeypatch.setattr("pulse.privacy.scrub.scrub_all", boom)
    result = runner.invoke(app, ["run", "--week", WEEK, "--dry-run",
                                 "--config", str(tmp_config())])
    assert result.exit_code == ExitCode.UNEXPECTED_ERROR == 1
    text = out(result)
    assert "Traceback" in result.output and "ValueError: something broke" in text
    assert "exit 1: unexpected error" in text
    assert "Stopped at analyze: unexpected ValueError: something broke" in text


def test_the_summary_goes_to_the_github_job_summary(publish_config, tmp_path, monkeypatch,
                                                    fake_mcp):
    fake_sources(monkeypatch, play_error="HTTP 403")
    target = tmp_path / "step_summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(target))
    result = runner.invoke(app, ["run", "--week", WEEK, "--config", str(publish_config())])
    assert result.exit_code == ExitCode.OK, result.output
    md = target.read_text(encoding="utf-8")
    assert md.startswith(f"### ReviewPulse · pulse run {WEEK}")
    assert "| Result | OK (exit 0: success) |" in md and f"| Note | {NOTE} |" in md
    assert "| Top themes |" in md and "| Publish | sent" in md
    # Public logs: no recipient, no Doc link, no review text.
    assert "me@example.com" not in md and TEST_DOC_ID not in md
    assert not any(r.text and r.text[:30] in md for r in app_store_reviews())


def test_on_github_actions_no_pulse_text_or_recipient_in_the_logs(publish_config, tmp_path,
                                                                   monkeypatch, fake_mcp):
    fake_sources(monkeypatch)
    path = str(publish_config())
    local = runner.invoke(app, ["run", "--week", WEEK, "--dry-run", "--config", path])
    pulse_md = (tmp_path / "runs" / WEEK / "pulse.md").read_text(encoding="utf-8")
    quote = next(line for line in pulse_md.splitlines() if line.startswith("• "))
    assert quote in local.output and "Action Ideas" in local.output   # printed locally

    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    result = runner.invoke(app, ["run", "--week", WEEK, "--config", path])
    assert result.exit_code == ExitCode.OK, result.output
    assert len(fake_mcp.sent) == 1 and fake_mcp.sent[0]["to"] == "me@example.com"
    assert quote not in result.output and "Action Ideas" not in result.output
    assert "the pulse text is not printed on GitHub Actions" in result.output
    assert "me@example.com" not in result.output
    assert "Email sent to PULSE_RECIPIENT" in result.output
    assert "── ReviewPulse · pulse run" in out(result) and "Publish sent" in out(result)


def test_on_github_actions_a_server_error_quoting_the_recipient_is_redacted(
        publish_config, tmp_path, monkeypatch, fake_mcp):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    target = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(target))
    path = str(publish_config())
    write_week(load_config(path, load_env_file=False))
    fake_mcp.fail["send_email"] = "Invalid input: recipient not allowed: me@example.com"
    result = runner.invoke(app, ["publish", "--week", WEEK, "--config", path])
    assert result.exit_code == ExitCode.PUBLISH_FAILED
    assert "recipient not allowed: PULSE_RECIPIENT" in out(result)
    assert "me@example.com" not in result.output
    assert "me@example.com" not in target.read_text(encoding="utf-8")


def test_the_summary_skips_a_missing_github_summary_file(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "no" / "such" / "dir.md"))
    s = RunSummary("run", WEEK)
    s.finish(0)
    assert s.write_step_summary() is None              # never fails the run


def test_failed_run_summary_lists_the_failure(publish_config, monkeypatch, fake_mcp):
    fake_sources(monkeypatch)
    fake_mcp.fail["append_to_doc"] = "Permission denied — check that the API is enabled."
    result = runner.invoke(app, ["run", "--week", WEEK, "--config", str(publish_config())])
    assert result.exit_code == ExitCode.PUBLISH_FAILED
    text = out(result).split("── ReviewPulse · pulse run")[1]
    assert "exit 11: publishing through MCP failed" in text
    assert "Stopped at publish: append_to_doc failed: Permission denied" in text
    assert fake_mcp.sent == []


# ------------------------------------------------------------- the embedding model download


class _FakeModel:
    @staticmethod
    def from_pretrained(path, force_download=False):
        return path


def test_model_download_is_retried(tmp_path, cfg, monkeypatch):
    import huggingface_hub
    import model2vec

    from pulse.analysis.engines.local_cluster import Model2VecEmbedder

    tries = []

    def download(repo, revision, cache_dir, local_files_only=False):
        tries.append(local_files_only)
        if local_files_only or len(tries) < 3:
            raise OSError("offline")
        return str(tmp_path / "model")

    monkeypatch.setattr(huggingface_hub, "snapshot_download", download)
    monkeypatch.setattr(model2vec, "StaticModel", _FakeModel)
    waits = []
    emb = Model2VecEmbedder(cfg.analysis.local, tmp_path, sleep=waits.append)
    assert tries == [True, False, False] and waits == [5.0]
    assert emb._model == str(tmp_path / "model")


def test_model_download_gives_up_with_a_clear_error(tmp_path, cfg, monkeypatch):
    import huggingface_hub
    import model2vec

    from pulse.analysis.engines.local_cluster import LocalEngineError, Model2VecEmbedder

    def download(*a, **k):
        raise OSError("no network")

    monkeypatch.setattr(huggingface_hub, "snapshot_download", download)
    monkeypatch.setattr(model2vec, "StaticModel", _FakeModel)
    waits = []
    with pytest.raises(LocalEngineError, match="after 3 attempts.*analysis.local.model"):
        Model2VecEmbedder(cfg.analysis.local, tmp_path, sleep=waits.append)
    assert waits == [5.0, 10.0]


def test_render_doc_entry_keeps_the_note(tmp_path):
    entry = render_doc_entry(WEEK, f"Groww Weekly Review Pulse — x\nSource: 1 review · {NOTE}\n")
    assert NOTE in entry
