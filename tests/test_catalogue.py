import copy

import pytest
import yaml

from pulse.analysis.catalogue import (
    Catalogue,
    CatalogueError,
    Figures,
    fill,
    load_catalogue,
)
from pulse.config import load_config

FIG = Figures(count=245, recent_count=41, share="18%", negative_share="91%", trend="rising")


@pytest.fixture
def raw(cfg):
    return yaml.safe_load(cfg.analysis.catalogue_path.read_text(encoding="utf-8"))


def test_real_catalogue_loads(cfg):
    cat = load_catalogue(cfg.analysis.catalogue_path)
    ids = [e.id for e in cat.themes]
    assert {"support", "charges", "performance_bugs", "withdrawals"} <= set(ids)
    assert all(len(e.label.split()) <= 4 for e in cat.themes)


@pytest.mark.parametrize(("field", "value", "message"), [
    ("summary", "About 40% of reviewers mention {kw1}", "digits"),
    ("summary", "Seen in {count} reviews about {kw1}", "unknown placeholders"),   # CMP-22
    ("action", "Fix the {kw9} problem", "unknown placeholders"),
    ("action", "Fix the {kw1 problem", "syntax"),
    ("signal", "fewer than 10 complaints", "digits"),
])
def test_bad_templates_are_rejected(raw, field, value, message):
    bad = copy.deepcopy(raw)
    bad["themes"][0][field] = value
    with pytest.raises(ValueError, match=message):
        Catalogue.model_validate(bad)


def test_labels_ids_and_duplicates_are_checked(raw):
    long_label = copy.deepcopy(raw)
    long_label["themes"][0]["label"] = "A label with far too many words"
    with pytest.raises(ValueError, match="1–4 words"):
        Catalogue.model_validate(long_label)
    reserved = copy.deepcopy(raw)
    reserved["themes"][0]["id"] = "other"
    with pytest.raises(ValueError, match="reserved"):
        Catalogue.model_validate(reserved)
    dup = copy.deepcopy(raw)
    dup["themes"].append(copy.deepcopy(dup["themes"][0]))
    with pytest.raises(ValueError, match="unique"):
        Catalogue.model_validate(dup)


def test_missing_or_broken_file(tmp_path):
    with pytest.raises(CatalogueError, match="not found"):
        load_catalogue(tmp_path / "missing.yaml")
    broken = tmp_path / "themes.yaml"
    broken.write_text("themes: [", encoding="utf-8")
    with pytest.raises(CatalogueError, match="not valid YAML"):
        load_catalogue(broken)


def test_match_needs_two_shared_keywords(cfg):
    cat = load_catalogue(cfg.analysis.catalogue_path)
    assert cat.match(["customer", "support", "worst", "service"]).id == "support"
    assert cat.match(["charges", "brokerage charges", "rs"]).id == "charges"
    assert cat.match(["option", "add", "account", "chart", "ipad", "screen"]).id == (
        "feature_requests")
    assert cat.match(["zebra", "support", "quantum"]) is None     # only one hit
    assert cat.match([]) is None


def test_fill_inserts_keywords_and_figures():
    t = "Audit the {recent_count} of {count} {kw1} cases ({trend}, {share}, {negative_share})"
    assert fill(t, ["refund", "bank"], FIG) == (
        "Audit the 41 of 245 refund cases (rising, 18%, 91%)")
    assert fill("{kw1}, {kw2} and {kw3}", ["ipo"]) == "ipo, ipo and ipo"
    assert fill("about {kw1}", []) == "about these reviews"
    assert fill("{count} items", [], Figures(1234, 5, "1%", "2%", "steady")) == "1,234 items"


def test_figures_make_actions_differ_by_week(cfg):
    cat = load_catalogue(cfg.analysis.catalogue_path)
    support = next(e for e in cat.themes if e.id == "support")
    week1 = fill(support.action, ["service"], FIG)
    week2 = fill(support.action, ["service"], Figures(198, 17, "15%", "88%", "easing"))
    assert week1 != week2 and "41" in week1 and "17" in week2


def test_config_requires_local_section_for_local_engine(tmp_config):
    from pulse.config import ConfigError

    path = tmp_config(lambda d: d["analysis"].pop("local"))
    with pytest.raises(ConfigError, match="analysis.local is required"):
        load_config(path, load_env_file=False)
