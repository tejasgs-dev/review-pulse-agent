# Architecture: Groww Weekly Review Pulse

> Companion to [problemStatement.md](problemStatement.md). This document describes **how** the system is built. The problem statement describes **what** it must do.

---

## 1. Goals and Non-Goals

### Goals
- Turn 8–12 weeks of public Groww reviews (Play Store and App Store) into a one-page weekly pulse of 250 words or fewer.
- Make sure every quote in the pulse is **verbatim** and every artifact is **free of PII**. Code enforces both rules; neither depends on a model.
- Run with **no paid API and no review text leaving the machine** by default: theme clustering uses local machine learning, and the pulse is composed by rules and templates. The Claude API is an optional engine (`--engine llm`), off by default.
- Append the pulse to **one Google Doc** and **send it by Gmail** to one address, through **MCP servers only**.
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
 │ listing (scraper) │   │  │ + PII   │  │ local   │  │ (MCP      │  │  └──────────────────┘
 ├───────────────────┤   │  │ scrub   │  │ ML      │  │  client)  │  │  ┌──────────────────┐
 │ App Store public  │──▶│  └─────────┘  └─────────┘  └───────────┘──┼─▶│ Gmail MCP        │──▶ Email sent
 │ RSS feed (JSON)   │   │        │            │             │       │  └──────────────────┘
 └───────────────────┘   │        ▼            ▼             ▼       │
                         │   ┌──────────────────────────────────┐    │
                         │   │ Local run store (data/, runs/)   │    │
                         │   └──────────────────────────────────┘    │
                         └───────────────────────────────────────────┘
                                         │
                                         ▼
              Optional, off by default: Claude API (`--engine llm`)
```

| External dependency | Role | Interface |
|---|---|---|
| Play Store public listing | Source of Android reviews | `google-play-scraper` request format, sent with httpx (§4.1); export-file import as a fallback |
| App Store customer-reviews RSS | Source of iOS reviews | Public JSON feed, no login |
| Local embedding model `minishlab/potion-base-8M` (model2vec) | Sentence embeddings for clustering and quote ranking | Downloaded once from Hugging Face (free, about 30 MB), pinned by revision, cached in `data/models/`, runs on CPU |
| Claude API *(optional)* | Clustering and composing with `--engine llm` only | Anthropic Python SDK (`pip install -e .[llm]`) |
| Google Docs MCP server | Create or update the pulse document | MCP tools |
| Gmail MCP server | Send the email to `PULSE_RECIPIENT` | MCP tools |

---

## 3. High-Level Pipeline

The system is a **code-orchestrated workflow**, not an open-ended agent. The steps are fixed and known in advance, and Python controls their order. By default every step runs locally: clustering uses sentence embeddings and k-means, and composing uses rules and templates, so a run costs nothing and gives the same result every time. The optional LLM engine (`--engine llm`) calls Claude at stages 4 and 5 instead.

```
┌────────┐   ┌──────────┐   ┌───────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐
│1 Ingest│──▶│2 Normalize│─▶│3 PII Scrub│──▶│4 Cluster │──▶│5 Compose │──▶│6 Validate│──▶│7 Publish │
│        │   │ + Dedupe │   │           │   │ (≤5)     │   │ Pulse    │   │ (guards) │   │ Docs +   │
│        │   │ + Window │   │           │   │ local ML │   │ rules    │   │          │   │ Gmail    │
└────────┘   └──────────┘   └───────────┘   └──────────┘   └──────────┘   └────┬─────┘   └──────────┘
                                                                ▲             │ fail
                                                                └─────────────┘ (next option, ≤2 rounds)
