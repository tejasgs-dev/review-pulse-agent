# Groww Weekly Review Pulse

Turns public Groww Play Store and App Store reviews into a one-page weekly pulse, published to Google Docs with a Gmail draft, through MCP servers only.

- What it must do: [docs/problemStatement.md](docs/problemStatement.md)
- How it is built: [docs/architecture.md](docs/architecture.md)
- Build order: [docs/implementation-plan.md](docs/implementation-plan.md)
- Edge cases: [docs/edge-case.md](docs/edge-case.md)

## Quick start (Phase 0)

```bash
python -m venv .venv
.venv\Scripts\activate          # macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"
copy .env.example .env          # then fill in the values
pulse --help
pulse doctor
pytest
```

Only `pulse doctor` does real work so far. The other commands validate their options and exit with code 3 ("not implemented yet"). A full setup guide arrives in Phase 6.
