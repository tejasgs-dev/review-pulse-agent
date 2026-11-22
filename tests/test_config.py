from pathlib import Path

import pytest
import yaml

from pulse.config import DEFAULT_CONFIG_PATH, ConfigError, load_config


def _write(tmp_path: Path, mutate) -> Path:
    data = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    mutate(data)
    p = tmp_path / "pulse.yaml"
    p.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return p


def test_default_config_loads(monkeypatch):
    monkeypatch.delenv("PULSE_RECIPIENT", raising=False)
    monkeypatch.delenv("PULSE_DOCS_FOLDER_ID", raising=False)
    cfg = load_config(load_env_file=False)
    assert cfg.product.app_store_id == "1404871703"
    assert cfg.window.weeks_back == 10
    assert cfg.analysis.max_words == 250
    assert cfg.publish.recipient is None  # unset env var becomes None, not ""


def test_env_vars_are_expanded(monkeypatch):
    monkeypatch.setenv("PULSE_RECIPIENT", "me@example.com")
    monkeypatch.setenv("PULSE_DOCS_FOLDER_ID", "folder123")
    cfg = load_config(load_env_file=False)
    assert cfg.publish.recipient == "me@example.com"
    assert cfg.publish.docs_folder_id == "folder123"


@pytest.mark.parametrize("weeks", [7, 13, 0, -1])
def test_weeks_back_out_of_range_is_rejected(tmp_path, weeks):  # NRM-01
    p = _write(tmp_path, lambda d: d["window"].__setitem__("weeks_back", weeks))
    with pytest.raises(ConfigError, match="weeks_back"):
        load_config(p, load_env_file=False)


@pytest.mark.parametrize("value", ["a@x.com, b@x.com", "not-an-email", "a@x.com;b@x.com"])
def test_recipient_must_be_one_valid_address(tmp_path, monkeypatch, value):  # GML-01
    monkeypatch.setenv("PULSE_RECIPIENT", value)
    with pytest.raises(ConfigError, match="recipient"):
        load_config(load_env_file=False)


def test_unknown_key_is_rejected(tmp_path):
    p = _write(tmp_path, lambda d: d["analysis"].__setitem__("max_wrods", 200))
    with pytest.raises(ConfigError, match="max_wrods"):
        load_config(p, load_env_file=False)


def test_play_columns_require_core_fields(tmp_path):  # ING-02
    p = _write(tmp_path, lambda d: d["ingest"]["play_columns"].pop("text"))
    with pytest.raises(ConfigError, match="text"):
        load_config(p, load_env_file=False)


def test_doc_title_needs_week_placeholder(tmp_path):
    p = _write(tmp_path, lambda d: d["publish"].__setitem__("doc_title", "Pulse"))
    with pytest.raises(ConfigError, match="iso_week"):
        load_config(p, load_env_file=False)


def test_missing_file_gives_clear_error(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yaml", load_env_file=False)


def test_mcp_servers_unconfigured_by_default():
    cfg = load_config(load_env_file=False)
    assert not cfg.mcp.docs.configured
    assert not cfg.mcp.gmail.configured
