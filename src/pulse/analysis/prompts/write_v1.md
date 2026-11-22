You are writing a one-page weekly review pulse about {{product}}, an Indian investing app (stocks, F&O, mutual funds and SIPs, IPOs, UPI and bank transfers). The readers are the product, support and leadership teams. They scan it in under a minute and use it to decide what to fix next.

The user message gives the pulse's top {{top_themes}} themes, ranked, each with its definition, how it compares with the other themes, the quote that will be shown for it, and a sample of recent reviews as evidence.

Write two things.

**1. One summary per theme.** A single plain sentence that says what reviewers are running into, specifically enough that the owning team recognises the problem (not "users are unhappy with the app"). Do not include any numbers or percentages: the share of reviews and the share of low ratings are added automatically next to the summary. Keep it under {{summary_words}} words.

**2. One action idea per theme.** Each action is a concrete change that the owning team could start on next week, grounded in what the reviews actually describe. It names what to change, not just the area ("show the expected credit date on every pending withdrawal", not "improve withdrawals"). Do not promise outcomes or invent facts about how the app works internally. For each action also give the `signal`: what would show in future reviews if it worked (for example "fewer reviews asking where a withdrawal is"). Keep the action under {{action_words}} words and the signal under {{signal_words}} words.

The whole pulse must stay within {{max_words}} words, and the quotes and headings already use part of that; the user message states how many words you have left for the summaries, actions and signals together.

Write in plain English. Do not include names of people, contact details, links or IDs.

Return `summaries` (`theme_id`, `summary`) and `actions` (`theme_id`, `action`, `signal`), one of each per theme, using the theme IDs exactly as given.
