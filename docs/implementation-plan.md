# Implementation Plan: Groww Weekly Review Pulse

> Built from [problemStatement.md](problemStatement.md) (**what**) and [architecture.md](architecture.md) (**how**). Section references such as §4.3 point to `architecture.md`.

---

## Overview

The work is split into **8 phases**. Each phase leaves the project runnable and ends with explicit exit criteria. The order front-loads the parts that need no external access (ingestion, privacy, analysis), so a complete **offline pulse** (`pulse run --dry-run`) works before any Google integration is attempted.

```
Phase 0 ──▶ Phase 1 ──▶ Phase 2 ──▶ Phase 3 ──▶ Phase 4 ──▶ Phase 5 ──▶ Phase 6 ──▶ Phase 7
Setup &     Ingest &    PII         Theme       Compose,    MCP         Hardening   Schedule &
decisions   normalize   scrubber    clustering  validate,   publishing  & CLI       first live
                                    & ranking   render                              runs
                                                   │
                                                   ▼
                                     ★ M1: offline pulse (dry run)
                                                               ★ M2: Doc + draft via MCP
                                                                                   ★ M3: weekly unattended
```

| Phase | Name | Rough effort | Milestone |
|---|---|---|---|
| 0 | Project setup and open decisions | 0.5–1 day | |
| 1 | Review ingestion and normalization | 1–2 days | |
| 2 | PII scrubber | 1 day | |
| 3 | Theme clustering and ranking | 1.5–2 days | |
| 4 | Composer, validator, renderer | 1.5–2 days | **M1: offline pulse** |
| 5 | MCP publishing (Docs + Gmail) | 1.5–2 days | **M2: end-to-end publish** |
| 6 | Orchestration, reliability, CLI hardening | 1 day | |
| 7 | Scheduling, first live runs, quality review | 1 day, then ongoing | **M3: weekly unattended** |
| 8 | *(Optional)* Enhancements | As needed | |

Phases 1 and 2 can run in parallel once the Phase 0 data models exist. Phase 5 can start alongside Phase 3 or 4, because it depends only on the `Pulse` model and a fake MCP server.

---

## Phase 0: Project Setup and Open Decisions

**Goal:** a project skeleton that installs and runs, with the blocking decisions from §13 settled.

### Tasks
- [x] Create the repository layout from §9: `src/pulse/…`, `tests/`, `config/`, `docs/`.
- [x] Add `pyproject.toml` (Python 3.11+) with these dependencies: `anthropic`, `mcp`, `pydantic`, `pyyaml`, `python-dotenv`, `typer` (CLI), `httpx`, `pandas`/`pyarrow` (parquet), `langdetect`, plus `pytest` and `ruff` for development.
- [x] Add `.gitignore` entries for `data/`, `runs/`, `.env`, and `__pycache__/`.
- [x] Add `.env.example` listing `ANTHROPIC_API_KEY`, `PULSE_RECIPIENT`, and `PULSE_DOCS_FOLDER_ID` with no values.
- [x] Create `config/pulse.yaml` with the structure in §8, plus a loader that validates it with pydantic.
- [x] Write `models.py` with the data model from §5 (`Review`, `CleanReview`, `Theme`, `Quote`, `ActionIdea`, `Pulse`).
- [x] Add a `pulse` CLI entry point with stub commands: `run`, `ingest`, `analyze`, `publish`, `doctor`.
- [ ] **Settle the open decisions** (§13):
  - [ ] **#1 MCP servers:** choose the Docs and Gmail MCP servers or connectors. Install them, finish their OAuth setup once, and list their tool names.
  - [x] **#2 Play Store data path:** confirm how to get a public review export. Write down the expected file format and columns. *(Revised 2026-11-21: public listing via `google-play-scraper`, approved; export files are the fallback. See §13 #2.)*
  - [x] **#3 iOS App Store ID:** look up Groww's iOS app ID and check that the public RSS feed responds for `country=in`.
  - [x] **#5 Language:** confirm Python.
- [ ] Set up CI (GitHub Actions or similar) that runs `ruff` and `pytest` on every push.

