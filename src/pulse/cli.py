"""`pulse` command-line entry point (architecture §4.8).

`pulse run` chains the stages; `ingest`, `analyze` and `publish` run one part from the saved
artifacts of the previous one. Every failure class has its own exit code (`ExitCode`), an
unexpected exception exits 1 with its traceback, and `run` and `publish` end with a short
summary (also written to the GitHub Actions job summary when $GITHUB_STEP_SUMMARY is set).
"""

from __future__ import annotations

import contextlib
import os
import sys
import traceback
from collections.abc import Iterator
from enum import IntEnum
from pathlib import Path
from typing import Annotated

import typer

from pulse import __version__
from pulse.config import ConfigError, PulseConfig, load_config
from pulse.summary import RunSummary, log_safe, on_github_actions
from pulse.weeks import last_completed_week, parse_iso_week


class ExitCode(IntEnum):
    """Kept in step with summary.EXIT_MEANINGS and the README's exit code table."""

    OK = 0
    UNEXPECTED_ERROR = 1  # a bug or an unclassified failure; the traceback is printed
    CONFIG_ERROR = 2
    DOCTOR_FAILED = 4
    INGEST_FAILED = 5     # every review source failed
    STORE_ERROR = 6
    TOO_FEW_REVIEWS = 7
    ANALYSIS_FAILED = 8   # the model's answers could not be made valid (Phase 3)
    LLM_FAILED = 9        # API error, refusal, or truncated response
    VALIDATION_FAILED = 10  # the composed pulse failed §4.6 after retries; nothing published
    PUBLISH_FAILED = 11     # an MCP step failed; publish.json records what succeeded


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
    help="ReviewPulse: weekly insights from Groww app reviews → Google Doc + email.",
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


def _engine_option(value: str | None) -> str | None:
    if value is not None and value not in ("local", "hybrid", "llm"):
        raise typer.BadParameter("must be local, hybrid or llm")
    return value


def _mode_option(value: str | None) -> str | None:
    if value is not None and value not in ("agent", "scripted"):
        raise typer.BadParameter("must be agent or scripted")
    return value


ModeOpt = Annotated[
    str | None,
    typer.Option("--mode", callback=_mode_option,
                 help="How to publish: agent (default: the LLM chooses the tool calls; the "
                      "guardrails stay in code; falls back to scripted) or scripted (the fixed "
                      "order, no model). Overrides publish.mode."),
]

EngineOpt = Annotated[
    str | None,
    typer.Option("--engine", callback=_engine_option,
                 help="hybrid (default: local ML clustering, the LLM writes the pulse; falls "
                      "back to rules), local (ML clustering and rules only, no API), or llm "
                      "(Claude clusters and writes; needs llm.provider: anthropic, paid). "
                      "Overrides analysis.engine."),
]


