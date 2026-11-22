"""`pulse` command-line entry point (architecture §4.8).

Phase 0: commands parse and validate their options; the pipeline stages are stubs.
`pulse doctor` already checks config and environment.
"""

from __future__ import annotations

import contextlib
import os
import sys
from enum import IntEnum
from pathlib import Path
from typing import Annotated

import typer

from pulse import __version__
from pulse.config import PROJECT_ROOT, ConfigError, PulseConfig, load_config
from pulse.weeks import last_completed_week, parse_iso_week


class ExitCode(IntEnum):
    OK = 0
    CONFIG_ERROR = 2
    NOT_IMPLEMENTED = 3
    DOCTOR_FAILED = 4
    INGEST_FAILED = 5
    STORE_ERROR = 6
    TOO_FEW_REVIEWS = 7
    ANALYSIS_FAILED = 8   # the model's answers could not be made valid (Phase 3)
    LLM_FAILED = 9        # API error, refusal, or truncated response
    VALIDATION_FAILED = 10  # the composed pulse failed §4.6 after retries; nothing published


def _utf8_console() -> None:
    # Edge case ORC-11: Windows consoles default to cp1252 and choke on ★ ₹ —.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            with contextlib.suppress(ValueError, OSError):
                stream.reconfigure(encoding="utf-8", errors="replace")


# Must run before typer renders anything, including --help.
_utf8_console()

app = typer.Typer(
    name="pulse",
    help="Groww Weekly Review Pulse: public reviews → one-page pulse → Google Doc + Gmail draft.",
    no_args_is_help=True,
    add_completion=False,
)


def _week_option(value: str | None) -> str:
    if value is None:
        return last_completed_week()
    try:
        parse_iso_week(value)
    except ValueError as e:
        raise typer.BadParameter(str(e)) from e
    return value


WeekOpt = Annotated[
    str | None,
    typer.Option("--week", help="ISO week, e.g. 2026-W47. Default: the week that just ended (IST).",
                 callback=_week_option),
]
ConfigOpt = Annotated[
    Path | None, typer.Option("--config", help="Path to pulse.yaml (default: config/pulse.yaml).")
]


def _load(config: Path | None) -> PulseConfig:
    try:
        return load_config(config)
    except ConfigError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(ExitCode.CONFIG_ERROR) from e


def _not_implemented(command: str, phase: int) -> None:
    typer.secho(f"`pulse {command}` is not implemented yet (planned for Phase {phase}).",
                fg=typer.colors.YELLOW, err=True)
    raise typer.Exit(ExitCode.NOT_IMPLEMENTED)


def _show_version(value: bool) -> None:
    if value:
        typer.echo(f"pulse {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option("--version", help="Show the version and exit.",
                     callback=_show_version, is_eager=True),
    ] = False,
) -> None:
    """Groww Weekly Review Pulse."""


@app.command()
def run(
    week: WeekOpt = None,
    weeks_back: Annotated[int | None, typer.Option("--weeks-back", min=8, max=12)] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Run every stage, skip MCP calls.")
    ] = False,
    skip_ingest: Annotated[bool, typer.Option("--skip-ingest")] = False,
    config: ConfigOpt = None,
) -> None:
    """Run the whole pipeline for one week: ingest, then stages 3–6, then publish.

    With --dry-run every stage runs and the pulse is written to runs/{week}/, but nothing is
    sent to Google Docs or Gmail.
    """
    cfg = _load(config)
    if weeks_back is not None:
        cfg = cfg.model_copy(update={"window": cfg.window.model_copy(
            update={"weeks_back": weeks_back})})
    if skip_ingest:
        typer.echo("Ingest skipped (--skip-ingest): using data/reviews.parquet as it is.")
    else:
        _ingest_stage(cfg, week, full=False, sources=None)
    _analysis_stages(cfg, week)
    if dry_run:
        typer.secho("Dry run: nothing was published.", fg=typer.colors.GREEN)
        return
    _not_implemented("run (publishing to Google Docs and Gmail)", 5)


@app.command()
def ingest(
    source: Annotated[str, typer.Option("--source", help="play_store | app_store | all")] = "all",
    week: WeekOpt = None,
    full: Annotated[bool, typer.Option(
        "--full", help="Re-fetch the whole review window instead of only new reviews.")] = False,
    config: ConfigOpt = None,
) -> None:
    """Fetch and normalize reviews into data/reviews.parquet."""
    if source not in {"play_store", "app_store", "all"}:
        raise typer.BadParameter("must be play_store, app_store, or all", param_hint="--source")
    cfg = _load(config)
    report = _ingest_stage(cfg, week, full=full,
                           sources=None if source == "all" else [source])
    if report.all_failed:
        raise typer.Exit(ExitCode.INGEST_FAILED)


