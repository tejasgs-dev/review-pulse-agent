"""Append a week's entry to the one shared Google Doc through MCP (architecture §4.7).

The Doc is made by hand once; its ID is PULSE_DOC_ID. Each week adds a separator line, a
heading like "Week of 9 Nov 2026 (2026-W46)" and the pulse as plain text. The heading always
holds the week's marker, "(2026-W46)". No doc is ever
created (`create_doc` cannot even be mapped).

`week_in_doc` is the duplicate check: the server's read-only `doc_contains_text` tool looks
for the marker (exact, case-sensitive); a week whose marker is in the Doc is already published.
"""

from __future__ import annotations

from dataclasses import dataclass

from pulse.publish.mcp_client import Caller, McpCallError


@dataclass
class AppendResult:
    title: str
    chars: int


async def week_in_doc(caller: Caller, *, doc_id: str, marker: str) -> bool:
    """Whether the week's marker is already in the Doc (exact, case-sensitive)."""
    r = await caller.call("doc_contains_text", doc_id=doc_id, text=marker)
    return r.status == "ok"


async def append_entry(caller: Caller, *, doc_id: str, content: str) -> AppendResult:
    r = await caller.call("append_to_doc", doc_id=doc_id, content=content)
    if r.status == "not_found":
        raise McpCallError(f"the Doc {doc_id} (PULSE_DOC_ID) was not found, or this account "
                           "cannot open it")
    return AppendResult(r.groups.get("title", ""), int(r.groups.get("count") or 0))
