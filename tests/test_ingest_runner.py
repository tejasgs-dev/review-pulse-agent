import codecs
import json
from datetime import UTC, date, datetime

from typer.testing import CliRunner

from pulse.cli import ExitCode, app
from pulse.config import load_config
from pulse.ingest import runner
from pulse.ingest.base import FetchResult, SourceError
from tests.conftest import LONG_TEXT, raw

NOW = datetime(2026, 11, 23, 4, 0, tzinfo=UTC)  # Monday 09:30 IST
WEEK = "2026-W47"


class FakeSource:
    def __init__(self, name, reviews=(), error=None, warnings=(), complete=True):
        self.name, self._reviews, self._error, self._warnings = name, list(reviews), error, warnings
        self.complete = complete
        self.calls: list[date] = []  # the `since` of every fetch

    def fetch(self, since, until):
        self.calls.append(since)
        if self._error:
            raise SourceError(self._error)
        return FetchResult(source=self.name, reviews=self._reviews, warnings=list(self._warnings),
                           complete=self.complete,
                           covered_since=since if self.complete else None)


def _factory(**sources):
    return lambda cfg, data_dir: sources


def _play(n, start=0, day=10):
    return [raw(rid=f"p{start + i}", text=f"Play review number {i}: {LONG_TEXT}",
                submitted=datetime(2026, 11, day, 6, tzinfo=UTC)) for i in range(n)]


def _app(n):
    return [raw(rid=f"a{i}", source="app_store", text=f"App store review {i}: {LONG_TEXT}",
                submitted=datetime(2026, 11, 12, 6, tzinfo=UTC)) for i in range(n)]


def test_ingests_both_sources_and_counts_window(cfg, tmp_path):
    old = raw(rid="old", text="Old review outside the window",
              submitted=datetime(2026, 8, 1, tzinfo=UTC))
    rating_only = raw(rid="ro", text=None)
    report = runner.run_ingest(
        cfg, week=WEEK, data_dir=tmp_path, runs_dir=tmp_path / "runs", now=NOW,
        source_factory=_factory(play_store=FakeSource("play_store", [*_play(3), old, rating_only]),
                                app_store=FakeSource("app_store", _app(2))))
    assert [s.status for s in report.sources] == ["ok", "ok"]
    assert report.upsert.new == 5
    assert report.in_window == {"play_store": 3, "app_store": 2}
    assert (tmp_path / "reviews.parquet").exists()
    assert all(s.raw_path and s.raw_path.exists() for s in report.sources)

    play = report.sources[0].cleaning
    assert (play.input, play.kept) == (5, 3)
    assert play.counts["dropped_too_short"] == 1 and play.counts["dropped_no_text"] == 1


def test_manifest_records_rule_counts_per_source(cfg, tmp_path):
    hindi = raw(rid="h", text="पैसे निकालने में बहुत समय लगता है और सपोर्ट टीम जवाब नहीं देती")
    short = raw(rid="s", text="Good app 👍")
    report = runner.run_ingest(
        cfg, week=WEEK, data_dir=tmp_path, runs_dir=tmp_path / "runs", now=NOW,
        source_factory=_factory(play_store=FakeSource("play_store", [*_play(2), hindi, short]),
                                app_store=FakeSource("app_store", _app(1))))
    assert report.manifest_path == tmp_path / "runs" / WEEK / "manifest.json"
    m = json.loads(report.manifest_path.read_text(encoding="utf-8"))
    play = m["ingest"]["sources"]["play_store"]
    assert play["kept"] == 2 and play["input"] == 4
    assert play["dropped"]["dropped_non_english"] == 1
    assert play["dropped"]["dropped_too_short"] == 1
    assert play["dropped_languages"] == {"hi": 1}
    assert play["emojis_removed"] == 1
    assert m["ingest"]["rules"] == {"strip_emojis": True, "min_words": 9, "language": "en"}
    assert m["ingest"]["sources"]["app_store"]["kept"] == 1


def test_manifest_keeps_other_sections(cfg, tmp_path):
    from pulse.manifest import update_manifest

    update_manifest(tmp_path / "runs", WEEK, "analysis", {"themes": 5})
    _run(cfg, tmp_path, FakeSource("play_store", _play(1)), FakeSource("app_store"), now=NOW)
    m = json.loads((tmp_path / "runs" / WEEK / "manifest.json").read_text(encoding="utf-8"))
    assert m["analysis"] == {"themes": 5} and "ingest" in m


