"""Phase 5: publishing through MCP (append to the shared Doc, then send one email), against an
in-process fake of gworkspace-mcp."""

import json

import httpx
import pytest
import yaml
from typer.testing import CliRunner

from pulse.cli import ExitCode, app
from pulse.config import PROJECT_ROOT, load_config
from pulse.publish import mcp_client as mc
from pulse.publish.mcp_client import wait_until_healthy as real_wait_until_healthy
from pulse.publish.runner import PublishError, publish_week, read_record
from pulse.render.markdown import DOC_SEPARATOR, plain_text, render_doc_entry, week_heading
from tests.conftest import TEST_DOC_ID

WEEK = "2026-W46"
HEADING = "Week of 9 Nov 2026 (2026-W46)"
DOC_URL = f"https://docs.google.com/document/d/{TEST_DOC_ID}/edit"
KEY = "test-key-0123456789-0123456789-xyz"
PULSE = ("Groww Weekly Review Pulse — Week of 9 Nov 2026\nSource: 3 reviews\n\nTop Themes\n"
         "1. Support (50% of reviews · 90% 1–2★) — Slow replies.\n")
TOOL_MAP = PROJECT_ROOT / "src/pulse/publish/tool_map.yaml"
runner = CliRunner()


def write_week(cfg, *, doc=PULSE, ok=True, failure=False):
    wd = cfg.paths.runs_path / WEEK
    wd.mkdir(parents=True, exist_ok=True)
    (wd / "pulse.md").write_text(doc, encoding="utf-8")
    (wd / "pulse.json").write_text(json.dumps({"week": WEEK, "email": {
        "subject": "Groww Review Pulse — Week of 9 Nov 2026", "body": doc}}), encoding="utf-8")
    (wd / "validation.json").write_text(json.dumps({"ok": ok}), encoding="utf-8")
    if failure:
        (wd / "failure.json").write_text("{}", encoding="utf-8")
    return wd


def doc_text(fake):
    return fake.docs[TEST_DOC_ID]["content"]


@pytest.fixture
def pcfg(publish_config):
    return load_config(publish_config(), load_env_file=False)


# ------------------------------------------------------------------ rendering


def test_doc_entry_is_plain_text_with_a_separator_and_week_heading():
    entry = render_doc_entry(WEEK, PULSE)
    lines = entry.splitlines()
    assert lines[0] == DOC_SEPARATOR and set(DOC_SEPARATOR) == {"─"}
    assert lines[1] == HEADING == week_heading(WEEK)
    assert lines[2] == "Source: 3 reviews"           # the pulse header line is replaced
    assert "Weekly Review Pulse" not in entry
    assert entry.endswith("Slow replies.\n")


def test_markdown_symbols_are_removed():
    md = "## Top Themes\n**Support** is `slow`\n- a point\n> quoted\n• kept\n1. kept"
    assert plain_text(md) == "Top Themes\nSupport is slow\n• a point\nquoted\n• kept\n1. kept"


# ------------------------------------------------------------------ the tool map


def test_real_tool_map_appends_and_sends_only():
    tm = mc.load_tool_map(TOOL_MAP)
    assert {spec.tool for _, _, spec in tm.mapped()} == {"append_to_doc", "doc_contains_text",
                                                          "send_email"}
    assert tm.spec("gmail", "send_email").args == mc.SEND_ARGS