```

| # | Stage | Input | Output | Model |
|---|---|---|---|---|
| 1 | **Ingest** | Play Store listing (or export files), App Store feed | `RawReview[]` | No |
| 2 | **Normalize / Dedupe / Window** | `RawReview[]` | `Review[]` inside the 8–12 week window | No |
| 3 | **PII Scrub** | `Review[]` | `CleanReview[]` with identifiers removed | No (rules-based) |
| 4 | **Cluster** | `CleanReview[]` | Up to 5 `Theme`s plus review→theme assignments | Local embeddings + k-means (Claude with `--engine llm`) |
| 5 | **Compose** | Top 3 themes, candidate quotes | `Pulse` draft (structured) | Rules + templates (Claude with `--engine llm`) |
| 6 | **Validate** | `Pulse` | Pass, or a list of errors to fix | No |
| 7 | **Publish** | `Pulse` | Entry appended to the shared Doc, email message ID | No (MCP tool calls) |

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

This runs **before any text reaches a model or any artifact**. Defense in depth means each layer still protects the output if another one fails:

1. **Drop identifying fields at ingestion.** Reviewer name, avatar URL, review ID, device model, app version per user, and reply metadata are never copied into `Review`. The only thing kept is an opaque internal key (`sha256(source + source_review_id)`), used to trace a quote back to its review.
2. **Redact identifiers inside the text** with deterministic regexes:
   - Email addresses → `[email]`
   - Phone numbers (Indian and international formats) → `[phone]`
   - PAN (`[A-Z]{5}[0-9]{4}[A-Z]`), Aadhaar-like 12-digit groups, UPI IDs (`name@bank`) → `[id]`
   - Account, order, or ticket numbers → `[number]`: any run of `privacy.number_min_digits` (8) or more digits (Groww ticket numbers are 8 digits), card-style 4-4-4 groups, and shorter numbers (`reference_min_digits`, 5+) right after a word such as "ticket", "complaint", "UTR" or "account". "order" counts only with an ID marker (`order id`, `order no`, `order #`), because in a trading app it usually precedes a quantity. Amounts and quantities are never redacted: a number after `₹`, `Rs` or `INR`, or before `shares`, `qty`, `units`, `lots`, `rs`, `/-`, `lakh`, `crore` or `worth`.
   - URLs and `@handles` → `[link]` / `[handle]`
   Overlapping matches are resolved longest-first, so an email inside a URL becomes a single `[link]`. Redaction repeats until the scanner finds nothing, which guarantees `find_pii(scrub(x).canonical_text) == []`. All patterns run in linear time (tested on 50 KB hostile input). **Known gap: personal names are not detected**; regexes cannot do this reliably (edge case PII-11). The quote picker skips any review that probably names a person: a name filter in the default engine, a prompt rule in the LLM engine (§4.5).
3. **Final output scan.** The same detectors run again over the finished pulse, the Doc body, and the email body just before publishing. Any hit stops the publish (see §4.6).

The scrubbed text is the **canonical text**. Quotes are checked against this version, so a redacted quote still counts as verbatim.

### 4.4 Theme Clustering (`analysis/cluster.py`, `analysis/engines/`, `analysis/rank.py`)

**Approach (default engine `local`): sentence embeddings + k-means, with rule-based routing of praise and vague text, and deterministic ranking in code.** No paid API is called and no review text leaves the machine. The implementation steps are in `implementation-plan.md` Phase 3 (L1–L8), and so is the measured comparison of TF-IDF + k-means, sentence embeddings + k-means, and BERTopic on 2026-W46.

**Input profile (2026-W46).** After the cleaning rules (§4.2: 9+ words, English only), 1,348 reviews remain from about 10,000 fetched (Play Store 1,202 · App Store 146). Ratings are bimodal (43% 1★, 35% 5★; 49% are 1–2★). About a fifth are 4–5★ with nothing specific to act on, about 70 are vague complaints ("worst app, useless"), and about 50 are romanized Hindi that passed the English filter. Many reviews mix topics.

**Why this method** (measured on W46, details in the plan):

| Method | Theme quality | Extra install | Determinism |
|---|---|---|---|
| TF-IDF + k-means | Poor: one catch-all cluster of 57% at k=5 | ≈ 160 MB | Seeded, but unstable across seeds (ARI 0.35) |
| MiniLM (torch) + k-means | Splits by tone more than product area | ≈ 1.1 GB | Seeded, very stable (ARI 0.93) |
| **model2vec + k-means (chosen)** | Five distinct product themes; `other` 9% | ≈ 200 MB + 30 MB model | Seeded; identical results across processes; ARI median 0.79 at k=5 |
| BERTopic | One topic of 97%: HDBSCAN finds no dense regions in these short reviews | ≈ 1.4 GB | Seeded UMAP: identical runs |

