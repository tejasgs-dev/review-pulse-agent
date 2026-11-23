# Implementation Plan: Groww Weekly Review Pulse

> Built from [problemStatement.md](problemStatement.md) (**what**) and [architecture.md](architecture.md) (**how**). Section references such as §4.3 point to `architecture.md`.

---

## Overview

The work is split into **8 phases**. Each phase leaves the project runnable and ends with explicit exit criteria. The order front-loads the parts that need no external access (ingestion, privacy, analysis), so a complete **offline pulse** (`pulse run --dry-run`) works before any Google integration is attempted. Analysis uses **local machine learning and rules by default, with no paid API** (revised 2026-11-22; the Claude API is an optional engine, `--engine llm`).

```
Phase 0 ──▶ Phase 1 ──▶ Phase 2 ──▶ Phase 3 ──▶ Phase 4 ──▶ Phase 5 ──▶ Phase 6 ──▶ Phase 7
Setup &     Ingest &    PII         Theme       Compose,    MCP         Hardening   Schedule &
decisions   normalize   scrubber    clustering  validate,   publishing  & CLI       first live
                                    & ranking   render                              runs
                                                   │
                                                   ▼
                                     ★ M1: offline pulse (dry run)
                                                               ★ M2: Doc + email via MCP
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
- [x] Add `.env.example` listing `ANTHROPIC_API_KEY`, `PULSE_RECIPIENT`, `PULSE_DOC_ID` and `PULSE_MCP_API_KEY` with no values.
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

## Phase 3: Theme Clustering and Ranking (local ML, no paid API)

**Goal:** at most 5 themes that fit the data, with every review assigned and the top 3 ranked in code, using **local machine learning only**. No review text leaves the machine and no paid API is called. *(§4.4)*

> **Revised 2026-11-22.** The first version of this phase used the Claude API (two LLM passes). That code is kept as an optional engine (`--engine llm`, off by default; see "Engines" below). The default engine is local and free. All numbers in this section were **measured on `runs/2026-W46/clean_reviews.jsonl`** on the development machine (Windows, CPU only) unless marked as an estimate.

### Data profile (2026-W46)

| Property | Value | Consequence for clustering |
|---|---|---|
| Volume | 1,348 reviews (Play Store 1,202 · App Store 146), 7 Sep – 15 Nov | Small: any method runs in seconds on a CPU |
| Length | median 22 words, p90 68, max 239 | Short texts: bag-of-words vectors are sparse, so embeddings help |
| Ratings | 1★ 43% · 2★ 6% · 3★ 7% · 4★ 9% · 5★ 35%; 49% are 1–2★ | Negative share separates themes well (used by `rank.py`) |
| Recency | 245 reviews in the last 14 days | Enough for the recency weight on large themes |
| Generic praise | 233 reviews (17%) match the praise rule below; one more micro-cluster of vague praise brings it to about 24% | Must be taken out **before** clustering, or it forms its own "theme" |
| Vague complaints | about 70 reviews of "worst app, useless, waste of money" with no product area | Becomes an `other` micro-cluster, not a theme |
| Hinglish leftovers | about 50 reviews that passed the English filter but are mostly romanized Hindi | Forms its own micro-cluster; routed to `other` |
| Multi-topic reviews | common ("app hung → order delayed → loss → support didn't reply") | One primary theme per review; the second-nearest theme is recorded as secondary |

### Method comparison

The three candidate approaches (TF-IDF + k-means, sentence embeddings + k-means, BERTopic), with sentence embeddings tried two ways: MiniLM on torch, and model2vec, a lighter static model. Quality for A and C is from clustering the non-praise reviews directly; B1 and B2 also ran through the full pipeline below (L1–L8), which is where B2's five themes come from.

| | **A. TF-IDF + k-means** | **B1. Sentence embeddings (MiniLM, torch) + k-means** | **B2. Static sentence embeddings (model2vec) + k-means** | **C. BERTopic (MiniLM + UMAP + HDBSCAN)** |
|---|---|---|---|---|
| Theme quality on W46 | Poor. k=5 leaves one catch-all cluster of 57%; the clean clusters it finds are support, charges, charts, mutual funds | Fair. Very stable, but groups reviews by **tone** more than by product area: three of five clusters read as "customer support" | **Best of the four.** Five distinct product themes: customer support (17% of reviews, 94% 1–2★), stocks & portfolio, bugs & updates, feature requests (charts, options), charges & brokerage (10%); `other` 9% | Unusable here: HDBSCAN finds one topic holding 97% of reviews plus a 3% Hinglish cluster, with `min_cluster_size` 20 or 10. More topics means swapping HDBSCAN for k-means, which is option B with extra weight |
| Silhouette (cosine), k=5, all non-praise reviews | 0.019 | 0.090 | 0.058 | n/a (density-based) |
| Stability: ARI vs 3 other seeds, k=5 | 0.35 | 0.93 | 0.54 with `n_init=10`; 0.75 with `n_init=50`; median 0.79 in the full pipeline | 1.00 between two runs, trivially (one topic) |
| Extra install size (measured, Windows) | scikit-learn + scipy ≈ 160 MB | torch CPU 552 MB + transformers 124 MB + model 88 MB ≈ **1.1 GB** total env | scikit-learn + model2vec ≈ 200 MB + model **30 MB** | MiniLM stack + umap, hdbscan, numba, plotly ≈ **1.4 GB** total env |
| Run time, this machine (1,115 reviews to cluster) | 0.4 s per k | model load 24 s (first run, includes download) + encode 10.7 s | model load 0.8 s + encode 0.1–0.7 s; whole pipeline 2.5 s | ≈ 32 s: model load 7 s + encode 11 s + fit 14 s (the first fit includes numba compilation; 2.5 s after that) |
| GitHub Actions, `ubuntu-latest` *(estimate)* | ≈ 20 s install, < 5 s run | ≈ 1.5–2 min install (CPU torch wheel), ≈ 20–40 s run | ≈ 20 s install, model from cache, < 10 s run | ≈ 2–2.5 min install, ≈ 40–60 s run (numba compiles again on every fresh runner) |
| Determinism with a fixed seed | Yes (`random_state`), but results change a lot between seeds | Yes: re-encoding is bit-identical; k-means seeded | **Yes: identical assignment hashes across two separate processes**; re-encoding bit-identical | Yes with `UMAP(random_state=42)` (UMAP then runs single-threaded): identical across two runs |
| Labels | Centroid top terms | c-TF-IDF top terms | c-TF-IDF top terms | c-TF-IDF top terms (built in) |

**Recommendation: B2, static sentence embeddings (model2vec `minishlab/potion-base-8M`) + k-means, inside the routing pipeline below.**
- It produced the clearest product themes on the real data. MiniLM is more stable, but it splits by tone, and a pulse needs product areas.
- It needs no torch: about 200 MB of dependencies plus a 30 MB model, a CI install in seconds, and a full run in a few seconds.
- It is deterministic: a fixed seed gives byte-identical assignments across runs.
- It runs on any CPU, offline after the first model download, and no review text leaves the machine.

TF-IDF + k-means is too weak on short reviews. BERTopic found no structure in these short reviews and is the heaviest install. Forcing it onto k-means would make it option B with 1.2 GB of extra dependencies.

### Pipeline (engine `local`)

**L1. General praise first (rule, before clustering).** A review is `general_praise` when it is rated 4–5★, contains a praise word (`good`, `great`, `best`, `easy`, `smooth`, `love`, …), and contains none of the "specific" words (`but`, `please`, `issue`, `fix`, `add`, `not`, `slow`, `charges`, `support`, `withdraw`, …). Both word lists live in config. On W46 this takes 233 reviews (17%).

**L2. Embed.** Sort reviews by ID, embed `canonical_text` with model2vec, and L2-normalize. The model is pinned by repo and revision (`analysis.local.model`, `analysis.local.model_revision`) and cached in `data/models/` (git-ignored). The first run downloads it from Hugging Face (free, no account, about 30 MB); later runs work offline.

**L3. Micro-clusters.** k-means with `k0 = 12` (`analysis.local.micro_clusters`), `n_init = 50`, `random_state = 42`. On W46 the 12 micro-clusters are coherent: app bugs and updates, money stuck at the bank, order execution and stop-loss losses, customer support (96% 1–2★), stocks and portfolio, charts and indicators, charges and brokerage, IPO and misc, plus leftover praise, vague complaints and Hinglish.

**L4. Route micro-clusters to the reserved buckets.** Keywords per micro-cluster come from **c-TF-IDF** (class-based TF-IDF, as BERTopic uses, built with scikit-learn's `CountVectorizer`).
- `general_praise`: under 25% of the micro-cluster is rated 1–2★ and more than half of it is 4–5★ with a praise word.
- `other`: at least 3 of its top 5 keywords are on the "vague" list (`worst`, `bad`, `useless`, `experience`, `waste`, …, plus common Hinglish words), or the cluster is loose (median cosine to its centroid under 0.55).
- Everything else is a theme micro-cluster. On W46: 9 theme, 2 `other`, 1 `general_praise`.

**L5. Choose the number of themes (at most 5) and cluster.** Run k-means again on the theme reviews only, for k = 5, 4, 3 (`n_init = 50`, `random_state = 42`). Keep the **largest k** for which both hold:
1. every cluster holds at least `min_theme_share` (3%) of all reviews;
2. the clustering is stable: the median ARI between seed 42 and seeds 1, 2, 3 is at least 0.75.

Silhouette is not used to choose k: on this data it is low and nearly flat for every k (0.02–0.09), so it cannot tell k values apart. On W46 the rule picks **k = 5** (median ARI 0.79, smallest theme 9.6%). k = 3 is the floor; if even k = 3 fails, the stage stops with a clear error, as today (`RankError`).

**L6. Per-review `other` and secondary theme.** A theme review whose cosine similarity to its own centroid is under 0.40 moves to `other`. The second-nearest theme centroid becomes `secondary_theme_id` when it is within 0.05 of the nearest. On W46 `other` ends at 9%. If `other` is above `max_other_share` (25%), the stage retries once with `k0 = 16`, then records `other_share_high` as today.

**L7. Label each theme from its keywords.**
1. Take the theme's top c-TF-IDF keywords (unigrams and bigrams, English and domain stop words removed).
2. Match them against the **theme catalogue** `config/themes.yaml`: each entry has an `id`, a `label` of at most 4 words, a keyword list, and the Phase 4 templates. The entry that shares the most of the top 6 keywords wins, with at least 2 shared.
3. With no match, the label is the top two keywords, title-cased ("Chart & Indicator"), and the ID is their snake_case (`kw_chart_indicator`).
4. Two themes that map to the same catalogue entry are merged.

The catalogue starts from the 8 seed themes plus what W46 showed (order execution & losses, stocks & portfolio, feature requests, IPO & mutual funds). Unmatched labels in a run are a signal to extend it. `description` = "Reviews mentioning {top keywords}", and `include` / `exclude` hold the matched keywords.

**L8. Quality checks** (recorded in `themes.json` and the manifest, as today):
- the stability ARI from L5 replaces the LLM consistency probe (warning below 0.75);
- `small_theme` and `other_share_high` warnings are unchanged;
- the rating/sentiment mismatch count is dropped, since the local engine has no sentiment (`sentiment` is `null` in assignments).

**Kept unchanged:** `rank.py` (the ranking formula in architecture §4.4, tie-breaks, `RankError`), the `general_praise` / `other` reserved buckets and their meaning, the `themes.json` schema (with `sentiment: null`), the PII rules (only `canonical_text` is used), and `validate.py`.

### Engines

| `analysis.engine` | Clustering | Composing | Needs |
|---|---|---|---|
| `local` *(default)* | Pipeline L1–L8 above | Rules and templates (Phase 4) | scikit-learn, model2vec; no API key |
| `llm` *(optional)* | The current two-pass Claude design (Pass A discovery, Pass B assignment, consistency probe) | The current LLM quote pick and writing | `pip install -e .[llm]` and `ANTHROPIC_API_KEY`; costs money |

`pulse analyze` and `pulse run` take `--engine local|llm`, overriding config. Both engines return the same `ClusterResult` and `Pulse`, so `rank.py`, the validator, the renderer and the publisher do not know which one ran. The engine and model are recorded in `manifest.json` and `themes.json`.

### Tasks
- [x] *(LLM engine, built first; kept as `--engine llm`)* `analysis/llm.py`, `analysis/prompts/`, two-pass clustering, consistency probe, offline tests with a fake model. **Superseded as the default** by the local engine below.
- [x] Dependencies: add `scikit-learn` and `model2vec` (pinned) to the core dependencies; move `anthropic` to an optional extra `[llm]`. `pulse doctor` reports the active engine and checks only what it needs.
- [x] Config: `analysis.engine` (default `local`); `analysis.local.{model, model_revision, micro_clusters: 12, n_init: 50, seed: 42, min_stability: 0.75, other_cosine: 0.40, secondary_margin: 0.05, praise_words, specific_words, vague_words}`; `config/themes.yaml` (theme catalogue).
- [x] Restructure: move the LLM clustering into `analysis/engines/llm_cluster.py` (no behavior change). Put the shared `ClusterResult`, `themes.json` writer and manifest section in `analysis/cluster.py`, and select the engine in one place.
- [x] `analysis/engines/local_cluster.py`: L1–L8. Every step deterministic: sorted input, fixed seeds, `threadpoolctl` limiting BLAS/OpenMP to 1 thread during clustering, ties broken by ID.
- [x] Model cache in `data/models/`, offline-safe. A missing model with no network fails clearly and names the config key.
- [x] CLI: `--engine local|llm` on `analyze` and `run`.
- [x] CI: `OMP_NUM_THREADS=1`, installs `.[dev,llm]`. No model cache is needed: offline tests use a hashing stub embedder (an autouse fixture), so CI never downloads the model; `tests/test_local_live.py` (`-m slow`, free) uses the real one.
- [x] Tests (offline, no network: a stub embedding function; `tests/test_local_cluster.py`, `tests/test_catalogue.py`):
  - [x] the praise rule: 4–5★ with a praise word and no specific word; "good app but please add X" is not praise;
  - [x] micro-cluster routing: a praise cluster, a vague cluster, a loose cluster;
  - [x] choosing k: picks the largest stable k; falls back to 4 or 3; stops below 3;
  - [x] the per-review `other` threshold and secondary margin;
  - [x] labelling: catalogue match, fallback label from keywords, labels of 4 words or fewer, merge of two clusters with the same catalogue entry;
  - [x] determinism: two runs on the same input give an identical `themes.json` apart from timestamps;
  - [x] the `--engine llm` path still passes its existing offline tests.
- [x] Keep `rank.py` and its tests unchanged.

### Deliverables
- `themes.json` for 2026-W46 from the local engine, with labels, shares, scores and assignments, and no API call.

*Status (2026-11-22): done. On W46: 5 themes (Customer support 16%, Stocks & portfolio 14%, Bugs & app updates 13%, Feature requests 12%, Charges & brokerage 9%), general praise 24%, `other` 12%, stability median ARI 0.83, all 1,348 reviews assigned, about 14 s including the model load, and no API call. Two runs give byte-identical `themes.json`. Still open: the timing on GitHub Actions, and the human spot check of 20 assignments.*

*Update (2026-11-22): the spot-check sheet is ready at `runs/2026-W46/theme_review_sample.md` (git-ignored): 20 reviews drawn at random with seed 46, 8 of them multi-topic (a secondary theme, or at least 2 keywords from each of two or more catalogue topics), each with its text, assigned and secondary theme and a blank "Correct? Y/N" column. Waiting for the project owner to fill it in; the exit criterion is at least 17 of 20 correct.*

### Exit criteria
- 5 or fewer themes on real data; 100% of reviews assigned; `other` at 25% or less.
- Two runs on the same input produce byte-identical assignments (same OS). CI on Linux produces the same theme labels and top 3.
- A human spot check: 20 random assignments, at least 85% judged correct, including at least 5 multi-topic reviews. If it falls short, tune the catalogue and word lists before changing the method.
- `pulse analyze` with the local engine finishes in under 60 seconds on GitHub Actions, with the model cached.
- The engine, model revision, timings and stability ARI are recorded in `manifest.json`.

### Risks
| Risk | Mitigation |
|---|---|
| Keyword labels are less readable than written ones ("Chart & Indicator") | The catalogue gives curated labels for known themes; unmatched labels are logged so the catalogue can grow; humans review the first pulses (Phase 7) |
| k-means splits by wording instead of issue on some weeks | Micro-clusters plus routing take out praise and vague text first; the stability rule lowers k when the split is not robust |
| Generic praise or vague text leaks into a theme | Rule L1 plus routing L4; the word lists are config, tuned after the first live runs |
| Float differences across CPUs change a few assignments | Single-threaded clustering, sorted input and fixed seeds; the exit criterion asks for identical results on one OS and the same labels and top 3 across OSes |
| The model download fails in CI | Cache `data/models/`; the model is pinned by revision; a clear error names the config key |
| Theme IDs change from week to week | Unchanged from before, but catalogue matches now give **stable IDs** for known themes, which helps the Phase 8 trend |

---

## Phase 4: Composer, Validator, Renderer ★ M1

**Goal:** a complete pulse that passes validation, produced fully offline, **without an LLM** in the default engine. *(§4.5, §4.6, §7)*

> **Revised 2026-11-22.** Quotes are now picked by rules, and summaries and actions come from per-theme templates. The LLM composer built first is kept for `--engine llm`. The validator, renderer, golden file and retry/failure handling are unchanged.

### Rule-based composer (engine `local`)

**Candidates (code, unchanged).** `select_candidates`: up to 8 reviews per top theme, 8–60 words, no profanity, no redaction tokens, recent first, ratings mixed, limits relaxed in steps (CMP-01/02/04, PII-22).

**Name filter (new, replaces the prompt rule for PII-11).** Without a model reading the candidates, code skips any candidate that probably names a person. It errs towards skipping, since there are up to 8 candidates per theme. A review is skipped when it matches any of:
- a title before a capitalized word (`Mr`, `Mrs`, `Ms`, `Sir`, `Madam`, `Dr`);
- `my name is`, or `I am` / `I'm` followed by a capitalized word;
- a capitalized word right before `from support` / `from customer care` / `from the team`;
- `agent`, `executive`, `advisor`, `representative`, `named` or `called` followed by a capitalized word;
- a capitalized word in mid-sentence that is not on an allowlist of product and finance terms (`Groww`, `UPI`, `IPO`, `SIP`, `F&O`, `NSE`, `BSE`, `Nifty`, `Sensex`, `iPhone`, `iPad`, `Android`, `Google`, month and day names, …).