### Deliverables
- A repo that installs; `pulse --help` works.
- Typed config and data models.
- A short decision record added to `architecture.md` §13 (status column updated).

### Exit criteria
- `pip install -e .` and `pulse --help` succeed.
- CI passes on an empty test suite.
- Decisions #1–#3 are settled, or a fallback is recorded for each.

---

## Phase 1: Review Ingestion and Normalization

**Goal:** a deduplicated set of Groww reviews from the last 8–12 weeks, from both stores, in one schema. *(§4.1, §4.2)*

### Tasks
- [x] `ingest/base.py`: the `ReviewSource` protocol and the `RawReview` type.
- [x] `ingest/play_file.py`: `PlayStoreFileSource`.
  - Reads CSV/JSON files from `data/imports/play/`.
  - Column mapping comes from config, to cope with different export formats.
  - **Drops identity fields** (name, avatar, device) at read time.
- [x] `ingest/app_store_rss.py`: `AppStoreRssSource`.
  - Pages 1–10 of the public JSON feed, with a delay between requests and a generic User-Agent.
  - Stops early once reviews are older than the window.
- [x] `ingest/play_public.py`: `PlayStorePublicSource`, built after decision #2 was revised. Public listing only, rate-limited, `hl=en` only, enabled in config (`ingest.play_public.enabled`).
- [x] Incremental ingest: weekly runs fetch only since the last complete fetch (`data/ingest_state.json`); `pulse ingest --full` re-fetches the window.
- [x] Save raw results to `data/raw/{source}/{run_id}.jsonl`.
- [x] `ingest/normalize.py`:
  - [x] Map to `Review`; compute `key = sha256(source + source_review_id)`.
  - [x] Window filter (`weeks_back`, default 10, valid range 8–12, timezone IST).
  - [x] Dedupe by `key` (newest version wins); a hash of `(rating, date, normalized_text)` only for rows without a source ID (edge case NRM-05).
  - [x] Cleaning rules, in order: strip emojis; drop reviews with no text left; drop reviews under `ingest.min_words` words (default 9, counted after emoji removal); drop non-English reviews (Hinglish is labelled `hi-Latn` and dropped).
  - [x] Language detection (word lists for English and Hinglish, then seeded `langdetect`).
  - [x] Report how many reviews each rule dropped, per source, in the `pulse ingest` output and in `runs/{week}/manifest.json`.
- [x] Persist the normalized store to `data/reviews.parquet` (upsert by `key`).
- [x] Wire up `pulse ingest --source play_store|app_store|all`.
- [x] Tests: synthetic fixtures for both sources covering window edges, duplicates, empty text, and non-English text; plus the cleaning rules: emoji-only review, exactly 8 words (dropped), exactly 9 words (kept), Hindi, Hinglish, English with emojis.

### Deliverables
- `pulse ingest` fills `data/reviews.parquet`.
- Unit tests for normalization.

### Exit criteria
- On real data, the count of reviews in the window is reported per source.
- No reviewer identity fields exist anywhere in `reviews.parquet`; a test asserts the schema.
- Re-running ingest on the same inputs adds 0 new rows.

### Risks
| Risk | Mitigation |
|---|---|
| The App Store feed returns few reviews (it only covers about 500 recent ones) | Acceptable. Record per-source counts in the pulse source line. |
| The Play Store listing blocks requests or changes its response format | The adapter fails loudly (no silent empty result), App Store-only runs still work, and export files are the fallback (`ingest.play_public.enabled: false`). The scraper version is pinned. |

---

## Phase 2: PII Scrubber

**Goal:** no identifying data reaches the LLM, the logs, or any artifact. *(§4.3, §11)*

### Tasks
- [x] `privacy/detectors.py`: regex detectors that return spans and labels for:
  - [x] Email addresses → `[email]`
  - [x] Phone numbers (Indian `+91` / 10-digit, and international) → `[phone]`
  - [x] PAN, Aadhaar-like 12-digit groups, UPI IDs → `[id]`
  - [x] Long digit runs (account, order, ticket numbers; length set in config) → `[number]`. *Threshold 8 digits (real Groww ticket numbers are 8), plus 5+ digit numbers right after "ticket", "UTR", "order id", …; amounts and share counts ("order of 50000 shares", "₹5000", "75000/-") are kept.*
  - [x] URLs and `@handles` → `[link]` / `[handle]`
