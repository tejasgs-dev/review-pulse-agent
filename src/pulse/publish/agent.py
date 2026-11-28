"""Agent mode for publishing (`publish.mode: agent`, the default).

The LLM decides the order of the publishing steps in a tool-calling loop. It gets four tools,
thin wrappers around the guarded publisher in `runner.py` (the same functions the scripted
order uses), and never raw MCP access:

- `check_already_published()`   read-only: is the week's marker in the Doc?
- `append_week_entry()`         append this week's validated pulse.md
- `verify_entry_in_doc()`       read-only: confirm the marker is now in the Doc
- `send_pulse_email(intro)`     send the email; the model writes only a short intro line

Safety stays in code (`Guard`), never in the prompt:

- `doc_id` and `recipient` are not tool parameters; code takes them from config. A call with
  any argument other than `reason` (and `intro` for the email) is refused unrun.
- Append only after a check found the week missing (or with --force), never with
  --email-only, and once. Email only after the entry is confirmed in the Doc, at most once per
  run (a second call is refused), and never retried. The intro is length-limited and scanned
  for PII, links and digits.
- An MCP tool error stops the run (PublishError, same hints as the scripted publisher).
- At most `publish.agent.max_steps` model turns; reaching it stops the run as a failure.
- The week must have passed validation (`runner.prepare`, VAL-07) before the agent starts.
- publish.json is written after every step in the scripted format, so --force, --email-only
  and resuming behave the same.

Every step (tool, arguments, result, the model's reason) goes to runs/{week}/agent_log.json.
If the model fails before the email was attempted (no key, rate limit, bad output), the
scripted publisher takes over: it re-checks the Doc and publish.json, so it never appends or
sends twice.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import anyio

from pulse.analysis.cluster import load_prompt
from pulse.analysis.llm import LlmError
from pulse.config import PulseConfig
from pulse.fileio import write_json
from pulse.privacy.scan import find_pii
from pulse.publish import mcp_client
from pulse.publish import runner as rn
from pulse.publish.docs_publisher import week_in_doc
from pulse.publish.mcp_client import (
    McpCallError,
    McpSetupError,
    McpToolError,
    verified_caller,
)
from pulse.render.markdown import week_of

PROMPT = "publish_agent_v2"   # v2: the work is done only once the email is sent
CODE_SEND_REASON = "sent by the code: the agent stopped after the Doc entry was confirmed"
Echo = Callable[[str], None]

_REASON = {"type": "string", "description": "One short sentence: why this step now."}
TOOLS: list[dict[str, Any]] = [
    {"type": "function", "function": {
        "name": "check_already_published",
        "description": "Read-only. Whether this week's entry is already in the shared Google "
                       "Doc, and what that means for this run.",
        "parameters": {"type": "object", "properties": {"reason": _REASON},
                       "required": ["reason"], "additionalProperties": False}}},
    {"type": "function", "function": {
        "name": "append_week_entry",
        "description": "Append this week's validated pulse to the Doc. Allowed only after a "
                       "check found the week missing (or with --force).",
        "parameters": {"type": "object", "properties": {"reason": _REASON},
                       "required": ["reason"], "additionalProperties": False}}},
    {"type": "function", "function": {
        "name": "verify_entry_in_doc",
        "description": "Read-only. Confirm this week's entry is in the Doc now.",
        "parameters": {"type": "object", "properties": {"reason": _REASON},
                       "required": ["reason"], "additionalProperties": False}}},
    {"type": "function", "function": {
        "name": "send_pulse_email",
        "description": "Send the pulse by email, once, after the entry is confirmed in the "
                       "Doc. Subject, body, recipient and Doc link are set by the program.",
        "parameters": {"type": "object", "properties": {
            "intro": {"type": "string", "description": "One plain sentence that opens the "
                      "email. No numbers, links, email addresses or names."},
            "reason": _REASON},
            "required": ["intro", "reason"], "additionalProperties": False}}},
]
ALLOWED_ARGS = {t["function"]["name"]: set(t["function"]["parameters"]["properties"])
                for t in TOOLS}
_LINK = re.compile(r"https?://|www\.|@", re.I)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------- the guard


@dataclass
class Guard:
    """What the agent has done and may do next. Every rule is checked here, in code."""

    force: bool
    email_only: bool
    resume: bool                 # this machine appended earlier but did not send
    checked: bool = False
    present: bool | None = None  # the check's answer
    appended: bool = False
    in_doc: bool = False         # the entry is confirmed in the Doc (check or verify)
    send_attempted: bool = False
    sent: bool = False

    @property
    def nothing_to_do(self) -> bool:
        """Already published, and this run neither forces nor resumes nor sends only."""
        return bool(self.present) and not (self.force or self.resume or self.email_only)

    def refuse(self, tool: str) -> str | None:
        """Why `tool` may not run now, or None when it may."""
        if tool == "append_week_entry":
            if not self.checked:
                return "check whether the week is already published first"
            if self.email_only:
                return "this is an --email-only run: it never appends"
            if self.appended:
                return "the entry was already appended in this run; it is never appended twice"
            if self.present and not self.force:
                return "the week's entry is already in the Doc; appending again would publish "\
                       "it twice"
            return None
        if tool == "send_pulse_email":
            if self.send_attempted:
                return "the email was already sent in this run; it is sent at most once"
            if self.nothing_to_do and not self.appended:
                return "the week is already published; there is nothing to send"
            if not self.in_doc:
                return "confirm the entry is in the Doc first (verify_entry_in_doc)"
            return None
        if tool == "verify_entry_in_doc" and not self.checked:
            return "check whether the week is already published first"
        return None


def check_intro(intro: str, cfg: PulseConfig) -> str | None:
    """Why the agent's intro line is not acceptable, or None."""
    text = intro.strip()
    limit = cfg.publish.agent.intro_max_chars
    if not text:
        return "write a short intro sentence"
    if len(text) > limit:
        return f"the intro has {len(text)} characters; the limit is {limit}"
    if "\n" in text:
        return "the intro must be one line"
    if re.search(r"\d", text):
        return "the intro may not contain numbers (the pulse below carries them)"
    if _LINK.search(text):
        return "the intro may not contain links or email addresses"
    if find_pii(text, number_min_digits=cfg.privacy.number_min_digits,
                reference_min_digits=cfg.privacy.reference_min_digits):
        return "the intro looks like it contains personal data"
    return None


