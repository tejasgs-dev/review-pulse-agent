"""A hand-built, valid Pulse plus the review data it cites. Synthetic text only."""

from __future__ import annotations

import copy
from datetime import date

from pulse.models import ActionIdea, CleanReview, Pulse, Quote, Theme

REVIEWS = {
    "r0101": CleanReview(
        id="r0101", key="k101", source="play_store", rating=1, review_date=date(2026, 11, 12),
        canonical_text="I asked to withdraw my money five days ago. It still has not reached "
                       "my bank account and nobody tells me when it will arrive."),
    "r0202": CleanReview(
        id="r0202", key="k202", source="app_store", rating=2, review_date=date(2026, 11, 10),
        canonical_text="Every morning at market open the order screen freezes for a minute, "
                       "so my orders go through late at a worse price."),
    "r0303": CleanReview(
        id="r0303", key="k303", source="play_store", rating=1, review_date=date(2026, 11, 8),
        canonical_text="Support closed my ticket without reading it and the chat bot keeps "
                       "sending the same copy paste answer every time."),
}
ASSIGNMENTS = {"r0101": "withdrawals", "r0202": "app_performance", "r0303": "support"}


def _quote(rid: str, theme_id: str) -> Quote:
    r = REVIEWS[rid]
    end = r.canonical_text.index(".") + 1 if rid == "r0101" else len(r.canonical_text)
    return Quote.from_span(r, theme_id, 0, end)


def valid_pulse() -> Pulse:
    return Pulse(
        week="2026-W46",
        window=(date(2026, 9, 7), date(2026, 11, 15)),
        review_count=1348,
        sources={"play_store": 1202, "app_store": 146},
        top_themes=[
            Theme(id="withdrawals", label="Withdrawal delays", description="d",
                  volume_share=0.183, negative_share=0.91,
                  summary="Money takes days to reach the bank, with no expected date shown"),
            Theme(id="app_performance", label="Market-open freezes", description="d",
                  volume_share=0.152, negative_share=0.88,
                  summary="Order screens freeze or lag at market open, delaying trades"),
            Theme(id="support", label="Unhelpful support", description="d",
                  volume_share=0.004, negative_share=0.95,
                  summary="Tickets are closed without a fix and replies are copy-pasted"),
        ],
        quotes=[_quote("r0101", "withdrawals"), _quote("r0202", "app_performance"),
                _quote("r0303", "support")],
        actions=[
            ActionIdea(theme_id="withdrawals",
                       action="Show the expected credit date on every pending withdrawal",
                       signal="fewer reviews asking where a withdrawal is"),
            ActionIdea(theme_id="app_performance",
                       action="Load-test order placement at 9:15 IST and fix the slowest step",
                       signal="fewer market-open freeze reviews"),
            ActionIdea(theme_id="support",
                       action="Reopen tickets automatically when the user replies unresolved",
                       signal="fewer closed-without-fix complaints"),
        ],
        general_praise_share=0.19,
        clustered_theme_count=5,
    )


def mutate(fn) -> Pulse:
    """A deep copy of the valid pulse with `fn` applied."""
    p = copy.deepcopy(valid_pulse())
    fn(p)
    return p