def test_shortened_edit_removes_stored_review(cfg, tmp_path):
    long_version = raw(rid="e", text=f"Edited later: {LONG_TEXT}")
    _run(cfg, tmp_path, FakeSource("play_store", [long_version]), FakeSource("app_store"),
         now=NOW)
    short_edit = raw(rid="e", text="Now bad", updated=datetime(2026, 11, 20, tzinfo=UTC))
    report = _run(cfg, tmp_path, FakeSource("play_store", [short_edit]), FakeSource("app_store"),
                  now=NOW)
    assert report.upsert.removed == 1 and report.upsert.total == 0


def test_rerun_adds_zero_rows(cfg, tmp_path):  # P1 exit criterion
    factory = _factory(play_store=FakeSource("play_store", _play(5)),
                       app_store=FakeSource("app_store", _app(3)))
    kw = dict(week=WEEK, data_dir=tmp_path, runs_dir=tmp_path / "runs", now=NOW,
              source_factory=factory)
    runner.run_ingest(cfg, **kw)
    again = runner.run_ingest(cfg, **kw)
    assert again.upsert.new == 0 and again.upsert.total == 8


def test_one_source_failing_does_not_stop_the_other(cfg, tmp_path):  # §10 row 1
    report = runner.run_ingest(
        cfg, week=WEEK, data_dir=tmp_path, runs_dir=tmp_path / "runs", now=NOW,
        source_factory=_factory(play_store=FakeSource("play_store", _play(2)),
                                app_store=FakeSource("app_store", error="HTTP 503")))
    assert [s.status for s in report.sources] == ["ok", "failed"]
    assert report.sources[1].error == "HTTP 503" and not report.all_failed
    assert report.in_window == {"play_store": 2, "app_store": 0}


def test_single_source_selection(cfg, tmp_path):
    report = runner.run_ingest(
        cfg, week=WEEK, sources=["app_store"], data_dir=tmp_path, now=NOW,
        source_factory=_factory(play_store=FakeSource("play_store", error="must not be called"),
                                app_store=FakeSource("app_store", _app(1))))
    assert [s.name for s in report.sources] == ["app_store"]


def test_real_adapters_are_built_from_config(cfg, tmp_path):
    sources = runner.build_sources(cfg, tmp_path)
    assert sources["app_store"].app_id == "1404871703"


def test_play_source_follows_the_config_switch(tmp_config, tmp_path):
    from pulse.ingest.play_file import PlayStoreFileSource
    from pulse.ingest.play_public import PlayStorePublicSource

    public = runner.build_sources(load_config(tmp_config(), load_env_file=False), tmp_path)
    assert isinstance(public["play_store"], PlayStorePublicSource)
    assert public["play_store"].package == "com.nextbillion.groww"
    assert public["play_store"].langs == ("en",)

    off = load_config(tmp_config(_play_public_off), load_env_file=False)
    assert isinstance(runner.build_sources(off, tmp_path)["play_store"], PlayStoreFileSource)


def _play_public_off(d):
    d["ingest"]["play_public"]["enabled"] = False


# -- incremental ingest ---------------------------------------------------------

def _run(cfg, tmp_path, play, app, **kw):
    return runner.run_ingest(cfg, week=WEEK, data_dir=tmp_path, runs_dir=tmp_path / "runs",
                             source_factory=_factory(play_store=play, app_store=app), **kw)


def test_first_run_backfills_then_later_runs_are_incremental(cfg, tmp_path):
    play, app = FakeSource("play_store", _play(2)), FakeSource("app_store", _app(1))
    first = _run(cfg, tmp_path, play, app, now=NOW)
    assert [s.mode for s in first.sources] == ["full", "full"]
    assert play.calls == [date(2026, 9, 14)]  # window start for W47 with weeks_back=10

    a_week_later = datetime(2026, 11, 30, 4, 0, tzinfo=UTC)
    second = _run(cfg, tmp_path, play, app, now=a_week_later)
    assert [s.mode for s in second.sources] == ["incremental", "incremental"]
    assert play.calls[-1] == date(2026, 11, 20)  # last covered day (23 Nov) minus 3 overlap days