# ---------------------------------------------------------------------------- the log


@dataclass
class AgentLog:
    path: Any
    week: str
    model: str
    max_steps: int
    steps: list[dict[str, Any]] = field(default_factory=list)
    started_at: str = field(default_factory=_now)
    outcome: str | None = None
    final_message: str | None = None
    fallback: dict[str, Any] | None = None
    tokens: int = 0
    finished_by_code: bool = False   # the agent stopped early; the code sent the email

    def add(self, **step: Any) -> None:
        self.steps.append({"n": len(self.steps) + 1, **step, "at": _now()})
        self.write()

    def write(self) -> None:
        write_json(self.path, {
            "week": self.week, "mode": "agent", "model": self.model,
            "max_steps": self.max_steps, "started_at": self.started_at,
            "outcome": self.outcome, "final_message": self.final_message,
            "fallback": self.fallback, "finished_by_code": self.finished_by_code,
            "tokens": self.tokens, "steps": self.steps})

    def lines(self) -> list[str]:
        """A readable version for the run summary."""
        out = []
        for s in self.steps:
            if s["tool"] is None:
                continue
            why = f" · “{s['reason']}”" if s.get("reason") else ""
            out.append(f"{s['tool']} → {s['status']}: {s['result']}{why}")
        return out


# ---------------------------------------------------------------------------- the run


@dataclass
class AgentResult:
    record: dict[str, Any]
    mode: str                    # "agent", or "scripted" after a fallback
    log: AgentLog | None
    fallback: str | None = None  # why the scripted publisher took over


def publish_week_agent(cfg: PulseConfig, week: str, llm: Any, *, force: bool = False,
                       email_only: bool = False, echo: Echo = lambda _m: None,
                       sleep: Callable[[float], None] = time.sleep) -> AgentResult:
    """Publish one week with the LLM choosing the tool calls. Raises PublishError on failure.
    Falls back to the scripted publisher when the model fails before the email was tried."""
    p = rn.prepare(cfg, week, force=force, email_only=email_only)
    record = p.record
    guard = Guard(force=force, email_only=email_only,
                  resume=record["_appended_here"] and not record["_sent_here"])
    log = AgentLog(cfg.paths.runs_path / week / "agent_log.json", week, llm.cfg.model,
                   cfg.publish.agent.max_steps)
    log.write()
    try:
        anyio.run(_run, cfg, week, llm, p, guard, log, echo)
    except BaseException as exc:
        llm_error = _find(exc, LlmError)
        if llm_error is not None and guard.sent:
            # The model failed after the email went out: the work is done.
            log.final_message = f"(the model failed after sending: {llm_error.kind})"
            return AgentResult(_finish(cfg, week, p, guard, log, echo), "agent", log)
        if llm_error is not None and not guard.send_attempted:
            return _fall_back(cfg, week, llm_error, log, force and not guard.appended,
                              email_only, echo, sleep)
        mcp_error = mcp_client.mcp_error(exc)
        if mcp_error is None and llm_error is None:
            p.save("failed")
            raise
        error = mcp_error or llm_error
        step = record.get("_step", "connect")
        record["failed_step"] = step
        p.save("partial" if (guard.appended or guard.resume) and not guard.sent else "failed")
        log.outcome = f"stopped: {error}"[:300]
        log.write()
        raise rn.PublishError(str(error), record=record,
                              setup=isinstance(error, McpSetupError),
                              server_tool=error.tool if isinstance(error, McpToolError) else None,
                              step=step) from exc
    return AgentResult(_finish(cfg, week, p, guard, log, echo), "agent", log)