@pytest.mark.parametrize("mutate, message", [
    (lambda m: m["docs"]["append_to_doc"].__setitem__("tool", "send_email"), "only gmail.send"),
    (lambda m: m["docs"]["append_to_doc"].__setitem__("tool", "drafts_Send"), "only gmail.send"),
    (lambda m: m["docs"]["append_to_doc"].__setitem__("tool", "create_doc"), "never called"),
    (lambda m: m["gmail"]["send_email"]["args"].__setitem__("bcc", "x@y.com"), "exactly"),
    (lambda m: m["gmail"]["send_email"]["args"].__setitem__("to", "other@x.com"), "exactly"),
    (lambda m: m["docs"]["append_to_doc"]["args"].__setitem__("send_now", True), "sending"),
    (lambda m: m["gmail"].__setitem__("send_email", None), "needs send_email"),
    (lambda m: m["docs"].__setitem__("create_doc", None), "unknown docs actions"),
    (lambda m: m["docs"]["append_to_doc"].__setitem__("ok", "(unclosed"), "bad pattern"),
])
def test_bad_tool_maps_are_refused(tmp_path, mutate, message):  # MCP-03, MCP-04, MCP-10
    raw = yaml.safe_load(TOOL_MAP.read_text("utf-8"))
    mutate(raw)
    path = tmp_path / "map.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(mc.McpSetupError, match=message):
        mc.load_tool_map(path)


@pytest.mark.parametrize("with_contains", [False, True])
def test_tool_map_is_checked_against_the_servers_tools(with_contains):  # MCP-01, MCP-02
    import anyio
    from mcp import Client

    from tests.fake_mcp import FakeWorkspace

    async def tools():
        async with Client(FakeWorkspace(with_contains=with_contains).server()) as c:
            return (await c.list_tools()).tools

    listed = anyio.run(tools)
    tm = mc.load_tool_map(TOOL_MAP)
    assert mc.check_tools(tm, "docs", listed) == []   # doc_contains_text may be missing
    assert mc.check_tools(tm, "gmail", listed) == []
    renamed = [t for t in listed if t.name != "append_to_doc"]
    assert "no tool 'append_to_doc'" in mc.check_tools(tm, "docs", renamed)[0]
    bad = tm.model_copy(update={"docs": {**tm.docs, "append_to_doc": tm.docs["append_to_doc"]
                                          .model_copy(update={"args": {"document_id": "x"}})}})
    assert "requires ['content']" in mc.check_tools(bad, "docs", listed)[0]


def test_results_are_read_from_the_text_and_errors_are_failures():  # MCP-08
    from types import SimpleNamespace as NS
    tm = mc.load_tool_map(TOOL_MAP)

    def result(text, is_error=False):
        return NS(content=[NS(text=text)], is_error=is_error)

    append = tm.spec("docs", "append_to_doc")
    ok = mc.interpret(append, result("Appended 12 characters to 'Pulse'."))
    assert ok.status == "ok" and ok.groups == {"count": "12", "title": "Pulse"}
    with pytest.raises(mc.McpCallError, match="Permission denied"):
        mc.interpret(append, result("Permission denied — check that the API is enabled"))
    check = tm.spec("docs", "doc_contains_text")
    assert mc.interpret(check, result("Text found in 'Pulse'.")).status == "ok"
    assert mc.interpret(check, result("Text not found in 'Pulse'.")).status == "not_found"
    with pytest.raises(mc.McpCallError):               # an unreadable doc is not "absent"
        mc.interpret(check, result("Document not found — check the document ID."))
    send = tm.spec("gmail", "send_email")
    assert mc.interpret(send, result("Email sent to me@example.com (message id: 18c)")
                        ).groups["message_id"] == "18c"
    with pytest.raises(mc.McpCallError, match="recipient not allowed"):
        mc.interpret(send, result("Invalid input: recipient not allowed: me@example.com"))
    with pytest.raises(mc.McpCallError, match="boom"):   # isError wins even over ok text
        mc.interpret(send, result("Email sent to a@b.co (message id: 1) boom", is_error=True))


# ------------------------------------------------------------------ publishing


