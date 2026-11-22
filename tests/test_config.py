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
    monkeypatch.delenv("PULSE_DOC_ID", raising=False)
    cfg = load_config(load_env_file=False)
    assert cfg.product.app_store_id == "1404871703"
    assert cfg.window.weeks_back == 10
    assert cfg.analysis.max_words == 250
    assert cfg.publish.recipient is None  # unset env var becomes None, not ""
    assert cfg.publish.doc_id is None


def test_env_vars_are_expanded(monkeypatch):
    monkeypatch.setenv("PULSE_RECIPIENT", "me@example.com")
    monkeypatch.setenv("PULSE_DOC_ID", "1AbCdEfGhIjKlMnOpQrStUvWxYz_0123456789-abcd")
    cfg = load_config(load_env_file=False)
    assert cfg.publish.recipient == "me@example.com"
    assert cfg.publish.doc_id == "1AbCdEfGhIjKlMnOpQrStUvWxYz_0123456789-abcd"
    assert cfg.publish.doc_url == ("https://docs.google.com/document/d/"
                                   "1AbCdEfGhIjKlMnOpQrStUvWxYz_0123456789-abcd/edit")


def test_doc_id_may_be_a_url(monkeypatch):
    monkeypatch.setenv("PULSE_DOC_ID", "https://docs.google.com/document/d/"
                                       "1AbCdEfGhIjKlMnOpQrStUvWxYz_0123/edit?tab=t.0")
    assert load_config(load_env_file=False).publish.doc_id == "1AbCdEfGhIjKlMnOpQrStUvWxYz_0123"


def test_bad_doc_id_is_rejected(monkeypatch):
    monkeypatch.setenv("PULSE_DOC_ID", "not a doc")
    with pytest.raises(ConfigError, match="PULSE_DOC_ID"):
        load_config(load_env_file=False)


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


def test_missing_file_gives_clear_error(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yaml", load_env_file=False)


def test_mcp_server_is_gworkspace_over_http(monkeypatch):
    monkeypatch.delenv("PULSE_MCP_API_KEY", raising=False)
    cfg = load_config(load_env_file=False)
    assert cfg.mcp.docs == cfg.mcp.gmail                  # one server for Docs and Gmail
    assert cfg.mcp.docs.transport == "http" and cfg.mcp.docs.configured
    assert cfg.mcp.docs.url.endswith(".onrender.com/mcp")
    assert cfg.mcp.docs.missing_headers == ["Authorization"]   # no key without .env