def _fall_back(cfg, week, error: LlmError, log: AgentLog, force: bool, email_only: bool,
               echo: Echo, sleep) -> AgentResult:
    reason = {"rate_limit": "rate_limited", "auth": "no_key_or_bad_key",
              "connection": "unreachable"}.get(error.kind, f"llm_{error.kind}")
    log.fallback = {"reason": reason, "error": str(error)[:300]}
    log.outcome = "fell back to the scripted publisher"
    log.write()
    echo(f"  ! the publishing agent stopped ({reason}); the scripted publisher takes over")
    record = rn.publish_week(cfg, week, force=force, email_only=email_only, echo=echo,
                             sleep=sleep)
    return AgentResult(record, "scripted", log, fallback=reason)


def _finish(cfg, week, p, guard: Guard, log: AgentLog, echo: Echo) -> dict[str, Any]:
    """Judge the outcome in code: done, nothing to do, or a failure."""
    record, save = p.record, p.save
    if record.get("_missing_entry"):
        record["failed_step"] = "check"
        save("failed")
        log.outcome = "refused: --email-only but the week is not in the Doc"
        log.write()
        raise rn.PublishError(
            f"--email-only: the Doc has no entry for {week} ({record['marker']}), so there is "
            "nothing to email a link to", record=record, step="check",
            hint=f"run `pulse publish --week {week}` (without --email-only) to append the entry "
                 "and send the email")
    if guard.sent:
        save("sent")
        log.outcome = ("published (the agent stopped before the email; the code sent it)"
                       if log.finished_by_code else "published")
        log.write()
        return record
    if guard.nothing_to_do and not guard.appended and not guard.send_attempted:
        rn._skip(record, save, echo, f"{record['marker']} is in the Doc")
        log.outcome = "already published: nothing to do"
        log.write()
        return record
    out_of_steps = log.outcome == "max_steps"
    why = (f"the agent used all {cfg.publish.agent.max_steps} steps without finishing"
           if out_of_steps else "the agent stopped before the week was published")
    entry_in_doc = guard.appended or guard.resume or (guard.present and guard.email_only)
    step = "send" if entry_in_doc else ("append" if guard.checked else "check")
    record["failed_step"] = step
    save("partial" if entry_in_doc else "failed")
    log.outcome = why
    log.write()
    raise rn.PublishError(why, record=record, step=step)


async def _run(cfg: PulseConfig, week: str, llm, p, guard: Guard, log: AgentLog,
               echo: Echo) -> None:
    record = p.record
    same_server = cfg.mcp.docs == cfg.mcp.gmail
    for server in ([cfg.mcp.docs] if same_server else [cfg.mcp.docs, cfg.mcp.gmail]):
        waited = await anyio.to_thread.run_sync(mcp_client.wait_until_healthy, server)
        if waited:
            echo(f"  MCP server answered its health check after {waited}s")
    async with AsyncExitStack() as stack:
        docs_client = await stack.enter_async_context(mcp_client.open_client(cfg.mcp.docs))
        docs = await verified_caller(docs_client, p.tool_map, "docs", cfg.mcp.docs)
        gmail_box: dict[str, Any] = {}

        async def gmail():
            if "caller" not in gmail_box:
                record["_step"] = "email_setup"
                client = docs_client if same_server else await stack.enter_async_context(
                    mcp_client.open_client(cfg.mcp.gmail))
                gmail_box["caller"] = await verified_caller(client, p.tool_map, "gmail",
                                                            cfg.mcp.gmail)
            return gmail_box["caller"]

        tools = _Tools(cfg, p, guard, docs, gmail, echo)
        try:
            await _loop(cfg, week, llm, tools, guard, log, echo)
            await _send_if_the_agent_stopped_early(tools, guard, log, echo)
        finally:
            record["calls"] += docs.calls