def _ingest_stage(cfg: PulseConfig, week: str, *, full: bool, sources: list[str] | None):
    """Stages 1–2: fetch, normalize, upsert into data/reviews.parquet, and report."""
    from pulse.ingest.normalize import DROP_RULES
    from pulse.ingest.runner import run_ingest
    from pulse.ingest.store import StoreSchemaError

    try:
        report = run_ingest(cfg, week=week, full=full, sources=sources)
    except StoreSchemaError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(ExitCode.STORE_ERROR) from e

    labels = {
        "dropped_bad_rating": "bad rating", "dropped_bad_date": "no date",
        "dropped_future_date": "future date", "dropped_no_text": "no text",
        "dropped_too_short": f"under {cfg.ingest.min_words} words",
        "dropped_non_english": "non-English",
    }
    start, end = report.window
    typer.echo(f"Ingest run {report.run_id} · window {start} – {end} ({report.week})")
    for s in report.sources:
        color = {"ok": typer.colors.GREEN, "partial": typer.colors.YELLOW}.get(
            s.status, typer.colors.RED)
        line = (f"  {s.name:<11} {typer.style(s.status, fg=color)} "
                f"({s.mode} since {s.since}) fetched {s.fetched}")
        if s.skipped:
            line += " · skipped " + ", ".join(f"{k} {v}" for k, v in sorted(s.skipped.items()))
        typer.echo(line)
        for w in s.warnings:
            typer.secho(f"      ! {w}", fg=typer.colors.YELLOW)
        if s.error:
            typer.secho(f"      ✗ {s.error}", fg=typer.colors.RED)
        c = s.cleaning
        if c.input:
            parts = [f"{labels[r]} {c.counts[r]}" for r in DROP_RULES if c.counts.get(r)]
            line = f"      kept {c.kept} of {c.input}"
            if parts:
                line += " · dropped: " + ", ".join(parts)
            typer.echo(line)
            if c.dropped_languages:
                typer.echo("      non-English by detected language: " + ", ".join(
                    f"{lang} {n}" for lang, n in c.dropped_languages.most_common()))
            extras = [f"emojis removed from {c.counts['emojis_removed']}"
                      if c.counts.get("emojis_removed") else "",
                      f"duplicates {c.counts['duplicate']}" if c.counts.get("duplicate") else ""]
            if any(extras):
                typer.echo("      " + " · ".join(e for e in extras if e))
    u = report.upsert
    typer.echo(f"  store: +{u.new} new, {u.updated} updated, {u.unchanged} unchanged, "
               f"{u.removed} removed → {u.total} total ({report.store_path.name})")
    typer.echo("  in window: " + ", ".join(
        f"{src} {report.in_window[src]}" for src in report.in_window))
    if report.manifest_path:
        typer.echo(f"  manifest: {report.manifest_path}")
    if report.total_in_window < cfg.analysis.min_reviews:
        typer.secho(
            f"  ! only {report.total_in_window} reviews in the window; analysis "
            f"needs at least {cfg.analysis.min_reviews} (analysis.min_reviews)",
            fg=typer.colors.YELLOW)
    return report


@app.command()
def analyze(week: WeekOpt = None, config: ConfigOpt = None) -> None:
    """Stages 3–6 only (no publishing): scrub, cluster, compose, validate."""
    _analysis_stages(_load(config), week)