- [x] `privacy/scrub.py`: `scrub(Review) -> CleanReview`. This produces `canonical_text` and assigns run-local IDs (`r0001`…).
- [x] `privacy/scan.py` (or a helper in `detectors.py`): `find_pii(text) -> list[Hit]`, reused by the final output gate in Phase 4.
- [x] Write `runs/{week}/clean_reviews.jsonl` (`pulse analyze` runs this stage; redaction counts go to `manifest.json`).
- [x] Tests:
  - [x] Positive cases for each detector, including Hinglish and mixed-format text.
  - [x] Negative cases that must **not** be redacted: ratings, prices like "₹500", years, "5 star", and short numbers.
  - [x] A property test: `find_pii(scrub(x).canonical_text) == []`.

### Deliverables
- Scrubber and scanner modules with a dedicated test file.

### Exit criteria
- All detector tests pass.
- A manual check of 100 real scrubbed reviews finds no PII left and no obviously wrong redactions. *(Sample prepared at `runs/2026-W46/pii_review_sample.md`: all 11 redacted reviews plus 89 random ones. Human check pending: findings not yet recorded.)*
- Personal names are a recorded known gap (PII-11), handled in Phase 4 by the quote-pick prompt.

---

## Phase 3: Theme Clustering and Ranking

**Goal:** at most 5 themes that fit the data, with every review assigned and the top 3 ranked in code. *(§4.4)*

### Data profile (2026-W46, `runs/2026-W46/clean_reviews.jsonl`)

What the real input looks like, because it drives the choices below:

| Property | Value | Consequence for clustering |
|---|---|---|
| Volume | 1,348 reviews (Play Store 1,202 · App Store 146), 7 Sep – 15 Nov | Small. The whole set fits in one request |
| Length | median 22 words, p90 68, max 239; about 41k words in total (≈80k input tokens with IDs, ratings, dates) | Pass A sees **every** review; no sampling needed |
| Ratings | 1★ 43% · 2★ 6% · 3★ 7% · 4★ 9% · 5★ 35%; 49% are 1–2★ | Strongly bimodal. Negative share separates themes well |
| Recency | 245 reviews in the last 14 days | Enough for a recency signal on the large themes, but noisy for small ones |
| Topic mix (keyword probe, overlapping, rough) | trading/F&O/charts ~22%, support ~17%, bugs/lag ~15%, charges ~12%, losses/fraud/trust ~10%, MF/SIP ~7%; KYC, payments, withdrawals, tax/statements ~3–4% each | 8 seed themes for 5 slots: the small seeds have to merge or fall to `other` |
| Generic praise | ~19% are 4–5★ with no specific topic ("very good app, easy to use") | Needs its own bucket (see S3), or it either takes a theme slot or inflates `other` |
| Multi-topic reviews | common: "app hung → order delayed → loss → support didn't reply" | Needs a primary-label rule (see S4) |
| Rating vs. text | some 5★ reviews are complaints or feature requests ("pls change the UPI method…") | Rating alone is not sentiment; the model reads the text |

### Clustering strategy

**S1. LLM clustering in two passes, not embeddings.** Embeddings plus k-means/HDBSCAN was considered and rejected: it needs another model provider and dependencies, its clusters have no labels or inclusion criteria (an LLM would still have to name them), and it splits short reviews by wording rather than by the issue. At about 1,350 short reviews the model can read the whole set in one request. Revisit only if the cleaned volume goes above about 20k reviews per window.