async def _send_if_the_agent_stopped_early(tools: _Tools, guard: Guard, log: AgentLog,
                                           echo: Echo) -> None:
    """The agent stopped (it said it was done, or ran out of steps) after the entry was
    confirmed in the Doc but without trying the email: the code sends it itself, through the
    same guarded tool, with no intro (2026-W47 on GitHub Actions: check -> append -> verify,
    then "Pulse published successfully" and no send). Nothing happens if the email was
    already tried, if the entry is not confirmed in the Doc, or if the guard would refuse the
    send (e.g. the week was already published)."""
    if guard.send_attempted or not guard.in_doc or guard.refuse("send_pulse_email"):
        return
    echo("  The agent stopped before sending the email; the code sends it now (no intro).")
    log.finished_by_code = True
    try:
        status, result = await tools.send()
    except (McpSetupError, McpCallError) as e:
        log.add(turn=None, tool="send_pulse_email", arguments={}, by="code",
                reason=CODE_SEND_REASON, status="error", result=str(e)[:300])
        raise
    log.add(turn=None, tool="send_pulse_email", arguments={}, by="code",
            reason=CODE_SEND_REASON, status=status, result=result)
    echo(f"  Code step {len(log.steps)}: send_pulse_email → {status}: {result}")


async def _loop(cfg, week, llm, tools: _Tools, guard: Guard, log: AgentLog, echo: Echo):
    system = load_prompt(PROMPT, product=cfg.product.name,
                         intro_max_chars=cfg.publish.agent.intro_max_chars)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": _briefing(cfg, week, tools.p, guard)},
    ]
    effort = cfg.llm.stage_effort("agent")
    for turn in range(1, cfg.publish.agent.max_steps + 1):
        completion = await anyio.to_thread.run_sync(lambda t=turn: llm.complete(
            name=f"agent_step{t:02d}", messages=messages, effort=effort, tools=TOOLS,
            tool_choice="auto"))
        usage = getattr(completion, "usage", None)
        log.tokens += getattr(usage, "total_tokens", 0) or 0
        choice = completion.choices[0]
        message = choice.message
        if choice.finish_reason == "length":
            raise LlmError("the publishing agent's answer hit the token limit",
                           kind="max_tokens")
        calls = list(getattr(message, "tool_calls", None) or [])
        if not calls:
            log.final_message = (message.content or "").strip()[:300]
            log.add(turn=turn, tool=None, arguments={}, reason=None, status="finished",
                    result=log.final_message)
            echo(f"  Agent finished: {log.final_message}")
            return
        messages.append({"role": "assistant", "content": message.content or "",
                         "tool_calls": [{"id": c.id, "type": "function", "function": {
                             "name": c.function.name, "arguments": c.function.arguments}}
                             for c in calls]})
        for c in calls:
            try:
                status, result, args = await tools.run(c.function.name, c.function.arguments)
            except (McpSetupError, McpCallError) as e:   # a tool error stops the run
                log.add(turn=turn, tool=c.function.name, arguments={}, reason=None,
                        status="error", result=str(e)[:300])
                raise
            reason = str(args.get("reason", ""))[:200] if isinstance(args, dict) else ""
            shown = {k: v for k, v in args.items() if k != "reason"} \
                if isinstance(args, dict) else args
            log.add(turn=turn, tool=c.function.name, arguments=shown, reason=reason,
                    status=status, result=result)
            echo(f"  Agent step {len(log.steps)}: {c.function.name} → {status}: {result}")
            messages.append({"role": "tool", "tool_call_id": c.id,
                             "content": f"{status}: {result}"})
    log.outcome = "max_steps"


def _briefing(cfg: PulseConfig, week: str, p, guard: Guard) -> str:
    """What the agent is told: the week and the run's options. No review text, recipient or
    Doc ID: it does not need them and cannot change them."""
    options = ("--force: re-publish even if the week is already in the Doc" if guard.force
               else "--email-only: send only the email; the entry must already be in the Doc"
               if guard.email_only else "normal: publish once")
    earlier = ("this machine appended the entry in an earlier run, but the email was not sent"
               if guard.resume else "no unfinished earlier run on this machine")
    themes = ", ".join(p.art.get("themes") or []) or "see the pulse"
    return (f"Publish week {week} ({week_of(week)}).\nRun option: {options}.\n"
            f"Earlier runs: {earlier}.\nThis week's top themes (for the intro): {themes}.")