This is a heuristic. The gap stays recorded, and humans still read the first pulses (Phase 7).

**Quote fit (rules; added 2026-11-22).** Sharing a keyword with the theme is not enough: the first W46 pulse quoted a review about spam calls and sold contact details under Stocks & portfolio because it said "selling" and "trading". A theme member may be quoted only when:
1. **its own theme centre is clearly the nearest:** it has no secondary theme (no other theme centre within `secondary_margin` of its own);
2. **it is not about a subject no theme covers:** it mentions none of `analysis.local.quote_off_topic_words` (spam, privacy, contact details, personal data, hacked, …);
3. **it is not mainly about another topic:** it matches at least one keyword of its own catalogue entry, and no other catalogue entry matches more of its words.

The up-to-8 candidates are then drawn from the members that fit (`select_candidates` on the fitting members), so the gate cannot empty the pool early. On W46 273 of the 575 top-theme reviews fit; `validation.json` records the counts per theme (`quote_fit`).

**Quote pick (rules).** For each top theme, score each remaining candidate and take the best (ties broken by review ID):
1. **Representative:** cosine similarity to the theme centroid (the embedding from L2).
2. **On topic:** the number of the theme's top keywords in the quote window (fit already requires one catalogue keyword).
3. **Typical voice:** when the theme's negative share is 50% or more, prefer a 1–2★ review; otherwise any rating.
4. **Short:** prefer 10–30 words.

