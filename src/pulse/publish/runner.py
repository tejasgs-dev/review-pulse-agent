"""Stage 7: publish a validated week through MCP only: append it to the shared Google Doc,
then email it to PULSE_RECIPIENT.

Reads the saved run artifacts (pulse.md is the Doc text; pulse.json holds the email subject
and body), so `pulse publish` never re-runs the analysis. Refuses to publish a week whose
validation failed (VAL-07), and stops before any call when PULSE_DOC_ID or PULSE_RECIPIENT is
missing.

No double sending. Before appending or sending:
1. If the server has the read-only `doc_contains_text` tool, the Doc is checked for the
   week's heading ("Week of 9 Nov 2026 (2026-W46)"). Found → both steps are skipped. This
   works on GitHub Actions, where runs/ is not kept.
2. `runs/{week}/publish.json` (rewritten after every step) is checked too. A week whose email
   was sent is not sent again without `--force`. A week whose Doc entry was appended but whose
   email failed resumes from the email (the entry is not appended twice).
With `--force` the email is sent again; the Doc entry is still appended only when it is not
already there.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anyio

from pulse.config import PulseConfig
from pulse.fileio import write_json
from pulse.publish import mcp_client
from pulse.publish.docs_publisher import append_entry, week_in_doc
from pulse.publish.email_sender import email_body, send_pulse
from pulse.publish.mcp_client import (
    McpCallError,
    McpSetupError,
    describe,
    load_tool_map,
    verified_caller,
)
from pulse.render.markdown import render_doc_entry, week_heading

Echo = Callable[[str], None]


class PublishError(RuntimeError):
    """Publishing stopped; publish.json says which steps succeeded."""

    def __init__(self, message: str, *, record: dict[str, Any] | None = None,
                 setup: bool = False) -> None:
        super().__init__(message)
        self.record = record or {}
        self.setup = setup


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
    return {"doc": doc, "subject": email["subject"], "body": email["body"]}


def publish_week(cfg: PulseConfig, week: str, *, force: bool = False,
                 echo: Echo = lambda _m: None) -> dict[str, Any]:
    """Publish one week. Returns the publish.json record (status "sent", or "skipped" when the
    week was already published); raises PublishError on failure."""
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
    heading = week_heading(week)
    if (prior.get("email") or {}).get("status") == "sent" and not force:
        echo(f"  {week} was already emailed at {prior['email'].get('at')} "
             f"(publish.json); use --force to send it again")
        return {**prior, "status": "skipped",
                "skip_reason": "already sent (publish.json); use --force to re-send"}

    doc_text = render_doc_entry(week, art["doc"])
    prior_doc = prior.get("doc") or {}
    record: dict[str, Any] = {
        "week": week, "heading": heading, "doc_id": cfg.publish.doc_id,
        "doc_url": cfg.publish.doc_url, "started_at": _now(), "status": "running",
        "forced": force, "duplicate_check": None,
        "servers": {"docs": describe(cfg.mcp.docs), "gmail": describe(cfg.mcp.gmail)},
        "doc": prior_doc if prior_doc.get("status") == "ok" else {},
        "email": {}, "calls": [],
    }
    if prior.get("email"):
        record["previous_email"] = {k: prior["email"].get(k)
                                    for k in ("status", "message_id", "at")}
    path = publish_path(runs, week)

    def save(status: str | None = None) -> None:
        if status:
            record["status"] = status
            record["finished_at"] = _now()
        write_json(path, record)

    save()
    try:
        anyio.run(_publish, cfg, tool_map, art, doc_text, record, save, echo)
    except BaseException as exc:
        e = mcp_client.mcp_error(exc)
        if e is None:
            save("failed")
            raise
        save("partial" if record["doc"].get("status") in ("ok", "present") else "failed")
        raise PublishError(str(e), record=record, setup=isinstance(e, McpSetupError)) from e
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
        async with mcp_client.open_client(cfg.mcp.gmail) as gmail_client:
            await _email_step(gmail_client, cfg, tool_map, art, record, save, echo)


async def _doc_step(docs, cfg: PulseConfig, doc_text, record, save, echo: Echo) -> bool:
    """Append the week's entry unless it is already there. False: stop, nothing to send."""
    doc_id, heading = cfg.publish.doc_id, record["heading"]
    try:
        present = await week_in_doc(docs, doc_id=doc_id, heading=heading)
        record["duplicate_check"] = "publish.json" if present is None else "doc_contains_text"
        if present and not record["forced"]:
            echo(f"  '{heading}' is already in the Doc: not appended or emailed again "
                 "(use --force to re-send the email)")
            record["doc"] = {"status": "present", "at": _now()}
            record["email"] = {"status": "skipped", "reason": "week heading already in the doc"}
            record["status"] = "skipped"
            save()
            return False
        if present or (present is None and record["doc"].get("status") == "ok"):
            where = "in the Doc" if present else "recorded in publish.json"
            echo(f"  Doc entry already {where}: not appended again")
            record["doc"] = {**record["doc"], "status": record["doc"].get("status") or "present",
                             "skipped_at": _now()}
            save()
            return True
        r = await append_entry(docs, doc_id=doc_id, content=doc_text)
    except (McpSetupError, McpCallError) as e:
        record["doc"] = {"status": "failed", "error": str(e), "at": _now()}
        save()
        raise
    record["doc"] = {"status": "ok", "action": "appended", "doc_title": r.title,
                     "chars": r.chars, "content_sha256": _sha(doc_text), "at": _now()}
    save()
    echo(f"  Doc entry '{heading}' appended: {cfg.publish.doc_url}")
    return True


async def _email_step(client, cfg: PulseConfig, tool_map, art, record, save,
                      echo: Echo) -> None:
    gmail = await verified_caller(client, tool_map, "gmail", cfg.mcp.gmail)
    to = cfg.publish.recipient
    try:
        body = email_body(art["body"], cfg.publish.doc_url or "")
        r = await send_pulse(gmail, recipient=to, to=to, subject=art["subject"], body=body)
    except (McpSetupError, McpCallError) as e:
        record["email"] = {"status": "failed", "error": str(e), "at": _now()}
        save()
        raise
    finally:
        record["calls"] += gmail.calls
    record["email"] = {"status": "sent", "message_id": r.message_id, "to": to,
                       "subject": art["subject"], "body_sha256": _sha(body), "at": _now()}
    save()
    echo(f"  Email sent to {to} (message id: {r.message_id})")
