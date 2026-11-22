from datetime import date

import pytest

from pulse.analysis.cluster import (
    Assignment,
    AssignmentBatch,
    ClusterError,
    Discovery,
    assign,
    check_batch,
    discover,
    format_review,
    load_prompt,
    run_clustering,
    seeded_shuffle,
    split_batches,
    stratified_sample,
    themes_payload,
    validate_discovery,
    write_themes,
)
from pulse.analysis.rank import GENERAL_PRAISE, OTHER
from tests.fake_llm import FakeLlm, clean_reviews, theme

WINDOW = (date(2026, 9, 7), date(2026, 11, 15))
THEMES = ["money_movement", "app_performance", "support", "charges"]


def taxonomy(ids=THEMES, examples=("r0001", "r0002", "r0003")) -> Discovery:
    return Discovery(themes=[theme(t, list(examples)) for t in ids],
                     taxonomy_changes=["merged payments and withdrawals"])


def round_robin(labels):
    """Label review rNNNN with labels[N % len(labels)]."""
    def label(rid):
        return labels[int(rid[1:]) % len(labels)], None, "negative"
    return label


# ------------------------------------------------------------------ input handling


def test_shuffle_is_stable_per_week_and_ignores_input_order():
    reviews = clean_reviews(50)
    a = [r.id for r in seeded_shuffle(reviews, "2026-W46")]
    b = [r.id for r in seeded_shuffle(list(reversed(reviews)), "2026-W46")]
    c = [r.id for r in seeded_shuffle(reviews, "2026-W47")]
    assert a == b
    assert a != c
    assert sorted(a) == sorted(r.id for r in reviews)
    assert a != sorted(a)


def test_stratified_sample_is_proportional_and_exact():
    reviews = clean_reviews(1000, rating=lambda i: 1 if i % 4 else 5)  # 75% 1★, 25% 5★
    sample = stratified_sample(reviews, 200, seed=7)
    assert len(sample) == 200 and len({r.id for r in sample}) == 200
    low = sum(r.rating == 1 for r in sample)
    assert 145 <= low <= 155
    assert [r.id for r in stratified_sample(reviews, 200, seed=7)] == [r.id for r in sample]


def test_stratified_sample_returns_everything_when_small():
    reviews = clean_reviews(10)
    assert stratified_sample(reviews, 50, seed=1) == reviews


def test_split_batches_balanced():
    sizes = [len(b) for b in split_batches(clean_reviews(1348), 500)]
    assert sizes == [450, 449, 449]
    assert [len(b) for b in split_batches(clean_reviews(500), 500)] == [500]
    assert split_batches([], 500) == []


def test_format_review_is_one_line():
    r = clean_reviews(1)[0]
    r2 = r.__class__(**{**r.__dict__, "canonical_text": "line one\nline two"})
    assert format_review(r2) == f"{r.id} | 1★ | {r.review_date} | line one line two"


def test_prompts_fill_every_placeholder():
    text = load_prompt("cluster_v1", product="Groww", max_themes=5, min_share_pct=3,
                       seed_themes="- `payments`")
    assert "Groww" in text and "{{" not in text
    with pytest.raises(ValueError, match="unfilled placeholder"):
        load_prompt("cluster_v1", product="Groww")
    assert "{{" not in load_prompt("assign_v1", product="Groww")


# ------------------------------------------------------------------ Pass A


def test_validate_rejects_more_than_five_themes():
    d = taxonomy([f"theme_{c}" for c in "abcdef"])
    errors = validate_discovery(d, known_ids={"r0001", "r0002", "r0003"}, max_themes=5)
    assert any("between 3 and 5" in e for e in errors)


def test_validate_rejects_reserved_duplicate_and_bad_examples():
    d = Discovery(themes=[
        theme("other", ["r0001", "r0002", "r0003"]),
        theme("support", ["r0001", "r0002", "r9999"]),
        theme("support", ["r0001", "r0001", "r0002"], label="Far too many words in label"),
    ], taxonomy_changes=[])
    errors = " | ".join(validate_discovery(d, known_ids={"r0001", "r0002", "r0003"},
                                           max_themes=5))
    assert "reserved bucket" in errors
    assert "used twice" in errors
    assert "not in the input: ['r9999']" in errors
    assert "distinct example_ids" in errors
    assert "1–4 words" in errors