**S2. Pass A (discovery) reads the full set.**
- Input: one line per review, `r0001 | 1★ | 2026-09-07 | <canonical_text>`. Shuffle with a seed derived from the ISO week, so the date order does not bias the model and the run can be reproduced. Above `analysis.max_discovery_reviews` (3,000), take a stratified sample by rating band (1–2★ / 3★ / 4–5★) × week.
- Context in the prompt: what Groww is (stocks, F&O, mutual funds, IPOs in India) and the 8 seed themes from config. Themes are discovered again each week, so IDs can change between weeks. Keeping them stable is deferred to Phase 8 (week-over-week trend).
- Granularity rules in the prompt:
  - A theme is a **product area or problem type that one team could own**: not a sentiment ("bad experience") and not a catch-all ("app issues").
  - Merge small related seeds instead of dropping them. Likely merge on this data: payments, withdrawals and redemption delays into one money-movement theme. The model decides.
  - A theme should hold at least ~3% of reviews. Smaller topics go to `other`.
- Output (structured, `messages.parse()`): **at most 5** themes, each `{id (snake_case), label (≤ 4 words, shown in the pulse), description, include, exclude, example_ids (3 review IDs)}`, plus `taxonomy_changes` listing what was merged, renamed or dropped compared with the seeds. Code checks that `example_ids` exist. Effort: `high`.

**S3. Two reserved buckets that are not themes.** `general_praise` (positive, nothing specific to act on) and `other` (specific, but outside the taxonomy). Neither counts toward the 5-theme limit, neither is ranked, and only `other` counts toward the 25% re-discovery rule. Without `general_praise`, about a fifth of the data would either become a top-3 theme nobody can act on or push `other` past 25% every week. The `general_praise` share is shown in the pulse source line (e.g. "19% general praise", Phase 4), so the pulse isn't read as all negative.

**S4. Pass B (assignment): one primary theme per review.**
- Batches of at most `analysis.assign_batch_size` (500), balanced in size (W46: 3 batches of about 450), run concurrently (at most 3 at once). The system prompt and theme definitions form a fixed prefix with `cache_control`; only the review batch changes. Effort: `medium`.
- Output per review: `{review_id, theme_id | general_praise | other, secondary_theme_id?, sentiment: positive|neutral|negative|mixed}`.
- **Primary-label rule:** the primary theme is the main thing the reviewer wants fixed; when that is unclear, choose the root cause over its consequence. "The app hung and I lost money" goes to performance, not losses. "Support never replied" goes to support only when support is the main complaint; otherwise it goes in `secondary_theme_id`. Ranking uses the primary theme only, so shares add up to 100%. Secondary counts are reported in `themes.json`.
- Code checks: every input ID appears exactly once; unknown theme IDs and IDs outside the batch are rejected; missing IDs are re-requested in a small follow-up batch (at most 2 retries), after which the stage fails with `failure.json`.

**S5. Quality checks after assignment (in code, recorded in `themes.json` and the manifest).**
- `other` above 25%: re-run Pass A once, with up to 50 `other` reviews as examples, then run Pass B again.
- A theme under 3%: flag it (`small_theme` warning). It can still be ranked.
- **Consistency probe:** assign a random 100 reviews a second time, in a separate call with a different order, and record the agreement rate. Below 85% is a warning: it usually means two themes overlap and their `include`/`exclude` rules need tightening. This is a cheap automated check alongside the human spot check.
- Rating vs. sentiment disagreements (for example, 4–5★ marked negative) are counted, for diagnosis only.

**S6. Ranking (`rank.py`, pure and deterministic).** Only the clustered themes are ranked (not `general_praise` or `other`).
```
volume_share   = theme reviews / all reviews in the window
negative_share = theme reviews rated 1–2★ / theme reviews          # rating, not LLM sentiment: deterministic
recency_weight = clamp(share_last_14d / volume_share, 0.5, 2.0)    # 1.0 if the theme has < 5 reviews in the last 14 days
score          = volume_share × (1 + negative_share) × recency_weight
```
Ties are broken by the count of negative reviews, then by `id`. Themes with no reviews score 0 and are never selected. Fewer than 3 rankable themes with reviews fails the stage, because the pulse needs exactly 3.

