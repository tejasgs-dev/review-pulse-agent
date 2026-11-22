# Groww Weekly Review Pulse

Turns public Groww Play Store and App Store reviews into a one-page weekly pulse, published to Google Docs with a Gmail draft, through MCP servers only.

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

Publishing to Google Docs and Gmail arrives in Phase 5 (`pulse run` without `--dry-run` stops before publishing until then). A full setup guide arrives in Phase 6.
