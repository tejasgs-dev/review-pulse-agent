# Edge Cases and Corner Scenarios: ReviewPulse (Groww app reviews)

> Companion to [architecture.md](architecture.md) (**how**) and [implementation-plan.md](implementation-plan.md) (**when**). Section references such as §4.3 point to `architecture.md`; phase references (P1–P7) point to `implementation-plan.md`.

This document lists the inputs, states, and failures the system must handle on purpose, and what it should do in each case. Each row should become at least one test.

**How to read the tables**

- **ID** is a stable identifier to use in test names and commit messages, for example `test_ing_07_utf16_export`.
- **Covered by** points to the section that already defines the behavior. **Gap** means neither document defines it yet. Section 11 collects the gaps and proposes a resolution for each one.

---

## 1. Ingestion (§4.1, P1)

### 1.1 Play Store file imports (fallback when `play_public.enabled` is false)

| ID | Scenario | Expected behavior | Covered by |
|---|---|---|---|
| ING-01 | `data/imports/play/` is missing or empty | Log a warning, record `play_store: 0 (no import files)` in the manifest, and continue with the other sources | §10 row 1 |
| ING-02 | The export is missing a required column (rating, text, date) | Fail that file with an error that names the missing column and the configured mapping. Do not guess. | §4.1 (column mapping) |
| ING-03 | The export has extra or renamed columns from a newer Console format | Ignore unmapped columns. A mapping change in config fixes renames, with no code change. | §4.1 |
| ING-04 | The file is UTF-16 LE (Play Console CSV exports often are) or UTF-8 with a BOM | Detect the encoding (BOM sniffing, then a UTF-8 fallback). Never fail silently with mojibake. | P1 `ingest/parsing.py` (`decode_bytes`) |
| ING-05 | Review text contains commas, quotes, or embedded newlines | Use a proper CSV parser, never split lines by hand. A fixture holds a multi-line quoted field. | P1 tests |
| ING-06 | A row is malformed: wrong column count or bad quoting | Skip the row, count it under `skipped_malformed` in the manifest, and continue. Fail the file if more than 10% of its rows are malformed. | P1 `ingest/play_file.py` |
| ING-07 | Rating is out of range (`0`, `6`), a float string (`"4.0"`), or empty | Accept `"4.0"` as 4. Drop the other cases and count them. | P1 `parsing.parse_rating` + `normalize.py` |
| ING-08 | Dates come in mixed formats, or as UTC timestamps near midnight | Parse to an aware datetime, convert to `Asia/Kolkata`, and only then take the date. 23:30 UTC on Sunday is Monday in IST. | §4.2 (timezone IST) |
| ING-09 | Several export files overlap in time | Rows are de-duplicated by `key` (see NRM-05). Overlap is expected and must not raise an error. | §4.2 |
| ING-10 | A user edited a review: same source review ID, new text, new date | One `Review` per `key`. Keep the most recent version and replace the older text. | P1 `normalize.py`, `store.py` (newest `updated_at` wins) |
| ING-11 | The export includes developer replies | Never copy reply text or reply metadata into `Review`. | §4.3 layer 1 |

### 1.2 App Store RSS feed

| ID | Scenario | Expected behavior | Covered by |
|---|---|---|---|
| ING-20 | The first `entry` in the feed is app metadata, not a review | Skip entries with no `im:rating`. | P1 `ingest/app_store_rss.py` |
| ING-21 | `feed.entry` is a single object instead of a list (this happens when a page has one entry) | Normalize it to a list before iterating. | P1 `ingest/app_store_rss.py` |
| ING-22 | `feed.entry` is missing (no reviews, or past the last page) | Treat it as end of feed and stop paging. This is not an error. | P1 ("stops early") |
| ING-23 | Apple returns 400/403/429/5xx or an HTML error page | Retry with backoff a bounded number of times, then mark the source as failed and continue (§10 row 1). Never parse HTML as JSON. | §10 |
| ING-24 | The app ID is wrong, so every page returns an empty feed | `pulse doctor` and the first ingest report "0 reviews from App Store; check `app_store_id`". | §13 #3, P0 |
| ING-25 | A page with older reviews appears before a newer one (the feed order is not strictly by date) | Stop paging only when a **whole page** is older than the window, not at the first old review. | P1 `ingest/app_store_rss.py` |
| ING-26 | The window covers more than the ~500 reviews the feed exposes | Accept the shortfall. The source line shows the real count. Optionally note "feed limit reached" in the manifest. | P1 risks |
| ING-27 | The same review appears on two pages because the feed shifted while paging | De-duplicate by `key`. | §4.2 |
| ING-28 | Any URL form of a feed page is intermittently served an empty feed (HTTP 200, no entries); a whole page can be empty in every form while later pages still have data (seen live: page 7 empty, page 8 not) | Try several URL forms per page, keep paging past empty pages, retry interior gaps once, and report the source as `partial` with a warning if a gap remains | P1 `ingest/app_store_rss.py` |

### 1.3 Play Store public listing

