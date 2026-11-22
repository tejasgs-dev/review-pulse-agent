"""An in-process stand-in for gworkspace-mcp (github.com/tejasgs-dev/GmailGdocMCPServer).

Same tool names, arguments and result text as the real server, including its habit of
reporting errors as plain text instead of `isError`. State lives in memory, every call is
recorded, and `fail` / `delay` inject errors and slow calls. `create_doc` and the draft tools
exist (as on the real server) so tests can prove they are never called.

`with_contains=True` adds `doc_contains_text`, the read-only tool the pulse asks the server to
add (README, "No double sending"); without it the server matches today's gworkspace-mcp.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import anyio
from mcp.server.mcpserver import MCPServer


@dataclass
class FakeWorkspace:
    docs: dict[str, dict[str, str]] = field(default_factory=dict)     # id -> title, content
    drafts: dict[str, dict[str, str]] = field(default_factory=dict)   # id -> to, subject, body
    calls: list[tuple[str, dict]] = field(default_factory=list)
    fail: dict[str, str] = field(default_factory=dict)       # tool -> text returned instead
    delay: dict[str, float] = field(default_factory=dict)    # tool -> seconds to sleep
    sent: list[dict] = field(default_factory=list)            # to, subject, body
    with_contains: bool = False
    _n: int = 0

    def _id(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}{self._n:04d}"

    def tool_names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def server(self) -> MCPServer:
        ws = self
        s = MCPServer("gworkspace")

        async def enter(name: str, args: dict) -> str | None:
            ws.calls.append((name, args))
            if name in ws.delay:
                await anyio.sleep(ws.delay[name])
            return ws.fail.get(name)

        @s.tool()
        async def send_email(to: str, subject: str, body: str, cc: str | None = None,
                             bcc: str | None = None) -> str:
            """Send a plain-text email from the authenticated Gmail account."""
            args = {"to": to, "subject": subject, "body": body, "cc": cc, "bcc": bcc}
            if (err := await enter("send_email", args)) is not None:
                return err
            ws.sent.append({"to": to, "subject": subject, "body": body})
            return f"Email sent to {to} (message id: {ws._id('msg')})"

        @s.tool()
        async def create_draft(to: str, subject: str, body: str, cc: str | None = None,
                               bcc: str | None = None) -> str:
            """Save a plain-text email as a Gmail draft (it is not sent)."""
            args = {"to": to, "subject": subject, "body": body}
            if (err := await enter("create_draft", args)) is not None:
                return err
            draft_id = ws._id("r-draft")
            ws.drafts[draft_id] = args
            return f"Draft created for {to} (draft id: {draft_id})"

        @s.tool()
        async def update_draft(draft_id: str, to: str, subject: str, body: str,
                               cc: str | None = None, bcc: str | None = None) -> str:
            """Replace the recipients, subject and body of an existing Gmail draft."""
            args = {"draft_id": draft_id, "to": to, "subject": subject, "body": body}
            if (err := await enter("update_draft", args)) is not None:
                return err
            if draft_id not in ws.drafts:
                return "Draft not found — check the draft ID."
            ws.drafts[draft_id] = {"to": to, "subject": subject, "body": body}
            return f"Draft updated for {to} (draft id: {draft_id})"

        @s.tool()
        async def append_to_doc(document_id: str, content: str,
                                add_newline_before: bool = True) -> str:
            """Append text to the end of an existing Google Doc."""
            args = {"document_id": document_id, "content": content,
                    "add_newline_before": add_newline_before}
            if (err := await enter("append_to_doc", args)) is not None:
                return err
            if document_id not in ws.docs:
                return "Document not found — check the document ID."
            doc = ws.docs[document_id]
            doc["content"] += ("\n" if add_newline_before else "") + content
            return f"Appended {len(content)} characters to '{doc['title']}'."

        if ws.with_contains:
            @s.tool()
            async def doc_contains_text(document_id: str, text: str) -> str:
                """Check whether a Google Doc's body contains the given text (read-only)."""
                args = {"document_id": document_id, "text": text}
                if (err := await enter("doc_contains_text", args)) is not None:
                    return err
                if document_id not in ws.docs:
                    return "Document not found — check the document ID."
                doc = ws.docs[document_id]
                found = text.strip() in doc["content"]
                return f"Text {'found' if found else 'not found'} in '{doc['title']}'."

        @s.tool()
        async def create_doc(title: str, content: str) -> str:
            """Create a new Google Doc with the given title and plain-text content."""
            if (err := await enter("create_doc", {"title": title, "content": content})):
                return err
            doc_id = ws._id("doc")
            ws.docs[doc_id] = {"title": title, "content": content}
            return (f"Created document '{title}' (id: {doc_id}, "
                    f"url: https://docs.google.com/document/d/{doc_id}/edit)")

        @s.tool()
        async def replace_doc_body(document_id: str, content: str) -> str:
            """Replace the entire body of an existing Google Doc with plain text."""
            args = {"document_id": document_id, "content": content}
            if (err := await enter("replace_doc_body", args)) is not None:
                return err
            if document_id not in ws.docs:
                return "Document not found — check the document ID."
            ws.docs[document_id]["content"] = content
            title = ws.docs[document_id]["title"]
            return f"Replaced the body of '{title}' with {len(content)} characters."

        @s.tool()
        async def find_doc(title: str) -> str:
            """Find a Google Doc created by this server, by exact title."""
            if (err := await enter("find_doc", {"title": title})) is not None:
                return err
            hits = [i for i, d in ws.docs.items() if d["title"] == title.strip()]
            if not hits:
                return f"No document found with title '{title.strip()}'."
            doc_id = hits[-1]
            many = len(hits) > 1
            suffix = f" — the most recently modified of {len(hits)} matches" if many else ""
            return (f"Found document '{title.strip()}' (id: {doc_id}, "
                    f"url: https://docs.google.com/document/d/{doc_id}/edit){suffix}")

        return s
