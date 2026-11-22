# Groww Weekly Review Pulse

Turns public Groww Play Store and App Store reviews into a one-page weekly pulse, appended to a Google Doc and emailed to you, through MCP servers only.

By default the analysis is **free and runs on your machine**: themes come from sentence embeddings (model2vec) and k-means, and the pulse is written by rules and templates. No API key is needed and no review text leaves the machine. The Claude API is an optional engine (`--engine llm`).

- What it must do: [docs/problemStatement.md](docs/problemStatement.md)
- How it is built: [docs/architecture.md](docs/architecture.md)
- Build order: [docs/implementation-plan.md](docs/implementation-plan.md)
- Edge cases: [docs/edge-case.md](docs/edge-case.md)

## Quick start

```bash
python -m venv .venv
.venv\Scripts\activate          # macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"         # add ,llm for the optional Claude engine
copy .env.example .env          # then fill in the values (publishing only)
pulse doctor
pulse ingest --week 2026-W46
pulse run --dry-run --skip-ingest --week 2026-W46   # writes runs/2026-W46/pulse.md
pytest
```

The first analysis downloads the embedding model (about 30 MB, pinned revision) into `data/models/`; later runs work offline. `data/` and `runs/` hold review data and are never committed.

### Publishing (Google Doc + email)

Publishing goes through one MCP server, [gworkspace-mcp](https://github.com/tejasgs-dev/GmailGdocMCPServer), deployed on Render (`mcp:` in `config/pulse.yaml`). The app never talks to Google itself. For each week it:

1. **appends** the pulse to one Google Doc that you created by hand (`append_to_doc`): a separator line, a heading like `Week of 9 Nov 2026 (2026-W46)`, then the pulse as plain text;
2. **sends** one email to `PULSE_RECIPIENT` with the pulse text and the Doc link (`send_email`).

It never calls `create_doc`. `send_email` is the only sending tool it may call, only for this step, and only to `PULSE_RECIPIENT` (no cc or bcc); otherwise the tool map is refused at startup.

Setup:

1. Create the Google Doc by hand once (any title, for example "Groww Weekly Review Pulse").
2. In `.env`, set `PULSE_DOC_ID` (the Doc's ID or URL), `PULSE_RECIPIENT` (one address) and `PULSE_MCP_API_KEY` (the server's `MCP_API_KEY`). Publishing stops with a clear error if `PULSE_DOC_ID` is missing.
3. On the server, `ENABLE_SEND_EMAIL=true` and `ALLOWED_RECIPIENTS` must include `PULSE_RECIPIENT`.
4. Check the setup, then publish a week that passed validation:

```bash
pulse doctor                       # health check, API key, list_tools, tool-map coverage
pulse publish --week 2026-W46      # from runs/2026-W46/ (pulse.md, pulse.json)
pulse publish --week 2026-W46 --force   # send that week's email again
pulse run --week 2026-W46          # the whole pipeline, then publish
```

`runs/{week}/publish.json` records each step. If the email fails after the Doc entry was appended, running `pulse publish` again sends the email without appending a second entry. The free Render service sleeps when idle, so the first call can wait up to a minute for it to wake.

#### No double sending

Before appending or sending, the publisher checks whether the week was already published:

- **In the Doc**, when the server has the read-only `doc_contains_text` tool: if the week's heading (`Week of 9 Nov 2026 (2026-W46)`) is already in the Doc, both steps are skipped. This works on GitHub Actions, where `runs/` is not kept.
- **In `runs/{week}/publish.json`**: a week whose email was sent is not sent again without `--force`. Until the server has `doc_contains_text`, this is the only guard, so it only works where `runs/` is kept (`pulse doctor` warns about this).

`--force` sends the email again. The Doc entry is still appended only if it is not already there.

To add `doc_contains_text` to gworkspace-mcp, add this to `src/gworkspace_mcp/docs.py`:

```python
def doc_contains_text(service, document_id: str, text: str) -> tuple[bool, str]:
    """Return (found, doc_title): whether ``text`` occurs in the document body. Read-only."""
    doc_id = extract_document_id(document_id)
    needle = (text or "").strip()
    if not needle:
        raise ValueError("'text' must not be empty.")
    if len(needle) > 500:
        raise ValueError("'text' must be at most 500 characters.")
    doc = service.documents().get(
        documentId=doc_id,
        fields="title,body(content(paragraph(elements(textRun(content)))))",
    ).execute()
    body = "".join(
        el.get("textRun", {}).get("content", "")
        for block in (doc.get("body") or {}).get("content") or []
        for el in (block.get("paragraph") or {}).get("elements") or []
    )
    return needle in body, doc.get("title", "(untitled)")
```

Then add this to `src/gworkspace_mcp/server.py`, and add `doc_contains_text` to `TOOLS`:

```python
def doc_contains_text(document_id: str, text: str) -> str:
    """Check whether an existing Google Doc's body contains the given text (read-only).

    Args:
        document_id: The Google Doc ID, or a full URL like
            https://docs.google.com/document/d/<ID>/edit
        text: The text to look for (exact, case-sensitive; max 500 characters).
    """
    try:
        found, title = docs.doc_contains_text(get_docs_service(), document_id, text)
        return f"Text {'found' if found else 'not found'} in '{title}'."
    except Exception as exc:  # noqa: BLE001
        return format_error(exc, context="docs")


TOOLS = (send_email, create_draft, update_draft, append_to_doc, create_doc, replace_doc_body,
         find_doc, doc_contains_text)
```

The tool only reads the Doc, and the server's existing Docs scope already covers it. `src/pulse/publish/tool_map.yaml` already maps it (`Text found in '…'.` / `Text not found in '…'.`). Once the server is redeployed, `pulse doctor` stops warning and publishing uses the check.