def test_publish_appends_to_the_doc_then_sends_one_email(pcfg, fake_mcp):
    write_week(pcfg)
    record = publish_week(pcfg, WEEK)
    assert fake_mcp.tool_names() == ["append_to_doc", "send_email"]
    assert doc_text(fake_mcp) == ("Groww Weekly Review Pulse\n\n"
                                  + render_doc_entry(WEEK, PULSE))
    (mail,) = fake_mcp.sent
    assert mail["to"] == "me@example.com"
    assert mail["subject"] == "Groww Review Pulse — Week of 9 Nov 2026"
    assert mail["body"] == PULSE.rstrip("\n") + f"\n\nFull pulse: {DOC_URL}\n"
    (_, args), = [c for c in fake_mcp.calls if c[0] == "send_email"]
    assert args["cc"] is None and args["bcc"] is None
    assert "create_doc" not in fake_mcp.tool_names()
    assert record["status"] == "sent" and record["duplicate_check"] == "publish.json"
    assert record["doc"]["status"] == "ok" and record["email"]["status"] == "sent"
    assert record["doc_url"] == DOC_URL and record["heading"] == HEADING
    assert read_record(pcfg.paths.runs_path, WEEK)["status"] == "sent"
    assert fake_mcp.connections == 1          # one server serves Docs and Gmail


def test_a_sent_week_is_not_sent_again_without_force(pcfg, fake_mcp):
    write_week(pcfg)
    publish_week(pcfg, WEEK)
    fake_mcp.calls.clear()
    again = publish_week(pcfg, WEEK)
    assert fake_mcp.calls == [] and fake_mcp.connections == 1   # not even connected
    assert again["status"] == "skipped" and "--force" in again["skip_reason"]
    assert read_record(pcfg.paths.runs_path, WEEK)["status"] == "sent"   # record kept
    forced = publish_week(pcfg, WEEK, force=True)
    assert fake_mcp.tool_names() == ["send_email"]    # the Doc entry is not appended twice
    assert len(fake_mcp.sent) == 2 and doc_text(fake_mcp).count(HEADING) == 1
    assert forced["status"] == "sent" and forced["forced"] is True
    assert forced["previous_email"]["status"] == "sent"


def test_week_heading_in_the_doc_skips_both_steps(pcfg, fake_mcp):  # no runs/ (Actions)
    fake_mcp.with_contains = True
    fake_mcp.docs[TEST_DOC_ID]["content"] += render_doc_entry(WEEK, PULSE)
    write_week(pcfg)
    record = publish_week(pcfg, WEEK)
    assert fake_mcp.tool_names() == ["doc_contains_text"]
    assert fake_mcp.calls[0][1] == {"document_id": TEST_DOC_ID, "text": HEADING}
    assert fake_mcp.sent == [] and record["status"] == "skipped"
    assert record["duplicate_check"] == "doc_contains_text"
    assert record["email"]["status"] == "skipped"
    fake_mcp.calls.clear()
    publish_week(pcfg, WEEK, force=True)             # --force re-sends, still no second entry
    assert fake_mcp.tool_names() == ["doc_contains_text", "send_email"]
    assert doc_text(fake_mcp).count(HEADING) == 1


def test_with_the_check_tool_a_new_week_is_appended_and_sent(pcfg, fake_mcp):
    fake_mcp.with_contains = True
    write_week(pcfg)
    record = publish_week(pcfg, WEEK)
    assert fake_mcp.tool_names() == ["doc_contains_text", "append_to_doc", "send_email"]
    assert record["status"] == "sent" and record["duplicate_check"] == "doc_contains_text"


def test_resume_after_the_email_failed_does_not_append_again(pcfg, fake_mcp):
    write_week(pcfg)
    fake_mcp.fail["send_email"] = "Invalid input: recipient not allowed: me@example.com"
    with pytest.raises(PublishError, match="recipient not allowed") as e:
        publish_week(pcfg, WEEK)
    saved = read_record(pcfg.paths.runs_path, WEEK)
    assert saved["status"] == "partial" and e.value.record["status"] == "partial"
    assert saved["doc"]["status"] == "ok" and saved["email"]["status"] == "failed"
    del fake_mcp.fail["send_email"]
    fake_mcp.calls.clear()
    record = publish_week(pcfg, WEEK)                 # no --force needed: nothing was sent
    assert fake_mcp.tool_names() == ["send_email"]
    assert record["status"] == "sent" and doc_text(fake_mcp).count(HEADING) == 1


