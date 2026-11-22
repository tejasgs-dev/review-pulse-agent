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
pulse publish --week 2026-W46 --force   # deliberately re-publish (append + email again)
pulse run --week 2026-W46          # the whole pipeline, then publish
```

`runs/{week}/publish.json` records each step. If the email fails after the Doc entry was appended, running `pulse publish` again sends the email without appending a second entry. The free Render service sleeps when idle, so the first call can wait up to a minute for it to wake.

#### No double publishing

Each week's heading ends with that week's **marker**, for example `(2026-W46)` in `Week of 9 Nov 2026 (2026-W46)`. Before appending or sending, the publisher asks the server's read-only `doc_contains_text` tool whether the marker is already in the Doc (exact, case-sensitive). If it is, the run logs "already published", skips both the append and the email, and exits successfully. The Doc is the record, so this also works on GitHub Actions, where `runs/` is not kept.

- `pulse doctor` checks that the server offers `doc_contains_text`. Without it, nothing is published.
- `--force` deliberately re-publishes: it appends the entry again and sends the email again.
- If the email failed after the append, a local re-run sends only the email (from `runs/{week}/publish.json`). On Actions, where that file is not kept, the week counts as published; the failed run shows the error, and `--force` sends it again.
