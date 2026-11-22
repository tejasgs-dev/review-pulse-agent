"""A scripted stand-in for the model, for clustering tests. No network, no cost."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date, timedelta

from pulse.analysis.cluster import Assignment, AssignmentBatch, Discovery, ThemeDef
from pulse.analysis.llm import Usage
from pulse.models import CleanReview

_LINE_ID = re.compile(r"^(r\d+) \|", re.MULTILINE)

# review ID -> (theme_id, secondary_theme_id, sentiment)
Labeller = Callable[[str], tuple[str, str | None, str]]


def theme(tid: str, examples: list[str], label: str | None = None) -> ThemeDef:
    return ThemeDef(id=tid, label=label or tid.replace("_", " ").title(),
                    description=f"Reviews about {tid}.", include=f"{tid} problems",
                    exclude="anything else", example_ids=examples)


class FakeLlm:
    """Answers `discover*` calls from a list of Discovery objects (one per call, the last one
    repeats) and assignment calls by applying `labeller` to the IDs in the batch text.

    `drop_once` lists review IDs left out of the first assignment answer that contains them,
    to exercise re-requests; `drop_always` leaves them out every time.
    """

    def __init__(self, discoveries: list[Discovery], labeller: Labeller, *,
                 drop_once: set[str] | None = None, drop_always: set[str] | None = None,
                 extra: list[Assignment] | None = None, pick=None, write=None) -> None:
        self.pick, self.write = pick, write  # composer overrides: call dict -> answer
        self.discoveries = discoveries
        self.labeller = labeller
        self.drop_once = set(drop_once or ())
        self.drop_always = set(drop_always or ())
        self.extra = list(extra or ())
        self.calls: list[dict] = []
        self.usage = Usage()

    def parse(self, *, name, system, prefix, user, schema, effort, max_tokens=64_000):
        self.calls.append({"name": name, "system": system, "prefix": prefix, "user": user,
                           "effort": effort, "schema": schema.__name__})
        self.usage.calls += 1
        if schema is Discovery:
            n = sum(1 for c in self.calls if c["schema"] == "Discovery")
            return self.discoveries[min(n, len(self.discoveries)) - 1]
        if schema.__name__ == "QuotePicks":
            return (self.pick or default_pick)(self.calls[-1])
        if schema.__name__ == "PulseWriting":
            return (self.write or default_write)(self.calls[-1])
        ids = _LINE_ID.findall(user)
        out = []
        for rid in ids:
            if rid in self.drop_always:
                continue
            if rid in self.drop_once:
                self.drop_once.discard(rid)
                continue
            tid, sec, sentiment = self.labeller(rid)
            out.append(Assignment(review_id=rid, theme_id=tid, secondary_theme_id=sec,
                                  sentiment=sentiment))
        out.extend(self.extra)
        self.extra = []
        return AssignmentBatch(assignments=out)

    def names(self) -> list[str]:
        return [c["name"] for c in self.calls]


_THEME_BLOCK = re.compile(r"^## Theme `([^`]+)`", re.MULTILINE)
_CANDIDATE = re.compile(r"^Candidate (r\d+) ", re.MULTILINE)
_SENTENCE = re.compile(r"^  \[(\d+)\] ", re.MULTILINE)
_WRITE_THEME = re.compile(r"^## \d+\. `([^`]+)`: (.+)$", re.MULTILINE)


def default_pick(call: dict):
    """The first candidate of each theme, all of its sentences."""
    from pulse.analysis.compose import QuotePick, QuotePicks

    picks = []
    blocks = _THEME_BLOCK.split(call["prefix"])[1:]
    for tid, body in zip(blocks[::2], blocks[1::2], strict=True):
        first = _CANDIDATE.split(body)
        rid, text = first[1], first[2]
        n = max(int(x) for x in _SENTENCE.findall(text))
        picks.append(QuotePick(theme_id=tid, review_id=rid, first_sentence=1, last_sentence=n))
    return QuotePicks(picks=picks)


def default_write(call: dict):
    from pulse.analysis.compose import ActionDraft, PulseWriting, ThemeSummary

    themes = _WRITE_THEME.findall(call["user"])
    return PulseWriting(
        summaries=[ThemeSummary(theme_id=t, summary=f"Reviewers describe problems with {t}")
                   for t, _ in themes],
        actions=[ActionDraft(theme_id=t, action=f"Fix the most reported {t} problem first",
                             signal=f"Fewer {t} complaints") for t, _ in themes],
    )


def clean_reviews(n: int, *, end: date = date(2026, 11, 15), rating=lambda i: 1,
                  span_days: int = 70) -> list[CleanReview]:
    """n synthetic scrubbed reviews spread over the `span_days` days ending on `end`."""
    width = max(4, len(str(n)))
    return [CleanReview(id=f"r{i:0{width}d}", key=f"k{i}", source="play_store",
                        rating=rating(i),
                        review_date=end - timedelta(days=(n - i) % span_days),
                        canonical_text=f"Synthetic review number {i} about the app experience")
            for i in range(1, n + 1)]