### Tasks
- [x] `analysis/llm.py`: a wrapper around the Anthropic SDK.
  - [x] Model `claude-opus-5-5`; adaptive thinking; `output_config.effort` set per call.
  - [x] Structured outputs from pydantic schemas: the schema goes in `output_config.format` (`anthropic.transform_schema`) and the text is validated in code *after* the `stop_reason` check, so a refused or truncated response is reported as such rather than as a parse error. (Used instead of `messages.parse()`, which validates during streaming.)
  - [x] Streaming plus `.get_final_message()` for large requests.
  - [x] Server-side `fallbacks: "default"` (beta `server-side-fallback-2026-07-01`), and a `stop_reason` check before reading any content.
  - [x] Error handling caught most-specific first: `RateLimitError` → `APIStatusError` → `APIConnectionError`.
  - [x] `cache_control` on the stable system prompt and the theme-definition prefix.
  - [x] Log scrubbed requests and responses to `runs/{week}/llm/`.
- [x] `analysis/prompts/`: versioned templates (`cluster_v1.md`, `assign_v1.md`) with the product context, the seed themes from config, the granularity rules (S2), the reserved buckets (S3), and the primary-label rule (S4).
- [x] Config: add `analysis.max_discovery_reviews` (3000), `analysis.assign_batch_size` (500), `analysis.min_theme_share` (0.03), `analysis.max_other_share` (0.25), `analysis.recency_days` (14), `analysis.consistency_sample` (100), plus `analysis.assign_concurrency` (3) and `analysis.min_agreement` (0.85).
- [x] `analysis/cluster.py`:
  - [x] Input formatting and seeded shuffle; stratified sample above `max_discovery_reviews` (S2).
  - [x] **Pass A (discovery):** 5 or fewer `{id, label, description, include, exclude, example_ids}` plus `taxonomy_changes`.
  - [x] **Pass B (assignment):** balanced, concurrent batches against the fixed definitions; output `{review_id, theme_id | general_praise | other, secondary_theme_id?, sentiment}`.
  - [x] Check that every review ID is assigned exactly once. Re-request any that are missing (at most 2 retries).
  - [x] Quality checks from S5: `other` re-discovery, small-theme flag, consistency probe, rating/sentiment disagreement count.
- [x] `analysis/rank.py`: the formula and tie-breaks in S6; select the top 3. Pure and deterministic.
- [x] Write `runs/{week}/themes.json`: taxonomy, `taxonomy_changes`, prompt versions, every assignment, per-theme stats (count, `volume_share`, `negative_share`, last-14-day share, `recency_weight`, score, rank, secondary count), `general_praise` and `other` shares, consistency agreement, and warnings.
- [x] Wire `pulse analyze` to run up to this stage. Exit codes: 8 (`ANALYSIS_FAILED`, answers still invalid after retries, or fewer than 3 rankable themes), 9 (`LLM_FAILED`: API error, refusal, truncation). Failures are recorded in the manifest's `cluster` section.
- [x] Tests:
  - [x] Unit tests for `rank.py`: ties, a theme with no reviews, the recency clamp and the under-5-reviews rule, `general_praise`/`other` excluded, fewer than 3 rankable themes fails.
  - [x] Schema tests with mocked LLM responses: more than 5 themes is rejected; an unknown theme ID is rejected; a duplicate or missing assignment triggers a re-request; `example_ids` that don't exist are rejected.
  - [x] Mocked test: an `other` share above 25% triggers exactly one re-discovery.
  - [x] Shuffle test: the same week gives the same order; a different week gives a different one.
  - [x] An `@slow` integration test on about 200 synthetic reviews against the real API (run on demand), including multi-topic reviews with a known primary label. *(`tests/test_cluster_live.py`; written, not yet run: needs API credentials.)*

### Deliverables
- `themes.json` for 2026-W46, with theme labels, shares, scores, and assignments.

*Status (2026-11-22): code and offline tests done (`pytest`: 331 passed, LLM mocked). The live W46 run, and with it every exit criterion below, is waiting for API credentials (`ANTHROPIC_API_KEY` in `.env` or an `ant auth login` profile).*

### Exit criteria
- 5 or fewer themes on real data; 100% of reviews assigned; `other` at 25% or less.
- A human spot check: 20 random assignments, at least 85% judged correct. Include at least 5 multi-topic reviews, to check the primary-label rule.
- Consistency probe agreement of at least 85%.
- The cost (tokens) and latency of one full run are recorded in `manifest.json`.