def test_discover_retries_with_errors_then_succeeds(cfg):
    reviews = clean_reviews(20)
    llm = FakeLlm([taxonomy([f"t_{c}" for c in "abcdef"]), taxonomy()], round_robin(THEMES))
    result = discover(llm, reviews, cfg, total_reviews=20)
    assert [t.id for t in result.themes] == THEMES
    assert llm.names() == ["discover", "discover_retry1"]
    assert "between 3 and 5 themes, not 6" in llm.calls[1]["user"]
    assert llm.calls[0]["effort"] == "high"
    assert "r0020 | 1★" in llm.calls[0]["prefix"]  # every review is sent


def test_discover_gives_up_after_retries(cfg):
    llm = FakeLlm([taxonomy([f"t_{c}" for c in "abcdef"])], round_robin(THEMES))
    with pytest.raises(ClusterError, match="failed validation after 3 attempts"):
        discover(llm, clean_reviews(20), cfg, total_reviews=20)


# ------------------------------------------------------------------ Pass B


def a(rid, tid, sec=None):
    return Assignment(review_id=rid, theme_id=tid, secondary_theme_id=sec, sentiment="negative")


def test_check_batch_flags_unknown_duplicates_and_outsiders():
    batch = ["r1", "r2", "r3", "r4", "r5"]
    result = AssignmentBatch(assignments=[
        a("r1", "support"),
        a("r2", "made_up"),                    # unknown theme -> re-request
        a("r3", "support"), a("r3", "charges"),  # conflicting duplicate -> re-request
        a("r4", "charges"), a("r4", "charges"),  # harmless duplicate -> kept
        a("r5", "support", sec="support"),     # secondary equal to primary -> nulled
        a("r9", "support"),                    # not in the batch -> ignored
    ])
    check = check_batch(batch, result, {"support", "charges"})
    assert sorted(check.accepted) == ["r1", "r4", "r5"]
    assert check.accepted["r5"].secondary_theme_id is None
    assert check.missing == ["r2", "r3"]
    assert check.problems == {"unknown_theme": 1, "conflicting_duplicate": 1, "duplicate": 1,
                              "bad_secondary": 1, "outside_batch": 1, "missing": 2}


def test_reserved_labels_are_accepted():
    check = check_batch(["r1", "r2"], AssignmentBatch(assignments=[
        a("r1", GENERAL_PRAISE), a("r2", OTHER)]), {"support"})
    assert not check.missing


def test_missing_assignment_is_re_requested(cfg):
    reviews = clean_reviews(30)
    llm = FakeLlm([taxonomy()], round_robin(THEMES), drop_once={"r0007", "r0011"})
    out = assign(llm, reviews, taxonomy().themes, cfg)
    assert len(out.assignments) == 30
    assert out.retries == 1
    retry = [c for c in llm.calls if "retry" in c["name"]]
    assert len(retry) == 1
    assert "r0007 |" in retry[0]["user"] and "r0001 |" not in retry[0]["user"]


def test_assignment_fails_when_reviews_stay_missing(cfg):
    llm = FakeLlm([taxonomy()], round_robin(THEMES), drop_always={"r0003"})
    with pytest.raises(ClusterError, match="still unlabelled after 2 re-requests"):
        assign(llm, clean_reviews(10), taxonomy().themes, cfg)


def test_batches_share_a_cached_theme_prefix(cfg, tmp_config):
    from pulse.config import load_config
    small = load_config(tmp_config(lambda d: d["analysis"].__setitem__(
        "assign_batch_size", 20)), load_env_file=False)
    llm = FakeLlm([taxonomy()], round_robin(THEMES))
    out = assign(llm, clean_reviews(50), taxonomy().themes, small)
    assert len(out.assignments) == 50
    calls = [c for c in llm.calls if c["schema"] == "AssignmentBatch"]
    assert len(calls) == 3                         # 17 + 17 + 16
    assert len({c["prefix"] for c in calls}) == 1  # identical prefix -> cache hits
    assert "`money_movement`" in calls[0]["prefix"]
    assert all(c["effort"] == "medium" for c in calls)


# ------------------------------------------------------------------ the whole stage