def _with_engine(cfg: PulseConfig, engine: str | None) -> PulseConfig:
    if engine is None or engine == cfg.analysis.engine:
        return cfg
    from pulse.config import check_engine_provider

    try:
        check_engine_provider(engine, cfg.llm.provider)
    except ValueError as e:
        typer.secho(f"--engine {engine}: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(ExitCode.CONFIG_ERROR) from e
    if engine in ("local", "hybrid") and cfg.analysis.local is None:
        typer.secho(f"--engine {engine} needs an analysis.local section in pulse.yaml",
                    fg=typer.colors.RED, err=True)
        raise typer.Exit(ExitCode.CONFIG_ERROR)
    return cfg.model_copy(update={"analysis": cfg.analysis.model_copy(update={"engine": engine})})


def _load(config: Path | None) -> PulseConfig:
    try:
        return load_config(config)
    except ConfigError as e:
        # A validation error quotes the bad value, so an invalid PULSE_RECIPIENT would appear.
        typer.secho(log_safe(str(e), os.environ.get("PULSE_RECIPIENT")),
                    fg=typer.colors.RED, err=True)
        raise typer.Exit(ExitCode.CONFIG_ERROR) from e


@contextlib.contextmanager
def _guarded(summary: RunSummary, *, show: bool) -> Iterator[None]:
    """Turn an unexpected exception into exit 1 (with its traceback on stderr), and, with
    `show`, end with the run summary whatever happened."""
    code: int = ExitCode.OK
    try:
        yield
    except typer.Exit as e:
        code = e.exit_code
        raise
    except Exception as e:
        code = ExitCode.UNEXPECTED_ERROR
        recipient = summary.recipient or os.environ.get("PULSE_RECIPIENT")
        summary.fail(summary.current_stage, f"unexpected {type(e).__name__}: {e}"[:300])
        typer.secho(log_safe(f"  ✗ unexpected error in {summary.current_stage}: "
                             f"{type(e).__name__}: {e}", recipient),
                    fg=typer.colors.RED, err=True)
        sys.stderr.write(log_safe(traceback.format_exc(), recipient))
        raise typer.Exit(code) from e
    finally:
        if show:
            summary.finish(code)
            typer.echo("")
            typer.echo(summary.render_text())
            summary.write_step_summary()


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
    """ReviewPulse: weekly insights from Groww app reviews."""


@app.command()
def run(
    week: WeekOpt = None,
    weeks_back: Annotated[int | None, typer.Option("--weeks-back", min=8, max=12)] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Run every stage, skip MCP calls.")
    ] = False,
    skip_ingest: Annotated[bool, typer.Option("--skip-ingest")] = False,
    engine: EngineOpt = None,
    mode: ModeOpt = None,
    config: ConfigOpt = None,
) -> None:
    """Run the whole pipeline for one week: ingest, then stages 3–6, then publish.

    With --dry-run every stage runs and the pulse is written to runs/{week}/, but nothing is
    sent to Google Docs or Gmail.

    If one source fails, the run goes on with the other and the pulse says so ("Play Store
    unavailable this week"); it stops if every source fails or fewer than analysis.min_reviews
    reviews are left. The run ends with a summary and a non-zero exit code on any failure.
    """
    summary = RunSummary("run", week)
    with _guarded(summary, show=True):
        cfg = _with_engine(_load(config), engine)
        summary.engine, summary.recipient = cfg.analysis.engine, cfg.publish.recipient
        if weeks_back is not None:
            cfg = cfg.model_copy(update={"window": cfg.window.model_copy(
                update={"weeks_back": weeks_back})})
        if skip_ingest:
            typer.echo("Ingest skipped (--skip-ingest): using data/reviews.parquet as it is.")
        else:
            report = _ingest_stage(cfg, week, full=False, sources=None, summary=summary)
            if report.all_failed:   # §10 row 1: no source answered, so there is nothing new
                summary.fail("ingest", "every review source failed")
                typer.secho("  ✗ every review source failed; nothing to analyse",
                            fg=typer.colors.RED, err=True)
                raise typer.Exit(ExitCode.INGEST_FAILED)
        _analysis_stages(cfg, week, summary)
        if dry_run:
            summary.publish = "dry run"
            summary.publish_detail = "nothing was published"
            typer.secho("Dry run: nothing was published.", fg=typer.colors.GREEN)
            return
        _publish_stage(cfg, week, summary=summary, mode=mode)


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
    summary = RunSummary("ingest", week)
    with _guarded(summary, show=False):
        cfg = _load(config)
        report = _ingest_stage(cfg, week, full=full,
                               sources=None if source == "all" else [source], summary=summary)
        if report.all_failed:
            raise typer.Exit(ExitCode.INGEST_FAILED)


def _ingest_stage(cfg: PulseConfig, week: str, *, full: bool, sources: list[str] | None,
                  summary: RunSummary):
    """Stages 1–2: fetch, normalize, upsert into data/reviews.parquet, and report."""
    from pulse.ingest.normalize import DROP_RULES
    from pulse.ingest.runner import run_ingest
    from pulse.ingest.store import StoreSchemaError

    summary.current_stage = "ingest"
    try:
        report = run_ingest(cfg, week=week, full=full, sources=sources)
    except StoreSchemaError as e:
        summary.fail("ingest", str(e))
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(ExitCode.STORE_ERROR) from e
    for s in report.sources:
        summary.sources[s.name] = {"status": s.status, "fetched": s.fetched,
                                   "in_window": report.in_window.get(s.name),
                                   "error": s.error}

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
def analyze(week: WeekOpt = None, engine: EngineOpt = None, config: ConfigOpt = None) -> None:
    """Stages 3–6 only (no publishing): scrub, cluster, compose, validate."""
    summary = RunSummary("analyze", week)
    with _guarded(summary, show=False):
        _analysis_stages(_with_engine(_load(config), engine), week, summary)


def _sources_from_manifest(cfg: PulseConfig, week: str) -> dict[str, dict]:
    """The week's last ingest, per source, from runs/{week}/manifest.json (empty if none)."""
    from pulse.manifest import read_manifest

    ingest = read_manifest(cfg.paths.runs_path, week).get("ingest") or {}
    in_window = ingest.get("in_window") or {}
    return {name: {"status": s.get("status"), "fetched": s.get("fetched", 0),
                   "in_window": in_window.get(name), "error": s.get("error")}
            for name, s in (ingest.get("sources") or {}).items() if isinstance(s, dict)}


