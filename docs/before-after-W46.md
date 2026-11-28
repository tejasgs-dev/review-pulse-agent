# Before and after: template actions vs LLM actions (week 2026-W46)

Same week, same data, same ML clustering. Only the writing step changed: before, the
`local` engine filled fixed templates from `config/themes.yaml`; after, the `hybrid` engine
lets an LLM (Groq, `openai/gpt-oss-120b`) read a sample of each theme's reviews and write the
action ideas. The run used a scratch folder, so nothing was published.

## What stayed the same (decided by the ML)

The scikit-learn clustering produced identical results in both runs: 1,348 reviews, the same
5 themes and the same top 3.

| Theme | Share of reviews | 1–2★ | Reviews in the last two weeks | Trend |
|---|---|---|---|---|
| Customer support | 16% | 94% | 41 | steady |
| Stocks & portfolio | 14% | 45% | 42 | rising |
| Bugs & app updates | 13% | 61% | 28 | steady |

Every number in the "after" actions below (41, 42, 28) was inserted by code from this table.
The LLM is not allowed to type a digit; it writes `{recent_count}` and the program fills it in.

## Action ideas

| Theme | Before: template (`local` engine) | After: LLM (`hybrid` engine) |
|---|---|---|
| Customer support | Audit the 41 support complaints from the last two weeks and give each one an owner and a reply deadline. | Add more support agents to answer queries promptly, raised across 41 recent reviews. |
| Stocks & portfolio | Find the most common trading complaint in the 42 portfolio reviews from the last two weeks (rising) and fix or explain it in the app. | Create separate debt and equity fund dashboards and enable watchlist, raised across 42 recent reviews. |
| Bugs & app updates | Triage the 28 bug reports from the last two weeks and ship a fix for the most common one in the next release. | Fix post-update crashes and loading failures, raised across 28 recent reviews. |

The template actions are the same shape every week ("find the most common complaint"); only
the figure changes. The LLM actions name the problem that the reviews describe.

## Where each LLM action comes from

The LLM saw 10 sample reviews per theme (filtered by code first: no reviews that name a
person, only reviews that clearly belong to the theme). Review IDs are from that week's run;
the review text itself stays out of this repository (architecture §6), so it is paraphrased
here.

- **Customer support**: several sample reviews describe getting no answer: no response four
  days after raising a query (r1271), waiting in the call queue (r0098), nobody picking up
  (r0771), chat support closing issues without talking (r1284), and only an automated reply
  (r1338).
- **Stocks & portfolio**: the sample is a mix of different feature requests. Separate debt and
  equity fund dashboards (r1347) and a watchlist for US investments (r1317) are each asked for
  once; others ask for SLBM, MTF on ETFs, BE-stock labels, and PPF or FDs. The action combines
  two real requests; it is not one problem that many reviewers share.
- **Bugs & app updates**: several sample reviews describe breakage after an update: the app
  stopped working after an update (r1246), crashes and not loading (r1115), login failing
  (r0689), many recent bugs (r1277), and a feature missing after an update (r1297).

## How the checks shaped the result

The LLM wrote three drafts; code rejected the first two and sent the reasons back:

1. Draft 1 was rejected because an action invented a target ("one business day"). A code
   check rejects time limits and amounts written as words; only the ML's figures are allowed.
2. Draft 2 was rejected because the pulse was 268 words; the limit is 250.
3. Draft 3 passed every check (246 words).

While the prompt was being tuned, earlier drafts showed the two problems these rules now
address: one action was built from a single review, and the figures were described wrongly
("42 requests", "28 crash reports", when 42 and 28 count all recent reviews in the theme). The
prompt now says what the figure counts and asks for problems that at least two reviews raise.

## Limits

- The LLM's wording changes from run to run; the checks guarantee the numbers, the 250-word
  limit, verbatim quotes and the 3 themes, not the exact phrasing.
- Some judgement cannot be checked by code. For example, "Stocks & portfolio" has no single
  shared problem, so its action names two individual requests. People still read the pulse
  before acting on it.
- The three calls used about 15,000 tokens. That is more than Groq's free limit of 8,000 tokens
  per minute, so the client waited 37 seconds as Groq asked. A run that passes on the first
  draft uses about 4,000 to 5,000 tokens.