def _analysis_stages(cfg: PulseConfig, week: str) -> None:
    from pulse.ingest.normalize import in_window
    from pulse.ingest.store import ReviewStore, StoreSchemaError
    from pulse.manifest import update_manifest
    from pulse.privacy.scrub import scrub_all, write_clean_reviews
    from pulse.weeks import review_window

    start, end = review_window(week, cfg.window.weeks_back)
    try:
        stored = ReviewStore(cfg.paths.data_path / "reviews.parquet").load()
    except StoreSchemaError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(ExitCode.STORE_ERROR) from e
    reviews = in_window(stored.values(), start, end)
    typer.echo(f"Analyze {week} · window {start} – {end} · {len(reviews)} reviews")
    if len(reviews) < cfg.analysis.min_reviews:  # §10 row 1
        typer.secho(f"  ✗ fewer than analysis.min_reviews={cfg.analysis.min_reviews} reviews; "
                    "run `pulse ingest` first", fg=typer.colors.RED, err=True)
        raise typer.Exit(ExitCode.TOO_FEW_REVIEWS)

    # Stage 3: PII scrub. Only counts are reported; found values are never printed or logged.
    result = scrub_all(reviews, number_min_digits=cfg.privacy.number_min_digits,
                       reference_min_digits=cfg.privacy.reference_min_digits)
    path = write_clean_reviews(result.reviews, cfg.paths.runs_path, week)
    update_manifest(cfg.paths.runs_path, week, "scrub", {
        "reviews": len(result.reviews), "reviews_with_pii": result.reviews_with_pii,
        "redactions": dict(result.hits.most_common()),
    })
    hits = ", ".join(f"{k} {v}" for k, v in result.hits.most_common()) or "none"
    typer.echo(f"  PII scrub: {result.reviews_with_pii} reviews had identifiers removed "
               f"({hits})")
    typer.echo(f"  wrote {path}")

    from pulse.analysis import llm as llm_mod

    llm = llm_mod.build_llm(cfg.llm, log_dir=cfg.paths.runs_path / week / "llm")
    cluster = _cluster_stage(cfg, week, (start, end), result.reviews, llm)
    _compose_stage(cfg, week, cluster, result.reviews, llm)


