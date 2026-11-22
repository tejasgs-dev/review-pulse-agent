"""Email the week's pulse to PULSE_RECIPIENT through MCP (architecture §4.7).

- The email is sent only after the Doc entry exists; its body ends with the Doc link.
- `send_email` is called with exactly one recipient, PULSE_RECIPIENT, and nothing else (no cc,
  no bcc). The tool map cannot add recipients (mcp_client.SEND_ARGS).
"""

from __future__ import annotations

from dataclasses import dataclass

from pulse.publish.mcp_client import Caller, McpCallError, McpSetupError


@dataclass
class SendResult:
    message_id: str


def email_body(body: str, doc_url: str) -> str:
    """The pulse text, then the Doc link (§7.2)."""
    if not doc_url:
        raise McpSetupError("the email needs the Doc URL (PULSE_DOC_ID)")
    return body.rstrip("\n") + f"\n\nFull pulse: {doc_url}\n"


async def send_pulse(caller: Caller, *, recipient: str, to: str, subject: str,
                     body: str) -> SendResult:
    """Send to `to`, which must be the configured recipient (one address)."""
    if not recipient or to.strip().lower() != recipient.strip().lower() or "," in to:
        raise McpSetupError("send_email is only ever called for PULSE_RECIPIENT")
    r = await caller.call("send_email", to=recipient, subject=subject, body=body)
    message_id = r.groups.get("message_id")
    if not message_id:
        raise McpCallError("send_email succeeded but its result had no message ID")
    return SendResult(message_id)