### Risks
| Risk | Mitigation |
|---|---|
| Themes are too broad, such as "app issues" | Granularity rules plus `include`/`exclude` criteria in the prompt (S2); review after the first live run |
| Large `other` bucket | `general_praise` is kept separate (S3); if `other` is still above 25%, re-run Pass A with examples from `other` (S5) |
| Theme IDs and labels change from week to week | Accepted in Phase 3: each week's pulse stands alone. Stable IDs are added with the week-over-week trend (Phase 8) |
| Support or losses are under-counted because of the root-cause rule | Secondary counts are reported in `themes.json`; revisit the rule after the first live runs (Phase 7) |
| Small, important topics (KYC, tax) disappear into `other` | Accepted for the weekly top 3. They stay visible in `themes.json`, and a theme from them appears automatically once its volume grows |

---

## Phase 4: Composer, Validator, Renderer ★ M1

**Goal:** a complete pulse that passes validation, produced fully offline. *(§4.5, §4.6, §7)*

### Tasks
- [x] `analysis/compose.py`:
  - [x] **Candidate selection (code):** up to 8 reviews per top theme; 8–60 words, no profanity or redaction tokens, mixed ratings, recent first; limits relax in steps when a theme has nothing eligible (CMP-01/02/04, PII-22).
  - [x] **Quote pick (LLM):** candidates are split into numbered sentences; the model returns `{theme_id, review_id, first_sentence, last_sentence}` per theme and **code slices** `canonical_text` from the first sentence's start to the last one's end, so the model never writes quote text. *(Changed from `{review_id, start, end}` character offsets, which LLMs count unreliably: edge cases CMP-10, CMP-12.)*
  - [x] The quote-pick prompt tells the model to **skip any candidate review that names a person** (a reviewer, a support agent or other staff, anyone else). The scrubber cannot detect names (known gap PII-11). Add a prompt test with a candidate that names a support agent.
  - [x] **Pulse writing (LLM):** one-line theme summaries plus 3 `ActionIdea`s (`theme_id`, `action`, `signal`), one per theme, as structured output. Words only: the renderer inserts every number (CMP-22).
- [x] `analysis/validate.py`: every check in §4.6.
  - [x] Theme count (5 or fewer clustered, exactly 3 in the pulse); quote count 3; action count 3.
  - [x] **Quote fidelity:** each quote is an exact substring of its review's `canonical_text`.
  - [x] Each quote's review belongs to the theme it is shown under; each action references a top-3 theme.
  - [x] **Word count of 250 or fewer** on the rendered body.
  - [x] **PII scan** on the rendered Doc body and email body.
  - [x] No leftover `<…>` placeholders or empty sections.
- [x] Retry loop: on failure, re-prompt only the failing part (quotes, writing, or both) with the error list and the previous answer (at most 2 retries, each validated in full); otherwise write `failure.json` with every attempt and stop with exit code 10 (`VALIDATION_FAILED`).
- [x] `render/markdown.py`: `Pulse` → Doc text (layout in §7.1) and email text (§7.2). Write `pulse.json`, `pulse.md`, and `validation.json`.
  - [x] Source line with the filter summary, e.g. `Source: 1,348 reviews (Play Store 1,202 · App Store 146), 9+ words, English only, 7 Sep – 15 Nov 2026` (`render/markdown.source_line`, built early; Phase 4 calls it).
  - [x] Extend the source line with the `general_praise` share from `themes.json`, e.g. `… 7 Sep – 15 Nov 2026 · 19% general praise` (§7.1), so the pulse isn't read as all negative. Rounded to a whole percent; omitted if the share is 0. Like the rest of the source line, it doesn't count toward the 250-word limit. Covered by the golden-file test.
- [x] Wire `pulse run --dry-run` through stages 1–6 (`--skip-ingest` uses the stored reviews). `pulse analyze` runs stages 3–6. Without `--dry-run`, `pulse run` stops before publishing until Phase 5.
- [x] Tests:
  - [x] Contract tests using hand-built **bad** pulses: an invented quote, 251 words, 4 themes, an email address in the body, a missing action. Each one must be rejected.
  - [x] Golden-file test: a fixture `Pulse` renders to the expected `pulse.md`.
  - [x] An end-to-end dry run on fixtures in CI, with the LLM mocked (`tests/test_cli.py`). An autouse fixture blocks the real API in every non-`slow` test.
  - [x] `@slow` live test: the quote pick skips a candidate that names a support agent (`tests/test_compose_live.py`; written, not yet run).