The quote is the window of 1–2 consecutive whole sentences (from `sentence_spans`) with the most theme keywords, within 8–35 words; failing that, the whole review if it is within 60 words. Code slices `canonical_text`, so quotes stay verbatim and on sentence boundaries. The three quotes come from three different reviews. The renderer prints each quote after the label of the theme it illustrates (`• Customer support: “…” (★1, Play Store)`), for both engines.

**Summaries, actions and signals (templates).** Each catalogue entry in `config/themes.yaml` has short templates (plus short forms for the word-count fallback):
- `summary`: "Reviewers hit errors, crashes or broken screens, often around {kw1} and {kw2}"
- `action`: "Triage the {recent_count} bug reports from the last two weeks and ship a fix for the most common one in the next release"
- `signal`: "fewer than {recent_count} bug reports in the next two weeks"

*(Revised 2026-11-22, project owner: actions and signals carry **that week's figures and top keywords**, so they don't read identically every week.)* Placeholders are filled by code: `{kw1}`–`{kw3}` with the theme's top keywords, and `{recent_count}` and `{trend}` with the computed stats. *(Revised 2026-11-22.)* **Actions and signals use one period only, the last two weeks:** `{recent_count}` (theme reviews in the last 14 days) and the `{trend}` word. The first W46 pulse mixed periods ("the 184 portfolio reviews … fewer than 42 … in the next two weeks": 184 was the whole 10-week window), so whole-window figures (`{count}`, `{share}`, `{negative_share}`) are now rejected in actions and signals when the catalogue loads, and every action, short action and signal names "the last two weeks" or "the next two weeks". Summaries may use keywords only, since the renderer already prints the shares next to them (CMP-22); templates never contain typed digits, so every figure comes from the stats. A theme with no catalogue match uses generic templates:
- summary: "Reviewers often mention {kw1}, {kw2} and {kw3}"
- action: "Review the {recent_count} reviews mentioning {kw1} from the last two weeks ({trend}) with the owning team and fix the most common problem"
- signal: "fewer than {recent_count} reviews mentioning {kw1} in the next two weeks"

