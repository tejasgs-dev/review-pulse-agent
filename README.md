# ReviewPulse

[![CI](https://github.com/tejasgs-dev/review-pulse-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/tejasgs-dev/review-pulse-agent/actions/workflows/ci.yml)

**ReviewPulse: weekly insights from Groww app reviews.**

ReviewPulse turns public Groww Play Store and App Store reviews into a one-page weekly pulse, appended to a Google Doc and emailed to you, through MCP servers only.

By default the analysis is **free and runs on your machine**: themes come from sentence embeddings (model2vec) and k-means, and the pulse is written by rules and templates. No API key is needed and no review text leaves the machine. The Claude API is an optional engine (`--engine llm`).

- What it must do: [docs/problemStatement.md](docs/problemStatement.md)
- How it is built: [docs/architecture.md](docs/architecture.md)
- Build order: [docs/implementation-plan.md](docs/implementation-plan.md)
- Edge cases: [docs/edge-case.md](docs/edge-case.md)

## Contents

- [Setup](#setup)
- [Connecting the MCP server (Google Doc + email)](#connecting-the-mcp-server-google-doc--email)
- [Configuration](#configuration)
- [Commands](#commands)
- [Exit codes](#exit-codes)
- [Running on GitHub Actions](#running-on-github-actions)
- [Website](#website)
- [Troubleshooting](#troubleshooting)

## Setup

You need Python 3.11 or newer and git.

```bash
git clone <this repo> && cd <repo>
python -m venv .venv
.venv\Scripts\activate          # macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"         # add ,llm for the optional Claude engine: ".[dev,llm]"
copy .env.example .env          # macOS/Linux: cp .env.example .env
pulse doctor --offline          # config, theme catalogue, model cache
pytest                          # the offline test suite (no network, no MCP, no API)
```

A first offline pulse, with no Google setup at all:

```bash
pulse ingest --week 2026-W46                 # fetch both stores into data/reviews.parquet
pulse run --dry-run --skip-ingest --week 2026-W46   # writes runs/2026-W46/pulse.md
```

The first analysis downloads the embedding model (about 30 MB, pinned revision) into `data/models/`; later runs work offline. `data/` and `runs/` hold review data and are never committed.

## Connecting the MCP server (Google Doc + email)

Publishing goes through one MCP server, [gworkspace-mcp](https://github.com/tejasgs-dev/GmailGdocMCPServer), deployed on Render (`mcp:` in `config/pulse.yaml`, Streamable HTTP). **The app never talks to Google itself**: the server holds the Google OAuth tokens and makes the API calls. For each week the app:

1. **checks** the Doc for the week's marker, e.g. `(2026-W46)`, with the read-only `doc_contains_text` tool. If it is there, the week is already published: nothing else happens and the run succeeds;
2. **appends** the pulse to one Google Doc that you created by hand (`append_to_doc`): a separator line, a heading like `ReviewPulse — Week of 9 Nov 2026 (2026-W46)`, then the pulse as plain text;
3. **sends** one email to `PULSE_RECIPIENT` with the pulse text and the Doc link (`send_email`).

It never calls `create_doc`. `send_email` is the only sending tool it may call, only for step 3, and only to `PULSE_RECIPIENT` (no cc or bcc); any other tool map is refused at startup.

Setup, once:

1. **Create the Google Doc by hand** (any title, for example "ReviewPulse: Groww app reviews"), in the Google account the server is authorized for, or share it with that account.
2. **In `.env`** set:
   - `PULSE_DOC_ID`: the Doc's ID or URL. Publishing stops before any call if it is missing.
   - `PULSE_RECIPIENT`: the one address the pulse is emailed to.
   - `PULSE_MCP_API_KEY`: the server's `MCP_API_KEY`. It is sent as `Authorization: Bearer …` and never logged.
3. **On the server**: `ENABLE_SEND_EMAIL=true`, and `ALLOWED_RECIPIENTS` must include `PULSE_RECIPIENT`.
4. **Check it**: `pulse doctor` wakes the server, lists its tools and checks each mapped tool exists with matching arguments.

**Authentication.** There are two credentials, and they fail differently:

- *The API key* (`PULSE_MCP_API_KEY`). If it is wrong, connecting fails with "the server rejected the API key"; this is not retried. Fix `.env` (or the Actions secret).
- *The server's Google access.* If the server's Google token expires or is revoked, the server's tools answer with Google errors (`invalid_grant`, `401`, "Permission denied"). Re-authorize Google **on the server** (see its README); nothing changes in this repo. Running again does not help until then, and the error message says so.

The free Render service sleeps after 15 idle minutes. The app polls `health_url` for up to `wake_timeout_seconds` (120 s) before connecting.

**No double publishing.** The Doc is the record: a week whose marker is in the Doc is never appended or emailed again, so a repeated run (or a retried Actions job) is safe. `--force` deliberately re-publishes. If the email failed after the append, `pulse publish` resumes from `runs/{week}/publish.json` locally, and `pulse publish --email-only` sends only the email anywhere, including on GitHub Actions where `runs/` is not kept.

## Configuration

| File | What it holds |
|---|---|
| `.env` (git-ignored; copy `.env.example`) | Secrets and per-user values: `PULSE_DOC_ID`, `PULSE_RECIPIENT`, `PULSE_MCP_API_KEY`, and `ANTHROPIC_API_KEY` for the LLM engine only. Environment variables take precedence, so on Actions they come from repository secrets. |
| `config/pulse.yaml` | Everything else. `${VAR}` references are filled from the environment. |
| `config/themes.yaml` | The theme catalogue for the local engine: stable IDs, labels, keywords, and the summary/action/signal templates. |
| `src/pulse/publish/tool_map.yaml` | Maps the app's actions (`doc_contains_text`, `append_to_doc`, `send_email`) to the server's tool names, arguments and result patterns. Checked against the server by `pulse doctor`. |

The `pulse.yaml` sections you are most likely to change:

| Section | Key settings |
|---|---|
| `product` | `play_package`, `app_store_id`, `app_store_country` |
| `window` | `weeks_back` (8–12), `timezone` |
| `ingest` | `request_delay_seconds`, `max_retries` (per feed page, exponential backoff), `min_words`, `play_public.enabled` (false = read Play Console exports from `data/imports/play/` instead) |
| `privacy` | `number_min_digits`, `reference_min_digits` |
| `analysis` | `engine` (`local` or `llm`), `min_reviews` (50: below this the run stops), `max_words` (250), `max_themes`, `top_themes`, `local.*` (model, revision, clustering and word lists) |
| `llm` | `model`, `effort` (LLM engine only) |
| `publish` | `doc_id`, `recipient` (from `.env`) |
| `mcp` | server `url`, `health_url`, timeouts; `retries` (2) and `retry_delay_seconds` (10, doubled each retry) for lost connections and timeouts |

## Commands

```bash
pulse run      [--week 2026-W47] [--weeks-back 10] [--dry-run] [--skip-ingest] [--engine local|llm]
pulse ingest   [--source play_store|app_store|all] [--week ...] [--full]
pulse analyze  [--week ...] [--engine local|llm]
pulse publish  [--week ...] [--force | --email-only]
pulse doctor   [--live | --offline]
pulse export-site [--week ...] [--out site/pulses]
pulse --version
```

| Command | What it does |
|---|---|
| `pulse run` | The whole pipeline for one week: ingest, PII scrub, themes, compose and validate, publish. Ends with a summary. |
| `pulse run --dry-run` | Every stage, writes `runs/{week}/pulse.md`, but makes no MCP call. |
| `pulse run --skip-ingest` | Uses `data/reviews.parquet` as it is. |
| `pulse ingest` | Fetches new reviews (incremental; `--full` re-fetches the whole window) into `data/reviews.parquet`. |
| `pulse analyze` | Scrub, cluster, compose, validate; no publishing. |
| `pulse publish` | Publishes a validated week from `runs/{week}/`. Skips a week already in the Doc. |
| `pulse publish --force` | Deliberately re-publishes: appends again and emails again. |
| `pulse publish --email-only` | Sends only the email, for a week whose entry is already in the Doc. Refuses if it is not there. |
| `pulse doctor` | Checks config, `.env` values, the theme catalogue or API key, and (with `--live`, the default) the MCP server and its tools. |
| `pulse export-site` | Writes a validated week's pulse data for the website to `site/pulses/{week}.json` and adds it to `site/pulses/index.json`. Allowlisted fields only (see [Website](#website)). |

`--week` defaults to the ISO week that just ended (Monday–Sunday, IST).

**When a source fails.** Feed requests are retried with backoff first. If the Play Store (or App Store) still fails, the run continues with the other store and publishes as usual, but the pulse's source line ends with "Play Store unavailable this week", and the run summary says so too. The run stops with exit 7 if fewer than `analysis.min_reviews` (50) reviews are left, and with exit 5 if every source failed.

**The run summary.** `pulse run` and `pulse publish` end with a block like this (on GitHub Actions it is also added to the job's summary page):

```
── ReviewPulse · pulse run 2026-W46 ──────────────────────────────
  Result            OK (exit 0: success)
  Engine            local
  Play Store        failed, 0 in the window (HTTP 403)
  App Store         ok, fetched 412, 412 in the window
  Note              Play Store unavailable this week
  Reviews analysed  412 (minimum 50)
  Top themes        Customer support · Charges & fees · Money stuck & withdrawals
  Words             238/250
  Publish           sent (Doc entry appended, email sent)
  Time              41.3s
```

It holds counts, statuses and theme labels only: no review text, no recipient address, no Doc link.

## Exit codes

| Code | Meaning | What to do |
|---|---|---|
| 0 | Success (also: the week was already published) | — |
| 1 | Unexpected error (a bug); the traceback is printed | Read the traceback; open an issue |
| 2 | Configuration error (`pulse.yaml`, `.env`, theme catalogue, a bad option) | Fix the named setting |
| 4 | `pulse doctor` found a problem | Fix each `FAIL` line |
| 5 | Every review source failed | See the source errors; try again later |
| 6 | The review store could not be read (written by an older version) | Move `data/reviews.parquet` and `data/ingest_state.json` aside, then run `pulse ingest --full` |
| 7 | Too few reviews in the window (`analysis.min_reviews`) | Check the sources; a store may be unavailable |
| 8 | Theme analysis failed (no stable themes, too much `other`) | See `runs/{week}/manifest.json` |
| 9 | Claude API failure (LLM engine) | See the message and `runs/{week}/llm/` |
| 10 | The pulse failed validation; nothing was published | See `runs/{week}/failure.json` |
| 11 | Publishing through MCP failed | Follow the hint printed with the error (re-run, `--email-only`, or fix the server) |

## Running on GitHub Actions

The scheduled workflow itself is set up in Phase 7. What it needs from this repo:

- **Secrets**: `PULSE_DOC_ID`, `PULSE_RECIPIENT`, `PULSE_MCP_API_KEY` as repository secrets, passed to the job as environment variables (no `.env` file on the runner).
- **Commands**: `pulse doctor && pulse run`. Every failure exits non-zero, so the job fails visibly; the run summary appears on the job page.
- **Cache `data/models/`** (keyed on `analysis.local.model_revision`), so the embedding model is not downloaded every week. The download is retried twice if it is.
- **Nothing is kept between runs.** Each run backfills the whole review window, and `publish.json` is gone afterwards: the Doc marker stops a second publish, and `pulse publish --email-only` sends a missed email. Do not upload `runs/` or `data/` as workflow artifacts in a public repository: they hold review text.
- **Public logs.** In a public repository anyone can read Actions logs. When `GITHUB_ACTIONS` is set, `pulse run` does not print the composed pulse (it is still in the Doc and the email), and the recipient's address is replaced by `PULSE_RECIPIENT` in every line (in any letter case), including server errors that quote it, config errors about an invalid `PULSE_RECIPIENT`, and unexpected errors with their tracebacks. The end-of-run summary never holds review text, the recipient or the Doc link.

## Website

`site/` is a static, read-only website for the pulses: **Dashboard** (latest week, with a week selector), **Past weeks** and **How it works**. It is plain HTML, CSS and JavaScript, with no build step, no backend and no secrets. The design came from Google Stitch; that export is kept in `site/stitch-export/` as a reference only.

**Data.** The pages read `site/pulses/index.json` and `site/pulses/{week}.json`, which `pulse export-site --week <W>` writes from `runs/<W>/`. These files are committed. They hold counts, theme labels and shares, the top-3 summaries, the three quotes (theme, text, rating, store) and the three actions, and nothing else: no review IDs, no keywords, no other review text (architecture §6, written exception). The export refuses a week that failed validation and re-scans every text field for PII.

```bash
pulse export-site --week 2026-W46
git add site/pulses && git commit -m "Add the 2026-W46 pulse to the site"
```

**Buttons.** *Pulse check* opens the `pulse-check.yml` GitHub Actions workflow in a new tab (the workflow is added in Phase 7). *System status* checks the MCP server's `GET /healthz` for up to 90 seconds ("Checking…", then "Waking up…", then "Online" or "Offline"), and shows the workflow's status badge and the latest published week. The browser can only read `/healthz` if the server sends `Access-Control-Allow-Origin` on that route.

**Preview locally.**

```bash
python -m http.server 8000 --directory site    # then open http://localhost:8000
```

**Hosting on Render (Static Site).**

1. In Render, choose **New → Static Site** and connect `tejasgs-dev/review-pulse-agent`.
2. Branch: `main`. Root directory: leave empty. Build command: leave empty (or `true` if Render requires one). **Publish directory: `site`**.
3. No environment variables are needed.
4. Create the site. Every push to `main` redeploys it, so a committed `pulse export-site` shows up within a minute or two.
5. Optional: under **Redirects/Rewrites**, nothing is needed (the pages are plain files). Under **Headers**, you can add `Cache-Control: no-cache` for `/pulses/*` so a new week shows immediately.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `Play Store failed (HTTP 403)` or `HTTP 429`, and the pulse says "Play Store unavailable this week" | Google Play blocks or throttles some networks (often cloud runners). The run goes on with the App Store. To stop relying on scraping, set `ingest.play_public.enabled: false` and drop Play Console exports into `data/imports/play/`. |
| Exit 7: `fewer than analysis.min_reviews=50 reviews` | One store is unavailable, or the window is quiet. Check the source lines in the summary. For a local test only, lower `analysis.min_reviews`. |
| Exit 5: every review source failed | Network down, or both stores blocking. Re-run later; check `ingest.user_agent` and `request_delay_seconds`. |
| `embedding model … could not be downloaded after 3 attempts` | No network to Hugging Face. Run once with network, or restore `data/models/` from the cache. Check `analysis.local.model` / `model_revision`. |
| `MCP server is not healthy at … after 120s` | The Render service did not wake up. It is retried; if it keeps failing, check the service on Render. Raise `mcp.docs.wake_timeout_seconds` for slow cold starts. |
| `the server rejected the API key` | `PULSE_MCP_API_KEY` does not match the server's `MCP_API_KEY`. |
| `the MCP server's '…' tool returned an error` (`invalid_grant`, `401`, Permission denied) | The server's Google access expired, was revoked, or cannot open the Doc. Re-authorize on the server, or share the Doc with its account. Then run the command the hint names. |
| `Document not found` | `PULSE_DOC_ID` is wrong, or the Doc is not shared with the server's Google account. The app never creates a doc. |
| `recipient not allowed` | Add `PULSE_RECIPIENT` to the server's `ALLOWED_RECIPIENTS`. |
| The Doc entry is there, but no email came (exit 11, "the Doc entry was appended, but no email was sent") | Run `pulse publish --week <week> --email-only`. Locally, plain `pulse publish` also resumes. |
| `Skipped …: already published` | The week's marker is already in the Doc. This is success. `--force` re-publishes on purpose. |
| `UnicodeEncodeError` on Windows | The CLI switches its output to UTF-8; for other tools set `PYTHONIOENCODING=utf-8`. |
| Exit 10: the pulse failed validation | Nothing was published. `runs/{week}/failure.json` lists each failed check; usually a template or catalogue fix in `config/themes.yaml`. |