### Deliverables
- **M1:** `pulse run --dry-run` produces `runs/{week}/pulse.md` from real reviews.

*Status (2026-11-22): code and offline tests done (`pytest`: 377 passed, LLM mocked). M1 on real data and the exit criteria below are waiting for API credentials, as for Phase 3. VAL-06 (Markdown escaping) is left for Phase 5, once the Docs MCP server's input format is known.*

### Exit criteria
- The dry run on real data passes every validator check.
- A human read: the quotes are relevant to their themes, and the actions are concrete and grounded in the themes.
- Checklist items from `problemStatement.md` §4 met offline: themes, quotes, actions, 250 words or fewer, no PII.

---

## Phase 5: MCP Publishing (Google Docs + Gmail) ★ M2

**Goal:** publish the pulse to Google Docs and create a Gmail draft **only through MCP**. *(§4.7)*

### Tasks
- [ ] `publish/mcp_client.py`: a session manager built on the official `mcp` SDK.
  - [ ] Connect over stdio or HTTP using the `mcp.docs` / `mcp.gmail` config; `list_tools()`; `call_tool()`; clean shutdown.
  - [ ] Timeouts, and clear errors when a server won't start or a tool fails.
- [ ] `publish/tool_map.yaml`: map the logical actions `find_doc`, `create_doc`, `replace_doc_body`, `move_to_folder`, `create_draft`, and optionally `update_draft` to the tools of the servers chosen in Phase 0.
- [ ] Startup check: every mapped tool exists in `list_tools()`; **refuse any mapped tool whose name contains `send`**.
- [ ] `publish/docs_publisher.py`: find-or-create by the title `Groww Weekly Review Pulse — {ISO week}` in the configured folder, then replace the body, then record `doc_id` and `doc_url`.
- [ ] `publish/gmail_drafter.py`: create a draft to `PULSE_RECIPIENT` with the subject from §7.2 and a body of the pulse text plus `Full pulse: {doc_url}`. Reuse or update the existing draft ID from `publish.json` when one exists.
- [ ] Write `runs/{week}/publish.json`, recording success or failure for each step.
- [ ] `pulse publish --week …` publishes from saved artifacts.
- [ ] `pulse doctor`: checks config, Anthropic credentials, that both MCP servers can be reached, and the tool-map coverage.
- [ ] Tests:
  - [ ] A **fake in-process MCP server** exposing the mapped tool names. Assert the call order, the arguments, and find-or-update behavior.
  - [ ] Assert that a mapped `send` tool makes startup fail.
  - [ ] Assert that resuming after a partial failure (Docs succeeded, Gmail failed) skips the Docs step.

### Deliverables
- **M2:** one command that turns real reviews into a Google Doc and a Gmail draft.

### Exit criteria
- A live run creates the Doc in the right folder and a draft addressed to the configured recipient that contains the note and the Doc link.
- Re-running the same week updates the same Doc and does not create a second draft.
- `grep` shows no Google REST endpoints and no OAuth client code anywhere in `src/`.
- No email is sent at any point.

### Risks
| Risk | Mitigation |
|---|---|
| The chosen MCP server has no "find by title" or "replace body" tool | Fall back to storing `doc_id` in `publish.json` (and listing the folder if possible), or to append-after-clear; record the choice in `tool_map.yaml` |
| The Gmail MCP server has no draft-update tool | Delete and recreate the draft if supported; otherwise skip and log |
| MCP OAuth tokens expire during unattended runs | `pulse doctor` runs before each scheduled run; document how to re-authenticate |

---

## Phase 6: Orchestration, Reliability, CLI Hardening

**Goal:** runs that are safe to repeat, can resume, and are observable. *(§4.8, §6, §10)*

