"""The publishing agent: the LLM chooses the tool calls, the guardrails are code. The model is
a scripted fake and the MCP server is the in-process fake: no network."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from pulse.analysis import llm as llm_mod
from pulse.analysis.llm import LlmError, Usage
from pulse.cli import ExitCode, app
from pulse.config import load_config
from pulse.publish.agent import TOOLS, check_intro, publish_week_agent
from pulse.publish.runner import PublishError, read_record
from tests.conftest import TEST_DOC_ID
from tests.test_publish import MARKER, WEEK, doc_text, write_week

runner = CliRunner()
INTRO = "This week reviewers mostly wrote about slow support replies."


def call(name: str, reason: str = "next step", **args) -> tuple[str, dict]:
    return name, {"reason": reason, **args}


CHECK = call("check_already_published", "find out first whether the week is out")
APPEND = call("append_week_entry", "the week is not in the Doc yet")
VERIFY = call("verify_entry_in_doc", "confirm before emailing")
SEND = call("send_pulse_email", "the entry is confirmed", intro=INTRO)
DONE = "done"


class FakeAgentLlm:
    """Each turn returns the next planned tool call, a final message (DONE), or raises the
    planned exception. Records every request."""

    def __init__(self, *plan):
        self.plan = list(plan)
        self.requests: list[dict] = []
        self.cfg = SimpleNamespace(model="openai/gpt-oss-120b")
        self.usage = Usage(provider="groq")

    def complete(self, *, name, messages, effort, max_tokens=None, **extra):
        self.requests.append({"name": name, "messages": [dict(m) for m in messages],
                              "effort": effort, **extra})
        step = self.plan.pop(0) if self.plan else DONE
        if isinstance(step, Exception):
            raise step
        usage = SimpleNamespace(total_tokens=600)
        if step == DONE:
            msg = SimpleNamespace(content="Published.", tool_calls=None)
            return SimpleNamespace(usage=usage, choices=[SimpleNamespace(
                message=msg, finish_reason="stop")])
        tool, args = step
        tc = SimpleNamespace(id=f"call_{len(self.requests)}", type="function",
                             function=SimpleNamespace(name=tool, arguments=json.dumps(args)))
        msg = SimpleNamespace(content="", tool_calls=[tc])
        return SimpleNamespace(usage=usage, choices=[SimpleNamespace(
            message=msg, finish_reason="tool_calls")])


@pytest.fixture
def pcfg(publish_config):
    return load_config(publish_config(), load_env_file=False)


def agent_log(cfg) -> dict:
    return json.loads((cfg.paths.runs_path / WEEK / "agent_log.json").read_text(
        encoding="utf-8"))


def statuses(cfg) -> list[tuple[str, str]]:
    return [(s["tool"], s["status"]) for s in agent_log(cfg)["steps"] if s["tool"]]


def publish_already_in_doc(fake_mcp):
    fake_mcp.docs[TEST_DOC_ID]["content"] += f"\nReviewPulse — Week of 9 Nov 2026 {MARKER}\n"


# ---------------------------------------------------------------------------- paths


def test_normal_path_check_append_verify_send(pcfg, fake_mcp):
    write_week(pcfg)
    llm = FakeAgentLlm(CHECK, APPEND, VERIFY, SEND, DONE)
    result = publish_week_agent(pcfg, WEEK, llm)

    assert result.mode == "agent" and result.record["status"] == "sent"
    assert fake_mcp.tool_names() == ["doc_contains_text", "append_to_doc",
                                     "doc_contains_text", "send_email"]
    (mail,) = fake_mcp.sent
    assert mail["to"] == "me@example.com"
    assert mail["body"].startswith(INTRO + "\n\n")
    assert MARKER in doc_text(fake_mcp)
    assert statuses(pcfg) == [("check_already_published", "ok"), ("append_week_entry", "ok"),
                              ("verify_entry_in_doc", "ok"), ("send_pulse_email", "ok")]
    log = agent_log(pcfg)
    assert log["outcome"] == "published" and log["final_message"] == "Published."
    assert log["steps"][0]["reason"] == "find out first whether the week is out"
    assert log["tokens"] == 5 * 600
    assert all(r["effort"] == "low" for r in llm.requests)      # agent: low reasoning effort
    assert read_record(pcfg.paths.runs_path, WEEK)["email"]["intro"] == INTRO


def test_already_published_means_no_append_and_no_send(pcfg, fake_mcp):
    write_week(pcfg)
    publish_already_in_doc(fake_mcp)
    llm = FakeAgentLlm(CHECK, APPEND, SEND, DONE)      # the model tries anyway
    result = publish_week_agent(pcfg, WEEK, llm)

    assert result.record["status"] == "skipped"
    assert fake_mcp.tool_names() == ["doc_contains_text"]
    assert fake_mcp.sent == []
    assert statuses(pcfg) == [("check_already_published", "ok"),
                              ("append_week_entry", "refused"), ("send_pulse_email", "refused")]
    assert agent_log(pcfg)["outcome"] == "already published: nothing to do"


def test_a_second_send_is_blocked(pcfg, fake_mcp):
    write_week(pcfg)
    llm = FakeAgentLlm(CHECK, APPEND, VERIFY, SEND, SEND, DONE)
    result = publish_week_agent(pcfg, WEEK, llm)
    assert result.record["status"] == "sent"
    assert len(fake_mcp.sent) == 1
    last = agent_log(pcfg)["steps"][4]
    assert (last["tool"], last["status"]) == ("send_pulse_email", "refused")
    assert "at most once" in last["result"]


def test_email_needs_the_entry_confirmed_first(pcfg, fake_mcp):
    write_week(pcfg)
    llm = FakeAgentLlm(SEND, CHECK, APPEND, SEND, VERIFY, SEND, DONE)
    publish_week_agent(pcfg, WEEK, llm)
    steps = statuses(pcfg)
    assert steps[0] == ("send_pulse_email", "refused")     # before any check
    assert steps[3] == ("send_pulse_email", "refused")     # appended but not verified
    assert steps[5] == ("send_pulse_email", "ok")
    assert len(fake_mcp.sent) == 1


def test_a_tool_error_stops_the_run(pcfg, fake_mcp):
    write_week(pcfg)
    fake_mcp.fail["append_to_doc"] = "Permission denied: the caller cannot edit this document"
    llm = FakeAgentLlm(CHECK, APPEND, VERIFY, SEND, DONE)
    with pytest.raises(PublishError) as e:
        publish_week_agent(pcfg, WEEK, llm)
    assert e.value.step == "append"
    assert fake_mcp.sent == []
    assert len(llm.requests) == 2               # the model was not asked again
    assert statuses(pcfg)[-1] == ("append_week_entry", "error")
    assert read_record(pcfg.paths.runs_path, WEEK)["status"] == "failed"


def test_the_send_is_never_retried(pcfg, fake_mcp):
    write_week(pcfg)
    fake_mcp.fail["send_email"] = "Gmail API error: backend error"
    llm = FakeAgentLlm(CHECK, APPEND, VERIFY, SEND, SEND, DONE)
    with pytest.raises(PublishError) as e:
        publish_week_agent(pcfg, WEEK, llm)
    assert e.value.step == "send"
    assert fake_mcp.tool_names().count("send_email") == 1
    assert read_record(pcfg.paths.runs_path, WEEK)["status"] == "partial"


def test_reaching_max_steps_stops_with_a_failure(pcfg, fake_mcp):
    write_week(pcfg)
    llm = FakeAgentLlm(*[CHECK] * 20)
    with pytest.raises(PublishError, match="all 8 steps"):
        publish_week_agent(pcfg, WEEK, llm)
    assert len(llm.requests) == pcfg.publish.agent.max_steps == 8
    assert fake_mcp.sent == [] and "append_to_doc" not in fake_mcp.tool_names()
    assert agent_log(pcfg)["outcome"].startswith("the agent used all 8 steps")


def test_stopping_before_publishing_is_a_failure(pcfg, fake_mcp):
    write_week(pcfg)
    llm = FakeAgentLlm(CHECK, APPEND, DONE)          # appended, then gave up, not verified
    with pytest.raises(PublishError) as e:
        publish_week_agent(pcfg, WEEK, llm)
    assert e.value.step == "send"                    # the CLI then suggests --email-only
    assert fake_mcp.sent == []                       # not confirmed in the Doc: no email
    assert read_record(pcfg.paths.runs_path, WEEK)["status"] == "partial"


# ------------------------------------------- the agent stops early: the code sends (W47)


def test_agent_stopping_after_verify_the_code_sends_the_email(pcfg, fake_mcp):
    """2026-W47 on GitHub Actions: check -> append -> verify, then "published" and no send."""
    write_week(pcfg)
    llm = FakeAgentLlm(CHECK, APPEND, VERIFY, DONE)
    result = publish_week_agent(pcfg, WEEK, llm)

    assert result.mode == "agent" and result.record["status"] == "sent"
    assert fake_mcp.tool_names() == ["doc_contains_text", "append_to_doc",
                                     "doc_contains_text", "send_email"]
    (mail,) = fake_mcp.sent
    assert mail["to"] == "me@example.com"
    assert not mail["body"].startswith(INTRO)                   # the code writes no intro
    assert doc_text(fake_mcp).count(MARKER) == 1                # appended once
    log = agent_log(pcfg)
    assert log["finished_by_code"] is True
    assert log["outcome"] == ("published (the agent stopped before the email; the code "
                              "sent it)")
    last = log["steps"][-1]
    assert (last["tool"], last["status"], last["by"]) == ("send_pulse_email", "ok", "code")
    assert "intro" not in read_record(pcfg.paths.runs_path, WEEK)["email"]


def test_agent_out_of_steps_after_verify_the_code_sends_the_email(pcfg, fake_mcp):
    write_week(pcfg)
    llm = FakeAgentLlm(CHECK, APPEND, VERIFY, *[VERIFY] * 20)    # loops until max_steps
    result = publish_week_agent(pcfg, WEEK, llm)
    assert result.record["status"] == "sent" and len(fake_mcp.sent) == 1
    assert len(llm.requests) == pcfg.publish.agent.max_steps
    assert agent_log(pcfg)["finished_by_code"] is True


def test_email_only_run_where_the_agent_stops_after_the_check(pcfg, fake_mcp):
    write_week(pcfg)
    publish_already_in_doc(fake_mcp)
    result = publish_week_agent(pcfg, WEEK, FakeAgentLlm(CHECK, DONE), email_only=True)
    assert result.record["status"] == "sent" and len(fake_mcp.sent) == 1
    assert "append_to_doc" not in fake_mcp.tool_names()


def test_the_code_never_sends_for_a_week_that_was_already_published(pcfg, fake_mcp):
    write_week(pcfg)
    publish_already_in_doc(fake_mcp)
    result = publish_week_agent(pcfg, WEEK, FakeAgentLlm(CHECK, DONE))
    assert result.record["status"] == "skipped"
    assert fake_mcp.sent == [] and fake_mcp.tool_names() == ["doc_contains_text"]
    assert agent_log(pcfg)["finished_by_code"] is False


def test_the_code_does_not_send_again_after_the_agent_tried(pcfg, fake_mcp):
    write_week(pcfg)
    fake_mcp.fail["send_email"] = "Gmail API error: backend error"
    llm = FakeAgentLlm(CHECK, APPEND, VERIFY, SEND, DONE)
    with pytest.raises(PublishError) as e:
        publish_week_agent(pcfg, WEEK, llm)
    assert e.value.step == "send"
    assert fake_mcp.tool_names().count("send_email") == 1       # never a second try
    assert agent_log(pcfg)["finished_by_code"] is False


def test_a_failed_code_send_is_partial_and_not_retried(pcfg, fake_mcp):
    write_week(pcfg)
    fake_mcp.fail["send_email"] = "Gmail API error: backend error"
    with pytest.raises(PublishError) as e:
        publish_week_agent(pcfg, WEEK, FakeAgentLlm(CHECK, APPEND, VERIFY, DONE))
    assert e.value.step == "send"
    assert fake_mcp.tool_names().count("send_email") == 1
    assert read_record(pcfg.paths.runs_path, WEEK)["status"] == "partial"
    last = agent_log(pcfg)["steps"][-1]
    assert (last["by"], last["status"]) == ("code", "error")


def test_the_prompt_says_the_email_is_part_of_the_work(pcfg, fake_mcp):
    write_week(pcfg)
    llm = FakeAgentLlm(CHECK, APPEND, VERIFY, SEND, DONE)
    publish_week_agent(pcfg, WEEK, llm)
    system = llm.requests[0]["messages"][0]["content"]
    assert "only half of the work" in system
    assert "Never say the pulse is published before the email is sent" in system


# ---------------------------------------------------------------------------- fixed by code


def test_recipient_and_doc_id_cannot_be_changed_by_the_model(pcfg, fake_mcp):
    for t in TOOLS:
        params = set(t["function"]["parameters"]["properties"])
        assert params <= {"reason", "intro"}, t["function"]["name"]
    write_week(pcfg)
    llm = FakeAgentLlm(
        CHECK,
        call("append_week_entry", doc_id="1AnotherDocAnotherDocAnotherDoc"),
        APPEND, VERIFY,
        call("send_pulse_email", intro=INTRO, to="attacker@example.org"),
        call("send_pulse_email", intro=INTRO, recipient="attacker@example.org", cc="x@y.z"),
        SEND, DONE)
    result = publish_week_agent(pcfg, WEEK, llm)
    assert result.record["status"] == "sent"
    assert [m["to"] for m in fake_mcp.sent] == ["me@example.com"]
    appends = [a for n, a in fake_mcp.calls if n == "append_to_doc"]
    assert [a["document_id"] for a in appends] == [TEST_DOC_ID]
    refused = [s for s in agent_log(pcfg)["steps"] if s["status"] == "refused"]
    assert len(refused) == 3 and all("unknown argument" in s["result"] for s in refused)
    # The model never even sees the address or the Doc ID.
    seen = json.dumps([r["messages"] for r in llm.requests])
    assert "me@example.com" not in seen and TEST_DOC_ID not in seen


@pytest.mark.parametrize("intro, problem", [
    ("", "short intro"),
    ("Support replies took 4 days on average.", "numbers"),
    ("Details at https://example.org/pulse", "links"),
    ("Write to someone@example.org with questions", "links"),
    ("x" * 201, "characters"),
])
def test_bad_intros_are_refused_without_using_up_the_send(pcfg, fake_mcp, intro, problem):
    assert problem in check_intro(intro, pcfg)
    write_week(pcfg)
    llm = FakeAgentLlm(CHECK, APPEND, VERIFY, call("send_pulse_email", intro=intro), SEND, DONE)
    result = publish_week_agent(pcfg, WEEK, llm)
    assert result.record["status"] == "sent" and len(fake_mcp.sent) == 1
    assert statuses(pcfg)[3] == ("send_pulse_email", "refused")


def test_an_unvalidated_week_never_reaches_the_model(pcfg, fake_mcp):
    write_week(pcfg, ok=False)
    llm = FakeAgentLlm(CHECK, APPEND, VERIFY, SEND)
    with pytest.raises(PublishError, match="did not pass validation"):
        publish_week_agent(pcfg, WEEK, llm)
    assert llm.requests == [] and fake_mcp.calls == []


# ---------------------------------------------------------------------------- run options


def test_email_only_never_appends(pcfg, fake_mcp):
    write_week(pcfg)
    publish_already_in_doc(fake_mcp)
    llm = FakeAgentLlm(CHECK, APPEND, SEND, DONE)
    result = publish_week_agent(pcfg, WEEK, llm, email_only=True)
    assert result.record["status"] == "sent"
    assert "append_to_doc" not in fake_mcp.tool_names() and len(fake_mcp.sent) == 1
    assert statuses(pcfg)[1] == ("append_week_entry", "refused")
    assert "--email-only" in llm.requests[0]["messages"][1]["content"]


def test_force_appends_again(pcfg, fake_mcp):
    write_week(pcfg)
    publish_already_in_doc(fake_mcp)
    result = publish_week_agent(pcfg, WEEK, FakeAgentLlm(CHECK, APPEND, VERIFY, SEND, DONE),
                                force=True)
    assert result.record["status"] == "sent"
    assert fake_mcp.tool_names().count("append_to_doc") == 1 and len(fake_mcp.sent) == 1


def test_running_the_same_week_twice_publishes_once(pcfg, fake_mcp):
    write_week(pcfg)
    publish_week_agent(pcfg, WEEK, FakeAgentLlm(CHECK, APPEND, VERIFY, SEND, DONE))
    again = publish_week_agent(pcfg, WEEK, FakeAgentLlm(CHECK, APPEND, VERIFY, SEND, DONE))
    assert again.record["status"] == "skipped"
    assert fake_mcp.tool_names().count("append_to_doc") == 1 and len(fake_mcp.sent) == 1


# ---------------------------------------------------------------------------- fallback


def test_model_failure_at_the_start_falls_back_to_scripted(pcfg, fake_mcp):
    write_week(pcfg)
    llm = FakeAgentLlm(LlmError("rate limited by Groq (429)", kind="rate_limit"))
    result = publish_week_agent(pcfg, WEEK, llm)
    assert (result.mode, result.fallback) == ("scripted", "rate_limited")
    assert result.record["status"] == "sent"
    assert fake_mcp.tool_names() == ["doc_contains_text", "append_to_doc", "send_email"]
    assert agent_log(pcfg)["fallback"]["reason"] == "rate_limited"


def test_model_failure_after_the_append_resumes_without_appending_again(pcfg, fake_mcp):
    write_week(pcfg)
    llm = FakeAgentLlm(CHECK, APPEND, LlmError("could not reach Groq", kind="connection"))
    result = publish_week_agent(pcfg, WEEK, llm)
    assert result.mode == "scripted" and result.record["status"] == "sent"
    assert fake_mcp.tool_names().count("append_to_doc") == 1
    assert len(fake_mcp.sent) == 1


def test_model_failure_after_the_send_still_counts_as_published(pcfg, fake_mcp):
    write_week(pcfg)
    llm = FakeAgentLlm(CHECK, APPEND, VERIFY, SEND, LlmError("timeout", kind="connection"))
    result = publish_week_agent(pcfg, WEEK, llm)
    assert result.mode == "agent" and result.record["status"] == "sent"
    assert len(fake_mcp.sent) == 1


# ---------------------------------------------------------------------------- CLI


def test_cli_agent_mode_shows_the_steps_in_the_summary(publish_config, fake_mcp, monkeypatch):
    path = publish_config()
    write_week(load_config(path, load_env_file=False))
    llm = FakeAgentLlm(CHECK, APPEND, VERIFY, SEND, DONE)
    monkeypatch.setattr(llm_mod, "build_llm", lambda cfg, log_dir=None: llm)
    result = runner.invoke(app, ["publish", "--week", WEEK, "--config", str(path)])
    assert result.exit_code == ExitCode.OK, result.output
    out = result.output
    assert "Publish mode" in out and "agent (groq openai/gpt-oss-120b, 4 tool calls" in out
    assert "Agent step 1" in out and "check_already_published → ok" in out
    assert "Agent step 4" in out and "send_pulse_email → ok: sent" in out
    manifest = json.loads((load_config(path, load_env_file=False).paths.runs_path / WEEK
                           / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["publish"]["mode"] == "agent"


def test_cli_scripted_mode_uses_no_model(publish_config, fake_mcp, monkeypatch):
    path = publish_config()
    write_week(load_config(path, load_env_file=False))
    monkeypatch.setattr(llm_mod, "build_llm", lambda cfg, log_dir=None: pytest.fail("model"))
    result = runner.invoke(app, ["publish", "--week", WEEK, "--mode", "scripted",
                                 "--config", str(path)])
    assert result.exit_code == ExitCode.OK, result.output
    mode_row = next(line for line in result.output.splitlines() if "Publish mode" in line)
    assert mode_row.split()[-1] == "scripted"
    assert "Agent step" not in result.output and len(fake_mcp.sent) == 1


def test_cli_without_a_key_falls_back_and_says_so(publish_config, fake_mcp):
    path = publish_config()
    write_week(load_config(path, load_env_file=False))
    result = runner.invoke(app, ["publish", "--week", WEEK, "--config", str(path)])
    assert result.exit_code == ExitCode.OK, result.output
    assert "scripted (fallback: no_key_or_bad_key)" in result.output
    assert len(fake_mcp.sent) == 1