class _Tools:
    """The four tools. Each returns (status, result text, parsed arguments)."""

    def __init__(self, cfg, p, guard: Guard, docs, gmail, echo: Echo) -> None:
        self.cfg, self.p, self.guard, self.docs, self.gmail, self.echo = (
            cfg, p, guard, docs, gmail, echo)

    async def run(self, name: str, raw_args: str) -> tuple[str, str, Any]:
        try:
            args = json.loads(raw_args or "{}")
        except json.JSONDecodeError:
            return "refused", "the arguments are not valid JSON", {}
        if not isinstance(args, dict):
            return "refused", "the arguments must be a JSON object", {}
        if name not in ALLOWED_ARGS:
            return "refused", f"there is no tool named {name!r}", args
        extra = sorted(set(args) - ALLOWED_ARGS[name])
        if extra:   # e.g. an attempt to pass a recipient or a doc ID
            return ("refused", f"unknown argument(s) {', '.join(extra)}: this tool takes only "
                    f"{', '.join(sorted(ALLOWED_ARGS[name]))}; the recipient and the Doc are "
                    "fixed by the program", args)
        why_not = self.guard.refuse(name)
        if why_not:
            return "refused", why_not, args
        handler = getattr(self, name)
        return (*await handler(args), args)

    async def check_already_published(self, args) -> tuple[str, str]:
        g, record = self.guard, self.p.record
        present = await rn.check_step(self.docs, self.cfg, record, self.p.save)
        g.checked, g.present = True, present
        if not present:
            if g.email_only:
                record["_missing_entry"] = True
                return "ok", ("not found: the week is not in the Doc, and this --email-only "
                              "run cannot append it; stop")
            return "ok", "not found: the week is not in the Doc yet"
        g.in_doc = True
        if g.force:
            return "ok", "found, but this run uses --force: you may append it again"
        if g.email_only:
            if record["doc"].get("status") != "ok":
                record["doc"] = {"status": "present", "at": _now()}
            self.p.save()
            return "ok", "found: this --email-only run sends only the email"
        if g.resume:
            record["doc"] = {**record["doc"], "resumed_at": _now()}
            self.p.save()
            return "ok", ("found: this machine appended it earlier but the email was not "
                          "sent; send the email, do not append again")
        return "ok", "found: the week is already published; nothing to do, stop now"

    async def append_week_entry(self, args) -> tuple[str, str]:
        r = await rn.append_step(self.docs, self.cfg, self.p.doc_text, self.p.record,
                                 self.p.save, self.echo)
        self.guard.appended = True
        return "ok", f"appended the entry '{self.p.record['heading']}' ({r.chars} characters)"

    async def verify_entry_in_doc(self, args) -> tuple[str, str]:
        record = self.p.record
        record["_step"] = "check"
        present = await week_in_doc(self.docs, doc_id=self.cfg.publish.doc_id,
                                    marker=record["marker"])
        record["verify"] = {"status": "ok", "found": present, "at": _now()}
        self.p.save()
        if present:
            self.guard.in_doc = True
            return "ok", f"verified: the marker {record['marker']} is in the Doc"
        return "ok", f"not found: the marker {record['marker']} is not in the Doc"

    async def send_pulse_email(self, args) -> tuple[str, str]:
        intro = str(args.get("intro", ""))
        problem = check_intro(intro, self.cfg)
        if problem:              # refused before anything is sent; the send is not used up
            return "refused", problem
        return await self.send(intro)

    async def send(self, intro: str = "") -> tuple[str, str]:
        """The one send, after the guard allowed it. Also used by the code (no intro) when
        the agent stopped before sending."""
        self.guard.send_attempted = True    # from here on, never again in this run
        gmail = await self.gmail()
        r = await rn.send_step(gmail, self.cfg, self.p.art, self.p.record, self.p.save,
                               self.echo, intro=intro)
        self.guard.sent = True
        return "ok", f"sent (message id {r.message_id})"


def _find(exc: BaseException, cls: type) -> Any:
    """`exc` or the first matching exception inside an exception group."""
    if isinstance(exc, cls):
        return exc
    for inner in getattr(exc, "exceptions", ()) or ():
        found = _find(inner, cls)
        if found is not None:
            return found
    return None


__all__ = ["AgentResult", "Guard", "TOOLS", "check_intro", "publish_week_agent"]
