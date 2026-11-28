"""Stage 7: publish a validated week through MCP only: append it to the shared Google Doc,
then email it to PULSE_RECIPIENT.

Reads the saved run artifacts (pulse.md is the Doc text; pulse.json holds the email subject
and body), so `pulse publish` never re-runs the analysis. Refuses to publish a week whose
validation failed (VAL-07), and stops before any call when PULSE_DOC_ID or PULSE_RECIPIENT is
missing.

No double publishing. Before appending or sending, the Doc is checked for the week's unique
marker, "(2026-W46)", with the server's read-only `doc_contains_text` tool (an exact,
case-sensitive match; every appended heading contains the marker exactly). Found → "already
published": neither step runs and the command succeeds. The Doc is the record, so this works
on GitHub Actions, where runs/ is not kept.

`runs/{week}/publish.json` (rewritten after every step) adds resuming: if this machine
appended the entry but the email failed, the next run sends the email without appending again
(the marker is in the Doc, but nothing was sent). On Actions, where publish.json is not kept,
such a week counts as published; `--email-only` sends just the email (it checks that the
entry is in the Doc and never appends).

Retries (§10). A connection failure or a timeout is retried up to `mcp.retries` times with
exponential backoff (`mcp.retry_delay_seconds`, doubled each time), and only where a retry
cannot publish twice: while connecting and checking; after an append that timed out (the retry
checks the Doc first: if the marker is there now, that was this run's entry and only the email
is sent); and while connecting for the email. The send itself is never retried, because the
email may have gone out, and a tool error (the server answered with an error) is never retried:
running again does not help until the server is fixed.

`--force` deliberately re-publishes: the entry is appended again and the email is sent again,
whatever the Doc or publish.json says. A forced run is only retried before its append.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anyio

from pulse.config import PulseConfig
from pulse.fileio import write_json
from pulse.publish import mcp_client
from pulse.publish.docs_publisher import AppendResult, append_entry, week_in_doc
from pulse.publish.email_sender import SendResult, email_body, send_pulse
from pulse.publish.mcp_client import (
    McpCallError,
    McpSetupError,
    McpToolError,
    describe,
    is_transient,
    load_tool_map,
    verified_caller,
)
from pulse.render.markdown import render_doc_entry, week_heading, week_marker

Echo = Callable[[str], None]


class PublishError(RuntimeError):
    """Publishing stopped; publish.json says which steps succeeded."""

    def __init__(self, message: str, *, record: dict[str, Any] | None = None,
                 setup: bool = False, server_tool: str | None = None,
                 step: str | None = None, hint: str | None = None) -> None:
        super().__init__(message)
        self.record = record or {}
        self.setup = setup
        self.server_tool = server_tool   # the MCP tool that reported an error, if one did
        self.step = step                 # connect | check | append | email_setup | send
        self.hint = hint                 # what to do next, when the CLI's default is wrong


def publish_path(runs_dir: Path, week: str) -> Path:
    return runs_dir / week / "publish.json"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_record(runs_dir: Path, week: str) -> dict[str, Any]:
    path = publish_path(runs_dir, week)
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def load_artifacts(runs_dir: Path, week: str) -> dict[str, str]:
    """The Doc text and email from a validated run; PublishError if the week is not ready."""
    wd = runs_dir / week
    if (wd / "failure.json").is_file():
        raise PublishError(f"{week} failed validation (see {wd / 'failure.json'}); nothing "
                           "is published", setup=True)
    try:
        validation = json.loads((wd / "validation.json").read_text(encoding="utf-8"))
        pulse = json.loads((wd / "pulse.json").read_text(encoding="utf-8"))
        doc = (wd / "pulse.md").read_text(encoding="utf-8")
    except FileNotFoundError as e:
        raise PublishError(f"{week} has no composed pulse yet ({Path(e.filename).name} is "
                           f"missing): run `pulse analyze --week {week}` first",
                           setup=True) from e
    if not validation.get("ok"):
        raise PublishError(f"{week} did not pass validation; nothing is published", setup=True)
    email = pulse.get("email") or {}
    if not email.get("subject") or not email.get("body") or not doc.strip():
        raise PublishError(f"{week}: pulse.json has no email subject or body", setup=True)
    themes = [t.get("label", "") for t in pulse.get("top_themes") or [] if isinstance(t, dict)]
    return {"doc": doc, "subject": email["subject"], "body": email["body"], "themes": themes}


def _can_retry(e: Exception, step: str, record: dict[str, Any]) -> bool:
    """Retry only a transient failure, and only where retrying cannot publish twice."""
    if not is_transient(e):
        return False
    if step in ("connect", "check"):
        return True
    if record["forced"]:          # a forced run appends whatever the Doc says: never twice
        return False
    if step == "append":          # the retry checks the Doc before appending again
        return not record["email_only"]
    return step == "email_setup"  # nothing was sent yet; "send" may have gone out


@dataclass
class Prepared:
    """Everything a publish run needs before its first MCP call (scripted or agent)."""

    art: dict[str, str]          # doc, subject, body from the validated run artifacts
    tool_map: Any
    doc_text: str                # the Doc entry, heading and marker included
    record: dict[str, Any]       # becomes publish.json; keys starting with "_" are not saved
    save: Callable[..., None]


def prepare(cfg: PulseConfig, week: str, *, force: bool = False,
            email_only: bool = False) -> Prepared:
    """Checks that stop before any MCP call (validation, settings, tool map), and the publish
    record, seeded from the earlier publish.json so resuming works. Raises PublishError."""
    if force and email_only:
        raise PublishError("--force and --email-only cannot be combined", setup=True)
    runs = cfg.paths.runs_path
    art = load_artifacts(runs, week)
    if not cfg.publish.doc_id:
        raise PublishError("PULSE_DOC_ID is not set: create the Google Doc by hand once and put "
                           "its ID in .env (the app never creates a doc)", setup=True)
    if not cfg.publish.recipient:
        raise PublishError("PULSE_RECIPIENT is not set: set one address in .env", setup=True)
    for name, server in (("docs", cfg.mcp.docs), ("gmail", cfg.mcp.gmail)):
        if not server.configured:
            raise PublishError(f"mcp.{name} is not configured in pulse.yaml", setup=True)
    try:
        tool_map = load_tool_map(cfg.mcp.tool_map_path)
    except McpSetupError as e:
        raise PublishError(str(e), setup=True) from e

    prior = read_record(runs, week)
    doc_text = render_doc_entry(week, art["doc"])
    marker = week_marker(week)
    if marker not in doc_text:      # render_doc_entry guarantees it; never append without it
        raise PublishError(f"the Doc entry for {week} lacks its marker {marker!r}", setup=True)
    prior_doc, prior_email = prior.get("doc") or {}, prior.get("email") or {}
    record: dict[str, Any] = {
        "week": week, "heading": week_heading(week), "marker": marker,
        "doc_id": cfg.publish.doc_id, "doc_url": cfg.publish.doc_url, "started_at": _now(),
        "status": "running", "forced": force, "email_only": email_only,
        "duplicate_check": None,
        "servers": {"docs": describe(cfg.mcp.docs), "gmail": describe(cfg.mcp.gmail)},
        # The earlier run's entries, kept until a step of this run actually replaces them, so a
        # failed check (or connection) never loses what was already published.
        "doc": dict(prior_doc), "email": dict(prior_email), "calls": [],
    }
    record["_step"] = "connect"
    # What this machine did last time: the entry appended here, and whether it was emailed.
    record["_appended_here"] = prior_doc.get("status") == "ok"
    record["_sent_here"] = prior_email.get("status") == "sent"
    record["_prior"] = {"doc": prior_doc, "email": prior_email}
    if prior.get("email"):
        record["previous_email"] = {k: prior["email"].get(k)
                                    for k in ("status", "message_id", "at")}
    path = publish_path(runs, week)

    def save(status: str | None = None) -> None:
        if status:
            record["status"] = status
            record["finished_at"] = _now()
        write_json(path, {k: v for k, v in record.items() if not k.startswith("_")})

    save()
    return Prepared(art, tool_map, doc_text, record, save)


def publish_week(cfg: PulseConfig, week: str, *, force: bool = False, email_only: bool = False,
                 echo: Echo = lambda _m: None,
                 sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """Publish one week with the fixed (scripted) order: check, append, send. Returns the
    publish.json record (status "sent", or "skipped" when the week was already published);
    raises PublishError on failure.

    `email_only`: send just the email for a week whose entry is already in the Doc; refused if
    the Doc has no entry for the week."""
    p = prepare(cfg, week, force=force, email_only=email_only)
    art, tool_map, doc_text, record, save = p.art, p.tool_map, p.doc_text, p.record, p.save
    marker = record["marker"]
    retries, delay = cfg.mcp.retries, cfg.mcp.retry_delay_seconds
    attempt = 0
    while True:
        try:
            anyio.run(_publish, cfg, tool_map, art, doc_text, record, save, echo)
            break
        except BaseException as exc:
            e = mcp_client.mcp_error(exc)
            if e is None:
                save("failed")
                raise
            step = record["_step"]
            if attempt < retries and _can_retry(e, step, record):
                wait = delay * 2 ** attempt
                attempt += 1
                record.setdefault("retries", []).append(
                    {"step": step, "error": str(e)[:300], "at": _now()})
                if step == "append":
                    # The append may have landed before the timeout. The retry checks the Doc
                    # first, and the marker there now can only be this run's entry.
                    record["_appended_here"] = True
                save()
                echo(f"  ! {e}: retrying in {wait:g}s (retry {attempt} of {retries})")
                sleep(wait)
                continue
            record["failed_step"] = step
            # After the append (email_setup, send) the entry is in the Doc: a partial publish.
            save("partial" if step in ("email_setup", "send") else "failed")
            raise PublishError(str(e), record=record, setup=isinstance(e, McpSetupError),
                               server_tool=e.tool if isinstance(e, McpToolError) else None,
                               step=step) from e
    if record.get("_missing_entry"):
        record["failed_step"] = "check"
        save("failed")
        raise PublishError(
            f"--email-only: the Doc has no entry for {week} ({marker}), so there is nothing to "
            "email a link to", record=record, step="check",
            hint=f"run `pulse publish --week {week}` (without --email-only) to append the entry "
                 "and send the email")
    save(record["status"] if record["status"] == "skipped" else "sent")
    return record


async def _publish(cfg: PulseConfig, tool_map, art, doc_text, record, save,
                   echo: Echo) -> None:
    same_server = cfg.mcp.docs == cfg.mcp.gmail
    for server in ([cfg.mcp.docs] if same_server else [cfg.mcp.docs, cfg.mcp.gmail]):
        waited = await anyio.to_thread.run_sync(mcp_client.wait_until_healthy, server)
        if waited:
            echo(f"  MCP server answered its health check after {waited}s")

    async with mcp_client.open_client(cfg.mcp.docs) as docs_client:
        docs = await verified_caller(docs_client, tool_map, "docs", cfg.mcp.docs)
        try:
            go_on = await _doc_step(docs, cfg, doc_text, record, save, echo)
        finally:
            record["calls"] += docs.calls
        if not go_on:
            return
        if same_server:
            await _email_step(docs_client, cfg, tool_map, art, record, save, echo)
    if not same_server:
        record["_step"] = "email_setup"
        async with mcp_client.open_client(cfg.mcp.gmail) as gmail_client:
            await _email_step(gmail_client, cfg, tool_map, art, record, save, echo)


def _skip(record, save, echo: Echo, reason: str) -> bool:
    echo(f"  {record['week']} already published ({reason}): not appended or emailed again "
         "(use --force to re-publish)")
    prior = record["_prior"]          # keep what earlier runs recorded (e.g. the message ID)
    record["doc"] = dict(prior["doc"]) or {"status": "present", "at": _now()}
    record["email"] = (dict(prior["email"]) if prior["email"].get("status") == "sent"
                       else {"status": "skipped", "reason": f"already published ({reason})"})
    record["status"] = "skipped"
    record["skip_reason"] = f"already published ({reason})"
    save()
    return False


async def _doc_step(docs, cfg: PulseConfig, doc_text, record, save, echo: Echo) -> bool:
    """Append the week's entry unless it is already published. False: stop, send nothing."""
    marker = record["marker"]
    resume = record["_appended_here"] and not record["_sent_here"]
    present = await check_step(docs, cfg, record, save)
    if record["email_only"]:
        if not present:
            record["_missing_entry"] = True       # publish_week refuses; nothing is sent
            return False
        echo(f"  --email-only: {marker} is in the Doc; sending only the email")
        if record["doc"].get("status") != "ok":
            record["doc"] = {"status": "present", "at": _now()}
        save()
        return True
    if not record["forced"]:
        if present and not resume:
            return _skip(record, save, echo, f"{marker} is in the Doc")
        if present and resume:
            echo("  Doc entry already appended: only the email is sent")
            if record["doc"].get("status") == "ok":
                record["doc"] = {**record["doc"], "resumed_at": _now()}
            else:   # a retry after an append that timed out: the entry did land
                record["doc"] = {"status": "ok", "action": "appended",
                                 "confirmed_by": "doc_contains_text", "at": _now()}
            save()
            return True
    await append_step(docs, cfg, doc_text, record, save, echo)
    return True


