"""The Groq provider (`GroqClient`): structured output, 429 handling, logging, errors, and the
provider config. The SDK is faked: no test reaches the real Groq API."""

from __future__ import annotations

import json
from types import SimpleNamespace

import groq
import httpx
import pytest
from pydantic import BaseModel
from typer.testing import CliRunner

from pulse.analysis import llm as llm_mod
from pulse.analysis.llm import GroqClient, LlmClient, LlmError, build_llm, strict_schema
from pulse.cli import ExitCode, app
from pulse.config import ConfigError, LlmConfig, load_config


class Answer(BaseModel):
    theme_id: str
    words: list[str]


GOOD = json.dumps({"theme_id": "support", "words": ["slow", "reply"]})
HEADERS = {"x-ratelimit-limit-tokens": "8000", "x-ratelimit-remaining-tokens": "6123",
           "x-ratelimit-reset-tokens": "14.1s", "x-ratelimit-remaining-requests": "998"}


def completion(content: str | None = GOOD, finish_reason: str = "stop", tool_calls=None):
    message = SimpleNamespace(content=content, tool_calls=tool_calls, refusal=None)
    usage = SimpleNamespace(prompt_tokens=1200, completion_tokens=300, total_tokens=1500,
                            completion_tokens_details=SimpleNamespace(reasoning_tokens=120))
    return SimpleNamespace(id="chatcmpl-1", model="openai/gpt-oss-120b", usage=usage,
                           choices=[SimpleNamespace(message=message,
                                                    finish_reason=finish_reason)])


def rate_limited(retry_after: str | None = "3") -> groq.RateLimitError:
    headers = {"retry-after": retry_after} if retry_after is not None else {}
    response = httpx.Response(429, headers=headers,
                              request=httpx.Request("POST", "https://api.groq.com/x"))
    return groq.RateLimitError("rate limited", response=response, body=None)


class FakeGroqSdk:
    """Stands in for `groq.Groq`: each create() returns or raises the next scripted item."""

    def __init__(self, *script):
        self.script = list(script)
        self.requests: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(
            with_raw_response=SimpleNamespace(create=self._create)))

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return SimpleNamespace(parse=lambda: item, headers=httpx.Headers(HEADERS))


def client(*script, retries: int = 4, max_wait: float = 90, log_dir=None, sleeps=None):
    cfg = LlmConfig.model_validate({"provider": "groq", "groq": {
        "rate_limit_retries": retries, "max_wait_seconds": max_wait}})
    sdk = FakeGroqSdk(*script)
    c = GroqClient(cfg, client=sdk, log_dir=log_dir,
                   sleep=(sleeps.append if sleeps is not None else lambda _s: None))
    return c, sdk


def call(c: GroqClient, name: str = "write"):
    return c.parse(name=name, system="sys", prefix="themes", user="go", schema=Answer,
                   effort="medium")