def _analysis_stages(cfg: PulseConfig, week: str, summary: RunSummary) -> None:
    from pulse.ingest.normalize import in_window
    from pulse.ingest.store import ReviewStore, StoreSchemaError
    from pulse.manifest import update_manifest
    from pulse.privacy.scrub import scrub_all, write_clean_reviews
    from pulse.render.markdown import unavailable_note
    from pulse.weeks import review_window

    summary.current_stage = "analyze"
    summary.engine = cfg.analysis.engine
    if not summary.sources:       # --skip-ingest or `pulse analyze`: the week's last ingest
        summary.sources = _sources_from_manifest(cfg, week)
    unavailable = summary.unavailable
    start, end = review_window(week, cfg.window.weeks_back)
    try:
        stored = ReviewStore(cfg.paths.data_path / "reviews.parquet").load()
    except StoreSchemaError as e:
        summary.fail("analyze", str(e))
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(ExitCode.STORE_ERROR) from e
    reviews = in_window(stored.values(), start, end)
    summary.reviews, summary.min_reviews = len(reviews), cfg.analysis.min_reviews
    typer.echo(f"Analyze {week} · window {start} – {end} · {len(reviews)} reviews")
    if unavailable:
        typer.secho(f"  ! {unavailable_note(unavailable)}: the pulse says so in its source "
                    "line", fg=typer.colors.YELLOW)
    if len(reviews) < cfg.analysis.min_reviews:  # §10 row 1
        why = (f"{unavailable_note(unavailable)} and only {len(reviews)} reviews are left"
               if unavailable else f"only {len(reviews)} reviews in the window")
        summary.fail("analyze", f"{why}; analysis needs at least {cfg.analysis.min_reviews} "
                     "(analysis.min_reviews)")
        typer.secho(f"  ✗ fewer than analysis.min_reviews={cfg.analysis.min_reviews} reviews"
                    + (f" ({unavailable_note(unavailable)})" if unavailable
                       else "; run `pulse ingest` first"), fg=typer.colors.RED, err=True)
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
    from pulse.analysis.catalogue import CatalogueError, load_catalogue

    llm = catalogue = llm_error = None
    if cfg.analysis.engine == "llm":
        llm = llm_mod.build_llm(cfg.llm, log_dir=cfg.paths.runs_path / week / "llm")
    else:
        try:
            catalogue = load_catalogue(cfg.analysis.catalogue_path)
        except CatalogueError as e:
            summary.fail("analyze", str(e).splitlines()[0])
            typer.secho(str(e), fg=typer.colors.RED, err=True)
            raise typer.Exit(ExitCode.CONFIG_ERROR) from e
    if cfg.analysis.engine == "hybrid":
        # The model is only used for writing; without it (no key) the rules write the pulse.
        try:
            llm = llm_mod.build_llm(cfg.llm, log_dir=cfg.paths.runs_path / week / "llm")
        except llm_mod.LlmError as e:
            llm_error = e
    # Clustering is always the local ML for local and hybrid: the model never sees it.
    cluster = _cluster_stage(cfg, week, (start, end), result.reviews,
                             llm if cfg.analysis.engine == "llm" else None, catalogue, summary)
    _compose_stage(cfg, week, cluster, result.reviews, llm, catalogue, summary,
                   unavailable=unavailable, llm_error=llm_error)


