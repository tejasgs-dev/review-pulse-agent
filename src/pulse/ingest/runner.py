"""`pulse ingest`: fetch every requested source, normalize, and upsert into the store.

Ingest is incremental. The first run for a source backfills the whole review window;
later runs fetch only from the end of the last complete fetch, minus `overlap_days`
to pick up edited reviews. `full=True` forces a whole-window fetch.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from pulse.config import PulseConfig
from pulse.ingest.app_store_rss import AppStoreRssSource
from pulse.ingest.base import ReviewSource, SourceError
from pulse.ingest.normalize import KEEP_LANGUAGE, SourceStats, in_window, normalize
from pulse.ingest.play_file import PlayStoreFileSource
from pulse.ingest.store import (
    Coverage,
    IngestState,
    ReviewStore,
    UpsertStats,
    new_run_id,
    save_raw,
)
from pulse.manifest import update_manifest
from pulse.models import SOURCES
from pulse.weeks import IST, review_window


@dataclass
class SourceReport:
    name: str
    status: str = "ok"            # ok | partial | failed
    mode: str = "full"            # full | incremental
    since: date | None = None     # the date this run fetched back to
    fetched: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    error: str | None = None
    raw_path: Path | None = None
    cleaning: SourceStats = field(default_factory=SourceStats)  # rule counts


@dataclass
class IngestReport:
    run_id: str
    week: str
    window: tuple[date, date]
    sources: list[SourceReport]
    upsert: UpsertStats
    in_window: dict[str, int]       # per source, cleaned reviews in the window
    store_path: Path
    manifest_path: Path | None = None

    @property
    def total_in_window(self) -> int:
        return sum(self.in_window.values())

    @property
    def all_failed(self) -> bool:
        return all(s.status == "failed" for s in self.sources)


def build_sources(cfg: PulseConfig, data_dir: Path) -> dict[str, ReviewSource]:
    ing = cfg.ingest
    play: ReviewSource
    if ing.play_public.enabled:
        from pulse.ingest.play_public import PlayStorePublicSource  # imports the scraper

        pp = ing.play_public
        play = PlayStorePublicSource(
            cfg.product.play_package, langs=pp.langs, country=pp.country,
            user_agent=ing.user_agent, delay_seconds=ing.request_delay_seconds,
            batch_size=pp.batch_size, max_reviews=pp.max_reviews, max_retries=ing.max_retries,
        )
    else:
        play = PlayStoreFileSource(
            data_dir / "imports" / "play", ing.play_columns,
            max_malformed_ratio=ing.max_malformed_ratio,
        )
    return {
        "play_store": play,
        "app_store": AppStoreRssSource(
            cfg.product.app_store_id, cfg.product.app_store_country,
            user_agent=ing.user_agent, delay_seconds=ing.request_delay_seconds,
            max_pages=ing.app_store_max_pages, max_retries=ing.max_retries,
        ),
    }


def _plan_since(
    cov: Coverage | None, has_rows: bool, window_start: date, overlap_days: int, full: bool
) -> tuple[str, date]:
    """Decide how far back to fetch: the whole window, or only since the last complete fetch."""
    if full or cov is None or not has_rows:
        return "full", window_start
    if cov.since > window_start or cov.until < window_start:
        return "full", window_start  # coverage does not reach back to this window
    return "incremental", max(window_start, cov.until - timedelta(days=overlap_days))


def run_ingest(
    cfg: PulseConfig,
    *,
    week: str,
    sources: list[str] | None = None,
    data_dir: Path | None = None,
    now: datetime | None = None,
    full: bool = False,
    runs_dir: Path | None = None,
    source_factory: Callable[[PulseConfig, Path], Mapping[str, ReviewSource]] | None = None,
) -> IngestReport:
    data_dir = data_dir or cfg.paths.data_path
    names = sources or list(SOURCES)
    start, end = review_window(week, cfg.window.weeks_back)
    today = (now or datetime.now(IST)).astimezone(IST).date()
    run_id = new_run_id(now)
    available = (source_factory or build_sources)(cfg, data_dir)

    store = ReviewStore(data_dir / "reviews.parquet")
    rows_per_source = Counter(r.source for r in store.load().values())
    state = IngestState(data_dir / "ingest_state.json")
    coverage = state.load()

    reports: list[SourceReport] = []
    fetched = []
    for name in names:
        mode, since = _plan_since(coverage.get(name), rows_per_source[name] > 0, start,
                                  cfg.ingest.overlap_days, full)
        report = SourceReport(name=name, mode=mode, since=since)
        reports.append(report)
        try:
            result = available[name].fetch(since, end)
        except SourceError as e:  # §10 row 1: continue with the other sources
            report.status, report.error = "failed", str(e)
            continue
        report.fetched = len(result)
        report.skipped = dict(result.skipped)
        report.warnings = list(result.warnings)
        if not result.complete:
            report.status = "partial"
        if len(result):
            report.raw_path = save_raw(result, data_dir, name, run_id)
        fetched.extend(result)
        if result.complete and result.covered_since is not None:
            prev = coverage.get(name)
            first = prev.since if (mode == "incremental" and prev) else result.covered_since
            coverage[name] = Coverage(since=first, until=today)

    cleaned = normalize(fetched, today=today, min_words=cfg.ingest.min_words)
    for report in reports:
        report.cleaning = cleaned.stats.get(report.name, SourceStats())
    upsert = store.upsert(cleaned.reviews, cleaned.rejected)
    state.save(coverage)  # after the store write, so coverage never claims unsaved reviews

    window_reviews = in_window(store.load().values(), start, end)
    counts = Counter(r.source for r in window_reviews)
    report = IngestReport(
        run_id=run_id, week=week, window=(start, end), sources=reports, upsert=upsert,
        in_window={s: counts.get(s, 0) for s in SOURCES}, store_path=store.path,
    )
    report.manifest_path = update_manifest(runs_dir or cfg.paths.runs_path, week, "ingest",
                                           manifest_section(report, cfg))
    return report


def manifest_section(report: IngestReport, cfg: PulseConfig) -> dict:
    """The `ingest` section of runs/{week}/manifest.json."""
    return {
        "run_id": report.run_id,
        "at": datetime.now(IST).isoformat(timespec="seconds"),
        "window": [report.window[0].isoformat(), report.window[1].isoformat()],
        "rules": {"strip_emojis": True, "min_words": cfg.ingest.min_words,
                  "language": KEEP_LANGUAGE},
        "sources": {
            s.name: {
                "status": s.status, "mode": s.mode,
                "since": s.since.isoformat() if s.since else None,
                "fetched": s.fetched, "skipped_by_adapter": s.skipped,
                "warnings": s.warnings, "error": s.error,
                **s.cleaning.to_json(),
            }
            for s in report.sources
        },
        "store": asdict(report.upsert),
        "in_window": report.in_window,
    }