Figures in actions and signals come from the same stats the renderer uses, so they always agree with the theme line; the validator's `no_stats_in_text` check still applies to summaries.

**Validation and fallbacks.** The same `validate.py`. Because the rules are deterministic, a retry means taking the next option, not asking again:
- a quote problem → the next-best candidate for that theme;
- too many words → shorten in steps, so the actions keep their full wording as long as possible: first shorter quotes (at most 25 words) and the short summaries (`summary_short`), then shorter quotes still (18 words) and the short actions (`action_short`, which also carry the two-week figure). *(Revised 2026-11-22: the first version switched every template to its short form at once, so on W46 a 270-word draft dropped to 204 words and lost the figures in its actions.)*

At most 2 fallback rounds; then `failure.json` and exit code 10, as today.

### Tasks
- [x] *(LLM composer, built first; kept as `--engine llm`)* quote pick by sentence range, summaries and actions, retry of the failing part only.
- [x] `analysis/validate.py`: every check in §4.6 (unchanged; applies to both engines).
- [x] `render/markdown.py`, `pulse.json`, `pulse.md`, `validation.json`, the source line with the general-praise share, the golden file (unchanged).
- [x] `pulse run --dry-run`, `--skip-ingest`, exit code 10, `failure.json` (unchanged).
- [x] Move the LLM composer to `analysis/engines/llm_compose.py`; keep candidate selection and `sentence_spans` shared.
- [x] `analysis/engines/rules_compose.py`: name filter, quote scoring and windowing, template filling, fallback rounds.
- [x] `config/themes.yaml`: a `summary`, `action`, `signal` and short forms for each catalogue entry, plus the generic templates; a schema check at load (`analysis/catalogue.py`): summaries may use only `{kw1}`–`{kw3}`; actions and signals may also use the week's figures (`{count}`, `{recent_count}`, `{share}`, `{negative_share}`, `{trend}`); no typed digits anywhere.
- [x] Tests:
  - [x] name filter: "Rahul from support was rude", "I am Priya Sharma", "Mr Verma", "agent Neha" are skipped; "UPI", "IPO", "Nifty", "iPad", "Monday" are not;
  - [x] quote scoring: deterministic choice, the keyword window, 1–2★ preference, three distinct reviews;
  - [x] templates: placeholders filled, the week's figures in actions and signals, no numbers in summaries, generic fallback for an unmatched theme;
  - [x] fallbacks: the next candidate on a quote failure; short templates on word-count overflow; `failure.json` after 2 rounds;
  - [x] end-to-end `pulse run --dry-run --skip-ingest` with the local engine on fixture reviews, using a stub embedding function in CI so no model download is needed;
  - [x] the existing validator contract tests keep passing; the golden file now has the theme label before each quote.