def _cluster_stage(cfg: PulseConfig, week: str, window, reviews, llm, catalogue,
                   summary: RunSummary):
    """Stage 4: theme clustering and ranking → runs/{week}/themes.json (§4.4)."""
    import time

    from pulse.analysis.cluster import ClusterError, manifest_section, write_themes
    from pulse.analysis.llm import LlmError
    from pulse.analysis.rank import GENERAL_PRAISE, OTHER, RankError
    from pulse.manifest import update_manifest

    runs = cfg.paths.runs_path
    engine = cfg.analysis.engine
    summary.current_stage = "cluster"
    started = time.monotonic()
    try:
        if engine == "llm":
            from pulse.analysis.engines.llm_cluster import run_llm_clustering

            typer.echo(f"  Theme clustering with {cfg.llm.model} ({len(reviews)} reviews)…")
            result = run_llm_clustering(llm, reviews, cfg, week=week, window=window)
        else:
            from pulse.analysis.engines import local_cluster

            typer.echo(f"  Theme clustering, local engine ({cfg.analysis.local.model}, "
                       f"{len(reviews)} reviews)…")
            embedder = local_cluster.build_embedder(cfg)
            result = local_cluster.run_local_clustering(
                reviews, cfg, week=week, window=window, embedder=embedder, catalogue=catalogue)
    except (LlmError, ClusterError, RankError) as e:
        code = ExitCode.LLM_FAILED if isinstance(e, LlmError) else ExitCode.ANALYSIS_FAILED
        update_manifest(runs, week, "cluster", {
            "status": "failed", "engine": engine, "error": str(e),
            "kind": getattr(e, "kind", type(e).__name__),
            "refusal_category": getattr(e, "category", None),
            **({"usage": llm.usage.to_dict()} if llm is not None else {}),
        })
        summary.fail("cluster", str(e))
        typer.secho(f"  ✗ {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code) from e

    path = write_themes(result, runs)
    section = manifest_section(result)
    section["seconds"] = round(time.monotonic() - started, 1)
    update_manifest(runs, week, "cluster", section)

    labels = {t.id: t.label for t in result.themes}
    summary.themes = [labels[t] for t in result.top if t in labels]
    summary.warnings += [w["message"] for w in result.warnings]
    for s in result.ranked:
        star = "★" if s.theme_id in result.top else " "
        typer.echo(f"   {star} {s.rank}. {labels[s.theme_id]:<28} {s.volume_share:6.1%}  "
                   f"neg {s.negative_share:4.0%}  recency ×{s.recency_weight:.2f}  "
                   f"score {s.score:.3f}")
    b = result.buckets
    typer.echo(f"     general praise {b[GENERAL_PRAISE].volume_share:.1%} · "
               f"other {b[OTHER].volume_share:.1%}"
               + (" · re-clustered once" if result.rediscovered else ""))
    probe = result.quality.get("consistency", {})
    if probe.get("agreement") is not None:
        typer.echo(f"     consistency probe: {probe['agreement']:.0%} of {probe['sample']} agree")
    stability = result.quality.get("stability", {})
    if stability.get("median_ari") is not None:
        typer.echo(f"     {len(result.themes)} themes · stability (median ARI vs 3 other seeds) "
                   f"{stability['median_ari']:.2f}")
    for w in result.warnings:
        typer.secho(f"     ! {w['message']}", fg=typer.colors.YELLOW)
    if engine == "llm":
        u = result.usage
        typer.echo(f"     {u.calls} calls · {u.input_tokens:,} in / {u.output_tokens:,} out "
                   f"tokens (cache read {u.cache_read_tokens:,}) · ≈${u.estimated_usd:.2f} · "
                   f"{section['seconds']}s")
    else:
        typer.echo(f"     no API calls · {section['seconds']}s")
    typer.echo(f"  wrote {path}")
    return result


def _compose_stage(cfg: PulseConfig, week: str, cluster, reviews, llm, catalogue,
                   summary: RunSummary, *, unavailable: list[str],
                   llm_error: Exception | None = None) -> None:
    """Stages 5–6: compose quotes, summaries and actions; validate; render (§4.5, §4.6, §7).

    hybrid: the LLM writes on top of the ML results. If the model fails (no key, rate limit
    after retries, bad output, or a pulse still failing validation after the rewrites), the
    rules write the pulse instead and the manifest and run summary say why."""
    import time

    from pulse.analysis.compose import (
        HYBRID_PROMPTS,
        LLM_PROMPTS,
        ComposeError,
        write_compose_artifacts,
        write_failure,
    )
    from pulse.analysis.llm import LlmError, Usage
    from pulse.manifest import update_manifest

    runs = cfg.paths.runs_path
    engine = cfg.analysis.engine
    summary.current_stage = "compose"
    before = Usage()
    if llm is not None:
        before.add(llm.usage)
    how = {"local": " by rules and templates…",
           "hybrid": f" with {cfg.llm.provider} {cfg.llm.model} on the ML results…"}
    typer.echo("  Composing the pulse (quotes, summaries, actions)" + how.get(engine, "…"))
    started = time.monotonic()
    try:
        if engine == "llm":
            from pulse.analysis.engines.llm_compose import compose_pulse_llm

            result = compose_pulse_llm(llm, cluster, reviews, cfg)
        elif engine == "hybrid":
            result = _compose_hybrid(cfg, cluster, reviews, llm, llm_error, catalogue)
        else:
            from pulse.analysis.engines.rules_compose import compose_pulse_rules

            result = compose_pulse_rules(cluster, reviews, cfg, catalogue)
    except (ComposeError, LlmError) as e:
        attempts = getattr(e, "attempts", [])
        path = write_failure(runs, week, stage="compose", error=str(e), attempts=attempts)
        update_manifest(runs, week, "compose", {
            "status": "failed", "engine": engine, "error": str(e), "attempts": len(attempts),
            "kind": getattr(e, "kind", type(e).__name__),
            "refusal_category": getattr(e, "category", None),
            **({"usage": _usage_since(llm.usage, before)} if llm is not None else {}),
        })
        summary.fail("compose", str(e))
        typer.secho(f"  ✗ {e}", fg=typer.colors.RED, err=True)
        typer.secho(f"  nothing was published; see {path}", fg=typer.colors.RED, err=True)
        raise typer.Exit(ExitCode.LLM_FAILED if isinstance(e, LlmError)
                         else ExitCode.VALIDATION_FAILED) from e

    result.pulse.unavailable_sources = list(unavailable)   # named in the source line
    paths = write_compose_artifacts(result, cfg, runs)
    summary.word_count, summary.max_words = result.report.word_count, cfg.analysis.max_words
    seconds = round(time.monotonic() - started, 1)
    prompts = HYBRID_PROMPTS if engine == "hybrid" else LLM_PROMPTS
    update_manifest(runs, week, "compose", {
        "status": "ok", "engine": engine, "composer": result.composer,
        **({"fallback": result.fallback} if result.fallback else {}),
        "attempts": len(result.attempts),
        "word_count": result.report.word_count, "max_words": cfg.analysis.max_words,
        **({"prompt_versions": prompts, "usage": _usage_since(llm.usage, before)}
           if llm is not None else {}),
        "seconds": seconds,
    })
    summary.compose = _compose_label(cfg, result)
    (runs / week / "failure.json").unlink(missing_ok=True)  # a stale failure from earlier
    typer.echo(f"     passed every check on attempt {len(result.attempts)} · "
               f"{result.report.word_count}/{cfg.analysis.max_words} words · {seconds}s")
    if llm is not None and engine == "hybrid":
        u = _usage_since(llm.usage, before)
        typer.echo(f"     {u['calls']} model calls · {u['input_tokens']:,} in / "
                   f"{u['output_tokens']:,} out tokens · {u['rate_limit_waits']} rate-limit "
                   f"waits · ≈${u['estimated_usd']:.2f} (per call: runs/{week}/llm/)")
    typer.echo(f"  wrote {paths['pulse_md']}, {paths['pulse_json'].name}, "
               f"{paths['validation'].name}")
    typer.echo("")
    if on_github_actions():   # public logs: the pulse quotes reviews; the summary is enough
        typer.echo("  (the pulse text is not printed on GitHub Actions; see the run summary)")
    else:
        typer.echo(paths["pulse_md"].read_text(encoding="utf-8"))


FALLBACK_REASONS = {"rate_limit": "rate_limited", "auth": "no_key_or_bad_key",
                    "schema": "bad_output", "max_tokens": "bad_output",
                    "connection": "unreachable", "api_status": "api_error", "refusal": "refused",
                    "missing_dependency": "missing_dependency"}


def _fallback_reason(e: Exception) -> str:
    from pulse.analysis.compose import ComposeError

    if isinstance(e, ComposeError):
        return "validation_failed"
    return FALLBACK_REASONS.get(getattr(e, "kind", ""), "llm_error")


def _compose_hybrid(cfg: PulseConfig, cluster, reviews, llm, llm_error, catalogue):
    """The LLM writes on top of the ML results; on any model failure the rules write instead
    (so the week still goes out). A rules failure is raised as usual."""
    from pulse.analysis.compose import ComposeError
    from pulse.analysis.engines.hybrid_compose import compose_pulse_hybrid
    from pulse.analysis.engines.rules_compose import compose_pulse_rules
    from pulse.analysis.llm import LlmError

    error = llm_error
    if llm is not None:
        try:
            return compose_pulse_hybrid(llm, cluster, reviews, cfg, catalogue)
        except (LlmError, ComposeError) as e:
            error = e
    reason = _fallback_reason(error) if error else "no_model"
    typer.secho(f"     ! the LLM could not write the pulse ({reason}: {str(error)[:200]}); "
                "the rules write it instead", fg=typer.colors.YELLOW)
    result = compose_pulse_rules(cluster, reviews, cfg, catalogue)
    result.engine = "hybrid"
    result.fallback = {"reason": reason, "error": str(error)[:300],
                       "llm_attempts": getattr(error, "attempts", [])}
    return result


def _compose_label(cfg: PulseConfig, result) -> str:
    """'llm (groq openai/gpt-oss-120b, attempt 1)' or 'rules (fallback: rate_limited)'."""
    if result.fallback:
        return f"rules (fallback: {result.fallback['reason']})"
    if result.composer == "llm":
        return f"llm ({cfg.llm.provider} {cfg.llm.model}, attempt {len(result.attempts)})"
    return "rules"


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
        provider=now.provider,
        rate_limit_waits=now.rate_limit_waits - before.rate_limit_waits,
    )
    return delta.to_dict()


