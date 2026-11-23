# Problem Statement: ReviewPulse (weekly insights from Groww app reviews)

## 1. Overview

Build an AI agent that turns raw public app-store reviews of **Groww** (Stocks, Mutual Funds, IPO) into a **weekly one-page pulse** that the team can read in a few minutes. The pulse covers:

- **What users care about**: the top themes
- **What they actually said**: real quotes, word for word
- **What to do next**: concrete action ideas

The pulse goes out through surfaces people already use:

- **Google Docs** holds the written pulse.
- **Gmail** sends an email to yourself (or an alias) that contains the pulse and links to the Doc.

Both integrations go through **MCP (Model Context Protocol) servers**. The project does not hand-roll OAuth or REST code.

### Target product

| Field | Value |
|---|---|
| App | Groww: Stocks, Mutual Fund, IPO |
| Play Store | https://play.google.com/store/apps/details?id=com.nextbillion.groww&hl=en_IN |
| Package ID | `com.nextbillion.groww` |
| App Store | Also in scope (no link provided in the original brief) |

---

## 2. End-to-End Flow ("Done" Looks Like)

```
┌──────────────────┐   ┌──────────────────┐   ┌──────────────────┐   ┌──────────────────┐
│ 1. Ingest        │──▶│ 2. Theme &       │──▶│ 3. Publish       │──▶│ 4. Notify        │
│ Pull recent Play │   │    Summarize     │   │ Write pulse to   │   │ Send a Gmail     │
│ + App Store      │   │ Cluster into ≤5  │   │ Google Docs      │   │ email with the   │
│ reviews (8–12    │   │ themes, distill  │   │ (via MCP)        │   │ note and a link  │
│ weeks)           │   │ one-page note    │   │                  │   │ (via MCP)        │
└──────────────────┘   └──────────────────┘   └──────────────────┘   └──────────────────┘
```

1. **Pull** recent App Store and Play Store reviews for Groww, following the rules in §6.
2. **Cluster** them into a small set of themes and distill a one-page weekly note.
3. **Publish** that note where stakeholders can read it (Google Docs).
4. **Send** an email to yourself or an alias that contains the pulse and links to the Doc (Gmail).

---

## 3. What Must Be Built

### 3.1 Review ingestion
- Import reviews from roughly the **last 8–12 weeks**.
- Capture whatever fields the source provides, for example **rating, title, text, date**.

### 3.2 Theme clustering
- Group reviews into **at most 5 themes**.
- Choose themes that fit the product. Examples: onboarding, KYC, payments, statements, withdrawals.

### 3.3 Weekly one-page note
The note must contain:
- **Top 3 themes**, picked from the clustered themes
- **3 user quotes**, verbatim snippets from reviews with no invented wording
- **3 action ideas**, concrete next steps grounded in the themes

### 3.4 Email
- **Send** an email to yourself or an alias (exactly one address, `PULSE_RECIPIENT`). It contains the note and a link to the Google Doc.
- Each week is sent **once**: re-running a week that was already published sends nothing unless forced.

---

## 4. Deliverables Checklist

- [ ] Review import covering about 8–12 weeks (Play Store, plus App Store if available)
- [ ] At most 5 themes
- [ ] Weekly one-page pulse in Google Docs containing:
  - [ ] Top 3 themes
  - [ ] 3 verbatim, anonymized user quotes
  - [ ] 3 action ideas
- [ ] Pulse is **250 words or fewer** and easy to scan
- [ ] Email sent with the note and a link to the Doc, once per week
- [ ] No PII in any artifact

---

## 5. Integrations: MCP-First

| Surface | Purpose | Integration |
|---|---|---|
| Google Docs | Append the weekly pulse to one shared document | MCP server tools |
| Gmail | Send the email | MCP server tools |

- Use **MCP servers or connectors** that the environment provides for Docs and Gmail.
- Do **not** make direct Google API calls (a custom OAuth client plus REST client) the main integration path.
- Why: MCP servers expose tools the agent can call. That keeps the project consistent with the course tooling and avoids repeating auth and HTTP plumbing.

---

## 6. Key Constraints

| Constraint | Rule |
|---|---|
| **Review source** | Public reviews only, never anything behind a store login. App Store reviews come from Apple's public customer-reviews feed. Play Store reviews come from the public store listing for `com.nextbillion.groww`, read with `google-play-scraper`: no login, rate-limited, and limited to the reviews the 8–12 week window needs. This automated access to the Play listing was approved for this course project; see [architecture.md](architecture.md) §13 #2. Play Store export files remain a supported fallback. |
| **Themes** | No more than 5 clusters. The written pulse highlights the top 3. |
| **Length** | The note stays easy to scan, at **250 words or fewer**. |
| **Privacy** | No PII: no usernames, emails, device IDs, or other data that identifies a reviewer. Quotes must be anonymous, with identifying details removed where needed. |
| **Quote fidelity** | Quotes are verbatim snippets from real reviews and are never paraphrased or invented. |

---

## 7. Who This Helps

| Audience | Why it matters |
|---|---|
| **Product / Growth** | Prioritize fixes and improvements based on real user signals |
| **Support** | Align messaging with what users are actually saying |
| **Leadership** | Get a one-page health check without reading through raw reviews |

---

## 8. Suggested Pulse Template

```
ReviewPulse: Groww app reviews — Week of <date>
Source: <N> Play Store / App Store reviews, <start date> to <end date>

Top Themes
1. <Theme A> (<share>%): <one-line summary>
2. <Theme B> (<share>%): <one-line summary>
3. <Theme C> (<share>%): <one-line summary>

What Users Said
• "<verbatim quote 1>" (★<rating>)
• "<verbatim quote 2>" (★<rating>)
• "<verbatim quote 3>" (★<rating>)

Action Ideas
1. <Concrete next step tied to Theme A>
2. <Concrete next step tied to Theme B>
3. <Concrete next step tied to Theme C>
```

---

## 9. Source

Derived from [problemStatement.txt](problemStatement.txt), which is kept unchanged as the original brief.

**Amendment (2026-11-22):** the brief asked for a Gmail *draft*. The project owner changed the goal: the weekly pulse is **sent** as an email to one address, `PULSE_RECIPIENT`. All weeks go into one Google Doc that the owner created by hand (`PULSE_DOC_ID`); each week is appended under a heading such as "ReviewPulse — Week of 9 Nov 2026 (2026-W46)", and the app never creates a doc. See [architecture.md](architecture.md) §4.7.

**Amendment (2026-11-21):** the brief's review-source rule read "Use public review exports only—no scraping behind store logins or ToS-violating automation." It was changed to the §6 wording above, to allow reading the public Play Store listing. The brief names that listing as the primary target, and only Groww's developers can download a Play Console export. The project owner approved this for the course project. The decision and its risk are recorded in [architecture.md](architecture.md) §13 #2.