**Pipeline**
1. **General praise first (rule).** A review rated 4–5★ with a praise word and none of the "specific" words (`but`, `please`, `issue`, `fix`, `add`, `charges`, `support`, …) is `general_praise` and is not clustered. Word lists are config.
2. **Embed** `canonical_text` with model2vec `minishlab/potion-base-8M` (pinned revision, L2-normalized, input sorted by review ID).
3. **Micro-clusters:** k-means with `k0 = 12`, `n_init = 50`, `random_state = 42`.
4. **Route micro-clusters** using their c-TF-IDF keywords: mostly positive with praise words → `general_praise`; mostly "vague" keywords, or loose (median cosine to centroid under 0.55) → `other`; the rest are theme micro-clusters.
5. **Choose k and cluster the theme reviews:** the largest k in 5, 4, 3 for which every cluster holds at least 3% of reviews and the median ARI between seed 42 and seeds 1–3 is at least 0.75. Silhouette is not used: on this data it is low and flat for every k. On W46 the rule picks k = 5.
6. **Per-review `other`:** a theme review with cosine similarity under 0.40 to its centroid goes to `other`. The second-nearest centroid, if within 0.05, is `secondary_theme_id`.
7. **Label from keywords:** match each theme's top c-TF-IDF keywords to the **theme catalogue** (`config/themes.yaml`: id, label of 4 words or fewer, keywords, Phase 4 templates). Two or more of the top 6 keywords must match; with no match the label is the top two keywords ("Chart & Indicator"). Catalogue matches give stable IDs for known themes from week to week.
8. **Checks:** stability ARI (warning under 0.75), `small_theme`, `other_share_high` (above 25%, one retry with `k0 = 16`). The local engine has no sentiment, so assignments carry `sentiment: null`.

**Reserved buckets (not themes, both engines)**
- `general_praise`: positive, with nothing specific to act on ("very good app, easy to use").
- `other`: specific but outside the themes, vague, or not clearly English.
- Neither counts toward the 5-theme limit and neither is ranked. The `general_praise` share is shown in the pulse source line (§7.1), so the pulse isn't read as all negative.

**Optional engine `llm`** (`--engine llm`, needs `ANTHROPIC_API_KEY`; built first and kept): two passes with Claude. Pass A reads every review and proposes at most 5 themes with `include`/`exclude` rules and example IDs; Pass B assigns every review in batches of 500 (primary theme = what the reviewer most wants fixed, root cause over consequence) and a sentiment; code re-requests missing or invalid labels, re-runs discovery once if `other` is above 25%, and measures agreement with a 100-review consistency probe. Both engines return the same `ClusterResult`, so everything after this stage is shared.

**Ranking (deterministic, in code, both engines):** only the clustered themes are ranked, not `general_praise` or `other`.
```
volume_share   = theme reviews / all reviews in the window
negative_share = theme reviews rated 1–2★ / theme reviews          # star rating, never a model's sentiment
recency_weight = clamp(share_last_14d / volume_share, 0.5, 2.0)    # 1.0 if the theme has < 5 reviews in the last 14 days
score          = volume_share × (1 + negative_share) × recency_weight
```
- `negative_share` uses the star rating, so ranking stays deterministic in both engines.
- The 14-day window (`analysis.recency_days`) gives more weight to recent reviews, since this is a *weekly* pulse. The under-5 rule keeps small themes from swinging on a handful of reviews (W46 has 245 reviews in its last 14 days).
- Ties are broken by the count of negative reviews, then by `id`. Themes with no reviews score 0 and are never selected. Fewer than 3 rankable themes fails the stage, because the pulse needs exactly 3.

The **top 3** by score go into the pulse. All themes (up to 5), every assignment, per-theme stats (count, `volume_share`, `negative_share`, last-14-day share, `recency_weight`, score, rank, secondary count), the `general_praise` and `other` shares, the engine and model, the stability (or consistency) measure, and any warnings are kept in `runs/{week}/themes.json`.

### 4.5 Pulse Composer (`analysis/compose.py`, `analysis/engines/`)

**Quote candidates (code, both engines).** For each top theme, code picks up to 8 *candidate* reviews: assigned to that theme, 8–60 words, no profanity, no redaction tokens, most recent first, with ratings mixed (one per rating band in turn). If a theme has nothing eligible, the limits relax in steps (5–80 words, then any review whose text is at most 30% placeholders); if there is still nothing, the run stops with a clear failure (edge cases CMP-01/02/04/16, PII-22). Each candidate is split into whole sentences (`sentence_spans`, code-point offsets).