ForceOpt = Annotated[bool, typer.Option(
    "--force", help="Deliberately re-publish: append the entry and send the email again.")]
EmailOnlyOpt = Annotated[bool, typer.Option(
    "--email-only", help="Send only the email, for a week whose entry is already in the Doc "
                         "(the email failed after the append, e.g. on GitHub Actions). Never "
                         "appends; refuses if the Doc has no entry for the week.")]


@app.command()
def publish(week: WeekOpt = None, force: ForceOpt = False, email_only: EmailOnlyOpt = False,
            mode: ModeOpt = None, config: ConfigOpt = None) -> None:
    """Stage 7 from saved run artifacts: append the week to the Google Doc (PULSE_DOC_ID), then
    email it to PULSE_RECIPIENT. A week already published is skipped unless --force."""
    if force and email_only:
        raise typer.BadParameter("use either --force or --email-only, not both",
                                 param_hint="--email-only")
    summary = RunSummary("publish", week)
    with _guarded(summary, show=True):
        cfg = _load(config)
        summary.recipient = cfg.publish.recipient
        _publish_stage(cfg, week, force=force, email_only=email_only, summary=summary,
                       mode=mode)


def _publish_agent_llm(cfg: PulseConfig, week: str):
    """The model for the publishing agent, or (None, why) when there is none to use."""
    from pulse.analysis import llm as llm_mod

    if cfg.llm.provider != "groq":
        return None, "agent_needs_groq"     # the agent uses Groq's tool calling
    try:
        llm = llm_mod.build_llm(cfg.llm, log_dir=cfg.paths.runs_path / week / "llm")
    except llm_mod.LlmError as e:
        return None, _fallback_reason(e)
    return llm, None