# ---------------------------------------------------------------------------- shared steps
# Used by the scripted order above and by the publishing agent (publish/agent.py), so both
# write the same publish.json and enforce the same rules.


async def check_step(docs, cfg: PulseConfig, record, save) -> bool:
    """The read-only duplicate check: is the week's marker in the Doc?"""
    record["_step"] = "check"
    try:
        present = await week_in_doc(docs, doc_id=cfg.publish.doc_id, marker=record["marker"])
    except (McpSetupError, McpCallError) as e:
        # Nothing was checked, appended or sent: the earlier doc/email entries stay as they are.
        record["check"] = {"status": "failed", "error": str(e), "at": _now()}
        save()
        raise
    record["duplicate_check"] = "doc_contains_text"
    record["check"] = {"status": "ok", "found": present, "at": _now()}
    return present


async def append_step(docs, cfg: PulseConfig, doc_text: str, record, save,
                      echo: Echo) -> AppendResult:
    """Append the validated entry once. Callers decide whether appending is allowed."""
    record["_step"] = "append"
    try:
        r = await append_entry(docs, doc_id=cfg.publish.doc_id, content=doc_text)
    except (McpSetupError, McpCallError) as e:
        previous = record["_prior"]["doc"]
        record["doc"] = {"status": "failed", "error": str(e), "at": _now(),
                         **({"previous": previous} if previous else {})}
        save()
        raise
    record["doc"] = {"status": "ok", "action": "appended", "doc_title": r.title,
                     "chars": r.chars, "content_sha256": _sha(doc_text), "at": _now()}
    record["_appended_here"] = True     # a retry from here on sends only the email
    save()
    echo(f"  Doc entry '{record['heading']}' appended: {cfg.publish.doc_url}")
    return r