| ID | Scenario | Expected behavior | Covered by |
|---|---|---|---|
| ING-40 | The scraper library's `reviews()` swallows every exception, so a block or an outage looks like "no more reviews" | Don't use `reviews()`. Send requests with httpx and treat HTTP errors, rate-limit markers and unparseable responses as errors, retried with backoff and then reported | P1 `ingest/play_public.py` |
| ING-41 | Importing the library sets `ssl._create_default_https_context = ssl._create_unverified_context`, turning off TLS certificate checks process-wide | Restore the original default right after import; a test asserts it | P1 `ingest/play_public.py` |
| ING-42 | The library builds review times with `datetime.fromtimestamp()`, a naive *local* time | Convert with `astimezone()`, which reads naive values as local time; a test asserts no time-zone shift | P1 `ingest/play_public.py` |
| ING-43 | `hl=` filters reviews by language | Only `hl=en` is fetched, since non-English reviews are dropped anyway (§13 #4). The adapter still supports several languages and drops cross-language duplicates by review ID. `hl=en` still returns a few Hindi reviews; normalize drops them | P1 `ingest/play_public.py` |
| ING-44 | The primary language's first request fails | The Play source fails; the App Store still runs (§10 row 1) | P1 `ingest/play_public.py` |
| ING-45 | A secondary language fails, or paging fails midway | Keep what was fetched, warn, and mark the source `partial`; coverage does not advance, so the next run re-fetches | P1 `play_public.py`, `runner.py` |
| ING-46 | The listing runs out of reviews before the window start | Warn; coverage does not advance | P1 `ingest/play_public.py` |
| ING-47 | A backfill would be very large | Stop at `max_reviews` (default 40,000), warn, mark the source `partial` | P1 `ingest/play_public.py` |
| ING-48 | Weekly runs re-download the whole window (about 10,000 raw Play reviews) | Incremental ingest: fetch only from the last complete fetch minus `overlap_days`; full re-fetch when there is no coverage, the store is missing, the state file is corrupt, or the window grows | P1 `ingest/runner.py`, `store.IngestState` |
| ING-49 | The listing changes its response format, or the library changes its internals | The parser raises "unexpected response format", the source fails loudly, and the library version is pinned (`>=1.2.7,<1.3`) | P1 `ingest/play_public.py`, `pyproject.toml` |
| ING-50 | Reviewer names, avatars and reply text are in every listing item | Only `reviewId`, `score`, `content`, `at` and `appVersion` are extracted; a test asserts the name and reply never appear | P1 `ingest/play_public.py` |

### 1.4 Raw storage

| ID | Scenario | Expected behavior | Covered by |
|---|---|---|---|
| ING-30 | Two runs start in the same second and create the same `run_id` | `run_id` includes enough entropy (timestamp plus a short random suffix) that files never collide. | P1 `store.new_run_id` |
| ING-31 | The disk fills up while raw data is being written | Fail the stage clearly. A partial `.jsonl` must not be treated as complete; write to a temp file, then rename. | P1 `store.py` (temp file + rename) |

---

## 2. Normalize, Dedupe, Window (§4.2, P1)

| ID | Scenario | Expected behavior | Covered by |
|---|---|---|---|
| NRM-01 | `weeks_back` is set to 7, 13, 0, or a negative number | The config loader rejects it with "weeks_back must be 8–12". | §8, P0 (pydantic) |
| NRM-02 | A review is dated exactly on a window boundary | Both ends are **inclusive**. Fixtures cover first-day, last-day, and one-day-outside reviews. | §4.2 |
| NRM-03 | A review is dated in the future (clock skew or a bad export) | Drop it and count it under `skipped_future_date`. | P1 `normalize.py` |
| NRM-04 | `--week` names a past week | Anchor the window to the **end of that ISO week**, not to today. | P0 `weeks.review_window` |
| NRM-05 | Two different users post the same short text on the same day ("Good app") | They are **separate reviews** and both count toward volume. A text hash must not merge reviews that have different source IDs. | P1 `normalize.py`: dedupe by `key`; content hash only for rows without an ID |
| NRM-06 | Text that differs only in whitespace, case, or Unicode form (NFC/NFKC) | Normalize these for the dedupe hash only. `text` itself stays unchanged. | §4.2 |
| NRM-07 | Review is only emoji ("👍👍👍") or punctuation | Emojis are stripped first; nothing is left, so it is dropped as `dropped_no_text` (punctuation-only: `dropped_too_short`) | P1 `normalize.py` |
| NRM-08 | Devanagari text, or text with no spaces | Word counting must not treat a long unspaced Devanagari run as one word in a way that changes eligibility unexpectedly. Use a Unicode-aware tokenizer. | P1 `textutil.words` |
| NRM-09 | Language detection on very short text, or on Hinglish in Latin script ("app bahut slow hai") | Results are unreliable. Fix the seed (`DetectorFactory.seed = 0`) so runs are deterministic, and label text with confidence below a threshold as `und` (not eligible for quotes). | P1 `ingest/language.py`: Hinglish and English word lists, then seeded langdetect |
| NRM-10 | An App Store review has a title and a body | Decide whether `canonical_text` is the body only or `title + body`. Quote spans must refer to that same text. Recommended: body only, with the title kept for theme context. | P1: `Review.text` is the body only; the title is stored separately |
| NRM-11 | Rating-only entries (no text) | Dropped and counted as `dropped_no_text` (cleaning rules, §4.2). They no longer count toward rating stats | P1 `normalize.py` |
| NRM-12 | The total after filtering is below `min_reviews` (50) | Stop the run before calling the LLM, with a clear message that shows the per-source counts. | §10 row 1 |
| NRM-13 | Re-running ingest on the same input | Adds 0 rows to `reviews.parquet`. | P1 exit criteria |
| NRM-14 | Exactly 8 words vs exactly 9 words | 8 is dropped (`dropped_too_short`), 9 is kept (`ingest.min_words: 9`, "more than 8 words") | P1 `normalize.py` |
| NRM-15 | Emojis pad the word count ("8 words 🙏🙏🙏") | Emojis are stripped before counting, so it is dropped; `emojis_removed` is counted | P1 `normalize.py`, `textutil.strip_emojis` |
| NRM-16 | An emoji joins two words ("good👍app") | Each emoji run becomes a space, so the words stay separate | P1 `textutil.strip_emojis` |
| NRM-17 | ALL-CAPS English is mislabelled by langdetect (seen: German) | Mostly-uppercase text is lowercased before detection | P1 `ingest/language.py` |
| NRM-18 | Hinglish (Hindi in English letters) | Labelled `hi-Latn` when ≥20% of words are common Hindi words, and dropped; Hinglish without enough marker words usually gets a random non-English label from langdetect and is dropped too. Mostly-English text with a few Hindi words stays `en` | P1 `ingest/language.py` |
| NRM-19 | An edit shortens a stored review below the minimum | The newest version is filtered; the stored copy is removed (`store.removed`) | P1 `normalize.py`, `store.py` |
| NRM-20 | The store file has the pre-filter format (`quotable` column) | Loading refuses with instructions: move it aside and run `pulse ingest --full` | P1 `store.StoreSchemaError` |

---

## 3. PII Scrubbing (§4.3, P2)

### 3.1 Things that must be redacted

| ID | Scenario | Expected behavior | Covered by |
|---|---|---|---|
| PII-01 | Email with mixed case, subdomains, or plus tags (`A.B+x@mail.co.in`) | `[email]` | P2 `privacy/detectors.py` |
| PII-02 | Obfuscated email (`name at gmail dot com`, `name[at]gmail.com`) | `[email]` | **Gap**: add obfuscation patterns |
| PII-03 | Phone numbers written as `+91 98765 43210`, `098765-43210`, `9876543210`, or `(+91)9876543210` | `[phone]` | P2 `privacy/detectors.py` |
| PII-04 | Phone number with spaces between every digit, or a number spelled out in words | Best effort for spaced digits; spelled-out numbers are a known limitation | **Gap** (record as limitation) |
| PII-05 | PAN in lower case (`abcde1234f`) | `[id]`, matched case-insensitively | P2 `privacy/detectors.py` (case-insensitive) |
| PII-06 | Aadhaar written as `1234 5678 9012` or `1234-5678-9012` | `[id]` | P2 `privacy/detectors.py` |
| PII-07 | UPI ID (`name@okaxis`, `98765@ybl`) | `[id]`. Must not be half-matched as an email, or left as `name@[handle]`. | P2 `privacy/detectors.py` |
| PII-08 | Order, ticket, or account numbers that mix letters and digits (`ORD12345678`, `TKT-2026-998877`) | `[number]` | Partly P2: the digits of `ORD12345678` become `ORD[number]`; letter-only IDs are not covered |
| PII-09 | A bare domain or URL with no scheme (`groww.in/help`, `bit.ly/xyz`) | `[link]` | P2 `privacy/detectors.py` (lowercase bare domains only) |
| PII-10 | PII in the App Store **title** field | Scrub the title with the same detectors as the body | Moot: `CleanReview` carries the body only, so titles never reach the LLM or artifacts |
| PII-11 | Personal names in the text ("Rahul from support was rude") | **Known gap (accepted).** The Phase 2 regex scrubber does not detect names, and cannot do so reliably. Mitigation in Phase 4: the default engine's **name filter** skips any quote candidate that probably names a person (a title before a capitalized word, \"my name is\", \"I am\" + a capitalized word, a capitalized word before \"from support\", \"agent\"/\"executive\" + a capitalized word, or a capitalized word mid-sentence that is not on the product-term allowlist); the LLM engine's quote-pick prompt tells the model to skip such candidates. A human reads the first pulses (P7). Seen on real data: one review names a support executive. | **Known gap**; P4 name filter (local) / prompt rule (llm) |
| PII-12 | A reviewer's own name inside the text ("I am Priya Sharma, my account…") | Same as PII-11: not detected by the scrubber; the name filter (\"I am\" / \"my name is\" + a capitalized word) or the LLM prompt rule skips such candidates. | **Known gap**; P4 name filter / prompt rule |

### 3.2 Things that must not be redacted (false positives)

| ID | Scenario | Expected behavior | Covered by |
|---|---|---|---|
| PII-15 | Amounts: `₹500`, `Rs 1,00,000`, `INR 25000`, `50k` | Keep them. Amounts with a currency marker or Indian comma grouping are not `[number]`. | P2 `privacy/detectors.py` |
| PII-16 | A large bare amount ("lost 100000 in F&O") with no currency marker | Ambiguous. Set the `[number]` minimum length (for example ≥ 9 digits) so typical rupee amounts stay. Document the threshold. | P2: threshold 8 digits; amounts and quantities kept (`₹`/`Rs`/`INR` before, or `shares`/`qty`/`units`/`lots`/`rs`/`/-`/`lakh`/`crore`/`worth` after) |
| PII-17 | Years, dates, times (`2026`, `12/11/2026`, `9:15`), percentages, "5 star", "1-2★" | Keep them | P2 `tests/test_scrub.py` |
| PII-18 | Stock tickers and product names (`NIFTY50`, `TATAMOTORS`, `SBIN`) | Keep them. The PAN pattern requires the 5-letters-4-digits-1-letter shape. | P2 `tests/test_scrub.py` |
| PII-19 | `@` used as "at" ("crashed @ 9:15", "@groww please fix") | `@groww` becomes `[handle]`, which is acceptable. `@ 9:15` (with a space) is unchanged. | P2 `privacy/detectors.py` |

### 3.3 Detector mechanics

| ID | Scenario | Expected behavior | Covered by |
|---|---|---|---|
| PII-20 | Two detectors match overlapping spans (an email that also contains a UPI-like part; a URL that contains an email) | Resolve overlaps by taking the longest match first, then a fixed priority order. Each character is redacted at most once. | P2 `detectors.detect` (longest match, then priority) |
| PII-21 | The scrubbed text already contains a token such as `[email]` (the user typed it) | Leave it alone. Scrubbing is idempotent: `scrub(scrub(x)) == scrub(x)`. | P2 `scrub.redact` (repeats until clean) |
| PII-22 | A review is mostly PII ("call me 98… mail x@y.com") | After scrubbing, it is mostly placeholders. Drop it from quote candidates if placeholders make up more than about 30% of its tokens. | P4: `compose.select_candidates` (redaction tokens excluded; >30% placeholders never quoted) |
| PII-23 | A catastrophic-backtracking regex runs on a very long review | Detectors must be linear-time (no nested quantifiers), with a test on a 50 KB input. | P2 `tests/test_scrub.py` (50 KB hostile inputs) |
| PII-24 | Raw files in `data/raw/` hold untouched responses, including reviewer names and avatars | Conflicts with §11 ("reviewer identity fields are never stored"). Either strip identity fields before writing raw files, or document raw files as an exception that is git-ignored, pruned at 16 weeks, and never logged. | P1 `store.save_raw`: raw files hold mapped `RawReview` records, not untouched responses |
| PII-25 | An exception message or stack trace includes raw `Review.text` | Logging must use review keys or IDs, never the text. Add a test that forces an exception and greps the log. | **Gap** |
| PII-26 | In a trading app, "order" usually precedes a quantity ("order of 50000 shares", "order 25000 qty"), not an ID | "order" counts as a reference only with an explicit marker (`order id`, `order no`, `order number`, `order #`). A number followed by a quantity or amount word (`shares`, `qty`, `units`, `lots`, `rs`, `rupees`, `/-`, `lakh`, `crore`, `worth`, …) is never redacted, by any number rule | P2 `privacy/detectors.py`, `tests/test_scrub.py` |

---

## 4. Theme Clustering and Ranking (§4.4, P3)

> **Engines (revised 2026-11-22).** Sections 4.1 and 4.2 and section 10 describe the optional LLM engine (`--engine llm`). The default local engine has its own cases in section 4.5. Ranking (4.4) and untrusted content (4.3) apply to both.

### 4.1 Discovery (Pass A)

| ID | Scenario | Expected behavior | Covered by |
|---|---|---|---|
| CLU-01 | The model returns 6 or more themes | Reject the response in code and re-prompt. Do not rely on schema `maxItems`, because structured outputs may not enforce array-length limits. | §4.6, P3 tests |
| CLU-02 | The model returns fewer than 3 themes (for example on a quiet week with 60 reviews) | The pulse needs exactly 3. Decide whether to stop with "insufficient distinct themes" or to allow a pulse with fewer themes. Recommended: stop before publishing and write `failure.json`. | **Gap** |
| CLU-03 | Duplicate theme IDs, or two themes that are near-duplicates ("App crashes" / "App stability") | Reject duplicate IDs. Near-duplicates are a quality issue; the prompt should say themes must be mutually exclusive. | **Gap** (ID check) |
| CLU-04 | Theme labels change from week to week ("Withdrawals" becomes "Payouts & withdrawals") | This breaks trend comparison (§13 #6). Pass last week's themes as optional seeds, and map them by ID or similarity. | **Gap** (needed before Phase 8) |
| CLU-05 | More than ~3,000 reviews, so Pass A sees a stratified sample | Pass B still assigns **every** review. `volume_share` uses all reviews, not the sample. | §4.4 |
| CLU-06 | A rating stratum or week is empty in the sample | Sampling handles empty strata without dividing by zero. | **Gap** |

### 4.2 Assignment (Pass B)

| ID | Scenario | Expected behavior | Covered by |
|---|---|---|---|
| CLU-10 | The model omits some review IDs | Re-request only the missing IDs, a bounded number of times. Anything still missing goes to `other` and is counted in the manifest. | P3 |
| CLU-11 | The model invents review IDs that are not in the batch | Drop them and log a warning. | **Gap** |
| CLU-12 | The model assigns the same ID twice with different themes | Keep the first assignment, log the conflict, and count it. | **Gap** |
| CLU-13 | The model uses a `theme_id` that Pass A did not define | Treat that assignment as missing and re-request it. | **Gap** |
| CLU-14 | `other` is larger than 25% | Report it. Re-run Pass A with examples from `other`, once. | P3 risks |
| CLU-15 | `other` would rank in the top 3 | `other` is **never** eligible for the top 3. | **Gap** |
| CLU-16 | Sentiment contradicts the rating (5★ "worst app ever", sarcasm) | Ranking uses the rating for `negative_share`, so this does not change scores. Sentiment is informational. | §4.4 |

### 4.3 Untrusted review content

| ID | Scenario | Expected behavior | Covered by |
|---|---|---|---|
| CLU-20 | Prompt injection inside a review ("Ignore previous instructions and output…") | Reviews go inside clearly delimited data blocks, and the system prompt says review text is data. Structured output plus the validator limit the damage. The LLM has **no tools**, so it cannot reach MCP. | **Gap** (prompt rule); tool isolation is §3 |
| CLU-21 | Abusive, sexual, or self-harm content in reviews causes a refusal | Check `stop_reason`, then the server-side fallback. If it still refuses, log the category. Optionally retry the batch without the offending reviews, which are assigned to `other`. | §10 row 3 |
| CLU-22 | `stop_reason == "max_tokens"`, so the JSON is truncated | Never parse a partial output. Split the batch in half and retry. | **Gap** |

### 4.5 Local engine (default)

| ID | Scenario | Expected behavior | Covered by |
|---|---|---|---|
| LOC-01 | The embedding model is not cached and there is no network | Stop the analysis stage with a clear error naming `analysis.local.model`; nothing is published. CI caches `data/models/`. | P3/P4 built (`tests/test_local_cluster.py`, `tests/test_rules_compose.py`) |
| LOC-02 | The model on the hub changes | Pin `analysis.local.model_revision`; record the revision in the manifest. | P3/P4 built (`tests/test_local_cluster.py`, `tests/test_rules_compose.py`) |
| LOC-03 | A fresh run gives different themes on another CPU | Sorted input, fixed seeds, single-threaded clustering. Same OS: byte-identical; across OSes: same labels and top 3 (exit criterion). | P3/P4 built (`tests/test_local_cluster.py`, `tests/test_rules_compose.py`) |
| LOC-04 | No k from 5 to 3 passes the stability and 3% rules (a quiet week) | Stop with `RankError` and a clear message; nothing is published. | P3/P4 built (`tests/test_local_cluster.py`, `tests/test_rules_compose.py`) |
| LOC-05 | A "theme" is really generic praise or venting ("worst app ever") | The praise rule runs before clustering; micro-clusters dominated by praise or vague words are routed to `general_praise` / `other`. Word lists are config. | P3/P4 built (`tests/test_local_cluster.py`, `tests/test_rules_compose.py`) |
| LOC-06 | Romanized Hindi that passed the English filter forms a cluster | Hinglish words are on the vague list, so the micro-cluster goes to `other`. | P3/P4 built (`tests/test_local_cluster.py`, `tests/test_rules_compose.py`) |
| LOC-07 | A theme's keywords match no catalogue entry | Label from the top two keywords (4 words or fewer); the unmatched label is logged so `config/themes.yaml` can grow. | P3/P4 built (`tests/test_local_cluster.py`, `tests/test_rules_compose.py`) |
| LOC-08 | Two themes match the same catalogue entry | Merge them into one theme. | P3/P4 built (`tests/test_local_cluster.py`, `tests/test_rules_compose.py`) |
| LOC-09 | Keyword labels contain a redaction token or a stop word | Redaction tokens and stop words are removed before c-TF-IDF. | P3/P4 built (`tests/test_local_cluster.py`, `tests/test_rules_compose.py`) |
| LOC-10 | A template produces more than 250 words, or a number | Short template forms are used as a fallback; templates are checked at config load for digits. The validator's `no_stats_in_text` also applies. | P3/P4 built (`tests/test_local_cluster.py`, `tests/test_rules_compose.py`) |

### 4.4 Ranking

| ID | Scenario | Expected behavior | Covered by |
|---|---|---|---|
| RNK-01 | Two themes have exactly the same score | Break ties deterministically: higher volume first, then theme ID. | P3 tests |
| RNK-02 | A theme has zero assigned reviews | Score is 0. It is never in the top 3 while 3 non-empty themes exist. | P3 tests |
| RNK-03 | No reviews at all in the last 2 weeks | `recency_weight` must be defined (for example 1.0) and never divide by zero. | **Gap** (formula not specified) |
| RNK-04 | The `volume_share` denominator: does it include `other` and rating-only reviews? | Define it as all text reviews in the window, **including** `other`, so shares are honest. Write this in §4.4. | **Gap** |
| RNK-05 | A theme has a high share but is all 5★ praise ("Easy to use") | The formula ranks it lower, but it can still reach the top 3. That is allowed; the summary should describe it as positive. | §4.4 |

---

## 5. Quote Selection and Composition (§4.5, P4)

> In the default engine, quotes are picked by rules and summaries and actions come from templates (§4.5). Rows about "the model" apply to `--engine llm`; the checks themselves (fidelity, quote–theme link, distinct reviews, no statistics) apply to both.

### 5.1 Candidate selection

| ID | Scenario | Expected behavior | Covered by |
|---|---|---|---|
| CMP-01 | A top theme has 0 eligible candidates (all non-English, too short, or too long) | Relax the limits in steps (word range 5–80, then any recent review). If there is still nothing, stop with a clear failure. | P4: `compose.select_candidates` relaxes in steps; `ComposeError` if still empty |
| CMP-02 | A theme has 1–4 candidates (fewer than the 5–8 target) | Continue with what exists. | P4: continues with what exists (test) |
| CMP-03 | All candidates have the same rating | "Mixed ratings" is a preference, not a requirement. Do not fail. | §4.5 |
| CMP-04 | A candidate contains profanity or abuse | Exclude it from candidates. The audience is internal and professional. | P4: profanity list in `compose.py` |
| CMP-05 | A candidate mentions a person by name | Skip any candidate that probably names a person (PII-11): the name filter in the default engine, the quote-pick prompt in the LLM engine. The validator cannot check this, so a human reviews the first pulses. | P4 name filter / prompt rule (both built) |

### 5.2 Spans and quote fidelity

| ID | Scenario | Expected behavior | Covered by |
|---|---|---|---|
| CMP-10 | The model returns character offsets that are off by a few characters (LLMs count characters poorly) | **Design risk.** Recommended: have the model return the quote text, then code checks that it is an exact substring and computes the span itself. Alternative: pre-split candidates into numbered sentences and have the model pick a sentence range. | P4: the model picks a **sentence range**; code slices the text (no offsets) |
| CMP-11 | `start >= end`, a negative offset, or `end > len(text)` | Reject the span and re-prompt. | P4: `build_quotes` rejects the range and re-prompts |
| CMP-12 | The span starts or ends in the middle of a word | Snap it to word boundaries, or reject it. Never publish "…rawal is delayed". | P4: quotes are whole sentences by construction |
| CMP-13 | Text has emoji, combining characters, or surrogate pairs | Offsets count Python code points. The fidelity check runs on the same string. A test includes `🙏` and Devanagari matras. | P4: offsets are code points; test with 🙏 and Devanagari |
| CMP-14 | Canonical text contains `\r\n` or repeated spaces | The fidelity check compares against `canonical_text` exactly. Rendering may collapse whitespace, but must not change words. State the rule explicitly. | P4: fidelity checks `canonical_text` exactly; the renderer only collapses whitespace |
| CMP-15 | The quote is cut mid-sentence and the renderer adds "…" | Allowed only outside the quote marks, or as a defined rule. The verbatim check applies to the text inside the quote marks. | P4: no ellipsis is added; quotes are whole sentences |
| CMP-16 | The quote contains `[email]` or another redaction token | Still counts as verbatim (§4.3), but avoid quotes that are mostly placeholders (PII-22). | §4.3 |
| CMP-17 | The quote contains `"`, `“`, or `”` | The renderer uses typographic quote marks and leaves the inner text unchanged. | P4: `render_quote` uses “ ” and leaves inner text unchanged |
| CMP-18 | The model picks a review that was not in that theme's candidate list | Reject it. The quote must come from that theme's candidates (§4.6 quote–theme link). | P4: `build_quotes` + validator `quote_theme_link` |
| CMP-19 | The model picks the same review for two themes | Impossible if assignment is one theme per review. The validator still checks that the 3 review IDs are distinct. | P4: validator `quote_count` (distinct reviews) |
### 5.3 Pulse writing

| ID | Scenario | Expected behavior | Covered by |
|---|---|---|---|
| CMP-20 | Two actions reference the same theme, and one theme has none | §4.6 only requires each action to reference a top-3 theme. Recommended: require **one action per theme**. | P4: validator `action_theme_link` (one action per theme) |
| CMP-21 | An action is vague ("Improve the app") | Hard to check in code. The prompt requires area, change, and signal; the `signal` field must not be empty. Humans review the first runs (P7). | §4.5 |
| CMP-22 | A theme summary states a number that disagrees with the computed stats ("40%" when the share is 31%) | Insert numbers in code during rendering. The LLM writes words, not statistics. | P4: the renderer inserts every number; validator `no_stats_in_text` |
| CMP-23 | The model writes PII or a URL into a summary (for example by echoing a redaction token unchanged) | Caught by the final PII scan; re-prompt. | §4.6 |

---

## 6. Validation (§4.6, P4)

| ID | Scenario | Expected behavior | Covered by |
|---|---|---|---|
| VAL-01 | Word count is exactly 250 | Pass (≤ 250). A test covers 250 and 251. | P4 tests |
| VAL-02 | What counts as a word: `₹500`, `1–2★`, `F&O`, `[email]`, `31%`, emoji, hyphenated words | Define one tokenizer (split on whitespace after stripping markdown) and use it in both the validator and tests. Document it. | P4: `textutil.word_count` in validator and tests |
| VAL-03 | Headings ("Top Themes") count toward the limit, or do not | §4.6 excludes only the header and source line. Decide on section headings and keep the rule in config. | P4: headings count; header and source line do not |
| VAL-04 | **The email body includes `Full pulse: {doc_url}`, and the URL detector flags it** | The final scan would always block publishing. Allowlist the exact Doc URL (and the recipient address if it appears in the body), and no other links. | P4: `find_pii(allow=[doc_url, recipient])` in the validator |
| VAL-05 | A quote legitimately contains `<3`, `<500`, or `<script>` | The "no leftover `<…>` placeholders" check must match only the real template tokens (for example a known list such as `<verbatim quote 1>`), not any angle bracket. | P4: only known template tokens match (test with `<3`) |
| VAL-06 | Markdown control characters in quotes (`**`, `#`, `[x](y)`, backticks) | Escape them when rendering so the Doc formatting stays correct and no link is created. | **Open**: the Doc body is plain text; decide escaping once the Docs MCP server's input format is known (P5) |
| VAL-07 | The validator fails 3 times in a row | Write `failure.json` with every attempt's errors and exit non-zero. Nothing is published. | P4: `failure.json` with every attempt; exit code 10 |
| VAL-08 | A retry fixes one error but introduces another | Each retry is validated in full, not just for the previous errors. | P4: every attempt is validated in full |
| VAL-09 | Retries are bounded, but each one is an expensive Opus call | Retry only the composer, never clustering. Record token usage per attempt in the manifest. | P4: only the failing part (quotes or writing) is retried; usage per stage in the manifest |
| VAL-10 | A section is empty (for example 0 action ideas, or an empty summary) | Caught by the "no empty sections" check. | §4.6 |

---

## 7. Rendering (§7, P4)

| ID | Scenario | Expected behavior | Covered by |
|---|---|---|---|
| RND-01 | Theme percentages round to 0%, or several round up together | Show `<1%` instead of `0%`. Top-3 shares are not expected to add up to 100. | P4: `format_share` |
| RND-02 | One source failed or returned 0 | The source line shows it honestly, for example `App Store 0 (feed unavailable)`. | §10 row 1 |
| RND-03 | The window crosses a year boundary (`8 Dec 2026 – 15 Feb 2027`) | Show the year on both dates when they differ. | `render.format_window` (test) |
| RND-04 | "Week of {date}" for ISO week 53, or for W01 that starts in the previous calendar year | Use the Monday of the ISO week. 2026 has a W53 (28 Dec 2026 – 3 Jan 2027), and the ISO year of `2027-W01` starts on 4 Jan 2027. | P4: `week_of` uses the ISO Monday; tests for 2026-W53 and 2027-W01 |
| RND-05 | Non-ASCII characters in the title, subject, or body (`—`, `★`, `₹`, `·`) | Keep everything as UTF-8 from end to end. The email subject needs RFC 2047 encoding; check that the MCP server does this. | **Gap** |
| RND-06 | The rendered Doc does not fit on one page because of long quotes | The 250-word limit usually keeps it on one page. Long quotes (60 words) are the main risk; consider `quote_max_words: 40`. | §4.6 |
| RND-07 | Readers can't tell which reviews the counts include | The source line states the cleaning rules: `Source: 1,348 reviews (Play Store 1,202 · App Store 146), 9+ words, English only, 7 Sep – 15 Nov 2026`, built from config | `render/markdown.source_line` |

---

## 8. MCP Publishing (§4.7, P5)

### 8.1 Startup and the tool map

| ID | Scenario | Expected behavior | Covered by |
|---|---|---|---|
| MCP-01 | A mapped tool is missing from `list_tools()` | Stop before any analysis output is published, and name the missing logical action. | §4.7 |
| MCP-02 | A mapped tool exists, but its input schema differs from the arguments in `tool_map.yaml` | Check the arguments against the tool's `inputSchema` at startup, not only the tool name. | **Gap** |
| MCP-03 | A mapped tool name contains `send` in any case or form (`sendDraft`, `drafts_send`, `create_and_send`) | Refuse at startup. Match case-insensitively. | §4.7 |
| MCP-04 | The draft tool has an argument that triggers sending (`send: true`, `send_now`) | Refuse any mapping that sets such an argument, and never pass one. | **Gap** |
| MCP-05 | The stdio server writes logs to stdout, which corrupts the protocol | Show the server's stderr in the error message. Document that the server must log to stderr. | **Gap** |
| MCP-06 | The server starts slowly, or never finishes starting | Startup timeout, then a clear error naming the command that was run. | P5 |
| MCP-07 | The server's OAuth token expired, and it tries an interactive browser consent during an unattended run | The run must not hang. A call timeout fails the publish, analysis artifacts are kept, and the error says to re-authenticate. | P5 risks |
| MCP-08 | A tool returns `isError: true` instead of raising an exception | Treat both the same way: as a failed step recorded in `publish.json`. | **Gap** |
| MCP-09 | The server's command path contains spaces or `+` (the repo folder is `AI Agent+MCP`) | Pass the command and arguments as a list, never as one shell string. | **Gap** |

### 8.2 Google Docs

| ID | Scenario | Expected behavior | Covered by |
|---|---|---|---|
| DOC-01 | `find_doc` returns more than one document with the same title (someone made a manual copy) | Prefer the `doc_id` recorded in `publish.json`. Otherwise use the oldest one in the configured folder and log a warning. Never update every match. | **Gap** |
| DOC-02 | The title search breaks on special characters (`—`, apostrophes) or on query escaping | Escape the search query. Match the exact title in code after searching. Use one dash character consistently in titles. | **Gap** |
| DOC-03 | The recorded `doc_id` was deleted or is in the trash | Treat it as not found and create a new doc. Update `publish.json`. Never write to a trashed doc. | **Gap** |
| DOC-04 | A doc with the right title exists outside the configured folder | Ignore it; search only inside the folder. | §4.7 |
| DOC-05 | `docs_folder_id` is wrong, or the account cannot access it | `pulse doctor` checks the folder. The publish fails clearly. | **Gap** (doctor check) |
| DOC-06 | `create_doc` succeeds but the response is lost (timeout), and the retry would create a duplicate | Before any create retry, run `find_doc` again. | **Gap** |
| DOC-07 | `replace_doc_body` fails partway and leaves an empty or half-written doc | Record the failure. The next `pulse publish` replaces the body again, which is safe because replacing is idempotent. | §10 |
| DOC-08 | A stakeholder edited or commented on the doc, and the re-run replaces the body | Expected: the generated doc is overwritten. Comments attached to replaced text may be lost. Document this in the README. | **Gap** (document it) |
| DOC-09 | `create_doc` cannot place the doc in a folder, and the move step fails | The doc exists in My Drive. Record a partial success, and have the next run retry only the move. | **Gap** |

### 8.3 Gmail drafts

| ID | Scenario | Expected behavior | Covered by |
|---|---|---|---|
| GML-01 | `PULSE_RECIPIENT` is not set, is not a valid address, or lists several addresses | The config loader rejects it before the run. Exactly one valid address. | P0 `config.PublishConfig` |
| GML-02 | The draft recorded in `publish.json` was deleted by the user | Updating it fails, so create a new draft and record its ID. | **Gap** |
| GML-03 | The user already **sent** the recorded draft, then the week is re-run | Do not create a new draft for a week that was already sent. Log "already sent". | **Gap** |
| GML-04 | The server cannot update drafts | Skip and log, or delete and recreate if supported. | P5 risks |
| GML-05 | Docs succeeded and Gmail failed | Resume from the Gmail step on the next `pulse publish`. | §10 |
| GML-06 | Gmail succeeds but Docs had failed (the order should prevent this) | The draft needs the Doc URL, so Gmail must never run without a `doc_url`. | §4.7 (implied) |

### 8.4 The publishing agent (`publish.mode: agent`)

| ID | Scenario | Expected behavior | Covered by |
|---|---|---|---|
| AGT-01 | The agent says it is done after the Doc entry is **confirmed**, but never tries the email (or it runs out of steps there) | The code sends the email itself, through the same guarded step, with no intro. The outcome says the code sent it; `agent_log.json` has `"finished_by_code": true`, and the step is marked `"by": "code"`. Nothing is sent if the email was already tried, if the entry is not confirmed in the Doc, or if the week was already published. | `publish/agent.py` (`_send_if_the_agent_stopped_early`); `tests/test_agent.py`; seen live in 2026-W47, see below |
| AGT-02 | The agent stops after appending but **before** confirming the entry | No email (the entry is not confirmed). The run fails as `partial` at the `send` step. | `tests/test_agent.py::test_stopping_before_publishing_is_a_failure` |
| AGT-03 | The model errors (no key, rate limit, unreachable) before the email was tried | The scripted publisher takes over; it re-checks the Doc, so it never appends twice. | `tests/test_agent.py` (fallback tests) |

#### Incident: 2026-W47, the agent stopped before sending the email (2026-11-28)

**What happened.** The first real `pulse-check.yml` run on GitHub Actions built the W47 pulse (hybrid engine, 1,286 reviews, 248/250 words). Then it handed publishing to the Groq agent (`openai/gpt-oss-120b`). The agent made three correct calls: `check_already_published` (not in the Doc), `append_week_entry`, and `verify_entry_in_doc` (confirmed). Then it replied "Pulse published successfully." and stopped, without calling `send_pulse_email`.

**How it was caught.** By code, not by the model. After the agent stops, `_finish` checks in code whether the email was sent. It wasn't, so the run was marked failed ("the agent stopped before the week was published", exit 11). `publish.json` was saved as `partial`, and the job turned red. The guardrails held: nothing was sent that shouldn't have been, and nothing was published twice. The workflow still exported and committed `site/pulses/2026-W47.json`, because the entry *was* in the Doc, so the Doc and the site match.

**Why it slipped through before.** The scripted fallback only took over when the model **raised an error**. A model that *thinks it is done too early* is a different failure, and no test covered it. The prompt also never said clearly that the email is part of the work.

**Fix.**
1. **Code:** when the agent stops with the entry confirmed in the Doc and the email never tried, the code sends it through the same guarded send step (AGT-01). It still sends at most once and never retries, and it never sends for a week that was already published.
2. **Prompt** `publish_agent_v2`: publishing has two parts, the Doc entry *and* the email. The work is done only once `send_pulse_email` answers "sent"; never say the pulse is published before that.
3. **Tests:** stops after verify; runs out of steps after verify; an `--email-only` run that stops after the check; no send for a week already published; no second send after the agent's own send failed; a failed code send ends as `partial` and is not retried.

**W47 itself.** The W47 email was skipped on purpose; the Doc entry and the site have the week. Re-sending it from a new runner would need the pulse rebuilt, and the LLM would write different wording from the Doc (see README, `--email-only`). The next scheduled run (W48) is the first live test of the fix.

**Lesson.** An LLM can say "done" when it isn't. The program, not the model, must decide whether the work is complete, and must have a safe way to finish it.

---

## 9. Orchestration, CLI, and Scheduling (§4.8, §4.9, P6, P7)

| ID | Scenario | Expected behavior | Covered by |
|---|---|---|---|
| ORC-01 | A scheduled run and a manual run for the same week overlap | Take a lock file (`runs/{week}/.lock`) when the run starts. The second run exits with "run in progress". Remove stale locks after a timeout. | **Gap** |
| ORC-02 | The machine is asleep or off at Monday 09:00 | Turn on the Task Scheduler setting "run as soon as possible after a missed start". The week is still computed correctly. | **Gap** |
| ORC-03 | The run starts at 00:30 IST on Monday, when it is still Sunday in UTC, or the machine is not set to IST | Compute the run week from `now()` in `Asia/Kolkata`, never from the system's local time zone. | §4.9 |
| ORC-04 | `--week 2026-W53`, `--week 2027-W01`, or an invalid `--week 2026-W54` | Validate ISO weeks: accept W53 only for years that have one, and reject the rest. | P0 `weeks.parse_iso_week` |
| ORC-05 | `pulse publish --week X` when `pulse.json` for that week is missing or corrupted | Clear error: "No validated pulse for X; run `pulse analyze --week X`". Never publish without `validation.json` passing. | **Gap** (the gate on validation status) |
| ORC-06 | The process crashes while writing an artifact | Write every artifact to a temp file and then rename it, so a stage never reads a half-written file. | **Gap** |
| ORC-07 | `--skip-ingest` with an old `reviews.parquet` (weeks since the last ingest) | Warn if the newest review is older than the window end minus 7 days. | **Gap** |
| ORC-08 | `--dry-run` | Makes no MCP connection and does not write `publish.json`. A test uses a fake MCP server that fails if it is ever called. | §4.8 |
| ORC-09 | The same week is re-run with a different config (for example a new `weeks_back`) | Overwrites the artifacts. `manifest.json` records the new config. Earlier attempts can be kept as `manifest.{n}.json`. | §6 |
| ORC-10 | ~~`pulse prune` deletes raw files that a week still needs for replay~~ | Not applicable: there is no prune command (nothing is kept between GitHub Actions runs; Phase 6). | §6 |
| ORC-11 | The Windows console uses cp1252, so printing `★`, `₹`, or `—` raises `UnicodeEncodeError` | Reconfigure stdout and stderr to UTF-8 at CLI startup (or set `PYTHONIOENCODING=utf-8` in the scheduled task). | P0 `cli._utf8_console` |
| ORC-12 | The scheduled task's working directory or path has spaces and `+` | Quote the paths in the Task Scheduler action. Set "Start in" to the repo root. | **Gap** |
| ORC-13 | Exit codes | 0 for success; a different non-zero code for config error, ingest shortfall, LLM failure, validation failure, and publish failure; 1 for an unexpected error. The scheduled `pulse doctor && pulse run` stops on the first failure. | P6 `cli.ExitCode` (table in README) |

---

## 10. Claude API (§4.5 model configuration, §10): `--engine llm` only

| ID | Scenario | Expected behavior | Covered by |
|---|---|---|---|
| LLM-01 | 429, 529 (overloaded), or 5xx | SDK retries, then stage-level backoff. Catch the most specific error first. | §10 |
| LLM-02 | The stream disconnects in the middle of a response | Retry the whole request. Never use the partial content. | **Gap** |
| LLM-03 | `ANTHROPIC_API_KEY` is missing or invalid (401) | `pulse doctor` catches it. The run fails before ingest finishes calling the API. | P5 doctor |
| LLM-04 | The input is unexpectedly larger than the context window | Use the stratified-sample path; count tokens before sending. | §4.4 |
| LLM-05 | The configured model is retired or renamed | A clear error that names the `llm.model` config key. | **Gap** |
| LLM-06 | Costs grow through retries (missing-ID re-requests, validator retries, refusal fallbacks) | Every retry loop is bounded. Optionally set a per-run token budget in config and stop when it is exceeded. | **Gap** (budget) |
| LLM-07 | Non-determinism: the same input gives different themes on two runs | Expected. P6's "stable artifacts" applies to idempotent publishing, not to identical LLM output. Write this down explicitly. | **Gap** (clarify P6) |

---

## 11. Open Gaps and Recommended Resolutions

Ordered by impact. The first four will block a milestone if they stay open.

| # | Gap | IDs | Recommendation | Blocks |
|---|---|---|---|---|
| 1 | The final PII scan flags the Doc URL in the email body | VAL-04 | Allowlist the exact `doc_url` in the output scan | M2 |
| 2 | LLM-produced character offsets are unreliable | CMP-10, CMP-12, CMP-13 | The model returns the quote text; code checks it is an exact substring and computes the span | M1 |
| 3 | Fewer than 3 themes, or a theme with no quotable candidates | CLU-02, CMP-01 | Stop before publishing with a specific failure reason; relax candidate filters in steps first | M1 |
| 4 | The placeholder check matches any `<…>` | VAL-05 | Match only the known template tokens | M1 |
| 5 | ✅ *Resolved in P1.* Text-hash dedupe merges different users' identical short reviews | NRM-05 | Dedupe by `key`; use the text hash only when a source ID is missing | Accuracy |
| 6 | ✅ *Resolved in P1.* Raw files keep reviewer identity, which contradicts §11 | PII-24 | Strip identity fields before writing raw files, or document the exception | Privacy |
| 7 | Personal names in review text | PII-11, PII-12, CMP-05 | **Recorded as a known gap.** The Phase 4 name filter (default) or quote-pick prompt (LLM engine) skips reviews that probably name a person; humans review the first pulses | Privacy |
| 8 | No lock for runs of the same week at the same time | ORC-01 | Lock file per week | M3 |
| 9 | Duplicate Docs and lost create responses | DOC-01, DOC-06 | Prefer the recorded `doc_id`; run find again before a create retry | M2 |
| 10 | Statistics written by the LLM or a template | CMP-22 | Insert every number in code during rendering | Accuracy |
| 11 | One action per theme is not enforced | CMP-20 | Add a validator check | Quality |
| 12 | Encoding: UTF-16 exports, Windows console, email subject | ING-04, ORC-11, RND-05 | UTF-8 everywhere; sniff the encoding of imports | M1 / M3 |
| 13 | ISO week 53 and year-boundary handling | ORC-04, RND-03, RND-04 | Add fixtures for `2026-W53` and `2027-W01` (W53 occurs about 5 weeks after today) | M3 |
| 14 | Undefined `recency_weight` and `volume_share` denominator | RNK-03, RNK-04 | Write the exact formulas into §4.4 | P3 |

Once a gap is resolved, update the matching section of `architecture.md` and change **Gap** in this file to that section reference.

---

## 12. Fixture Checklist

These are the synthetic fixtures (`tests/fixtures/`, no real user data) needed to cover the cases above:

- [ ] `play_utf16.csv`: UTF-16 LE with BOM, a multi-line quoted field, one malformed row, a `"4.0"` rating, a rating of 6
- [ ] `play_overlap_a.csv` / `play_overlap_b.csv`: overlapping exports, one edited review (same ID, new text)
- [ ] `appstore_page_single_entry.json`: `entry` as an object; `appstore_page_metadata_first.json`; `appstore_empty.json`; `appstore_error.html`
- [ ] `window_edges.jsonl`: reviews on the first day, last day, day −1, day +1, a future date, and 23:30 UTC on a Sunday
- [ ] `identical_short.jsonl`: two different users, same text "Good app", same date
- [ ] `pii_positive.txt` / `pii_negative.txt`: every row in §3.1 and §3.2, including Hinglish, Devanagari, and obfuscated email
- [ ] `quotes_unicode.jsonl`: emoji, Devanagari matras, CRLF, inner quote marks, `<3`, markdown characters
- [ ] `pulse_bad_*.json`: invented quote, 251 words, 4 themes, email address in body, missing action, two actions on one theme, duplicate quote review
- [ ] `weeks.json`: `2026-W53`, `2027-W01`, `2026-W54` (invalid), a Monday 00:30 IST timestamp
- [ ] `fake_mcp_server`: mapped tools, a `sendDraft` variant, an `isError` response, a slow start, duplicate titles, a trashed doc, a sent draft