### Tasks
- [ ] `orchestrator.py`: each stage reads the previous stage's saved artifacts, so `--skip-ingest` and stage-level re-runs work.
- [ ] `manifest.json`: config snapshot, git SHA, model IDs, prompt versions, per-source counts, token usage, timings, and status for each stage.
- [ ] Implement every failure behavior in §10:
  - [ ] A source fails → continue and record it; stop if fewer than `min_reviews` (default 50).
  - [ ] Backoff at the stage level on top of the SDK's own retries.
  - [ ] Log the refusal category and stop the stage cleanly.
  - [ ] Partial publish → resume.
- [ ] Clear CLI exit codes (0 for success, a non-zero code per failure class) and readable summaries.
- [ ] `pulse prune`: remove raw files older than 16 weeks.
- [ ] A README with setup, MCP server installation and authentication, configuration, commands, and troubleshooting.

### Deliverables
- A hardened CLI, the README, and complete run manifests.

### Exit criteria
- A simulated failure test for each row of §10 behaves as specified.
- Two identical runs in a row produce one Doc, one draft, and stable artifacts.

---

## Phase 7: Scheduling, First Live Runs, Quality Review ★ M3

**Goal:** a weekly pulse that runs unattended and that stakeholders find useful. *(§4.9)*

### Tasks
- [ ] Set up the scheduler: Windows Task Scheduler (Mondays 09:00 IST, running `pulse doctor && pulse run`), or a scheduled GitHub Actions workflow.
- [ ] Compute the run week as the ISO week that just ended (Monday–Sunday, IST).
- [ ] Do **3 consecutive live weekly runs**. For each one:
  - [ ] A human check against the `problemStatement.md` §4 checklist.
  - [ ] Feedback from Product, Support, and Leadership readers: is it scannable, are the themes useful, can the actions be acted on?
- [ ] Tune the prompts, seed themes, and ranking weights based on that feedback. Bump the prompt versions.

### Deliverables
- **M3:** a weekly Doc and Gmail draft that appear without anyone running anything.
- A short notes file of what was tuned after the first runs.

### Exit criteria
- 3 scheduled runs in a row succeed with no manual steps.
- All checklist items in `problemStatement.md` §4 are ticked for each run.

---

## Phase 8 (Optional): Enhancements

Pick from these once M3 is stable:

- [ ] **Stable theme IDs across weeks** (needed for the trend): pass last week's taxonomy from `runs/{prev_week}/themes.json` to Pass A as the main seed, reuse a theme's `id` while it still fits, and list changes against last week in `taxonomy_changes` (§4.4).
- [ ] **Week-over-week trend:** "↑/↓ vs last week" next to each theme, using `runs/` history (§13 #6).
- [ ] **Quality eval set:** a small labeled set for theme coherence and usefulness of actions; run it in CI when prompts change.
- [ ] **Rating trend line:** average rating per week across the window, shown in the source line.
- [ ] **Cost tuning:** measure whether a lower effort level on Pass B gives the same assignment accuracy.

---

## Requirement → Phase Map

| Requirement (`problemStatement.md`) | Phase |
|---|---|
| Import 8–12 weeks of reviews (rating, title, text, date) | 1 |
| Public reviews only, nothing behind a login (amended, §13 #2) | 0 (decision), 1 |
| No PII in any artifact | 2, 4 (output gate) |
| At most 5 themes | 3, 4 |
| Top 3 themes, 3 verbatim quotes, 3 action ideas | 3, 4 |
| 250 words or fewer, easy to scan | 4 |
| Pulse in Google Docs via MCP | 5 |
| Gmail draft to self or alias via MCP | 5 |
| MCP-first, no hand-written Google OAuth or REST | 5 (enforced by exit criteria) |
| Weekly cadence | 7 |

---

## Definition of Done (Project)

- [ ] `pulse run` turns live public reviews into a Google Doc and a Gmail draft, through MCP only.
- [ ] Every validator check in §4.6 passes on each published run.
- [ ] The test suite (unit, contract, fake-MCP, dry-run end-to-end) passes in CI.
- [ ] The README lets a new developer set up and run the project from scratch.
- [ ] 3 consecutive scheduled weekly runs succeed with no manual steps.