def test_partial_fetch_does_not_advance_coverage(cfg, tmp_path):
    play = FakeSource("play_store", _play(2), complete=False)
    app = FakeSource("app_store", _app(1))
    _run(cfg, tmp_path, play, app, now=NOW)
    again = _run(cfg, tmp_path, play, app, now=NOW)
    assert again.sources[0].mode == "full"         # play gap must be re-fetched
    assert again.sources[1].mode == "incremental"  # app store was complete


def test_full_flag_forces_whole_window(cfg, tmp_path):
    play, app = FakeSource("play_store", _play(2)), FakeSource("app_store", _app(1))
    _run(cfg, tmp_path, play, app, now=NOW)
    again = _run(cfg, tmp_path, play, app, now=NOW, full=True)
    assert [s.mode for s in again.sources] == ["full", "full"]


def test_missing_store_forces_full_even_with_state(cfg, tmp_path):
    play, app = FakeSource("play_store", _play(2)), FakeSource("app_store", _app(1))
    _run(cfg, tmp_path, play, app, now=NOW)
    (tmp_path / "reviews.parquet").unlink()
    again = _run(cfg, tmp_path, play, app, now=NOW)
    assert [s.mode for s in again.sources] == ["full", "full"]


def test_wider_window_than_coverage_forces_full(tmp_config, tmp_path):
    cfg10 = load_config(tmp_config(), load_env_file=False)
    cfg12 = load_config(tmp_config(lambda d: d["window"].__setitem__("weeks_back", 12)),
                        load_env_file=False)
    play, app = FakeSource("play_store", _play(2)), FakeSource("app_store", _app(1))
    _run(cfg10, tmp_path, play, app, now=NOW)
    again = _run(cfg12, tmp_path, play, app, now=NOW)
    assert again.sources[0].mode == "full" and play.calls[-1] == date(2026, 8, 31)


def test_corrupt_state_file_means_full_fetch(cfg, tmp_path):
    play, app = FakeSource("play_store", _play(2)), FakeSource("app_store", _app(1))
    _run(cfg, tmp_path, play, app, now=NOW)
    (tmp_path / "ingest_state.json").write_text("{not json", encoding="utf-8")
    assert _run(cfg, tmp_path, play, app, now=NOW).sources[0].mode == "full"


# -- CLI ----------------------------------------------------------------------

def test_cli_ingest_end_to_end_with_play_file(tmp_config, tmp_path, monkeypatch):
    play_dir = tmp_path / "data" / "imports" / "play"
    play_dir.mkdir(parents=True)
    csv_text = "Review Link,Star Rating,Review Text,Review Submit Date and Time\r\n" + "".join(
        f"id{i},{1 + i % 5},Withdrawal review number {i} took five days to reach my bank,"
        f"2026-11-1{i % 9}T06:00:00Z\r\n"
        for i in range(60))
    (play_dir / "export.csv").write_bytes(codecs.BOM_UTF16_LE + csv_text.encode("utf-16-le"))

    real_build = runner.build_sources

    def build(cfg, data_dir):  # the real Play file adapter; App Store kept offline
        return {"play_store": real_build(cfg, data_dir)["play_store"],
                "app_store": FakeSource("app_store", _app(2))}

    monkeypatch.setattr(runner, "build_sources", build)
    args = ["ingest", "--week", WEEK, "--config", str(tmp_config(_play_public_off))]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    assert "+62 new" in result.output
    assert "play_store 60" in result.output and "kept 60 of 60" in result.output
    assert (tmp_path / "data" / "reviews.parquet").exists()

    again = CliRunner().invoke(app, args)
    assert "+0 new" in again.output


def test_cli_ingest_all_sources_failed_exit_code(tmp_config, monkeypatch):
    monkeypatch.setattr(runner, "build_sources", _factory(
        play_store=FakeSource("play_store", error="no file"),
        app_store=FakeSource("app_store", error="HTTP 503")))
    result = CliRunner().invoke(app, ["ingest", "--week", WEEK, "--config", str(tmp_config())])
    assert result.exit_code == ExitCode.INGEST_FAILED
    assert "only 0 reviews in the window" in result.output


def test_window_reported_in_output(tmp_config, monkeypatch):
    monkeypatch.setattr(runner, "build_sources", _factory(
        play_store=FakeSource("play_store", _play(1)), app_store=FakeSource("app_store")))
    result = CliRunner().invoke(app, ["ingest", "--week", WEEK, "--config", str(tmp_config())])
    assert f"window {date(2026, 9, 14)} – {date(2026, 11, 22)}" in result.output