def test_email_never_sent_when_the_append_failed(pcfg, fake_mcp):
    write_week(pcfg)
    fake_mcp.fail["append_to_doc"] = "Permission denied — check that the API is enabled."
    with pytest.raises(PublishError, match="Permission denied"):
        publish_week(pcfg, WEEK)
    assert "send_email" not in fake_mcp.tool_names() and fake_mcp.sent == []
    saved = read_record(pcfg.paths.runs_path, WEEK)
    assert saved["status"] == "failed" and saved["doc"]["status"] == "failed"


def test_a_missing_doc_fails_clearly_and_never_creates_one(pcfg, fake_mcp):
    del fake_mcp.docs[TEST_DOC_ID]
    write_week(pcfg)
    with pytest.raises(PublishError, match="PULSE_DOC_ID.*was not found|was not found"):
        publish_week(pcfg, WEEK)
    assert fake_mcp.tool_names() == ["append_to_doc"] and fake_mcp.docs == {}


def test_a_failed_check_stops_before_appending(pcfg, fake_mcp):
    fake_mcp.with_contains = True
    fake_mcp.fail["doc_contains_text"] = "Google API error 500: backend"
    write_week(pcfg)
    with pytest.raises(PublishError, match="500"):
        publish_week(pcfg, WEEK)
    assert fake_mcp.tool_names() == ["doc_contains_text"]


def test_a_slow_tool_times_out_instead_of_hanging(publish_config, fake_mcp):  # MCP-07
    def short(d):
        d["mcp"]["docs"]["call_timeout_seconds"] = 0.3
        d["mcp"]["gmail"]["call_timeout_seconds"] = 0.3
    cfg = load_config(publish_config(short), load_env_file=False)
    write_week(cfg)
    fake_mcp.delay["append_to_doc"] = 5
    with pytest.raises(PublishError, match="did not answer within"):
        publish_week(cfg, WEEK)
    assert fake_mcp.sent == []


@pytest.mark.parametrize("kwargs, message", [
    ({"ok": False}, "did not pass validation"),
    ({"failure": True}, "failed validation"),
])
def test_unvalidated_weeks_are_never_published(pcfg, fake_mcp, kwargs, message):
    write_week(pcfg, **kwargs)
    with pytest.raises(PublishError, match=message):
        publish_week(pcfg, WEEK)
    assert fake_mcp.calls == []


@pytest.mark.parametrize("var, message", [("PULSE_DOC_ID", "PULSE_DOC_ID is not set"),
                                          ("PULSE_RECIPIENT", "PULSE_RECIPIENT")])
def test_missing_doc_id_or_recipient_stops_before_any_call(publish_config, fake_mcp,
                                                          monkeypatch, var, message):
    monkeypatch.setenv(var, "")
    cfg = load_config(publish_config(), load_env_file=False)
    write_week(cfg)
    with pytest.raises(PublishError, match=message) as e:
        publish_week(cfg, WEEK)
    assert e.value.setup and fake_mcp.calls == []


def test_missing_key_is_reported(publish_config, monkeypatch):
    monkeypatch.setenv("PULSE_MCP_API_KEY", "")
    cfg = load_config(publish_config(), load_env_file=False)
    assert cfg.mcp.docs.missing_headers == ["Authorization"]
    with pytest.raises(mc.McpSetupError, match="PULSE_MCP_API_KEY"):
        mc._transport(cfg.mcp.docs)


@pytest.mark.parametrize("to", ["other@example.com", "me@example.com,other@example.com"])
def test_send_refuses_any_other_recipient(to):
    import anyio

    from pulse.publish.email_sender import send_pulse

    class NoCalls:
        async def call(self, *a, **k):
            raise AssertionError("must not be called")

    with pytest.raises(mc.McpSetupError, match="only ever called for PULSE_RECIPIENT"):
        anyio.run(lambda: send_pulse(NoCalls(), recipient="me@example.com", to=to,
                                     subject="s", body="b"))