def _publish_stage(cfg: PulseConfig, week: str, *, summary: RunSummary, force: bool = False,
                   email_only: bool = False, mode: str | None = None) -> None:
    from pulse.manifest import update_manifest
    from pulse.publish.agent import publish_week_agent
    from pulse.publish.runner import PublishError, publish_path, publish_week

    summary.current_stage = "publish"
    recipient = summary.recipient = cfg.publish.recipient

    def shown(text: str) -> str:
        """On GitHub Actions (public logs) the recipient's address is never printed, even
        where a server error quotes it."""
        return log_safe(text, recipient)

    what = "send only the email to" if email_only else "append to the Google Doc, then email"
    typer.echo(shown(f"Publishing {week} through MCP ({what} "
                     f"{recipient or 'PULSE_RECIPIENT'})…"))
    mode = mode or cfg.publish.mode

    def echo(line: str) -> None:
        typer.echo(shown(line))

    llm, fallback = _publish_agent_llm(cfg, week) if mode == "agent" else (None, None)
    if mode == "agent" and llm is None:
        typer.secho(f"  ! the publishing agent is not available ({fallback}); the scripted "
                    "publisher takes over", fg=typer.colors.YELLOW)
    used = "agent" if llm is not None else "scripted"
    try:
        if llm is not None:
            typer.echo(f"  The publishing agent ({cfg.llm.provider} {cfg.llm.model}) chooses "
                       f"the tool calls; at most {cfg.publish.agent.max_steps} steps.")
            result = publish_week_agent(cfg, week, llm, force=force, email_only=email_only,
                                        echo=echo)
            record, used, fallback = result.record, result.mode, result.fallback
        else:
            record = publish_week(cfg, week, force=force, email_only=email_only, echo=echo)
    except PublishError as e:
        _note_publish_mode(cfg, week, summary, used, fallback)
        if e.record:
            update_manifest(cfg.paths.runs_path, week, "publish",
                            {**_publish_summary(e.record), "mode": used,
                             **({"fallback": fallback} if fallback else {})})
        hint = _publish_hint(e, week)
        summary.fail("publish", str(e))     # redacted there: the summary never has it
        summary.publish = e.record.get("status") or "failed"
        summary.publish_detail = hint
        typer.secho(shown(f"  ✗ {e}"), fg=typer.colors.RED, err=True)
        if e.record:
            typer.secho(f"  {hint}; see {publish_path(cfg.paths.runs_path, week)}",
                        fg=typer.colors.RED, err=True)
        raise typer.Exit(ExitCode.CONFIG_ERROR if e.setup and not e.record
                         else ExitCode.PUBLISH_FAILED) from e
    _note_publish_mode(cfg, week, summary, used, fallback)
    update_manifest(cfg.paths.runs_path, week, "publish",
                    {**_publish_summary(record), "mode": used,
                     **({"fallback": fallback} if fallback else {})})
    retries = len(record.get("retries", []))
    retried = f", after {retries} {'retry' if retries == 1 else 'retries'}" if retries else ""
    summary.publish = record["status"]
    if record["status"] == "skipped":       # not an error: the week is already out
        summary.publish_detail = record.get("skip_reason", "already published") + retried
        typer.secho(f"Skipped {week}: {record.get('skip_reason', 'already published')} "
                    f"{record['doc_url']}", fg=typer.colors.YELLOW)
        return
    summary.publish_detail = ("email only" if email_only
                              else "Doc entry appended, email sent") + retried
    typer.secho(f"Published {week}: {record['doc_url']}", fg=typer.colors.GREEN)


