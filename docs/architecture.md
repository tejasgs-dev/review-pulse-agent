# Architecture: Groww Weekly Review Pulse

> Companion to [problemStatement.md](problemStatement.md). This document describes **how** the system is built. The problem statement describes **what** it must do.

---

## 1. Goals and Non-Goals

### Goals
- Turn 8–12 weeks of public Groww reviews (Play Store and App Store) into a one-page weekly pulse of 250 words or fewer.
- Make sure every quote in the pulse is **verbatim** and every artifact is **free of PII**. Code enforces both rules; neither depends on the prompt alone.
- Publish the pulse to **Google Docs** and create a **Gmail draft** through **MCP servers only**.
- Re-running the same week updates that week's pulse and does not create duplicates.
- Run unattended once a week, and also on demand from the command line.

### Non-Goals
- Real-time monitoring or alerting.
- Scraping behind logins, or any access that needs a store account. The one automated reader of a public store page (the Play Store listing) is an approved exception; see §13 #2.
- Direct Google REST/OAuth code. MCP servers own auth and HTTP.
- A web UI. The surfaces are Google Docs and Gmail.

---

## 2. System Context

```
                         ┌───────────────────────────────────────────┐
                         │          Review Pulse Agent (Python)      │
  Public review sources  │                                           │   MCP (stdio / HTTP)
 ┌───────────────────┐   │  ┌─────────┐  ┌─────────┐  ┌───────────┐  │  ┌──────────────────┐
 │ Play Store public │──▶│  │ Ingest  │─▶│ Analyze │─▶│ Publish   │──┼─▶│ Google Docs MCP  │──▶ Google Docs
 │ listing (scraper) │   │  │ + PII   │  │ (Claude)│  │ (MCP      │  │  └──────────────────┘
 ├───────────────────┤   │  │ scrub   │  │         │  │  client)  │  │  ┌──────────────────┐
 │ App Store public  │──▶│  └─────────┘  └─────────┘  └───────────┘──┼─▶│ Gmail MCP        │──▶ Gmail draft
 │ RSS feed (JSON)   │   │        │            │             │       │  └──────────────────┘
 └───────────────────┘   │        ▼            ▼             ▼       │
                         │   ┌──────────────────────────────────┐    │
                         │   │ Local run store (data/, runs/)   │    │
                         │   └──────────────────────────────────┘    │
                         └───────────────────────────────────────────┘
                                         │
                                         ▼
                                Claude API (Opus 5.5)
```

| External dependency | Role | Interface |
|---|---|---|
| Play Store public listing | Source of Android reviews | `google-play-scraper` request format, sent with httpx (§4.1); export-file import as a fallback |
| App Store customer-reviews RSS | Source of iOS reviews | Public JSON feed, no login |
| Claude API | Theme clustering, quote selection, pulse writing | Anthropic Python SDK |
| Google Docs MCP server | Create or update the pulse document | MCP tools |
| Gmail MCP server | Create the draft email | MCP tools |

---

## 3. High-Level Pipeline

The system is a **code-orchestrated workflow**, not an open-ended agent. The steps are fixed and known in advance. Python controls their order, and Claude is called at the steps that need judgment. This keeps runs predictable, cheap to retry, and easy to test.

```
┌────────┐   ┌──────────┐   ┌───────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐
│1 Ingest│──▶│2 Normalize│─▶│3 PII Scrub│──▶│4 Cluster │──▶│5 Compose │──▶│6 Validate│──▶│7 Publish │
│        │   │ + Dedupe │   │           │   │ (≤5)     │   │ Pulse    │   │ (guards) │   │ Docs +   │
│        │   │ + Window │   │           │   │ Claude   │   │ Claude   │   │          │   │ Gmail    │
└────────┘   └──────────┘   └───────────┘   └──────────┘   └──────────┘   └────┬─────┘   └──────────┘
                                                                ▲             │ fail
                                                                └─────────────┘ (retry ≤2 with errors)
```

| # | Stage | Input | Output | LLM? |
|---|---|---|---|---|
| 1 | **Ingest** | Play Store listing (or export files), App Store feed | `RawReview[]` | No |
| 2 | **Normalize / Dedupe / Window** | `RawReview[]` | `Review[]` inside the 8–12 week window | No |
| 3 | **PII Scrub** | `Review[]` | `CleanReview[]` with identifiers removed | No (rules-based) |
| 4 | **Cluster** | `CleanReview[]` | Up to 5 `Theme`s plus review→theme assignments | Yes |
| 5 | **Compose** | Top 3 themes, candidate quotes | `Pulse` draft (structured) | Yes |
| 6 | **Validate** | `Pulse` | Pass, or a list of errors to fix | No |
| 7 | **Publish** | `Pulse` | Google Doc URL and Gmail draft ID | No (MCP tool calls) |

---

## 4. Component Design

### 4.1 Ingestion (`ingest/`)

Each store has a pluggable **adapter** that implements one interface:

```python
class ReviewSource(Protocol):
    name: str                                    # "play_store" | "app_store"
    def fetch(self, since: date, until: date) -> Iterable[RawReview]: ...
```