def test_the_api_key_never_reaches_artifacts_or_reprs(pcfg, fake_mcp):
    wd = write_week(pcfg)
    publish_week(pcfg, WEEK)
    assert KEY not in (wd / "publish.json").read_text("utf-8")
    assert KEY not in repr(pcfg.mcp.docs) and KEY not in str(pcfg.mcp)
    assert pcfg.mcp.docs.headers["Authorization"].get_secret_value() == f"Bearer {KEY}"


# ------------------------------------------------------------------ health check


def _server(**kw):
    from pulse.config import McpServerConfig
    return McpServerConfig(transport="http", url="https://x.example/mcp",
                           health_url="https://x.example/healthz", **kw)


def _clock(step=5):
    t = iter(range(0, 10_000, step))
    return lambda: next(t)


def test_wait_until_healthy_polls_until_the_server_wakes():
    answers = iter([503, 502, 200])
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(next(answers))))
    waited = real_wait_until_healthy(_server(), sleep=lambda s: None, clock=_clock(),
                                     client=http)
    assert waited == 15          # started at 0; the third poll is read at 15 on a 5 s clock


def test_wait_until_healthy_gives_up_with_a_clear_error():
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    with pytest.raises(mc.McpSetupError, match=r"not healthy .* after 30s \(HTTP 503\)"):
        real_wait_until_healthy(_server(wake_timeout_seconds=30), sleep=lambda s: None,
                                clock=_clock(), client=http)


# ------------------------------------------------------------------ CLI


def test_cli_publish_force_and_doctor_with_the_fake_server(publish_config, fake_mcp):
    path = publish_config()
    cfg = load_config(path, load_env_file=False)
    write_week(cfg)
    result = runner.invoke(app, ["publish", "--week", WEEK, "--config", str(path)])
    assert result.exit_code == ExitCode.OK, result.output
    assert f"Doc entry '{HEADING}' appended: {DOC_URL}" in result.output
    assert "Email sent to me@example.com (message id: msg" in result.output
    manifest = json.loads((cfg.paths.runs_path / WEEK / "manifest.json").read_text("utf-8"))
    assert manifest["publish"]["status"] == "sent"
    assert "me@example.com" not in json.dumps(manifest["publish"])
    again = runner.invoke(app, ["publish", "--week", WEEK, "--config", str(path)])
    assert again.exit_code == ExitCode.OK and "use --force" in again.output
    assert len(fake_mcp.sent) == 1
    forced = runner.invoke(app, ["publish", "--week", WEEK, "--force", "--config", str(path)])
    assert forced.exit_code == ExitCode.OK and len(fake_mcp.sent) == 2
    doctor = runner.invoke(app, ["doctor", "--config", str(path)])
    assert "answers list_tools (7 tools)" in doctor.output, doctor.output
    assert "every mapped docs tool exists" in doctor.output
    assert "no 'doc_contains_text' tool yet" in doctor.output


def test_cli_publish_failure_exit_code(publish_config, fake_mcp):
    path = publish_config()
    write_week(load_config(path, load_env_file=False))
    fake_mcp.fail["send_email"] = "Google API error 500: backend"
    result = runner.invoke(app, ["publish", "--week", WEEK, "--config", str(path)])
    assert result.exit_code == ExitCode.PUBLISH_FAILED
    assert "run `pulse publish --week 2026-W46` again to resume" in result.output


def test_cli_publish_without_doc_id_is_a_config_error(publish_config, fake_mcp, monkeypatch):
    monkeypatch.setenv("PULSE_DOC_ID", "")
    path = publish_config()
    write_week(load_config(path, load_env_file=False))
    result = runner.invoke(app, ["publish", "--week", WEEK, "--config", str(path)])
    assert result.exit_code == ExitCode.CONFIG_ERROR
    assert "PULSE_DOC_ID is not set" in result.output
