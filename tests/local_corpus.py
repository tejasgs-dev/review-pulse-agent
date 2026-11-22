"""A synthetic review corpus for the local engine: four product topics with their own words,
plus generic praise and vague venting. No real user data."""

from __future__ import annotations

from datetime import date, timedelta

from pulse.models import CleanReview

END = date(2026, 11, 15)
WINDOW = (date(2026, 9, 7), END)

TOPICS = {
    "support": [
        "Customer support never replied to my ticket and the chat agent closed my complaint.",
        "I called customer care three times about my complaint and nobody from support helped.",
        "The support team closes every ticket without reading it and the chat reply is copy paste.",
        "No response from customer service on email or chat for two weeks about my ticket.",
    ],
    "charges": [
        "Brokerage charges are too high and hidden charges were deducted from my account balance.",
        "They deducted hidden fees and the brokerage charge on every order is far too expensive.",
        "Very high brokerage and extra charges on intraday orders, the fee breakdown is unclear.",
        "Hidden charges again this month, the brokerage fees eat all my profit on small trades.",
    ],
    "withdrawals": [
        "My withdrawal money is stuck and still not credited to my bank account after waiting.",
        "Withdraw request pending for a week, the funds never reached my bank account at all.",
        "Money is stuck in withdrawal processing and the bank credit never arrives on time.",
        "I requested a withdrawal of my funds and the money is still not credited to the bank.",
    ],
    "bugs": [
        "The app crashes after the latest update and the screen keeps loading with an error.",
        "Since the new update the app is slow, it hangs on the loading screen and shows errors.",
        "Constant crash and glitch after update, the login screen shows an error and freezes.",
        "After every update there is a new bug, the app freezes and the screen stops loading.",
    ],
}
PRAISE = "Very good app, easy to use and a smooth experience for investing every day."
VAGUE = "Worst app, worst experience, useless and a waste, worst ever, thanks for nothing."


def corpus(per_topic: int = 24, praise: int = 20, vague: int = 6) -> list[CleanReview]:
    rows: list[tuple[str, int, str]] = []
    for topic, texts in TOPICS.items():
        for i in range(per_topic):
            rating = 1 if i % 4 else 2
            rows.append((topic, rating, f"{texts[i % len(texts)]} (#{i})"))
    rows += [("praise", 5, f"{PRAISE} (#{i})") for i in range(praise)]
    rows += [("vague", 1, f"{VAGUE} (#{i})") for i in range(vague)]
    out = []
    for n, (topic, rating, text) in enumerate(rows, start=1):
        out.append(CleanReview(id=f"r{n:04d}", key=f"{topic}-{n}", source="play_store",
                               rating=rating,
                               review_date=END - timedelta(days=(n * 7) % 60),
                               canonical_text=text))
    return out


def topic_of(review: CleanReview) -> str:
    """The topic the synthetic review was written for (kept in its key)."""
    return review.key.rsplit("-", 1)[0]