def _cluster_stage(cfg: PulseConfig, week: str, window, reviews, llm):
    """Stage 4: theme clustering and ranking → runs/{week}/themes.json (§4.4)."""
    import time

    from pulse.analysis import llm as llm_mod
    from pulse.analysis.cluster import (
        ClusterError,
        manifest_section,
        run_clustering,
        write_themes,
    )
    from pulse.analysis.rank import GENERAL_PRAISE, OTHER, RankError
    from pulse.manifest import update_manifest

    runs = cfg.paths.runs_path
    typer.echo(f"  Theme clustering with {cfg.llm.model} ({len(reviews)} reviews)…")
    started = time.monotonic()
    try:
        result = run_clustering(llm, reviews, cfg, week=week, window=window)
    except (llm_mod.LlmError, ClusterError, RankError) as e:
        code = ExitCode.LLM_FAILED if isinstance(e, llm_mod.LlmError) else (
            ExitCode.ANALYSIS_FAILED)
        update_manifest(runs, week, "cluster", {
            "status": "failed", "error": str(e),
            "kind": getattr(e, "kind", type(e).__name__),
            "refusal_category": getattr(e, "category", None),
            "usage": llm.usage.to_dict(),
        })
        typer.secho(f"  ✗ {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code) from e

    path = write_themes(result, runs)
    section = manifest_section(result)
    section["seconds"] = round(time.monotonic() - started, 1)
    update_manifest(runs, week, "cluster", section)

    labels = {t.id: t.label for t in result.themes}
    for s in result.ranked:
        star = "★" if s.theme_id in result.top else " "
        typer.echo(f"   {star} {s.rank}. {labels[s.theme_id]:<28} {s.volume_share:6.1%}  "
                   f"neg {s.negative_share:4.0%}  recency ×{s.recency_weight:.2f}  "
                   f"score {s.score:.3f}")
    b = result.buckets
    typer.echo(f"     general praise {b[GENERAL_PRAISE].volume_share:.1%} · "
               f"other {b[OTHER].volume_share:.1%}"
               + (" · re-discovered once" if result.rediscovered else ""))
    probe = result.quality["consistency"]
    if probe.get("agreement") is not None:
        typer.echo(f"     consistency probe: {probe['agreement']:.0%} of {probe['sample']} agree")
    for w in result.warnings:
        typer.secho(f"     ! {w['message']}", fg=typer.colors.YELLOW)
    u = result.usage
    typer.echo(f"     {u.calls} calls · {u.input_tokens:,} in / {u.output_tokens:,} out tokens "
               f"(cache read {u.cache_read_tokens:,}) · ≈${u.estimated_usd:.2f} · "
               f"{section['seconds']}s")
    typer.echo(f"  wrote {path}")
    return result


def _compose_stage(cfg: PulseConfig, week: str, cluster, reviews, llm) -> None:
    """Stages 5–6: compose quotes, summaries and actions; validate; render (§4.5, §4.6, §7)."""
    import time

    from pulse.analysis import llm as llm_mod
    from pulse.analysis.compose import (
        QUOTE_PROMPT,
        WRITE_PROMPT,
        ComposeError,
        compose_pulse,
        write_compose_artifacts,
        write_failure,
    )
    from pulse.manifest import update_manifest

    runs = cfg.paths.runs_path
    before = llm_mod.Usage()
    before.add(llm.usage)
    typer.echo("  Composing the pulse (quotes, summaries, actions)…")
    started = time.monotonic()
    try:
        result = compose_pulse(llm, cluster, reviews, cfg)
    except (ComposeError, llm_mod.LlmError) as e:
        attempts = getattr(e, "attempts", [])
        path = write_failure(runs, week, stage="compose", error=str(e), attempts=attempts)
        update_manifest(runs, week, "compose", {
            "status": "failed", "error": str(e), "attempts": len(attempts),
            "kind": getattr(e, "kind", type(e).__name__),
            "refusal_category": getattr(e, "category", None),
            "usage": _usage_since(llm.usage, before),
        })
        typer.secho(f"  ✗ {e}", fg=typer.colors.RED, err=True)
        typer.secho(f"  nothing was published; see {path}", fg=typer.colors.RED, err=True)
        raise typer.Exit(ExitCode.LLM_FAILED if isinstance(e, llm_mod.LlmError)
                         else ExitCode.VALIDATION_FAILED) from e

    paths = write_compose_artifacts(result, cfg, runs)
    seconds = round(time.monotonic() - started, 1)
    update_manifest(runs, week, "compose", {
        "status": "ok", "attempts": len(result.attempts),
        "word_count": result.report.word_count, "max_words": cfg.analysis.max_words,
        "prompt_versions": {"quote_pick": QUOTE_PROMPT, "write": WRITE_PROMPT},
        "usage": _usage_since(llm.usage, before), "seconds": seconds,
    })
    (runs / week / "failure.json").unlink(missing_ok=True)  # a stale failure from earlier
    typer.echo(f"     passed every check on attempt {len(result.attempts)} · "
               f"{result.report.word_count}/{cfg.analysis.max_words} words · {seconds}s")
    typer.echo(f"  wrote {paths['pulse_md']}, {paths['pulse_json'].name}, "
               f"{paths['validation'].name}")
    typer.echo("")
    typer.echo(paths["pulse_md"].read_text(encoding="utf-8"))


def _usage_since(now, before) -> dict:
    from pulse.analysis.llm import Usage

    delta = Usage(
        calls=now.calls - before.calls,
        input_tokens=now.input_tokens - before.input_tokens,
        output_tokens=now.output_tokens - before.output_tokens,
        cache_read_tokens=now.cache_read_tokens - before.cache_read_tokens,
        cache_write_tokens=now.cache_write_tokens - before.cache_write_tokens,
        seconds=now.seconds - before.seconds,
        models={m: n - before.models.get(m, 0) for m, n in now.models.items()
                if n - before.models.get(m, 0)},
    )
    return delta.to_dict()


@app.command()
def publish(week: WeekOpt = None, config: ConfigOpt = None) -> None:
    """Stage 7 from saved run artifacts."""
    _load(config)
    _not_implemented("publish", 5)


@app.command()
def doctor(config: ConfigOpt = None) -> None:
    """Check config, credentials, and MCP setup."""
    cfg = _load(config)
    checks: list[tuple[str, bool, str]] = [("config/pulse.yaml is valid", True, "")]

    has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    checks.append(("ANTHROPIC_API_KEY is set", has_key,
                   "set it in .env, or use an `ant auth login` profile"))
    checks.append(("PULSE_RECIPIENT is set", cfg.publish.recipient is not None,
                   "set one address in .env"))
    checks.append(("PULSE_DOCS_FOLDER_ID is set", cfg.publish.docs_folder_id is not None,
                   "set the Drive folder ID in .env"))
    for name, server in (("docs", cfg.mcp.docs), ("gmail", cfg.mcp.gmail)):
        checks.append((f"MCP server '{name}' is configured", server.configured,
                       f"pending decision §13 #1: set mcp.{name} in pulse.yaml"))
    tool_map = PROJECT_ROOT / cfg.mcp.tool_map
    checks.append(("tool map file exists", tool_map.is_file(), str(tool_map)))

    failed = 0
    for label, ok, hint in checks:
        mark = typer.style("ok  ", fg=typer.colors.GREEN) if ok else typer.style(
            "FAIL", fg=typer.colors.RED)
        typer.echo(f"[{mark}] {label}" + ("" if ok else f"  ({hint})"))
        failed += not ok
    typer.echo("Live checks (API call, MCP list_tools) arrive in Phase 5.")
    if failed:
        raise typer.Exit(ExitCode.DOCTOR_FAILED)


if __name__ == "__main__":
    app()