- [x] Follow-up fixes after the first W46 read (2026-11-22), each with a regression test:
  - [x] quote fit: nearest-centre margin, `quote_off_topic_words`, and "not mainly another catalogue topic" (`QuoteFit` in `rules_compose.py`; `tests/test_rules_compose.py`: a spam/contact-details review sharing theme keywords, a review mainly about charges, a review with a secondary theme, and an end-to-end compose that never quotes an off-topic review);
  - [x] each quote labelled with its theme name, following the quote's theme rather than its position (`tests/test_render.py`);
  - [x] one period in actions and signals: whole-window placeholders rejected, every template checked against the W46 figures 184 / 42 (`tests/test_catalogue.py`, `tests/test_rules_compose.py`);
  - [x] staged word-count fallback: a small overflow shortens summaries and quotes but keeps the full actions (`tests/test_rules_compose.py`).

### Deliverables
- **M1:** `pulse run --dry-run` produces `runs/{week}/pulse.md` from real reviews, with no API key.

*Status (2026-11-22): **M1 reached with the local engine.** `pulse run --dry-run --skip-ingest --week 2026-W46` passes every validator check on the first attempt (246/250 words) with no API key and no network after the model is cached. The name filter flags 13% of W46 reviews as possibly naming a person; they are never quoted. Still open: the human read of the pulse.*