**Default engine `local`: rules and templates, no model writing text**
- **Name filter (PII-11).** Code skips any candidate that probably names a person: a title before a capitalized word (`Mr`, `Ms`, `Sir`, `Madam`), `my name is`, `I am` + a capitalized word, a capitalized word before `from support` / `from customer care`, `agent` / `executive` / `advisor` + a capitalized word, or any capitalized word that is neither common in that run's reviews (seen in lowercase at least twice) nor on an allowlist of product and finance terms (`Groww`, `UPI`, `IPO`, `SIP`, `F&O`, `NSE`, `Nifty`, `iPad`, month and day names, …). It errs towards skipping: on W46 it rules out 13% of reviews as quotes.
- **Quote pick.** Each remaining candidate is scored by similarity to the theme centroid, presence of the theme's top keywords, a 1–2★ rating when the theme is mostly negative, and a length of 10–30 words; ties are broken by review ID. The quote is the window of 1–2 whole sentences with the most theme keywords (8–35 words), else the whole review (60 words or fewer). Code slices `canonical_text`, so quotes are always verbatim and start and end on a sentence boundary. The three quotes come from three different reviews.
- **Summaries and actions.** Each theme catalogue entry (`config/themes.yaml`) carries a `summary`, an `action` and a `signal` template, plus short forms; themes with no catalogue match use generic templates. Code fills the placeholders: `{kw1}`–`{kw3}` with the theme's top keywords, and, in actions and signals only, **that week's figures for one period, the last two weeks** (`{recent_count}`, `{trend}`), so the actions change from week to week and the action's figure and the signal's target always cover the same span. Whole-window figures (`{count}`, `{share}`, `{negative_share}`) are rejected there when the catalogue loads. Templates never contain typed digits, and summaries carry no figures: **every number in the pulse comes from the computed stats**, never from text someone wrote (CMP-22).
- **Fallbacks.** If validation fails, the rules take the next option rather than asking again: the next-best candidate for a quote problem; one-sentence quotes and then the short templates for too many words. At most 2 rounds; each round is validated in full.

**Optional engine `llm`** (`--engine llm`): Claude picks one quote per theme by **sentence number** (review ID plus first and last sentence; it never writes quote text or counts characters, CMP-10), skipping any candidate that names a person, and writes one summary and one action idea per theme, words only. On a validation failure only the failing part (quotes, writing, or both) is asked for again, with the errors and the previous answer, at most 2 retries.

| Setting (LLM engine only) | Value | Why |
|---|---|---|
| Model | `claude-opus-5-5` | Default current model |
| Thinking | Adaptive (always on for Opus 5.5) | Clustering and prioritizing need judgment |
| Effort | `high` for clustering and composing; `medium` for batch assignment | Opus 5.5 defaults to `medium`, so the level is set explicitly per call |
| Output format | Structured outputs (`output_config.format`), validated in code after the `stop_reason` check | Schema-valid JSON for themes, assignments, and the pulse |
| Streaming | On, with `get_final_message()` | Avoids HTTP timeouts on long inputs and outputs |
| Refusal handling | Check `stop_reason`; server-side `fallbacks: "default"` (beta `server-side-fallback-2026-07-01`) | Required handling for Opus 5.5 |
| Prompt caching | `cache_control` on the stable system prompt and theme definitions | Pass B batches share the same prefix |

### 4.6 Validator (`analysis/validate.py`)