def test_run_clustering_end_to_end(cfg):
    reviews = clean_reviews(120, rating=lambda i: 1 if i % 3 else 5)
    labels = THEMES + [GENERAL_PRAISE]
    llm = FakeLlm([taxonomy()], round_robin(labels))
    result = run_clustering(llm, reviews, cfg, week="2026-W46", window=WINDOW)

    assert len(result.assignments) == 120
    assert len(result.top) == 3
    assert result.rediscovered is False
    assert result.buckets[GENERAL_PRAISE].count == 24
    assert result.quality["consistency"]["agreement"] == 1.0
    assert result.quality["consistency"]["sample"] == 100
    assert sum(s.count for s in result.ranked) + 24 == 120

    payload = themes_payload(result)
    assert payload["top_themes"] == result.top
    assert [t["rank"] for t in payload["themes"]] == [1, 2, 3, 4]
    assert payload["buckets"]["general_praise"]["share"] == pytest.approx(0.2)
    assert payload["prompt_versions"] == {"cluster": "cluster_v1", "assign": "assign_v1"}
    assert len(payload["assignments"]) == 120
    assert payload["taxonomy_changes"] == ["merged payments and withdrawals"]


def test_high_other_share_triggers_exactly_one_rediscovery(cfg):
    reviews = clean_reviews(60)
    # 3 themes + `other` for half the reviews: 50% > 25% both times
    labels = ["money_movement", "app_performance", "support", OTHER, OTHER, OTHER]
    llm = FakeLlm([taxonomy(), taxonomy()], round_robin(labels))
    result = run_clustering(llm, reviews, cfg, week="2026-W46", window=WINDOW)

    discover_calls = [c for c in llm.calls if c["schema"] == "Discovery"]
    assert len(discover_calls) == 2
    assert "left 50% of the reviews in `other`" in discover_calls[1]["user"]
    assert any(n.startswith("reassign_") for n in llm.names())
    assert result.rediscovered
    assert "other_share_high" in [w["code"] for w in result.warnings]


def test_other_under_limit_does_not_rediscover(cfg):
    labels = THEMES + [OTHER]  # 20% other
    llm = FakeLlm([taxonomy()], round_robin(labels))
    result = run_clustering(llm, clean_reviews(50), cfg, week="2026-W46", window=WINDOW)
    assert sum(c["schema"] == "Discovery" for c in llm.calls) == 1
    assert not result.rediscovered


def test_small_theme_and_low_agreement_warnings(cfg):
    reviews = clean_reviews(200)

    def label(rid):
        n = int(rid[1:])
        if n == 5:
            return "charges", None, "negative"   # 1 of 200 -> small theme
        return ["money_movement", "app_performance", "support"][n % 3], None, "negative"

    class Flaky(FakeLlm):
        """Answers the probe differently, to force disagreement."""

        def parse(self, **kw):
            result = super().parse(**kw)
            if kw["name"].startswith("probe"):
                for x in result.assignments:
                    x.theme_id = "support"
            return result

    llm = Flaky([taxonomy()], label)
    result = run_clustering(llm, reviews, cfg, week="2026-W46", window=WINDOW)
    codes = [w["code"] for w in result.warnings]
    assert "small_theme" in codes
    assert "low_agreement" in codes
    assert result.quality["consistency"]["agreement"] < 0.85


def test_large_input_uses_a_stratified_sample(tmp_config):
    from pulse.config import load_config
    c = load_config(tmp_config(lambda d: d["analysis"].update(
        {"max_discovery_reviews": 100, "consistency_sample": 0})), load_env_file=False)
    from pulse.analysis.cluster import week_seed
    reviews = clean_reviews(300)
    sample = stratified_sample(seeded_shuffle(reviews, "2026-W46"), 100,
                               week_seed("2026-W46", "sample"))
    llm = FakeLlm([taxonomy(examples=[r.id for r in sample[:3]])], round_robin(THEMES))
    result = run_clustering(llm, reviews, c, week="2026-W46", window=WINDOW)
    prefix = llm.calls[0]["prefix"]
    assert "a stratified sample of 300" in prefix
    assert prefix.count("★ |") == 100
    assert result.discovery_input == 100
    assert len(result.assignments) == 300          # Pass B still labels everything
    assert result.quality["consistency"] == {"sample": 0, "agreement": None}


def test_write_themes(cfg, tmp_path):
    import json
    llm = FakeLlm([taxonomy()], round_robin(THEMES))
    result = run_clustering(llm, clean_reviews(40), cfg, week="2026-W46", window=WINDOW)
    path = write_themes(result, tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert path == tmp_path / "2026-W46" / "themes.json"
    assert {"themes", "buckets", "assignments", "quality", "warnings"} <= data.keys()
    assert all({"id", "label", "include", "exclude", "score", "recency_weight",
                "secondary_count"} <= t.keys() for t in data["themes"])
