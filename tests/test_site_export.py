"""`pulse export-site`: the scrubbed site JSON (allowlisted fields only) and the week index.
Synthetic text only."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pulse.cli import ExitCode, app
from pulse.config import load_config
from pulse.site_export import ExportRefusedError, MissingPulseError, build_week, export_week

runner = CliRunner()

THEMES = [
    ("support", "Unhelpful support", 0.20, 0.944954128440367, 200, 1, True),
    ("app_speed", "Slow app", 0.15, 0.6, 150, 2, True),
    ("charges", "Hidden charges", 0.12, 0.5, 120, 4, True),
    ("features", "Feature requests", 0.13, 0.4, 130, 5, False),
    ("kyc", "KYC delays", 0.10, 0.7, 100, 3, False),
]
TOP = ["support", "app_speed", "charges"]


def _write_run(runs: Path, week: str, *, ok: bool = True, quote_text: str | None = None,
               review_count: int = 1000) -> None:
    d = runs / week
    d.mkdir(parents=True, exist_ok=True)
    pulse = {
        "week": week,
        "window": ["2026-09-07", "2026-11-15"],
        "review_count": review_count,
        "sources": {"play_store": 900, "app_store": 100},
        "top_themes": [
            {"id": tid, "label": label, "description": "SECRET keyword list",
             "volume_share": share, "negative_share": neg, "avg_rating": 1.5, "score": 0.3,
             "summary": f"Summary for {label}", "review_count": n}
            for tid, label, share, neg, n, _, _ in THEMES if tid in TOP
        ],
        "quotes": [
            {"review_id": f"r00{i}", "theme_id": tid, "span": [0, 40],
             "text": quote_text if (quote_text and i == 0) else f"The app keeps failing me {tid}.",
             "rating": i + 1, "source": "play_store" if i else "app_store"}
            for i, tid in enumerate(TOP)
        ],
        "actions": [
            {"theme_id": tid, "action": f"Fix the {tid} problem", "signal": f"fewer {tid} reviews"}
            for tid in TOP
        ],
        "general_praise_share": 0.24,
        "email": {"subject": "s", "body": "EMAIL BODY must not leak"},
    }
    themes = {
        "week": week,
        "themes": [
            {"id": tid, "label": label, "description": "d", "include": "SECRET", "exclude": "",
             "example_ids": ["r001"], "top": top, "count": n, "negative_count": 1,
             "volume_share": share, "negative_share": neg, "rank": rank}
            for tid, label, share, neg, n, rank, top in THEMES
        ],
        "buckets": {"general_praise": {"count": 1, "share": 0.24},
                    "other": {"count": 1, "share": 0.12}},
        "assignments": [{"review_id": "r001", "theme_id": "support"}],
    }
    (d / "pulse.json").write_text(json.dumps(pulse), encoding="utf-8")
    (d / "themes.json").write_text(json.dumps(themes), encoding="utf-8")
    (d / "validation.json").write_text(json.dumps({"ok": ok}), encoding="utf-8")
    (d / "clean_reviews.jsonl").write_text('{"text": "RAW REVIEW TEXT"}\n', encoding="utf-8")


@pytest.fixture
def cfg(tmp_config):
    return load_config(tmp_config(), load_env_file=False)


def test_build_week_keeps_only_allowlisted_fields(cfg):
    _write_run(cfg.paths.runs_path, "2026-W46")
    data = build_week(cfg, "2026-W46")

    assert set(data) == {"schema_version", "week", "week_label", "window", "review_count",
                         "sources", "filters", "general_praise_share", "other_share", "themes",
                         "top_themes", "quotes", "actions"}
    assert data["week_label"] == "Week of 9 Nov 2026"
    assert data["window"] == {"start": "2026-09-07", "end": "2026-11-15"}
    assert data["sources"] == {"play_store": 900, "app_store": 100}
    assert data["filters"] == {"min_words": cfg.ingest.min_words, "language": "en"}
    assert data["other_share"] == 0.12
    assert set(data["quotes"][0]) == {"theme_id", "theme_label", "text", "rating", "source"}
    assert data["quotes"][0]["theme_label"] == "Unhelpful support"

    text = json.dumps(data)
    for leaked in ("review_id", "span", "r001", "SECRET", "EMAIL BODY", "RAW REVIEW TEXT",
                   "assignments", "description"):
        assert leaked not in text


def test_all_five_themes_sorted_by_share_with_full_precision(cfg):
    _write_run(cfg.paths.runs_path, "2026-W46")
    data = build_week(cfg, "2026-W46")
    assert [t["id"] for t in data["themes"]] == [
        "support", "app_speed", "features", "charges", "kyc"]
    assert [t["top"] for t in data["themes"]] == [True, True, False, True, False]
    # Not pre-rounded: 0.944954 must still display as 94%, not 95%.
    assert data["themes"][0]["negative_share"] == 0.944954128440367


def test_summaries_and_actions_end_like_the_doc(cfg):
    _write_run(cfg.paths.runs_path, "2026-W46")
    data = build_week(cfg, "2026-W46")
    assert data["top_themes"][0]["summary"] == "Summary for Unhelpful support."
    assert data["actions"][0]["action"] == "Fix the support problem."
    assert data["actions"][0]["signal"] == "fewer support reviews."


def test_refuses_a_pulse_that_failed_validation(cfg):
    _write_run(cfg.paths.runs_path, "2026-W46", ok=False)
    with pytest.raises(ExportRefusedError, match="did not pass validation"):
        build_week(cfg, "2026-W46")


def test_refuses_pii_in_a_quote(cfg, tmp_path):
    _write_run(cfg.paths.runs_path, "2026-W46",
               quote_text="Call me on 9876543210 or mail someone@example.com about it")
    out = tmp_path / "site"
    with pytest.raises(ExportRefusedError, match="personal data"):
        export_week(cfg, "2026-W46", out)
    assert not out.exists()


def test_missing_week(cfg, tmp_path):
    with pytest.raises(MissingPulseError, match="no composed pulse yet"):
        export_week(cfg, "2026-W47", tmp_path / "site")


def test_index_is_newest_first_without_duplicates(cfg, tmp_path):
    out = tmp_path / "site"
    for week in ("2026-W46", "2026-W48", "2026-W47"):
        _write_run(cfg.paths.runs_path, week)
        export_week(cfg, week, out)
    _write_run(cfg.paths.runs_path, "2026-W47", review_count=1234)
    export_week(cfg, "2026-W47", out)

    index = json.loads((out / "index.json").read_text(encoding="utf-8"))
    assert index["latest"] == "2026-W48"
    assert [w["week"] for w in index["weeks"]] == ["2026-W48", "2026-W47", "2026-W46"]
    w47 = index["weeks"][1]
    assert w47["review_count"] == 1234
    assert w47["top_theme"] == {"label": "Unhelpful support", "share": 0.2}
    assert sorted(p.name for p in out.iterdir()) == [
        "2026-W46.json", "2026-W47.json", "2026-W48.json", "index.json"]


def test_corrupt_index_is_refused(cfg, tmp_path):
    out = tmp_path / "site"
    out.mkdir()
    (out / "index.json").write_text("not json", encoding="utf-8")
    _write_run(cfg.paths.runs_path, "2026-W46")
    with pytest.raises(ExportRefusedError, match="not a valid site index"):
        export_week(cfg, "2026-W46", out)


def test_cli_export_site(tmp_config, tmp_path):
    path = tmp_config()
    _write_run(tmp_path / "runs", "2026-W46")
    out = tmp_path / "site"
    result = runner.invoke(app, ["export-site", "--week", "2026-W46", "--config", str(path),
                                 "--out", str(out)])
    assert result.exit_code == ExitCode.OK, result.output
    assert (out / "2026-W46.json").exists()
    assert json.loads((out / "index.json").read_text(encoding="utf-8"))["latest"] == "2026-W46"


def test_cli_export_site_exit_codes(tmp_config, tmp_path):
    path = tmp_config()
    out = tmp_path / "site"
    args = ["export-site", "--config", str(path), "--out", str(out)]
    missing = runner.invoke(app, [*args, "--week", "2026-W47"])
    assert missing.exit_code == ExitCode.CONFIG_ERROR
    _write_run(tmp_path / "runs", "2026-W46", ok=False)
    refused = runner.invoke(app, [*args, "--week", "2026-W46"])
    assert refused.exit_code == ExitCode.VALIDATION_FAILED
    assert not out.exists()


SITE_PULSES = Path(__file__).resolve().parents[1] / "site" / "pulses"
WEEK_KEYS = {"schema_version", "week", "week_label", "window", "review_count", "sources",
             "filters", "general_praise_share", "other_share", "themes", "top_themes", "quotes",
             "actions"}


def test_committed_site_data_is_allowlisted():
    """The files in site/pulses/ are public: only the export's fields, exactly 3 quotes per
    week (architecture §6 exception), and only files the index lists."""
    index = json.loads((SITE_PULSES / "index.json").read_text(encoding="utf-8"))
    listed = {w["week"] for w in index["weeks"]}
    files = {p.stem for p in SITE_PULSES.glob("*.json")} - {"index"}
    assert files == listed
    assert {p.name for p in SITE_PULSES.iterdir()} == {f"{w}.json" for w in listed} | {"index.json"}
    for week in listed:
        data = json.loads((SITE_PULSES / f"{week}.json").read_text(encoding="utf-8"))
        assert set(data) == WEEK_KEYS
        assert len(data["quotes"]) == 3
        for q in data["quotes"]:
            assert set(q) == {"theme_id", "theme_label", "text", "rating", "source"}