These are hard checks run in code, the same for both engines. If any fails, the composer tries again: the rules engine takes its next option (another candidate, a shorter quote, the short template), and the LLM engine is re-prompted with the list of errors. At most 2 retries. If it still fails, the run stops **before publishing** and writes `runs/{week}/failure.json`.

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
├── docs_publisher.py  # Duplicate check and append of the week's entry to the shared Doc
├── email_sender.py    # Sends the pulse + Doc link to PULSE_RECIPIENT
├── runner.py          # The publish flow and publish.json
└── tool_map.yaml      # Logical action → concrete tool name/args for the chosen server
```

**Why `tool_map.yaml`:** different Google Workspace MCP servers name their tools differently, for example `append_text` or `docs_append`. Publisher code calls *logical* actions (`doc_contains_text`, `append_to_doc`, `send_email`). A small mapping file resolves them to the tools of whichever server is configured. At startup, `list_tools()` checks that every mapped tool exists and stops early if one is missing (`doc_contains_text` is optional: see below).

**One shared Doc.** The project owner creates one Google Doc by hand and puts its ID in `PULSE_DOC_ID` (`publish.doc_id`; a Doc URL is accepted too). Every week is appended to it. **The app never creates a doc:** `create_doc` cannot be mapped, and publishing stops before any call if `PULSE_DOC_ID` is missing.

**Publish flow (one week):**
1. **Already published?** Each week has a unique marker, `({ISO week})`, for example `(2026-W46)` (`render/markdown.week_marker`). The heading `Week of {Monday} {marker}` always ends with it, exactly. Call `doc_contains_text(doc_id, marker)`; the server matches exactly and case-sensitively and replies `found: true` / `found: false` (any other reply is a failure, never "not published"). Found → log "already published", skip both steps, status `skipped`, exit 0. The Doc is the record, so this is the guard on GitHub Actions too. The tool is required: `pulse doctor` checks it, and without it nothing is published.
2. **Append** with `append_to_doc(doc_id, entry)`. The entry is plain text (no Markdown symbols): a separator line (`────…`), the heading, then the pulse without its own header line (`render/markdown.render_doc_entry`).
3. **Send** with `send_email(to=<PULSE_RECIPIENT>, subject="Groww Review Pulse — Week of {date}", body=<pulse text + "Full pulse: " + Doc link>)`. Never runs if the append failed.
4. `publish.json` is rewritten after every step. A skip, or a failure before a step runs (connecting, the check), keeps the earlier run's doc and email entries; a failed append or send keeps them under `previous`; `failed_step` names the step. When a server tool reports an error, `pulse publish` says the server (or its Google access or settings) needs fixing, since running again will not help; a timeout or lost connection says to run again. If the send fails after the append, a re-run on the same machine sends without appending again, even though the marker is in the Doc. `--force` deliberately re-publishes: append and send again.

**Sending rules.** `send_email` is the only sending tool the app may call, only for step 3, and only to `PULSE_RECIPIENT`:
- a tool whose name contains `send` may be mapped only to the `gmail.send_email` action (MCP-03);
- that action's arguments must be exactly `to`, `subject` and `body` from the publisher (no `cc`, `bcc` or fixed values);
- `email_sender.send_pulse` refuses any `to` other than the one configured address;
- the server also restricts recipients to its own `ALLOWED_RECIPIENTS`.

**Known gap (GitHub Actions):** a failed send after a successful append is not retried automatically there (no `publish.json`, and the marker is already in the Doc). The run fails, so it is visible, and `pulse publish --week … --force` re-publishes.

**Auth:** MCP servers handle Google OAuth and their own token storage. The app never reads or stores Google credentials. It only knows how to start or reach the servers (command, args, URL), which it gets from config.

**Choosing the MCP server:** any MCP server or connector the environment provides for Google Docs and Gmail will work. Examples: a community Google Workspace MCP server run locally over stdio, or a hosted connector. This choice is recorded as an open decision in §13. Moving between servers only means editing `tool_map.yaml` and the server config.

### 4.8 Orchestrator and CLI (`pulse/cli.py`)

```
pulse run     [--week 2026-W47] [--weeks-back 10] [--dry-run] [--skip-ingest] [--engine local|llm]
pulse ingest  [--source play_store|app_store|all] [--week ...] [--full]
pulse analyze [--week ...] [--engine local|llm]   # stages 3–6 only, no publishing
pulse publish [--week ...]           # stage 7 from saved run artifacts
pulse doctor                         # checks config, MCP tools, and what the engine needs
```

- `--dry-run` runs every stage, writes `runs/{week}/pulse.md`, and **skips MCP calls**. Use it for review and in CI.
- `--engine` overrides `analysis.engine` (default `local`). `llm` needs the `[llm]` extra and `ANTHROPIC_API_KEY`; `pulse doctor` checks the embedding model for `local` and the API credential only for `llm`.
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
    id: str                  # snake_case: a catalogue ID ("charges") or "kw_<keywords>" (local),
                             # or the LLM's ID (llm)
    label: str               # 4 words or fewer, e.g. "Charges & brokerage"
    description: str
    review_ids: list[str]
    volume_share: float
    negative_share: float
    avg_rating: float
    score: float
    summary: str             # one line from a template (local) or the LLM; numbers are added in code

@dataclass
class Quote:
    review_id: str
    theme_id: str
    span: tuple[int, int]    # character offsets into canonical_text
    text: str                # = canonical_text[span[0]:span[1]], filled in by code
    rating: int
    source: str              # "play_store" | "app_store", shown next to the quote

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
    general_praise_share: float  # shown in the source line
    clustered_theme_count: int   # 5 or fewer
```

---

## 6. Run Artifacts and Storage

Everything is local files. There is no database: the volume is small and plain files are easy to inspect and replay.