*Update (2026-11-22): follow-up fixes after the first read are done (see the last task above). The W46 dry run passes on the second attempt, at 241/250 words (the first draft is 270 words, so the summaries and quotes are shortened; the actions keep their full wording). The Stocks & portfolio quote is now an on-topic options-trading review instead of the spam-calls one, which fails the fit gate on "contact details". All three actions and signals use the last-two-weeks counts (41, 42, 28). 451 offline tests pass. Still open: the human read of the new pulse, and the theme spot check (Phase 3).*

### Exit criteria
- The dry run on real W46 data passes every validator check, with no network call after the model is cached.
- A human read: the quotes are relevant to their themes and name no one; the actions are concrete enough for the owning team (template wording tuned where they are not).
- Checklist items from `problemStatement.md` §4 met offline: themes, quotes, actions, 250 words or fewer, no PII.

---

## Phase 5: MCP Publishing (Google Doc + email) ★ M2

**Goal:** append the pulse to one Google Doc and **send** it by email to `PULSE_RECIPIENT`, **only through MCP**. *(§4.7)*

**Change of goal (2026-11-22, project owner):** the pulse is sent, not drafted. One Google Doc, created by hand, holds every week (`PULSE_DOC_ID`); the app never calls `create_doc`. `send_email` replaces the old "refuse any send tool" rule, for this step only and only to `PULSE_RECIPIENT`.