async def _email_step(client, cfg: PulseConfig, tool_map, art, record, save,
                      echo: Echo) -> None:
    record["_step"] = "email_setup"
    gmail = await verified_caller(client, tool_map, "gmail", cfg.mcp.gmail)
    await send_step(gmail, cfg, art, record, save, echo)


async def send_step(gmail, cfg: PulseConfig, art, record, save, echo: Echo,
                    intro: str = "") -> SendResult:
    """Send the one email to PULSE_RECIPIENT (taken from config, never from a caller). Never
    retried. `intro` is an optional first line, written and checked by the agent."""
    to = cfg.publish.recipient
    record["_step"] = "send"
    intro = intro.strip()
    try:
        text = f"{intro}\n\n{art['body']}" if intro else art["body"]
        body = email_body(text, cfg.publish.doc_url or "")
        r = await send_pulse(gmail, recipient=to, to=to, subject=art["subject"], body=body)
    except (McpSetupError, McpCallError) as e:
        previous = record["_prior"]["email"]
        record["email"] = {"status": "failed", "error": str(e), "at": _now(),
                           **({"previous": previous} if previous else {})}
        save()
        raise
    finally:
        record["calls"] += gmail.calls
    record["email"] = {"status": "sent", "message_id": r.message_id, "to": to,
                       "subject": art["subject"], "body_sha256": _sha(body), "at": _now(),
                       **({"intro": intro} if intro else {})}
    save()
    echo(f"  Email sent to {to} (message id: {r.message_id})")
    return r