```
data/
├── imports/play/                 # drop Play Store exports here
├── raw/{source}/{run_id}.jsonl   # mapped fetch results (no reviewer identity fields)
├── ingest_state.json             # per-source coverage for incremental ingest
├── models/                       # cached embedding model (pinned revision), local engine
└── reviews.parquet               # normalized, deduped review store (all time)

runs/{iso_week}/
├── manifest.json                 # config snapshot, git SHA, engine, model IDs, counts, timings
├── clean_reviews.jsonl           # scrubbed input used for this run
├── themes.json                   # up to 5 themes + stats + assignments + general_praise/other shares
├── pulse.json                    # structured Pulse
├── pulse.md                      # rendered note (what goes into Docs and email)
├── validation.json               # check results
├── publish.json                  # doc_id, heading, marker, append + email status, message_id, timestamps
├── failure.json                  # only when a run stopped before publishing
└── llm/                          # LLM engine only: request/response logs (scrubbed text only)
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

### 7.2 Doc entry and email

**Doc entry** (appended to the shared Doc, plain text):

```
────────────────────────────────────────
Week of 9 Nov 2026 (2026-W46)
Source: 1,348 reviews (…)

Top Themes
…
```

**Email** (sent, not drafted):

- **To:** `PULSE_RECIPIENT` from config (yourself or an alias), exactly one address
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
  engine: local                     # local (default, free) | llm (optional, Claude API)
  min_theme_share: 0.03
  max_other_share: 0.25
  recency_days: 14
  theme_catalogue: config/themes.yaml   # labels, keywords, summary/action/signal templates
  local:                            # §4.4 pipeline
    model: minishlab/potion-base-8M
    model_revision: bf8b056651a2c21b8d2565580b8569da283cab23
    micro_clusters: 12
    n_init: 50
    seed: 42
    min_stability: 0.75             # median ARI vs 3 other seeds, used to choose k
    other_cosine: 0.40
    secondary_margin: 0.05
    praise_words: [good, great, best, easy, smooth, love, ...]
    specific_words: [but, please, issue, fix, add, not, slow, charges, support, ...]
    vague_words: [worst, bad, useless, waste, experience, ...]
  # LLM engine only: seed_themes, max_discovery_reviews, assign_batch_size,
  # assign_concurrency, consistency_sample, min_agreement (see config/pulse.yaml)

llm:                                # used only with --engine llm
  model: claude-opus-5-5
  effort: { cluster: high, assign: medium, compose: high }
  fallbacks: default

publish:
  doc_id: ${PULSE_DOC_ID}           # the one Doc, made by hand; the app never creates one
  recipient: ${PULSE_RECIPIENT}

mcp:
  docs:  { transport: stdio, command: "<server cmd>", args: [] }
  gmail: { transport: stdio, command: "<server cmd>", args: [] }
  tool_map: publish/tool_map.yaml
```

| Env var | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` *(or an `ant auth login` profile)* | Claude API credentials, **only for `--engine llm`**; the default engine needs none |
| `PULSE_RECIPIENT` | The one email recipient (self or alias), never hardcoded |
| `PULSE_DOC_ID` | The shared Google Doc the pulse is appended to (ID or URL) |
| `PULSE_MCP_API_KEY` | Bearer key for the MCP server |

---

## 9. Repository Layout

```
AI Agent+MCP/
├── docs/
│   ├── problemStatement.md
│   └── architecture.md
├── config/
│   ├── pulse.yaml
│   └── themes.yaml               # theme catalogue: labels, keywords, templates
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
│   │   ├── cluster.py            # ClusterResult, themes.json, engine selection
│   │   ├── compose.py            # candidates, sentence spans, ComposeResult, artifacts
│   │   ├── engines/
│   │   │   ├── local_cluster.py  # embeddings + k-means pipeline (default)
│   │   │   ├── rules_compose.py  # name filter, quote rules, templates (default)
│   │   │   ├── llm_cluster.py    # optional: two-pass Claude clustering
│   │   │   └── llm_compose.py    # optional: Claude quote pick and writing
│   │   ├── llm.py                # optional: Anthropic client wrapper
│   │   ├── prompts/              # optional: versioned prompt templates
│   │   ├── rank.py
│   │   └── validate.py
│   ├── render/
│   │   └── markdown.py           # Pulse → Doc text / email text
│   └── publish/
│       ├── mcp_client.py
│       ├── docs_publisher.py
│       ├── email_sender.py
│       ├── runner.py
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
| *(LLM engine)* Claude API 429 / 5xx / network error | SDK retries (`max_retries`), then exponential backoff at the stage level. Errors are caught most-specific first (`RateLimitError` → `APIStatusError` → `APIConnectionError`). |
| *(LLM engine)* `stop_reason == "refusal"` | The server-side fallback handles it. If it still refuses, log the category and stop the stage. |
| *(LLM engine)* Structured output fails validation | Re-prompt with the validation errors, up to 2 times, then stop before publishing. |
| *(Local engine)* Embedding model missing and no network | Stop the analysis stage with a clear error naming `analysis.local.model`; nothing is published. CI caches `data/models/`. |
| *(Local engine)* No stable k ≥ 3, or `other` above 25% after the retry | Stop with `RankError` / record `other_share_high`, as for the LLM engine. |
| Validator fails (quotes, word count, PII) | The composer tries its next option (rules) or re-prompts (LLM), at most 2 times. **Nothing is published** on a final failure. |
| MCP server unreachable or tool missing | `pulse doctor` and startup checks report it clearly. Analysis results are kept, so `pulse publish` can be re-run later. |
| Docs succeeds but Gmail fails | `publish.json` records partial success, and re-running `pulse publish` sends the email without appending again. |
| Duplicate run for the same week | The week's marker, e.g. `(2026-W46)`, found in the Doc (`doc_contains_text`) skips both steps and exits 0; `--force` re-publishes. |
| `PULSE_DOC_ID` missing or wrong | Missing: stop before any call. Wrong or not shared: `append_to_doc` fails clearly and nothing is sent; no doc is ever created. |