| Adapter | Source | Notes |
|---|---|---|
| `PlayStorePublicSource` | Public listing reviews for `com.nextbillion.groww`, newest first, `country=in`, one pass per language in `ingest.play_public.langs` (default `en` only, since non-English reviews are dropped; was `en`, `hi`; `hl=` filters reviews by language) | **Primary path** (§13 #2). No login, no cookies. Requests are spaced by `request_delay_seconds`, and paging stops once a page reaches back past the window start, or at `max_reviews`. It uses `google-play-scraper`'s request format and parser, but sends requests itself with httpx, because the library's `reviews()` helper swallows errors (a block looks like "no more reviews") and sets no timeout or User-Agent. Importing the library disables TLS certificate checks process-wide, so the adapter restores the default right after import. The library's version is pinned, since its internals are used. The library returns review times as naive *local* datetimes; they are converted with `astimezone()`. Only `reviewId`, `score`, `content`, `at` and `appVersion` are extracted, so reviewer names, avatars and reply text are never read. A live backfill fetched 10,400 reviews (about 1,000 a week) in about 100 seconds. |
| `PlayStoreFileSource` | CSV/JSON export dropped in `data/imports/play/` | **Fallback,** used when `ingest.play_public.enabled` is false. Column mapping is set in config. |
| `AppStoreRssSource` | `https://itunes.apple.com/{country}/rss/customerreviews/page={n}/id={app_id}/sortby=mostrecent/json`, plus equivalent orderings of the path segments | Public, no auth. `app_id` (`1404871703`) and `country=in` come from config. **Observed 2026-11-21:** any given URL form is sometimes served an empty feed (HTTP 200, no entries), and which form works changes from minute to minute. Occasionally every form of a page is empty while later pages still have data. The adapter therefore tries several URL forms per page, keeps paging past empty pages up to `app_store_max_pages`, re-tries interior gaps once, and reports the source as `partial` if a gap remains. A live run returned 400 reviews (4 Sep – 19 Nov), which covers a 10-week window. |

Rules every adapter follows:
- Polite throttling (configurable delay) and a descriptive, generic User-Agent.
- Fetch results are saved to `data/raw/{source}/{run_id}.jsonl` so a run can be replayed without fetching again. These are the mapped `RawReview` records, not untouched HTTP responses, so reviewer names, avatars and devices are never written to disk.
- Adapters do not decide anything about content. They only fetch and map fields.
- **Incremental ingest:** the first run for a source backfills the whole window. Later runs fetch only from the end of the last *complete* fetch, minus `ingest.overlap_days` (default 3) to pick up edited reviews. Coverage is stored in `data/ingest_state.json` and only advances after a complete fetch, so a partial run never hides a gap. A full re-fetch happens when there is no coverage, the store is missing, or the window reaches further back than the coverage; `pulse ingest --full` forces one.
- The Play Store column mapping (`ingest.play_columns`) accepts only a fixed set of fields (`review_id`, `rating`, `title`, `text`, `date`, `updated`, `app_version`), so an identity column cannot be mapped in by mistake.

### 4.2 Normalize, Dedupe, Window (`ingest/normalize.py`)

- Maps every source to one `Review` schema (see §5).
- **Window:** keeps reviews where `review_date ∈ [run_date − N weeks, run_date]`, with `N` configurable from 8 to 12 (default 10).
- **Dates:** every timestamp is converted to IST before taking the date. A review's date is the date of its latest version (its last edit, if the source reports one). Reviews dated after today are dropped.
- **Dedupe:** removes repeats by `key`, which comes from the store's own review ID, so a review that appears in more than one export or feed page is kept once, and the newest version of an edited review wins. Rows with no source ID fall back to a hash of `(rating, date, normalized_text)`. Two different users posting the same short text ("Good app") stay two reviews.
- **Cleaning rules** (tutor requirement, applied in this order to the newest version of each review): **(1)** strip emojis from the text (title too); **(2)** drop the review if no text is left (rating-only or emoji-only); **(3)** drop it if it has fewer than `ingest.min_words` words (default 9, i.e. "more than 8 words"), counted after emojis are removed; **(4)** drop it unless it is English. Every dropped review is counted per source and per rule, with non-English drops broken down by detected language, in the `pulse ingest` output and in `runs/{week}/manifest.json` (`ingest.sources.<source>.dropped`). If an edit makes a stored review fail a rule, the stored copy is removed. Rating-only reviews are no longer kept for rating statistics.
- **Language:** only English is kept. Short or mixed reviews defeat `langdetect` (it labels "Best app ever for trading" Norwegian, and ALL-CAPS English as German), so word lists check for romanized Hindi and English first, ALL-CAPS text is lowercased, and `langdetect` decides the rest only above a confidence threshold (else `und`). **Hinglish** (Hindi in English letters) is labelled `hi-Latn` when at least 20% of its words are common Hindi words (`hai`, `nahi`, `kya`, …) and is dropped like any non-English review. Mostly-English text with a few Hindi words stays `en`.

### 4.3 PII Scrubber (`privacy/scrub.py`)

This runs **before any text reaches the LLM or any artifact**. Defense in depth means each layer still protects the output if another one fails:

1. **Drop identifying fields at ingestion.** Reviewer name, avatar URL, review ID, device model, app version per user, and reply metadata are never copied into `Review`. The only thing kept is an opaque internal key (`sha256(source + source_review_id)`), used to trace a quote back to its review.
2. **Redact identifiers inside the text** with deterministic regexes:
   - Email addresses → `[email]`
   - Phone numbers (Indian and international formats) → `[phone]`
   - PAN (`[A-Z]{5}[0-9]{4}[A-Z]`), Aadhaar-like 12-digit groups, UPI IDs (`name@bank`) → `[id]`
   - Account, order, or ticket numbers → `[number]`: any run of `privacy.number_min_digits` (8) or more digits (Groww ticket numbers are 8 digits), card-style 4-4-4 groups, and shorter numbers (`reference_min_digits`, 5+) right after a word such as "ticket", "complaint", "UTR" or "account". "order" counts only with an ID marker (`order id`, `order no`, `order #`), because in a trading app it usually precedes a quantity. Amounts and quantities are never redacted: a number after `₹`, `Rs` or `INR`, or before `shares`, `qty`, `units`, `lots`, `rs`, `/-`, `lakh`, `crore` or `worth`.
   - URLs and `@handles` → `[link]` / `[handle]`
   Overlapping matches are resolved longest-first, so an email inside a URL becomes a single `[link]`. Redaction repeats until the scanner finds nothing, which guarantees `find_pii(scrub(x).canonical_text) == []`. All patterns run in linear time (tested on 50 KB hostile input). **Known gap: personal names are not detected**; regexes cannot do this reliably (edge case PII-11). The Phase 4 quote-pick prompt skips any review that names a person (§4.5).
3. **Final output scan.** The same detectors run again over the finished pulse, the Doc body, and the email body just before publishing. Any hit stops the publish (see §4.6).

The scrubbed text is the **canonical text**. Quotes are checked against this version, so a redacted quote still counts as verbatim.

### 4.4 Theme Clustering (`analysis/cluster.py`, `analysis/rank.py`)

**Approach: LLM-driven clustering with structured output, in two passes, plus deterministic ranking in code.** The implementation steps are in `implementation-plan.md` Phase 3 (S1–S6).

**Input profile (2026-W46).** After the cleaning rules (§4.2: 9+ words, English only), 1,348 reviews remain from about 10,000 fetched (Play Store 1,202 · App Store 146). They total about 41k words, roughly 80k input tokens with IDs, ratings and dates, so the whole set fits in one request. Ratings are bimodal (43% 1★, 35% 5★; 49% are 1–2★). About a fifth are 4–5★ with nothing specific to act on. Many reviews mix topics ("the app hung, I lost money, support never replied"), and some 5★ reviews are complaints or feature requests.

**Why not embeddings.** Embeddings plus k-means/HDBSCAN was considered and rejected. It needs another model provider and more dependencies, its clusters come without labels or inclusion criteria (an LLM would still have to name them), and it groups short reviews by wording rather than by issue. Reconsider it only if the cleaned volume goes above about 20k reviews per window.

**Pass A: Theme discovery**
- Input: every scrubbed review, one line each: `r0001 | 1★ | 2026-09-07 | <canonical_text>`. The order is shuffled with a seed derived from the ISO week, so date order does not bias the model and a run can be repeated exactly. Above `analysis.max_discovery_reviews` (3,000), a stratified sample by rating band (1–2★ / 3★ / 4–5★) × week is used.
- The prompt states the product context (a stock, F&O, mutual fund, and IPO investing app in India) and lists **seed themes** to consider: onboarding/KYC, payments and fund transfers, withdrawals, app performance and bugs, charges and brokerage, customer support, F&O and trading features, statements and tax reports. The model may merge, rename, or drop seeds based on the data.
- Granularity rules: a theme is a **product area or problem type that one team could own**, not a sentiment ("bad experience") or a catch-all ("app issues"). Small related seeds are merged rather than dropped (for example, payments, withdrawals and redemption delays may become one money-movement theme). A theme should hold at least `analysis.min_theme_share` (3%) of reviews; smaller topics go to `other`.
- Output (structured, schema-validated): **at most 5** themes, each with `id` (snake_case), `label` (4 words or fewer, shown in the pulse), `description`, `include`, `exclude`, and `example_ids` (3 review IDs, which code checks exist), plus `taxonomy_changes` listing what was merged, renamed or dropped compared with the seeds. Effort: `high`.
- Themes are discovered again each week, so theme IDs can change between weeks. Keeping IDs stable (by passing last week's taxonomy as the main seed) is part of the week-over-week trend in Phase 8 (§13 #6).

**Reserved buckets (not themes)**
- `general_praise`: positive, with nothing specific to act on ("very good app, easy to use").
- `other`: specific, but outside the taxonomy.
- Neither counts toward the 5-theme limit and neither is ranked. Only `other` counts toward the re-discovery rule below. The `general_praise` share is shown in the pulse source line (§7.1), so the pulse isn't read as all negative.

**Pass B: Assignment**
- Each review gets exactly one **primary** label: a theme ID, `general_praise`, or `other`. An optional `secondary_theme_id` and a `sentiment` (`positive|neutral|negative|mixed`) are recorded too.
- **Primary-label rule:** the main thing the reviewer wants fixed; when that is unclear, the root cause over its consequence. "The app hung and I lost money" goes to performance, not losses. Support is primary only when support is the main complaint; otherwise it is the secondary theme. Ranking uses the primary label only, so shares add up to 100%. Secondary counts are reported in `themes.json`.
- Batches of at most `analysis.assign_batch_size` (500), balanced in size (W46: 3 batches of about 450), run concurrently (at most 3 at once), with the theme definitions from Pass A held fixed. The system prompt and theme definitions are a cached prefix (`cache_control`); only the batch changes. Effort: `medium`.
- Code checks: every input ID appears exactly once; unknown theme IDs and IDs outside the batch are rejected; missing IDs are re-requested in a small follow-up batch (at most 2 retries), after which the stage fails with `failure.json`.

**Quality checks (code, recorded in `themes.json` and `manifest.json`)**
- `other` above `analysis.max_other_share` (25%): re-run Pass A once with up to 50 `other` reviews as examples, then Pass B again.
- A theme below 3%: `small_theme` warning (it can still be ranked).
- **Consistency probe:** `analysis.consistency_sample` (100) random reviews are assigned a second time, in a separate call with a different order. Agreement below 85% is a warning; it usually means two themes overlap.
- Rating vs. sentiment disagreements (for example, 4–5★ marked negative) are counted, for diagnosis only.

**Ranking (deterministic, in code):** only the clustered themes are ranked, not `general_praise` or `other`.
```
volume_share   = theme reviews / all reviews in the window
negative_share = theme reviews rated 1–2★ / theme reviews          # rating, not LLM sentiment
recency_weight = clamp(share_last_14d / volume_share, 0.5, 2.0)    # 1.0 if the theme has < 5 reviews in the last 14 days
score          = volume_share × (1 + negative_share) × recency_weight
```
- `negative_share` uses the star rating, not the model's sentiment, so ranking stays deterministic.
- The 14-day window (`analysis.recency_days`) gives more weight to recent reviews, since this is a *weekly* pulse. The under-5 rule keeps small themes from swinging on a handful of reviews (W46 has 245 reviews in its last 14 days).
- Ties are broken by the count of negative reviews, then by `id`. Themes with no reviews score 0 and are never selected. Fewer than 3 rankable themes fails the stage, because the pulse needs exactly 3.

The **top 3** by score go into the pulse. All themes (up to 5), the taxonomy, every assignment, per-theme stats (count, `volume_share`, `negative_share`, last-14-day share, `recency_weight`, score, rank, secondary count), the `general_praise` and `other` shares, the consistency agreement, and any warnings are kept in `runs/{week}/themes.json`.

### 4.5 Pulse Composer (`analysis/compose.py`)

**Quote candidate selection (code first, then the LLM):**
1. For each top theme, code picks up to 8 *candidate* reviews: assigned to that theme, 8–60 words, no profanity, no redaction tokens, most recent first, with ratings mixed (one per rating band in turn). If a theme has nothing eligible, the limits relax in steps (5–80 words, then any review whose text is at most 30% placeholders); if there is still nothing, the run stops with a clear failure (edge cases CMP-01/02/04/16, PII-22).
2. Each candidate is split into **numbered sentences**. Claude chooses **one quote per theme** (3 in total) and returns the `review_id` plus a first and last sentence number. The prompt tells it to skip any candidate that names a person (a reviewer, a support agent or other staff), because the scrubber cannot detect names (§4.3, PII-11). It never returns quote text and never counts characters (LLM character offsets are unreliable, CMP-10).
3. Code builds the quote by slicing `canonical_text` from the start of the first sentence to the end of the last. Because of this, the model **cannot invent wording**, and a quote always starts and ends on a sentence boundary (CMP-12).

**Writing the pulse:**
- Claude receives the top 3 themes with their definitions, how they compare (as context only), the 3 chosen quotes, a sample of recent reviews per theme, and the number of words left in the 250-word budget.
- It returns one summary per theme and **one action idea per theme** (`theme_id`, `action`, `signal`), words only. **Every number in the pulse is inserted by the renderer** from the computed stats (CMP-22); a summary that states a statistic fails validation.
- Action ideas must be concrete and doable. They name the change and the signal that would show it worked.
- If validation fails, only the failing part is asked for again (quotes, writing, or both), with the error list and the previous answer, at most 2 retries; each attempt is validated in full.

**Model configuration** (from the `claude-api` defaults):

| Setting | Value | Why |
|---|---|---|
| Model | `claude-opus-5-5` | Default current model |
| Thinking | Adaptive (always on for Opus 5.5) | Clustering and prioritizing need judgment |
| Effort | `high` for clustering and composing; `medium` for batch assignment | Opus 5.5 defaults to `medium`, so the level is set explicitly per call |
| Output format | Structured outputs (`output_config.format` / `client.messages.parse()`) | Guarantees schema-valid JSON for themes, assignments, and the pulse |
| Streaming | On for large clustering requests | Avoids HTTP timeouts on long inputs and outputs |
| Refusal handling | Check `stop_reason`; server-side `fallbacks: "default"` (beta `server-side-fallback-2026-07-01`) | Required handling for Opus 5.5 |
| Prompt caching | `cache_control` on the stable system prompt and theme definitions | Pass B batches share the same prefix |

### 4.6 Validator (`analysis/validate.py`)

These are hard checks run in code. If any fails, the composer is re-prompted with the list of errors (at most 2 retries). If it still fails, the run stops **before publishing** and writes `runs/{week}/failure.json`.

| Check | Rule |
|---|---|
| Theme count | Clustering returned 5 or fewer themes; the pulse has exactly 3 |
| Quote count | Exactly 3 quotes, one per top theme, from 3 different reviews |
| **Quote fidelity** | Each `quote.text` is an exact substring of the canonical text of `quote.review_id` |
| Quote–theme link | Each quote's review is assigned to the theme it is shown under |
| Action count | Exactly 3 actions, **one per top-3 theme**, each with an action and a signal |
| No statistics in text | Summaries contain no percentages or counts; the renderer adds them |
| Quote length | Each quote is 8–60 words (`quote_min_words`, `quote_max_words`) |
| **Word count** | Rendered pulse body has **250 words or fewer**: the three sections including their headings; the header and source line are excluded. Words are whitespace tokens with a letter or digit (`textutil.word_count`). |
| **PII scan** | No detector hits in the rendered Doc body or email body. Only the pulse's own Doc URL and the recipient address are allowed (VAL-04) |
| No placeholders | No leftover template tokens (a known list such as `<verbatim quote 1>` or `{doc_url}`, never any angle bracket: VAL-05) and no empty sections |

### 4.7 Publisher: MCP Client Layer (`publish/`)

The app is an **MCP client** that uses the official MCP Python SDK. It starts or connects to the configured Docs and Gmail MCP servers and calls their tools. **There is no Google REST code in this repo.**

```
publish/
├── mcp_client.py      # Session management: connect, list_tools, call_tool, close
├── docs_publisher.py  # Maps a Pulse to Docs MCP tool calls
├── gmail_drafter.py   # Maps a Pulse + Doc URL to a Gmail MCP draft call
└── tool_map.yaml      # Logical action → concrete tool name/args for the chosen server
```

**Why `tool_map.yaml`:** different Google Workspace MCP servers name their tools differently, for example `create_document` or `docs_create`. Publisher code calls *logical* actions (`find_doc`, `create_doc`, `replace_doc_body`, `create_draft`). A small mapping file resolves them to the tools of whichever server is configured. At startup, `list_tools()` checks that every mapped tool exists and stops early if one is missing.

**Docs flow (idempotent per week):**
1. `find_doc(title="Groww Weekly Review Pulse — {ISO week}")` inside the configured Drive folder.
2. If found, `replace_doc_body(doc_id, content)`. If not, `create_doc(title, content)`, then move it to the folder.
3. Record `doc_id` and `doc_url` in `runs/{week}/publish.json`.

**Gmail flow:**
1. `create_draft(to=<PULSE_RECIPIENT>, subject="Groww Review Pulse — Week of {date}", body=<pulse text + Doc link>)`.
2. **Only create a draft, never send.** The tool map has no send action, and the publisher refuses any tool whose name includes `send`.
3. If a draft ID already exists for this week in `publish.json`, the run updates or replaces that draft (when the server supports it) or skips creating a new one.

**Auth:** MCP servers handle Google OAuth and their own token storage. The app never reads or stores Google credentials. It only knows how to start or reach the servers (command, args, URL), which it gets from config.

**Choosing the MCP server:** any MCP server or connector the environment provides for Google Docs and Gmail will work. Examples: a community Google Workspace MCP server run locally over stdio, or a hosted connector. This choice is recorded as an open decision in §13. Moving between servers only means editing `tool_map.yaml` and the server config.

### 4.8 Orchestrator and CLI (`pulse/cli.py`)

```
pulse run     [--week 2026-W47] [--weeks-back 10] [--dry-run] [--skip-ingest]
pulse ingest  [--source play_store|app_store|all] [--week ...] [--full]
pulse analyze [--week ...]           # stages 2–6 only, no publishing
pulse publish [--week ...]           # stage 7 from saved run artifacts
pulse doctor                         # checks config, MCP tools, API credentials
```

- `--dry-run` runs every stage, writes `runs/{week}/pulse.md`, and **skips MCP calls**. Use it for review and in CI.
- Each stage reads the previous stage's saved output, so any stage can be re-run alone.

### 4.9 Scheduling

| Option | When to use |
|---|---|
| Windows Task Scheduler / cron (`pulse run` every Monday 09:00 IST) | Local, single user (the default) |
| GitHub Actions on a schedule | Shared or team use; MCP servers must be reachable from the runner, and secrets go in the repo's secret store |

The run week is the **ISO week that just ended** (Monday–Sunday, IST), so a Monday run covers the previous week.

---

## 5. Data Model

```python
@dataclass(frozen=True)
class Review:
    key: str                 # sha256(source + source_review_id), opaque, never published
    source: Literal["play_store", "app_store"]
    rating: int              # 1–5
    title: str | None
    text: str                # emoji-free review text (kept locally only)
    review_date: date        # IST date of the latest version
    language: str            # always "en": non-English reviews are dropped at ingest
    app_version: str | None  # aggregate use only, never shown per review
    updated_at: datetime     # UTC time of the latest version; the newest version wins

@dataclass(frozen=True)
class CleanReview:
    id: str                  # short run-local ID, e.g. "r0042"
    key: str
    source: str
    rating: int
    review_date: date
    canonical_text: str      # PII-scrubbed; the only text used downstream

@dataclass
class Theme:
    id: str                  # "t1".."t5"
    label: str               # e.g. "Withdrawals & payouts"
    description: str
    review_ids: list[str]
    volume_share: float
    negative_share: float
    avg_rating: float
    score: float

@dataclass
class Quote:
    review_id: str
    theme_id: str
    span: tuple[int, int]    # character offsets into canonical_text
    text: str                # = canonical_text[span[0]:span[1]], filled in by code
    rating: int

@dataclass
class ActionIdea:
    theme_id: str
    action: str              # the concrete next step
    signal: str              # how we'd know it worked

@dataclass
class Pulse:
    week: str                # "2026-W46"
    window: tuple[date, date]
    review_count: int
    sources: dict[str, int]  # {"play_store": 812, "app_store": 143}
    top_themes: list[Theme]  # exactly 3
    quotes: list[Quote]      # exactly 3
    actions: list[ActionIdea]# exactly 3
```

---

## 6. Run Artifacts and Storage

Everything is local files. There is no database: the volume is small and plain files are easy to inspect and replay.

```
data/
├── imports/play/                 # drop Play Store exports here
├── raw/{source}/{run_id}.jsonl   # mapped fetch results (no reviewer identity fields)
├── ingest_state.json             # per-source coverage for incremental ingest
└── reviews.parquet               # normalized, deduped review store (all time)

runs/{iso_week}/
├── manifest.json                 # config snapshot, git SHA, model IDs, counts, timings
├── clean_reviews.jsonl           # scrubbed input used for this run
├── themes.json                   # up to 5 themes + stats + assignments + general_praise/other shares
├── pulse.json                    # structured Pulse
├── pulse.md                      # rendered note (what goes into Docs and email)
├── validation.json               # check results
├── publish.json                  # doc_id, doc_url, draft_id, timestamps
└── llm/                          # request/response logs (scrubbed text only)
```

- `data/` and `runs/` are **git-ignored**. Even scrubbed review text stays out of version control.
- Retention: raw files older than 16 weeks can be pruned (`pulse prune`).

---

## 7. Rendered Output

### 7.1 Google Doc (one page)

```
Groww Weekly Review Pulse — Week of 9 Nov 2026
Source: 1,348 reviews (Play Store 1,202 · App Store 146), 9+ words, English only, 7 Sep – 15 Nov 2026 · 19% general praise

Top Themes
1. Withdrawal delays (18% of reviews · 91% 1–2★) — Money takes days to reach the bank, with no expected date shown.
2. Market-open freezes (15% of reviews · 88% 1–2★) — Order screens freeze or lag at market open, delaying trades.
3. Unhelpful support (9% of reviews · 95% 1–2★) — Tickets are closed without a fix and replies are copy-pasted.

What Users Said
• “<verbatim quote 1>” (★1, Play Store)
• “<verbatim quote 2>” (★2, App Store)
• “<verbatim quote 3>” (★1, Play Store)

Action Ideas
1. Withdrawal delays: Show the expected credit date on every pending withdrawal. Watch for: fewer reviews asking where a withdrawal is.
2. Market-open freezes: Load-test order placement at 9:15 IST and fix the slowest step. Watch for: fewer market-open freeze reviews.
3. Unhelpful support: Reopen tickets automatically when the user replies unresolved. Watch for: fewer closed-without-fix complaints.
```
*(These themes and numbers are examples of the layout only, not real findings, except the source line, which shows the real 2026-W46 counts. "Week of" is the Monday of the ISO week. Shares come from `themes.json` and are inserted by the renderer; a non-zero share under 0.5% shows as `<1%`. The golden-file test, `tests/fixtures/golden_pulse.md`, pins this layout. The "19% general praise" figure is a rough keyword estimate until Phase 3 produces the real share. The line is built by `render/markdown.source_line`, which always states the cleaning rules from config ("9+ words, English only") and adds the `general_praise` share from `themes.json` (§4.4), so a mostly negative pulse still shows how many reviewers were simply happy. The source line does not count toward the 250-word limit.)*

### 7.2 Gmail draft

- **To:** `PULSE_RECIPIENT` from config (yourself or an alias)
- **Subject:** `Groww Review Pulse — Week of {date}`
- **Body:** the same rendered note in plain text, followed by `Full pulse: {doc_url}`

---

## 8. Configuration

`config/pulse.yaml` holds settings that are not secret. Secrets and per-user values come from environment variables or `.env`, which is git-ignored.

```yaml
product:
  name: Groww
  play_package: com.nextbillion.groww
  app_store_id: "<verify>"          # Groww iOS app ID
  app_store_country: in

window:
  weeks_back: 10                    # 8–12
  timezone: Asia/Kolkata

ingest:                             # abridged; see config/pulse.yaml
  request_delay_seconds: 1.5
  overlap_days: 3
  play_public:                      # §13 #2
    enabled: true
    langs: [en]
    country: in
    max_reviews: 40000

analysis:
  max_themes: 5
  top_themes: 3
  quotes: 3
  actions: 3
  max_words: 250
  quote_min_words: 8
  quote_max_words: 60
  seed_themes: [onboarding_kyc, payments, withdrawals, performance_bugs,
                charges, support, trading_features, statements_tax]
  max_discovery_reviews: 3000       # above this, Pass A uses a stratified sample (§4.4)
  assign_batch_size: 500
  min_theme_share: 0.03
  max_other_share: 0.25             # above this, Pass A runs again with `other` examples
  recency_days: 14
  consistency_sample: 100

llm:
  model: claude-opus-5-5
  effort: { cluster: high, assign: medium, compose: high }
  fallbacks: default

publish:
  docs_folder_id: ${PULSE_DOCS_FOLDER_ID}
  recipient: ${PULSE_RECIPIENT}
  doc_title: "Groww Weekly Review Pulse — {iso_week}"

mcp:
  docs:  { transport: stdio, command: "<server cmd>", args: [] }
  gmail: { transport: stdio, command: "<server cmd>", args: [] }
  tool_map: publish/tool_map.yaml
```

| Env var | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` *(or an `ant auth login` profile)* | Claude API credentials |
| `PULSE_RECIPIENT` | Draft recipient (self or alias), never hardcoded |
| `PULSE_DOCS_FOLDER_ID` | Target Drive folder for the pulse docs |

---

## 9. Repository Layout

```
AI Agent+MCP/
├── docs/
│   ├── problemStatement.md
│   └── architecture.md
├── config/
│   └── pulse.yaml
├── src/pulse/
│   ├── cli.py
│   ├── orchestrator.py
│   ├── models.py                 # dataclasses from §5
│   ├── ingest/
│   │   ├── base.py               # ReviewSource protocol
│   │   ├── play_file.py
│   │   ├── play_public.py        # optional, off by default
│   │   ├── app_store_rss.py
│   │   └── normalize.py
│   ├── privacy/
│   │   ├── scrub.py
│   │   └── detectors.py
│   ├── analysis/
│   │   ├── llm.py                # Anthropic client wrapper (structured outputs, retries, fallbacks)
│   │   ├── prompts/              # versioned prompt templates
│   │   ├── cluster.py
│   │   ├── rank.py
│   │   ├── compose.py
│   │   └── validate.py
│   ├── render/
│   │   └── markdown.py           # Pulse → Doc text / email text
│   └── publish/
│       ├── mcp_client.py
│       ├── docs_publisher.py
│       ├── gmail_drafter.py
│       └── tool_map.yaml
├── tests/
│   ├── fixtures/                 # synthetic reviews, no real user data
│   ├── test_scrub.py
│   ├── test_validate.py
│   ├── test_rank.py
│   └── test_publish_mcp.py       # runs against a fake MCP server
├── data/   (git-ignored)
├── runs/   (git-ignored)
├── .env.example
└── pyproject.toml
```

---

## 10. Error Handling and Reliability

| Failure | Behavior |
|---|---|
| A source is down or the feed returns nothing | Continue with the other sources; record it in `manifest.json` and in the pulse source line. Stop the run if the total is below `min_reviews` (default 50). |
| Claude API 429 / 5xx / network error | SDK retries (`max_retries`), then exponential backoff at the stage level. Errors are caught most-specific first (`RateLimitError` → `APIStatusError` → `APIConnectionError`). |
| `stop_reason == "refusal"` | The server-side fallback handles it. If it still refuses, log the category and stop the stage. |
| Structured output fails validation | Re-prompt with the validation errors, up to 2 times, then stop before publishing. |
| Validator fails (quotes, word count, PII) | Same as above. **Nothing is published.** |
| MCP server unreachable or tool missing | `pulse doctor` and startup checks report it clearly. Analysis results are kept, so `pulse publish` can be re-run later. |
| Docs succeeds but Gmail fails | `publish.json` records partial success, and re-running `pulse publish` resumes from the Gmail step. |
| Duplicate run for the same week | Find-or-update on the Doc title; existing draft ID in `publish.json` prevents a second draft. |

---

## 11. Security and Privacy

- **Least data:** reviewer identity fields are never stored. Only scrubbed text reaches the LLM, the logs, or any artifact.
- **Two PII gates:** redaction before analysis, and a scan of the final output that blocks publishing on any hit (§4.3, §4.6).
- **Known gap: personal names.** Neither gate detects names (PII-11). The quote-pick prompt skips reviews that name a person, and humans review the first pulses (Phase 7).
- **No Google credentials in the app:** OAuth tokens live with the MCP servers. The app holds only an Anthropic credential.
- **Draft only:** the Gmail publisher cannot send email (§4.7).
- **Secrets:** environment variables or `.env` only. `.env.example` documents the variable names without values.
- **Store compliance:** nothing behind a login is ever read. The App Store feed is a public feed. The Play Store listing reader was approved by the project owner for this course project (§13 #2); it is public-only, throttled, bounded by the window and `max_reviews`, and can be switched off (`ingest.play_public.enabled: false`) to fall back to export files.
- **Logs:** LLM request and response logs hold scrubbed text only and stay in git-ignored `runs/`.

---

## 12. Testing Strategy

| Layer | What is tested | How |
|---|---|---|
| Unit | PII detectors, normalization, dedupe, window, ranking formula, word counting, quote-span slicing | pytest with synthetic fixtures |
| Contract | Validator rejects invented quotes, more than 250 words, 4 themes in the pulse, leftover PII | Hand-built bad `Pulse` objects |
| LLM integration | Clustering returns 5 or fewer themes, every review is assigned, schema is valid | Small recorded fixture set; marked `@slow` and run on demand (it costs money) |
| MCP integration | Logical actions map to tool calls, find-or-update behavior, refusal of `send` tools | A fake in-process MCP server exposing the same tool names |
| End-to-end | `pulse run --dry-run` on fixtures produces a valid `pulse.md` | CI on every PR |
| Quality eval *(later)* | Theme coherence and usefulness of actions | Small labeled set plus human review of the first few weekly pulses |

---

## 13. Open Decisions

| # | Decision | Options | Current leaning | Status (2026-11-21) |
|---|---|---|---|---|
| 1 | Which Docs and Gmail MCP servers | A community Google Workspace MCP server (stdio), a hosted connector, or the environment's built-in connectors | Whatever the course environment provides; isolated behind `tool_map.yaml` | **Open.** Needs the user to choose and authenticate a server. Fallback: `mcp.docs` / `mcp.gmail` are blank in `pulse.yaml`, `tool_map.yaml` lists the logical actions with no tools mapped, and `pulse doctor` reports both as not configured. This blocks Phase 5 only. |
| 2 | Play Store data path | Manual export files vs. a public-listing fetcher | Export files first; fetcher only after a Terms of Service check | **Settled (revised 2026-11-21): public listing via `google-play-scraper`, approved by the project owner for this course project.** Reason: the brief names the Play Store listing and `com.nextbillion.groww` as the primary target, and only Groww's developers can download a Play Console export, so an export-only rule would leave Android reviews (most of Groww's users) out of scope. Conditions: public listing only, no login or cookies, rate-limited, fetching only what the review window needs (incremental weekly runs, `max_reviews` cap), and no reviewer identity fields read. **Risk accepted:** Google Play's Terms of Service restrict automated access to Play. This is acceptable for a course project but should be re-reviewed before any production or commercial use. Fallback: set `ingest.play_public.enabled: false` to read export files from `data/imports/play/` (mapping in `ingest.play_columns`, Play Console format by default). The problem statement's review-source rule (§6) was amended to match. |
| 3 | Groww iOS App Store ID | Look up and verify | Must be confirmed before the first App Store ingest | **Settled.** `1404871703` ("Groww Stocks, Mutual Fund, IPO", bundle `com.nextbillion.groww`), confirmed through the iTunes Search API. The `in` RSS feed responds, but intermittently serves empty pages; see §4.1 for how the adapter handles that. |
| 4 | Non-English reviews | Exclude from quotes vs. translate vs. drop | Count toward volume, exclude from quotes (no translated "verbatim" quotes) | **Settled (2026-11-21, tutor requirement): dropped at ingest.** Only English reviews are kept; Hindi, Hinglish (`hi-Latn`) and every other language are dropped and counted per source (§4.2). The Play Store fetch no longer requests `hl=hi`. |
| 5 | Implementation language | Python (assumed in this document) vs. TypeScript | Python | **Settled.** Python 3.11+ (developed on 3.12). |
| 6 | Week-over-week trend line in the pulse | Include a "↑/↓ vs last week" marker per theme | Nice to have; data is already kept in `runs/` | Unchanged (Phase 8). Needs stable theme IDs across weeks (last week's taxonomy passed to Pass A as the main seed, §4.4), which is also deferred to Phase 8. |

---

## 14. Requirement Traceability

| Requirement (problemStatement.md) | Where it is met |
|---|---|
| Import 8–12 weeks of reviews (rating, title, text, date) | §4.1, §4.2, `window.weeks_back` |
| Public reviews only, nothing behind a login (amended 2026-11-21, §13 #2) | §4.1 adapter rules, §11 |
| At most 5 themes | §4.4 Pass A, §4.6 theme-count check |
| Pulse: top 3 themes, 3 quotes, 3 actions | §4.4 ranking, §4.5, §4.6 |
| Verbatim quotes, no invented wording | §4.5 span-based extraction, §4.6 fidelity check |
| 250 words or fewer, easy to scan | §4.6 word count, §7.1 layout |
| No PII | §4.3, §4.6 PII scan, §11 |
| Pulse in Google Docs via MCP | §4.7 Docs flow |
| Gmail draft to self or alias via MCP | §4.7 Gmail flow, §7.2 |
| MCP-first, no hand-written Google OAuth or REST | §4.7, §11 |