def _note_publish_mode(cfg: PulseConfig, week: str, summary: RunSummary, used: str,
                       fallback: str | None) -> None:
    """The run summary's publish mode and, for the agent, its steps from agent_log.json."""
    import json

    if used != "agent":
        summary.publish_mode = f"scripted (fallback: {fallback})" if fallback else "scripted"
        return
    try:
        log = json.loads((cfg.paths.runs_path / week / "agent_log.json")
                         .read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        log = {}
    steps = [s for s in log.get("steps", []) if s.get("tool")]
    summary.publish_mode = (f"agent ({cfg.llm.provider} {cfg.llm.model}, {len(steps)} tool "
                            f"calls, {log.get('tokens', 0):,} tokens)")
    summary.agent_steps = [
        f"{s['tool']} → {s['status']}: {s['result']}"
        + (f" · “{s['reason']}”" if s.get("reason") else "") for s in steps]


def _publish_hint(e, week: str) -> str:
    """What to do after a failed publish. A tool error comes from the server itself, so
    running again only helps after the server (or its Google access or settings) is fixed.
    Once the entry is in the Doc, the way on is `--email-only`: it works on GitHub Actions too,
    where publish.json is not kept."""
    if e.hint:
        return e.hint
    appended = e.step in ("email_setup", "send")
    done = {"connect": "nothing was checked, appended or sent",
            "check": "nothing was appended or sent",
            "append": "nothing was appended or sent",
            "email_setup": "the Doc entry was appended, but no email was sent",
            "send": "the Doc entry was appended, but no email was sent"}.get(e.step, "")
    done = f" ({done})" if done else ""
    command = f"`pulse publish --week {week}" + (" --email-only`" if appended else "`")
    if e.server_tool:
        return (f"the MCP server's {e.server_tool!r} tool returned an error{done}: the server "
                "(or its Google access or settings) needs fixing; running again will not help "
                f"until then. After the fix, run {command}")
    if e.setup:
        return (f"the MCP server does not offer the tools in tool_map.yaml{done}: fix the "
                f"server or the tool map, then run {command}")
    if appended:
        return f"run {command} to send the email without appending again{done}"
    return f"run {command} again to resume{done}"


def _publish_summary(record: dict) -> dict:
    """The manifest's publish section: statuses and IDs, no email body or recipient."""
    doc, email = record.get("doc", {}), record.get("email", {})
    return {"status": record.get("status"), "servers": record.get("servers"),
            "doc_id": record.get("doc_id"), "doc_url": record.get("doc_url"),
            "duplicate_check": record.get("duplicate_check"),
            "doc": {k: doc.get(k) for k in ("status", "action")},
            "email": {k: email.get(k) for k in ("status", "message_id")},
            "calls": len(record.get("calls", []))}


def _mcp_live_checks(cfg: PulseConfig, warnings: list[str], *,
                     live: bool) -> list[tuple[str, bool, str]]:
    """Tool map validity, then (live) health, connection and tool coverage for each server."""
    import anyio

    from pulse.publish import mcp_client as mc

    try:
        tool_map = mc.load_tool_map(cfg.mcp.tool_map_path)
    except mc.McpSetupError as e:
        return [("tool map is valid (send only to PULSE_RECIPIENT, never create_doc)", False,
                 str(e).splitlines()[0])]
    out = [("tool map is valid (send only to PULSE_RECIPIENT, never create_doc)", True, "")]
    if not live:
        return out
    servers = {"docs": cfg.mcp.docs}
    if cfg.mcp.gmail != cfg.mcp.docs:
        servers["gmail"] = cfg.mcp.gmail
    for name, server in servers.items():
        label = "docs + gmail" if len(servers) == 1 else name
        if not server.configured:
            continue
        if server.health_url:
            try:
                waited = mc.wait_until_healthy(server)
            except mc.McpSetupError as e:
                out.append((f"MCP server is up ({server.health_url})", False, str(e)))
                continue
            out.append((f"MCP server is up ({server.health_url})", True, ""))
            if waited:
                warnings.append(f"the MCP server took {waited}s to wake up")
        if server.missing_headers:
            continue          # reported above; list_tools needs the key
        try:
            async def tools(server=server):
                async with mc.open_client(server) as client:
                    return (await client.list_tools()).tools

            listed = anyio.run(tools)
        except BaseException as exc:  # noqa: BLE001 — task groups wrap the real error
            e = mc.mcp_error(exc)
            if e is None and not isinstance(exc, Exception):
                raise
            out.append((f"MCP server '{label}' answers list_tools", False, str(e or exc)))
            continue
        out.append((f"MCP server '{label}' answers list_tools ({len(listed)} tools)", True, ""))
        for srv in (["docs", "gmail"] if len(servers) == 1 else [name]):
            problems = mc.check_tools(tool_map, srv, listed)
            out.append((f"every mapped {srv} tool exists with matching arguments",
                        not problems, "; ".join(problems)))
        names = {t.name for t in listed}
        check = tool_map.spec("docs", "doc_contains_text").tool
        out.append((f"duplicate check tool {check!r} is available", check in names,
                    "the server must offer it: without it nothing is published"))
        unmapped = sorted(n for n in names if "send" in n.lower()
                          and n != tool_map.spec("gmail", "send_email").tool)
        if unmapped:
            warnings.append(f"the server also offers {', '.join(unmapped)}; it is not mapped "
                            "and is never called")
    return out


def _groq_live_check(cfg: PulseConfig, warnings: list[str]) -> list[tuple[str, bool, str]]:
    """One tiny structured call (a fixed prompt, no review text) to prove the key works."""
    from pydantic import BaseModel

    from pulse.analysis import llm as llm_mod

    class Ping(BaseModel):
        ok: bool

    label = f"Groq answers with this key ({cfg.llm.groq.model})"
    try:
        client = llm_mod.build_llm(cfg.llm)
        answer = client.parse(name="doctor_ping", system="Reply with JSON only.", prefix=None,
                              user='Return {"ok": true}.', schema=Ping, effort="low",
                              max_tokens=512)
    except llm_mod.LlmError as e:
        if e.kind == "rate_limit":   # the key works; the free tier is busy right now
            warnings.append(f"Groq is rate-limiting this key right now: {e}")
            return [(label, True, "")]
        hints = {"auth": "check GROQ_API_KEY in .env (console.groq.com/keys)",
                 "connection": "no network to api.groq.com"}
        return [(label, False, hints.get(e.kind, str(e)[:200]))]
    u = client.usage
    return [(f"{label}: {u.input_tokens + u.output_tokens} tokens, {u.seconds:.1f}s",
             answer.ok, "the model did not return ok: true")]


@app.command()
def doctor(
    config: ConfigOpt = None,
    live: Annotated[bool, typer.Option(
        "--live/--offline",
        help="Reach the MCP server and check its tools, and test the Groq key with one tiny "
             "call.")] = True,
) -> None:
    """Check config, credentials, and MCP setup. With --live (the default) it also reaches the
    MCP server and checks its tools, and makes one tiny Groq call to test GROQ_API_KEY."""
    cfg = _load(config)
    checks: list[tuple[str, bool, str]] = [("config/pulse.yaml is valid", True, "")]
    warnings: list[str] = []

    checks.append((f"analysis engine: {cfg.analysis.engine}", True, ""))
    checks.append((f"LLM provider: {cfg.llm.provider} ({cfg.llm.model})", True, ""))
    if cfg.llm.provider == "groq" and not os.environ.get("GROQ_API_KEY"):
        # Not a failure: every Groq step falls back to rules or the scripted publisher.
        warnings.append("GROQ_API_KEY is not set: the LLM steps will fall back to rules and "
                        "the scripted publisher (free key at console.groq.com/keys)")
    elif cfg.llm.provider == "groq" and live:
        checks.extend(_groq_live_check(cfg, warnings))
    if cfg.analysis.engine == "llm":
        has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
        checks.append(("ANTHROPIC_API_KEY is set (LLM engine)", has_key,
                       "set it in .env, or use an `ant auth login` profile"))
    else:
        from pulse.analysis.catalogue import CatalogueError, load_catalogue
        from pulse.analysis.engines.local_cluster import model_is_cached

        try:
            load_catalogue(cfg.analysis.catalogue_path)
            catalogue_ok, catalogue_hint = True, ""
        except CatalogueError as e:
            catalogue_ok, catalogue_hint = False, str(e).splitlines()[0]
        checks.append(("theme catalogue is valid", catalogue_ok, catalogue_hint))
        if not model_is_cached(cfg):
            warnings.append("embedding model is not cached yet: the first `pulse analyze` "
                            "downloads it (about 30 MB) into data/models/")
    checks.append(("PULSE_RECIPIENT is set", cfg.publish.recipient is not None,
                   "set one address in .env"))
    checks.append(("PULSE_DOC_ID is set", cfg.publish.doc_id is not None,
                   "create the Google Doc by hand once and put its ID in .env"))
    for name, server in (("docs", cfg.mcp.docs), ("gmail", cfg.mcp.gmail)):
        checks.append((f"MCP server '{name}' is configured", server.configured,
                       f"set mcp.{name} in pulse.yaml"))
        if server.missing_headers:
            checks.append((f"MCP '{name}' credentials are set", False,
                           "set PULSE_MCP_API_KEY in .env"))
    checks.extend(_mcp_live_checks(cfg, warnings, live=live))

    failed = 0
    for label, ok, hint in checks:
        mark = typer.style("ok  ", fg=typer.colors.GREEN) if ok else typer.style(
            "FAIL", fg=typer.colors.RED)
        typer.echo(log_safe(f"[{mark}] {label}" + ("" if ok else f"  ({hint})"),
                            cfg.publish.recipient))
        failed += not ok
    for w in warnings:
        typer.echo(log_safe(f"[{typer.style('warn', fg=typer.colors.YELLOW)}] {w}",
                            cfg.publish.recipient))
    if failed:
        raise typer.Exit(ExitCode.DOCTOR_FAILED)


@app.command("export-site")
def export_site(
    week: WeekOpt = None,
    config: ConfigOpt = None,
    out: Annotated[Path | None, typer.Option(
        "--out", help="Where to write the site JSON (default: site/pulses).")] = None,
) -> None:
    """Write the week's validated pulse (themes, 3 quotes, 3 actions, counts) to site/pulses/
    for the static website, and add it to site/pulses/index.json. Never copies raw reviews."""
    from pulse.site_export import (
        DEFAULT_SITE_PULSES,
        ExportRefusedError,
        MissingPulseError,
        export_week,
    )

    cfg = _load(config)
    try:
        target = export_week(cfg, week, out or DEFAULT_SITE_PULSES)
    except MissingPulseError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(ExitCode.CONFIG_ERROR) from e
    except ExportRefusedError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(ExitCode.VALIDATION_FAILED) from e
    typer.echo(f"Wrote {target} and updated {target.parent / 'index.json'}")


if __name__ == "__main__":
    app()