---

## 11. Security and Privacy

- **Least data:** reviewer identity fields are never stored. Only scrubbed text reaches a model, the logs, or any artifact. With the default engine, **no review text leaves the machine**: the embedding model runs locally. Only `--engine llm` sends scrubbed text to the Claude API.
- **Two PII gates:** redaction before analysis, and a scan of the final output that blocks publishing on any hit (§4.3, §4.6).
- **Known gap: personal names.** Neither PII gate detects names (PII-11). The quote picker skips reviews that probably name a person (a rule-based name filter by default, a prompt rule in the LLM engine), and humans review the first pulses (Phase 7).
- **No Google credentials in the app:** OAuth tokens live with the MCP servers. The default engine needs no credential at all; `--engine llm` needs an Anthropic credential.
- **Send to one address only:** `send_email` is called only for the publish step and only to `PULSE_RECIPIENT`; the tool map cannot add recipients (§4.7).
- **Secrets:** environment variables or `.env` only. `.env.example` documents the variable names without values.
- **Store compliance:** nothing behind a login is ever read. The App Store feed is a public feed. The Play Store listing reader was approved by the project owner for this course project (§13 #2); it is public-only, throttled, bounded by the window and `max_reviews`, and can be switched off (`ingest.play_public.enabled: false`) to fall back to export files.
- **Logs:** LLM request and response logs (LLM engine only) hold scrubbed text only and stay in git-ignored `runs/`.

---

## 12. Testing Strategy

| Layer | What is tested | How |
|---|---|---|
| Unit | PII detectors, normalization, dedupe, window, ranking formula, word counting, quote-span slicing | pytest with synthetic fixtures |
| Contract | Validator rejects invented quotes, more than 250 words, 4 themes in the pulse, leftover PII | Hand-built bad `Pulse` objects |
| Local clustering | Praise rule, routing, choice of k, labels, determinism (two runs give identical `themes.json`) | pytest with a stub embedding function (no download); a `@slow` check with the real model on fixtures, free |
| Rules composer | Name filter, quote scoring, templates, fallbacks | pytest with synthetic candidates |
| LLM integration *(optional engine)* | Clustering returns 5 or fewer themes, every review is assigned, schema is valid | Fake model offline; `@slow` live tests on demand (they cost money) |
| MCP integration | Logical actions map to tool calls, append-then-send order, no double sending, `--force`, refusal of other `send` tools and of `create_doc` | A fake in-process MCP server exposing the same tool names |
| End-to-end | `pulse run --dry-run` on fixtures produces a valid `pulse.md` | CI on every PR |
| Quality eval *(later)* | Theme coherence and usefulness of actions | Small labeled set plus human review of the first few weekly pulses |

---

## 13. Open Decisions

| # | Decision | Options | Current leaning | Status (2026-11-22) |
|---|---|---|---|---|
| 1 | Which Docs and Gmail MCP servers | A community Google Workspace MCP server (stdio), a hosted connector, or the environment's built-in connectors | Whatever the course environment provides; isolated behind `tool_map.yaml` | **Settled (2026-11-22, project owner): the project's own [gworkspace-mcp](https://github.com/tejasgs-dev/GmailGdocMCPServer), deployed on Render**, one server for Docs and Gmail over Streamable HTTP (`https://gworkspace-mcp.onrender.com/mcp`, `Authorization: Bearer <PULSE_MCP_API_KEY>`). It holds the Google OAuth token; in remote mode `send_email` is exposed only with `ENABLE_SEND_EMAIL=true`, and it accepts only its `ALLOWED_RECIPIENTS`. Its tools report results and errors as text, so `tool_map.yaml` gives a success and a not-found pattern per action. **Update (2026-11-22, project owner): send, not draft.** The pulse is appended to one hand-made Doc (`PULSE_DOC_ID`) and emailed with `send_email`. The server now has the read-only `doc_contains_text` (exact, case-sensitive; replies `found: true/false`), which the publisher requires for its "already published" check. |
| 2 | Play Store data path | Manual export files vs. a public-listing fetcher | Export files first; fetcher only after a Terms of Service check | **Settled (revised 2026-11-21): public listing via `google-play-scraper`, approved by the project owner for this course project.** Reason: the brief names the Play Store listing and `com.nextbillion.groww` as the primary target, and only Groww's developers can download a Play Console export, so an export-only rule would leave Android reviews (most of Groww's users) out of scope. Conditions: public listing only, no login or cookies, rate-limited, fetching only what the review window needs (incremental weekly runs, `max_reviews` cap), and no reviewer identity fields read. **Risk accepted:** Google Play's Terms of Service restrict automated access to Play. This is acceptable for a course project but should be re-reviewed before any production or commercial use. Fallback: set `ingest.play_public.enabled: false` to read export files from `data/imports/play/` (mapping in `ingest.play_columns`, Play Console format by default). The problem statement's review-source rule (§6) was amended to match. |
| 3 | Groww iOS App Store ID | Look up and verify | Must be confirmed before the first App Store ingest | **Settled.** `1404871703` ("Groww Stocks, Mutual Fund, IPO", bundle `com.nextbillion.groww`), confirmed through the iTunes Search API. The `in` RSS feed responds, but intermittently serves empty pages; see §4.1 for how the adapter handles that. |
| 4 | Non-English reviews | Exclude from quotes vs. translate vs. drop | Count toward volume, exclude from quotes (no translated "verbatim" quotes) | **Settled (2026-11-21, tutor requirement): dropped at ingest.** Only English reviews are kept; Hindi, Hinglish (`hi-Latn`) and every other language are dropped and counted per source (§4.2). The Play Store fetch no longer requests `hl=hi`. |
| 5 | Implementation language | Python (assumed in this document) vs. TypeScript | Python | **Settled.** Python 3.11+ (developed on 3.12). |
| 6 | Week-over-week trend line in the pulse | Include a "↑/↓ vs last week" marker per theme | Nice to have; data is already kept in `runs/` | Unchanged (Phase 8). Needs stable theme IDs across weeks. The local engine's theme catalogue already gives stable IDs for known themes; the LLM engine would pass last week's taxonomy to Pass A as the main seed. |
| 7 | Analysis engine | Claude API (paid) vs. local machine learning (free) | Local by default | **Settled (2026-11-22, project owner): no paid LLM API by default.** Clustering uses model2vec embeddings + k-means and composing uses rules and templates; measured on W46 against TF-IDF + k-means, MiniLM + k-means and BERTopic (plan Phase 3). The Claude engine built first stays available as `--engine llm`, off by default. |

---

## 14. Requirement Traceability

| Requirement (problemStatement.md) | Where it is met |
|---|---|
| Import 8–12 weeks of reviews (rating, title, text, date) | §4.1, §4.2, `window.weeks_back` |
| Public reviews only, nothing behind a login (amended 2026-11-21, §13 #2) | §4.1 adapter rules, §11 |
| At most 5 themes | §4.4 choice of k (5 or fewer), §4.6 theme-count check |
| Pulse: top 3 themes, 3 quotes, 3 actions | §4.4 ranking, §4.5, §4.6 |
| Verbatim quotes, no invented wording | §4.5 sentence-span extraction by code, §4.6 fidelity check |
| 250 words or fewer, easy to scan | §4.6 word count, §7.1 layout |
| No PII | §4.3, §4.6 PII scan, §11 |
| Pulse in Google Docs via MCP | §4.7 publish flow (append to the shared Doc) |
| Email to self or alias via MCP (sent, amended 2026-11-22) | §4.7 publish flow, §7.2 |
| MCP-first, no hand-written Google OAuth or REST | §4.7, §11 |