def test_success_sends_a_strict_schema_and_logs_tokens(tmp_path, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_do_not_log_me")
    c, sdk = client(completion(), log_dir=tmp_path)
    assert call(c) == Answer(theme_id="support", words=["slow", "reply"])

    (req,) = sdk.requests
    assert req["model"] == "openai/gpt-oss-120b"
    assert req["reasoning_effort"] == "medium"
    fmt = req["response_format"]
    assert fmt["type"] == "json_schema" and fmt["json_schema"]["strict"] is True
    assert req["messages"][1]["content"] == "themes\n\ngo"

    assert c.usage.provider == "groq" and c.usage.calls == 1
    assert c.usage.estimated_usd == 0.0            # free tier: no cost in manifest.json
    (log,) = tmp_path.glob("*.json")
    entry = json.loads(log.read_text(encoding="utf-8"))
    assert entry["response"]["tokens"] == {"prompt": 1200, "completion": 300,
                                           "reasoning": 120, "total": 1500}
    assert entry["response"]["rate_limit"]["x-ratelimit-remaining-tokens"] == "6123"
    assert entry["request"]["provider"] == "groq"
    assert "gsk_do_not_log_me" not in log.read_text(encoding="utf-8")


def test_429_waits_for_retry_after_then_succeeds(tmp_path):
    sleeps: list[float] = []
    c, sdk = client(rate_limited("3"), completion(), log_dir=tmp_path, sleeps=sleeps)
    assert call(c).theme_id == "support"
    assert sleeps == [3.0]
    assert len(sdk.requests) == 2
    assert c.usage.rate_limit_waits == 1
    entry = json.loads(next(tmp_path.glob("*.json")).read_text(encoding="utf-8"))
    assert entry["response"]["rate_limit_waits_seconds"] == [3.0]


def test_429_without_retry_after_backs_off_exponentially():
    sleeps: list[float] = []
    c, _ = client(rate_limited(None), rate_limited(None), completion(), sleeps=sleeps)
    call(c)
    assert sleeps == [2.0, 4.0]


def test_429_gives_up_after_the_configured_retries():
    sleeps: list[float] = []
    c, sdk = client(*[rate_limited("1")] * 3, retries=2, sleeps=sleeps)
    with pytest.raises(LlmError) as e:
        call(c)
    assert e.value.kind == "rate_limit"
    assert sleeps == [1.0, 1.0] and len(sdk.requests) == 3


def test_a_wait_longer_than_max_wait_fails_at_once():
    """The daily limit: retry-after is hours away, so do not sleep through it."""
    sleeps: list[float] = []
    c, _ = client(rate_limited("3600"), max_wait=90, sleeps=sleeps)
    with pytest.raises(LlmError) as e:
        call(c)
    assert e.value.kind == "rate_limit" and sleeps == []


def test_bad_json_is_a_schema_error():
    c, _ = client(completion('{"theme_id": "support"'))
    with pytest.raises(LlmError) as e:
        call(c)
    assert e.value.kind == "schema"


def test_truncated_output_is_max_tokens():
    c, _ = client(completion(GOOD[:10], finish_reason="length"))
    with pytest.raises(LlmError) as e:
        call(c)
    assert e.value.kind == "max_tokens"


def test_rejected_key_is_auth():
    response = httpx.Response(401, request=httpx.Request("POST", "https://api.groq.com/x"))
    c, _ = client(groq.AuthenticationError("bad key", response=response, body=None))
    with pytest.raises(LlmError) as e:
        call(c)
    assert e.value.kind == "auth"


def test_missing_key_is_auth(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(LlmError) as e:
        GroqClient(LlmConfig())
    assert e.value.kind == "auth" and "GROQ_API_KEY" in str(e.value)


def test_strict_schema_requires_every_field_and_forbids_extras():
    class Inner(BaseModel):
        a: str
        b: int = 3

    class Outer(BaseModel):
        items: list[Inner]
        note: str = ""

    schema = strict_schema(Outer)
    assert schema["required"] == ["items", "note"]
    assert schema["additionalProperties"] is False
    inner = schema["$defs"]["Inner"]
    assert inner["required"] == ["a", "b"] and inner["additionalProperties"] is False
    assert "default" not in json.dumps(schema)


# ---------------------------------------------------------------------------- config


def test_default_provider_is_groq_with_separate_efforts():
    cfg = load_config(load_env_file=False)
    assert cfg.llm.provider == "groq"
    assert cfg.llm.model == "openai/gpt-oss-120b"
    assert cfg.llm.stage_effort("compose") == "medium"
    assert cfg.llm.stage_effort("agent") == "low"


def test_legacy_llm_fields_mean_anthropic():
    cfg = LlmConfig.model_validate({"model": "claude-opus-5-5", "fallbacks": None})
    assert cfg.provider == "anthropic"
    assert cfg.model == "claude-opus-5-5" and cfg.fallbacks is None


def test_build_llm_picks_the_provider(monkeypatch):
    """`build_llm` here is the real function (imported before conftest patches the module);
    the SDK constructors are faked, so nothing connects anywhere."""
    sdk_kwargs: dict = {}
    monkeypatch.setattr(llm_mod, "_groq",
                        lambda: SimpleNamespace(Groq=lambda **kw: sdk_kwargs.update(kw)))
    monkeypatch.setattr(llm_mod, "_anthropic", lambda: SimpleNamespace(Anthropic=object))
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    assert isinstance(build_llm(LlmConfig()), GroqClient)
    assert sdk_kwargs["max_retries"] == 0          # 429s are handled by GroqClient itself
    assert isinstance(build_llm(LlmConfig(provider="anthropic")), LlmClient)


def test_llm_engine_needs_anthropic_in_the_config(tmp_config):
    path = tmp_config(lambda d: d["analysis"].__setitem__("engine", "llm"))
    with pytest.raises(ConfigError, match="needs llm.provider: anthropic"):
        load_config(path, load_env_file=False)


def test_llm_engine_on_the_cli_needs_anthropic(tmp_config):
    result = CliRunner().invoke(app, ["analyze", "--week", "2026-W47", "--engine", "llm",
                                      "--config", str(tmp_config())])
    assert result.exit_code == ExitCode.CONFIG_ERROR
    assert "needs llm.provider: anthropic" in result.output
