"""The summary printed at the end of `pulse run` (and written to the GitHub Actions job summary).

Each stage fills in its part as it goes, so a run that stops early still says how far it got,
why it stopped, and which exit code it returned. The summary holds counts, statuses and theme
labels only: never review text, the recipient's address or the Doc link, because Actions logs
and job summaries of a public repository are public.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from pulse.models import SOURCES
from pulse.render.markdown import SOURCE_LABELS, unavailable_note

# What each exit code means, for the summary and the README (kept in step with cli.ExitCode).
EXIT_MEANINGS = {
    0: "success",
    1: "unexpected error (a bug or an unclassified failure; see the traceback)",
    2: "configuration error",
    4: "pulse doctor found a problem",
    5: "every review source failed",
    6: "the review store could not be read",
    7: "too few reviews in the window (analysis.min_reviews)",
    8: "theme analysis failed",
    9: "Claude API failure (LLM engine)",
    10: "the pulse failed validation; nothing was published",
    11: "publishing through MCP failed",
}


def on_github_actions() -> bool:
    """True on a GitHub Actions runner, whose logs are public in a public repository."""
    return bool(os.environ.get("GITHUB_ACTIONS"))


def redact_recipient(text: str, recipient: str | None) -> str:
    """Replace the recipient's address with 'PULSE_RECIPIENT' (server errors can quote it)."""
    return text.replace(recipient, "PULSE_RECIPIENT") if recipient else text


@dataclass
class RunSummary:
    command: str
    week: str
    engine: str | None = None
    started: float = field(default_factory=time.monotonic)
    current_stage: str = "setup"         # where an unexpected error is reported
    # Per source: status (ok | partial | failed | not fetched), fetched, in_window, error.
    sources: dict[str, dict] = field(default_factory=dict)
    reviews: int | None = None           # reviews in the window that went into analysis
    min_reviews: int | None = None
    themes: list[str] = field(default_factory=list)   # top theme labels, in rank order
    word_count: int | None = None
    max_words: int | None = None
    publish: str | None = None           # sent | skipped | dry run | failed | partial
    publish_detail: str = ""
    warnings: list[str] = field(default_factory=list)
    stage: str | None = None             # the stage that failed
    error: str | None = None
    exit_code: int | None = None
    seconds: float | None = None

    @property
    def unavailable(self) -> list[str]:
        return [n for n in SOURCES if self.sources.get(n, {}).get("status") == "failed"]

    def fail(self, stage: str, error: str) -> None:
        if self.error is None:          # keep the first, most specific failure
            self.stage, self.error = stage, error

    def finish(self, exit_code: int) -> None:
        self.exit_code = int(exit_code)
        self.seconds = round(time.monotonic() - self.started, 1)

    # ------------------------------------------------------------------ rendering

    def _rows(self) -> list[tuple[str, str]]:
        rows: list[tuple[str, str]] = []
        code = self.exit_code
        result = "OK" if code == 0 else "FAILED"
        rows.append(("Result", f"{result} (exit {code}: {EXIT_MEANINGS.get(code, 'unknown')})"))
        if self.engine:
            rows.append(("Engine", self.engine))
        for name in SOURCES:
            s = self.sources.get(name)
            if s is None:
                continue
            label = SOURCE_LABELS.get(name, name)
            text = s["status"]
            if s["status"] != "failed":
                text += f", fetched {s.get('fetched', 0):,}"
            if s.get("in_window") is not None:
                text += f", {s['in_window']:,} in the window"
            if s.get("error"):
                text += f" ({s['error']})"
            rows.append((label, text))
        if self.unavailable:
            rows.append(("Note", unavailable_note(self.unavailable)))
        if self.reviews is not None:
            text = f"{self.reviews:,}"
            if self.min_reviews is not None:
                text += f" (minimum {self.min_reviews})"
            rows.append(("Reviews analysed", text))
        if self.themes:
            rows.append(("Top themes", " · ".join(self.themes)))
        if self.word_count is not None:
            rows.append(("Words", f"{self.word_count}/{self.max_words}"))
        if self.publish:
            rows.append(("Publish", self.publish
                         + (f" ({self.publish_detail})" if self.publish_detail else "")))
        if self.error:
            rows.append(("Stopped at", f"{self.stage}: {self.error}"))
        for w in self.warnings:
            rows.append(("Warning", w))
        if self.seconds is not None:
            rows.append(("Time", f"{self.seconds}s"))
        return rows

    def render_text(self) -> str:
        rows = self._rows()
        width = max(len(k) for k, _ in rows)
        lines = [f"── pulse {self.command} {self.week} " + "─" * 30]
        lines += [f"  {k:<{width}}  {v}" for k, v in rows]
        return "\n".join(lines)

    def render_markdown(self) -> str:
        def cell(v: str) -> str:
            return v.replace("|", "\\|").replace("\n", " ")

        lines = [f"### pulse {self.command} {self.week}", "", "| | |", "|---|---|"]
        lines += [f"| {k} | {cell(v)} |" for k, v in self._rows()]
        return "\n".join(lines) + "\n"

    def write_step_summary(self) -> Path | None:
        """Append to $GITHUB_STEP_SUMMARY when it is set (GitHub Actions). Never fails the run."""
        target = os.environ.get("GITHUB_STEP_SUMMARY")
        if not target:
            return None
        try:
            with open(target, "a", encoding="utf-8") as f:
                f.write(self.render_markdown() + "\n")
        except OSError:
            return None
        return Path(target)