**Server (§13 #1, settled 2026-11-22):** [gworkspace-mcp](https://github.com/tejasgs-dev/GmailGdocMCPServer) on Render, one server for both Docs and Gmail, over Streamable HTTP with a bearer key (`PULSE_MCP_API_KEY` in `.env`). Used tools: `doc_contains_text` (read-only, exact and case-sensitive, replies `found: true/false`), `append_to_doc`, `send_email` (needs `ENABLE_SEND_EMAIL=true`).

### Tasks
- [x] `publish/mcp_client.py`: built on the official `mcp` SDK (v2 `Client`).
  - [x] Connect over stdio or Streamable HTTP from the `mcp.docs` / `mcp.gmail` config (header values are secrets, expanded from `.env`); `list_tools()`; `call_tool()`; clean shutdown. One connection when Docs and Gmail are the same server.
  - [x] Poll `health_url` until 200 before connecting (the free Render service sleeps; `wake_timeout_seconds` 120), then connect and call with timeouts (MCP-06/07). Clear errors: a 401 names `PULSE_MCP_API_KEY`; errors wrapped in task-group exception groups are unwrapped.
- [x] `publish/tool_map.yaml`: `docs.append_to_doc`, `docs.doc_contains_text` (required), `gmail.send_email`, with argument templates and result patterns (`ok`, `not_found`), because this server returns text, not structured results.
- [x] Startup check: every mapped tool exists in `list_tools()` with every argument in its input schema and every required field given (MCP-01/02); **A tool whose name contains `send` may only be `gmail.send_email`, whose arguments must be exactly `to`/`subject`/`body`** (MCP-03); any argument that could send is refused (MCP-04); `create_doc` can never be mapped (MCP-10); `isError` or unexpected text is a failure (MCP-08).
- [x] `config`: `publish.doc_id` from `PULSE_DOC_ID` (an ID or a Doc URL, validated); `docs_folder_id` and `doc_title` removed. Publishing stops before any call when it is missing.
- [x] `render/markdown.render_doc_entry`: plain text (Markdown symbols stripped), a separator line, the heading `Week of 9 Nov 2026 (2026-W46)` (always ending with the exact marker `(2026-W46)`), then the pulse without its header line.
- [x] `publish/docs_publisher.py`: `week_in_doc` (the duplicate check, `doc_contains_text`) and `append_entry` (`append_to_doc`; a missing Doc fails clearly).
- [x] `publish/email_sender.py`: one email to `PULSE_RECIPIENT` with the §7.2 subject and the pulse text plus `Full pulse: {doc_url}`; any other `to` is refused before the call.
- [x] `publish/runner.py`: check → append → send. The week's marker found in the Doc → "already published", both steps skipped, exit 0; after a failed send, a local re-run sends without appending again; `--force` re-publishes (append and send).
- [x] `runs/{week}/publish.json`, rewritten after every step (status `sent` / `skipped` / `partial` / `failed`, the duplicate check used, append and email status, message ID, hashes, per-call timings; no payloads, no key). The manifest's `publish` section holds statuses and IDs only.
- [x] `pulse publish --week … [--force]` from saved artifacts (`pulse.md`, `pulse.json`), refusing weeks that failed validation; `pulse run` without `--dry-run` publishes after the analysis. Exit code 11 on a failed step, 2 on missing setup.
- [x] `pulse doctor`: `PULSE_DOC_ID`, health check, credentials, `list_tools`, tool-map coverage, a check that the server offers `doc_contains_text`, and a warning for unmapped `send` tools; `--offline` skips the network.
- [x] Tests (`tests/test_publish.py`, against `tests/fake_mcp.py`, an in-process fake with gworkspace-mcp's exact tools and reply text; an autouse guard stops any offline test from reaching a real server):
  - [x] the Doc entry layout and Markdown stripping; the call order `append_to_doc → send_email`, one recipient, no cc/bcc, `create_doc` never called;
  - [x] not yet published → appended and emailed; already published (marker in the Doc, no `publish.json`) → both skipped; `--force` → published again; only the exact marker counts (`(2026-w46)`, `( 2026-W46 )`, another week do not); doctor fails without the tool;
  - [x] resume after a failed send does not append again; nothing is sent when the append failed; a missing Doc fails clearly and is never created;
  - [x] bad tool maps are refused (send tools elsewhere, cc/bcc, `create_doc`, `send_now`); a slow tool times out; unvalidated weeks and a missing Doc ID, recipient or key make no calls; the key never reaches `publish.json`.
- [x] `doc_contains_text` added to gworkspace-mcp and deployed (2026-11-22), so GitHub Actions cannot publish a week twice.

### Deliverables
- **M2:** one command that turns real reviews into an entry in the shared Google Doc and an email in the recipient's inbox.

*Status (2026-11-22): live. 2026-W46 was appended to the shared Doc and emailed; a second run is skipped as already published.*

### Exit criteria
- A live run appends the week's entry to the shared Doc and sends one email to the configured recipient that contains the note and the Doc link.
- Re-running the same week sends nothing and appends nothing (without `--force`).
- `grep` shows no Google REST endpoints and no OAuth client code anywhere in `src/`.
- No email goes to any address other than `PULSE_RECIPIENT`, and no doc is ever created.

### Risks
| Risk | Mitigation |
|---|---|
| The send fails after the append, on Actions with the Doc check | The run fails visibly; `pulse publish --week … --force` re-publishes |
| MCP OAuth tokens expire during unattended runs | `pulse doctor` runs before each scheduled run; document how to re-authenticate |

---

## Phase 6: Orchestration, Reliability, CLI Hardening

**Goal:** runs that are safe to repeat, can resume, and are observable. *(§4.8, §6, §10)*

**Status (2026-11-23):** done for unattended GitHub Actions runs. Two items are deferred to later (marked below); neither blocks Phase 7.

### Tasks
- [x] ~~`orchestrator.py`~~ *Not needed.* Each stage already reads the previous stage's saved artifacts from `cli.py`: `analyze` reads `data/reviews.parquet`, `publish` reads `runs/{week}/`, and `--skip-ingest` works. A separate module would only move code.
- [ ] `manifest.json` *(partly done)*: each stage writes its own section (ingest counts and status per source, scrub, cluster engine/model/revision/prompt versions/usage/timings, compose, publish).
  - [ ] **Later (G7):** config snapshot, git SHA, `pulse` version, ingest and scrub timings, an overall run status and exit code.
- [x] Implement every failure behavior in §10:
  - [x] A source fails → continue and record it; stop if fewer than `min_reviews` (default 50). The pulse source line and the run summary say "Play Store unavailable this week"; if every source fails, `pulse run` stops at ingest with exit 5 (`cli.run`, `render.markdown.source_line`, `Pulse.unavailable_sources`).
  - [x] Retries with backoff for network calls: review feeds (3 retries, built in Phase 1), MCP connect/check/append-confirm/email-setup (`mcp.retries`, `mcp.retry_delay_seconds`; `publish.runner._can_retry`), and the embedding model download (2 retries; `Model2VecEmbedder`). The send itself and tool errors are never retried.
  - [ ] **Later (G8):** *(LLM engine)* backoff at the stage level on top of the SDK's own retries (`LlmError.retryable` is set but not used yet).
  - [x] *(LLM engine)* Log the refusal category and stop the stage cleanly (`LlmRefusal`; `refusal_category` in the manifest; exit 9).
  - [x] *(Local engine)* Missing embedding model with no network: a clear error naming `analysis.local.model` (`LocalEngineError`, built with Phase 3).
  - [x] Partial publish → resume: locally from `publish.json`; on GitHub Actions (nothing kept) with `pulse publish --email-only`, which checks the entry is in the Doc and never appends.
- [x] Clear CLI exit codes (0 for success, a non-zero code per failure class, 1 for an unexpected error with its traceback) and a readable summary at the end of `pulse run` and `pulse publish`, also written to `$GITHUB_STEP_SUMMARY` (`cli.ExitCode`, `cli._guarded`, `summary.py`). The summary holds no review text, recipient or Doc link.
- [x] ~~`pulse prune`~~ *Not needed:* nothing is kept between GitHub Actions runs.
- [x] A README with setup, MCP server connection and authentication, configuration, commands, exit codes, GitHub Actions notes, and troubleshooting.

### Deliverables
- A hardened CLI, the README, and complete run manifests (manifest additions deferred: G7).

### Exit criteria
- [x] A simulated failure test for each row of §10 behaves as specified (`tests/test_reliability.py`, with `test_publish.py`, `test_cli.py`, `test_llm*.py`, `test_local_cluster.py`).
- [x] Two identical runs in a row produce one Doc entry, one email, and stable artifacts (`test_two_identical_runs_publish_once`).

---

## Phase 7: Scheduling, First Live Runs, Quality Review ★ M3

**Goal:** a weekly pulse that runs unattended and that stakeholders find useful. *(§4.9)*

### Tasks
- [ ] Set up the scheduler: Windows Task Scheduler (Mondays 09:00 IST, running `pulse doctor && pulse run`), or a scheduled GitHub Actions workflow.
- [ ] Compute the run week as the ISO week that just ended (Monday–Sunday, IST).
- [ ] Do **3 consecutive live weekly runs**. For each one:
  - [ ] A human check against the `problemStatement.md` §4 checklist.
  - [ ] Feedback from Product, Support, and Leadership readers: is it scannable, are the themes useful, can the actions be acted on?
- [ ] Tune the theme catalogue (labels, keywords, templates), the praise/specific/vague word lists, and the ranking weights based on that feedback. For the LLM engine, tune and version the prompts.

### Deliverables
- **M3:** a weekly Doc entry and email that appear without anyone running anything.
- A short notes file of what was tuned after the first runs.

### Exit criteria
- 3 scheduled runs in a row succeed with no manual steps.
- All checklist items in `problemStatement.md` §4 are ticked for each run.

---

## Phase 8 (Optional): Enhancements

Pick from these once M3 is stable:

- [ ] **Stable theme IDs across weeks** (needed for the trend): the local engine already reuses catalogue IDs for known themes; extend this to keyword-labelled themes by matching each new theme's centroid to last week's (`runs/{prev_week}/themes.json`). For `--engine llm`, pass last week's taxonomy to Pass A as the main seed (§4.4).
- [ ] **Week-over-week trend:** "↑/↓ vs last week" next to each theme, using `runs/` history (§13 #6).
- [ ] **Quality eval set:** a small labeled set for theme coherence and usefulness of actions; run it in CI when prompts change.
- [ ] **Rating trend line:** average rating per week across the window, shown in the source line.
- [ ] **Engine comparison:** on the quality eval set, compare the local engine with `--engine llm` (theme coherence, quote relevance, action usefulness) to decide whether the paid engine is ever worth it.
- [ ] *(LLM engine)* **Cost tuning:** measure whether a lower effort level on Pass B gives the same assignment accuracy.

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
| Email sent to self or alias via MCP | 5 |
| MCP-first, no hand-written Google OAuth or REST | 5 (enforced by exit criteria) |
| Weekly cadence | 7 |

---

## Definition of Done (Project)

- [ ] `pulse run` turns live public reviews into a Doc entry and a sent email, through MCP only.
- [ ] Every validator check in §4.6 passes on each published run.
- [ ] The test suite (unit, contract, fake-MCP, dry-run end-to-end) passes in CI.
- [ ] The README lets a new developer set up and run the project from scratch.
- [ ] 3 consecutive scheduled weekly runs succeed with no manual steps.
